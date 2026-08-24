# BTC 15m P0-P2 优化设计

## 目标

在不改变现有生产部署、不伪造历史 CLOB 证据的前提下，完成三层升级：

1. P0：修正 Research Paper 的 taker 执行时延、市场规则快照和拒绝诊断。
2. P1：新增独立于 maker 机会的 taker 规划器，并验证不同信号稳定性策略。
3. P2：新增市场相对概率（offset/residual）研究链路、因子组消融和 OOF 验证。

本次只实现 BTC 15m，现有方向 Logistic 模型和 maker 策略保留为 control。新模型未通过数据与 sealed holdout 门槛前，不进入 VPS 运行路径。

## 不变量

- 决策只能使用 `available_ts <= decision_ts` 的数据。
- CLOB 规则在每个市场开始时冻结并哈希；重放必须读取同一快照。
- Polymarket 服务器 taker delay 与本机/网络/insert latency 分开记录，不能重复计算。
- taker 必须按到达时刻的完整可见 ask 深度、逐层费用和可成交数量估值。
- P2 的市场价格必须是因果同步数据；缺失、过旧、双 token 不一致时 fail closed。
- 旧 epoch ledger 不迁移、不覆写；语义变化使用新 epoch。

## P0：执行真实性与可诊断性

### 市场规则

`PaperMarketRules` 增加 `taker_server_delay_ms`。`PublicPaperRulesClient` 从 CLOB 的 `itode` 读取服务器延迟开关：启用时使用 250ms，未启用时为 0ms。未知或类型错误直接拒绝规则快照。该字段进入 `rules_sha256` 和 JSON，保证 live/replay 一致。

### 时延组成

`PaperExecutionConfig` 保留 client-side 的 submit/insert latency，并新增规则级 server delay。FAK 到达时间为：

```text
decision_ts + client_taker_latency + rules.taker_server_delay
```

maker 的 insert/cancel 语义不受影响。订单和诊断记录分别输出 client latency、server delay、total latency。

### 诊断

每次被评估的独立 taker 候选输出结构化结果，包括：side、probability、requested/filled size、VWAP、fee/share、gross edge、buffer、net edge、price age 和拒绝原因。拒绝原因使用封闭枚举，至少覆盖：无方向、概率无效、book 缺失/过旧、规则缺失、深度不足、低于最小数量、edge 不足、信号不稳定、余额不足。

## P1：独立 taker 规划

### 核心接口

新增纯函数 `plan_independent_taker_order(...) -> TakerPlanDecision`。输入只有预测、双 token book、冻结规则、余额与策略配置；不接收 maker `OrderPlan`，因此不会因 maker 无被动价而丢失机会。

规划顺序：

1. 由 `p_up` 同时计算 Up 与 Down 的 fair probability。
2. 在两侧按完整 ask depth 模拟最多 `max_shares` 的 FAK 成交。
3. 逐层计算 taker fee，得到实际 VWAP 与 fee/share。
4. 计算 `net_edge = fair_probability - VWAP - fee/share - slippage_buffer - model_uncertainty_buffer`。
5. 选择净 edge 最大且通过 minimum edge、最小订单、余额和数据新鲜度的唯一侧。

### 信号稳定性

将确认状态扩展为 side + net-edge 稳定性：连续信号必须同侧、按预期 cadence 到达、且最新 net edge 相对窗口峰值的衰减不超过配置值。

由于当前模型 artifact 明确绑定 5 秒训练/推理 cadence，本次不把它强行改为 1–2 秒。预注册两个独立 taker challenger：

- `independent_fak_2x5s`：两次 5 秒同侧确认。
- `independent_fak_stable_3x5s`：三次 5 秒确认且限制 edge 衰减。

这样能检验“快成交”与“稳定 edge”之间的取舍，又不违反模型协议。未来只有新 artifact 明确支持更短 cadence 时，才允许 1–2 秒 challenger。

### 组合与 epoch

保留现有 maker、maker-gated immediate FAK 和 maker-then-FAK 作为 paired controls，新增两个独立 challenger。配置 epoch 升级为 `paper-v3-independent-fak`，旧 ledger 保留只读。

## P2：市场相对概率模型

### 模型定义

市场概率先以冻结的 logit anchor 进入 residual-feature challenger：

```text
logit(p_true) ~= f(logit(p_market_up), boundary residual, time/quality, control features)
```

第一版 residual 使用 L2 Logistic Regression，避免在同步样本仍少时放大方差。当前 sklearn estimator 没有固定 offset 接口；若强行实现会引入自定义优化器和新 artifact 类型。因此本轮不声称 market logit 的系数固定为 1，而以成对 OOF 结果检验是否真正学到市场相对残差。LightGBM 只作为后续同一协议下的可选 challenger，不得替换 control，除非同时改善 log loss、Brier、calibration 且 block-bootstrap CI 支持改善。

### 因子组

- `market_relative`：当前已持久化证据支持的合成 Up mid logit 与 price age。Up/Down 独立 mid、可执行 ask、spread、dual-token parity gap 必须先扩展 `OpeningMarketObservation` 的因果 artifact contract，归入 P2b，不能从现有字段伪造。
- `boundary_state`：BTC 距离 Chainlink opening reference、剩余时间、剩余波动、boundary z-score、时间交互。
- `microstructure`：PM L2 imbalance/microprice、近期成交/订单流，以及已同步可用的 Binance/Chainlink basis 或 lag 属于 P2b；当前历史 observation 没有这些字段，本轮只建立 fail-closed 扩展边界。

所有因子组都有显式 schema 和 availability policy。缺少历史数据的组不填零冒充可用，而是标记不可训练并从该次实验排除。

### 验证与 artifact

- 以 market/连续时间块分组，禁止同一市场跨 partition。
- train、early-stop、calibration、test、sealed holdout 相互隔离。
- OOF 预测用于比较 control、offset-only 和逐组增量消融。
- 输出 paired log loss/Brier 差异、calibration、block-bootstrap CI、样本/市场数和拒绝原因。
- artifact metadata 绑定 source hashes、rule epoch、feature schema、factor groups、partition protocol 和 model cadence。
- 同步 CLOB 样本不足或 sealed holdout 未通过时，研究命令非零退出且不发布 champion。

## 验收

- 新时延与规则序列化测试证明 250ms 只计算一次，live/replay 到达时刻一致。
- 独立 taker 在 maker 无计划时仍能下单；能正确选择另一侧、跨档 VWAP、费用、余额和拒绝原因。
- 两个 challenger 在同一事件流运行且 ledger/看板能区分。
- residual pipeline 通过合成可辨识数据测试、时间因果测试、grouped OOF/holdout 隔离测试和缺失 CLOB fail-closed 测试。
- BTC focused tests、ruff、format check 和完整 `tests/btc_short_horizon` 通过；完整测试使用可写 `--basetemp`。

### P2 MVP promotion boundary

The current market-relative implementation is a research-only MVP. Development
must freeze deterministic paired-dataset, feature-schema and split/model
protocol hashes; sealed holdout must reject replacement lineage. Metadata may
be built only from the matching accepted typed sealed-holdout result, never
from a caller-provided boolean. It binds source hash, rule epoch, ingest version
and all frozen research hashes.

`OpeningMarketObservation` does not persist a pair-level collector-session
identity. Up/Down numeric epoch IDs belong to independent token streams, so
numeric equality is not synchronization proof. Until P2b adds an explicit
causal pair-session contract, every market-relative contract is ineligible for
runtime promotion. Per-token mid/ask, spread, parity, full-depth and
microstructure factors are also P2b; the MVP must not invent them from the
current aggregate observation.
