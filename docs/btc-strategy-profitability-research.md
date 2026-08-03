# BTC 15m Bot 净收益优化研究

更新时间：2026-07-31

## 结论

当前最应优先优化的不是“继续堆因子”或“把 Python 再快几毫秒”，而是先把**模型相对 Polymarket 可执行价格的净 edge**与**每种执行路线自己的机会定义**做正确。

建议优先级：

1. 以完整深度、实际 fee、延迟后盘口计算 maker / taker 各自的可执行净 edge，并补齐每个拒绝环节的分布诊断。
2. 将当前方向模型升级为“市场相对/残差模型”，把剩余时间与三个开盘阶段显式纳入。
3. 只增加有经济含义、可做独立 ablation 的因子族，优先 Polymarket L2 与跨市场时差，而非继续堆通用技术指标。
4. 在 forward OOS 证明存在 edge 后，再按实测 edge decay 优化网络、签名和代码热路径。

这只是研究优先级，不构成盈利承诺。当前 Paper 样本尚不足以证明策略可盈利。

## 一手证据

### 1. Polymarket 的显示价格不是可成交价格

**证据：**Polymarket 的 midpoint 只是 best bid 与 best ask 的平均值；真正买入要吃 ask。官方同时提供完整 order book，并明确建议按深度估算给定规模的实际成交价。参见 [Prices and Order Books](https://docs.polymarket.com/market-data/prices-order-books)。

**项目推断：**模型不应只比较 `p_fair - midpoint`。对 FAK 应比较：

```text
taker_net_edge
= p_fair
- latency 后完整 ask depth 的可成交 VWAP
- 每价位 taker fee
- slippage buffer
- model uncertainty buffer
```

Maker 则应单独比较被动挂单价格、成交概率和 adverse-selection 损耗。两个路线不应被同一个价格门槛替代。

### 2. Maker、FAK 的收益与成交语义不同

**证据：**FAK 会立即成交当前可得数量并取消剩余；post-only 只能配合 GTC/GTD，若跨过盘口会被拒绝。FAK 的 `price` 是最差成交价保护，不是预期成交价。参见 [Create Order](https://docs.polymarket.com/trading/orders/create)。

**证据：**官方当前说明，部分 crypto/finance Up/Down 市场会对 marketable order 施加 250ms taker delay；延迟期间订单不能取消，延迟结束后才重新验证并撮合。2026-07-31 抽查的 BTC 15m CLOB market metadata 返回 `itode=true`。参见 [Order Lifecycle](https://docs.polymarket.com/concepts/order-lifecycle) 与 [抽查市场 metadata](https://clob.polymarket.com/clob-markets/0xc2787e6e9e653431c47513560d210b3a7b71a32a5e6ac8fbd0e79bb9c3ce493f)。

**项目推断：**服务端 taker delay 必须与客户端 decision→POST、网络、签名和响应延迟分开建模，不能被一个更小的静态总延迟替代。当前 Paper 的 200ms `order_latency_ms` 低于已确认的 250ms 服务端延迟，因此现有 FAK PnL 只能视为偏乐观的研究结果；下一轮应至少重放 250ms 服务端延迟叠加实测网络延迟的情景。

**证据：**当前官方费率按 `C × feeRate × p × (1-p)` 计算；maker 不收交易费，taker 收费，且参数应从具体市场读取。当前 Crypto 表列示 `feeRate=0.07`。参见 [Fees](https://docs.polymarket.com/trading/fees)。

**证据：**Crypto maker rebate 当前为 20%，但它按市场内相对贡献分配、每日支付且参数可变；taker rebate 也取决于最近 30 日加权成交量，低于 2,000 wV 的 Tier 0 为 0%。参见 [Maker Rebates](https://docs.polymarket.com/programs/maker-rebates) 与 [Taker Rebates](https://docs.polymarket.com/programs/taker-rebates)。

**项目推断：**正式基础结论仍应把 maker/taker rebate 都设为 0；只有取得真实账户级 payout 后，才单列为增量情景。不能靠奖励把负交易 edge 包装成正收益。

### 3. Polymarket 价格本身必须作为强基线

**证据：**Wolfers 与 Zitzewitz 的原始研究给出一组充分条件，并发现预测市场价格通常是平均信念的有用、虽可能有偏的估计。[Interpreting Prediction Market Prices as Probabilities](https://www.nber.org/papers/w12200)

**项目推断：**只预测 BTC 最终 Up/Down，即使准确率略高，也不代表能击败已经包含集体信息的 Polymarket 价格。更贴近交易目标的建模方式是：

```text
target / decision = calibrated outcome probability - executable market price - costs
```

模型可以输出 `p_fair`，但训练、选择与下单门槛必须相对当时因果可见的 Polymarket 价格评估；Polymarket 数据不能只是报告期对照。

### 4. L2 与 order flow 是值得验证的因子族，但不是盈利证明

**证据：**Cont、Kukanov、Stoikov 在原始实证研究中发现，短时间价格变化与 order-flow imbalance 呈稳健关系，影响斜率与市场深度负相关，而单纯 trade volume 的关系更噪声。[The Price Impact of Order Book Events](https://arxiv.org/abs/1011.6402)

**证据：**Sirignano 与 Cont 在大规模 order-book 数据上发现，价格与订单流的历史序列能改善样本外方向预测。[Universal Features of Price Formation](https://arxiv.org/abs/1803.06917)

**限制：**以上结果来自股票 limit order book，不是 Polymarket BTC 15m 的直接证据。

**项目推断：**值得优先验证的新增因子是 Polymarket 双 token 深度、microprice、multi-level imbalance、撤单/新增/成交 OFI、spread、book age，以及 Binance/Chainlink 与 Polymarket 的短时信息差。每个因子族必须通过独立 OOF ablation；没有增量净 edge 就删除。

### 5. Maker 的低成交率与 adverse selection 是同一个优化问题

**证据：**Lehalle 与 Mounjid 的研究将 limit-order placement、liquidity imbalance、adverse selection 与 latency 放在同一框架中；对买入限价单，越可能出现价格下跌时越容易成交，而 latency 会削弱利用流动性信息撤单/重挂的价值。[Limit Order Strategic Placement with Adverse Selection Risk and the Role of Latency](https://arxiv.org/abs/1610.00261)

**证据：**使用不真实 fill probability、且没有追踪 adverse fills，会夸大短周期策略回测表现。[Market Simulation under Adverse Selection](https://arxiv.org/abs/2409.12721)

**项目推断：**不能通过单纯把 maker 价格挂得更激进来追求成交率。优化目标应是：

```text
每个候选机会的期望收益
= fill_probability × conditional_net_edge
```

必须同时看未成交机会成本、fill 后 1/3/10/30/60 秒 markout、queue 压力和 cancel race。

### 6. 更快只会保护已有 edge，不会制造 edge

**证据：**Polymarket 官方说明 matching engine 主区域为 `eu-west-2`，最近的非受限区域为 `eu-west-1`。[Trading Overview](https://docs.polymarket.com/trading/overview)

**项目推断：**首尔 VPS 到主撮合区的网络路径在实盘 maker 撤单与 taker 报价变化上可能吃亏，因此 Canary 必须实测 decision→POST→ack、user-channel fill、cancel→ack 与 edge decay。但当前 Paper 若连策略机会都没有，把本地代码从数毫秒优化到更低不会提高机会数。只有当“决策时 edge 为正、P99 到达时 edge 消失”成为主要拒绝原因，迁移近端 VPS、预签名或改写热路径才升为最高优先级。

### 7. 增加因子和阈值会迅速增加回测过拟合风险

**证据：**Bailey 等人的原始研究指出，在同一历史样本上尝试大量策略变体而不校正多重检验，会提高 false positive；普通 holdout 也不能消除反复查看同一 holdout 造成的选择偏差。[Backtest Overfitting in Financial Markets](https://escholarship.org/uc/item/4hn4t174)

**项目推断：**不能看到 Paper 结果后连续放宽 threshold、改阶段、加因子，再把同一批数据称为 OOS。应预注册少量候选、保留 trial ledger、使用不重叠时间 fold、block bootstrap、多重比较校正与一次性 sealed holdout。

## 对当前实现的具体判断

当前 baseline 是：

- 3–180 秒、5 秒 cadence、连续两次确认；
- maker 需要 `1¢ safety_buffer + 10¢ minimum_edge`；
- direct FAK 和 maker→FAK 使用 `3¢ minimum_taker_net_edge + 0.5¢ slippage + 3¢ model uncertainty`，再扣真实 fee 与深度；
- 每个市场最多一次 placement cycle，核心价格区间为 20%–80%。

当前 `immediate_fak` 仍先经过 `plan_opening_mispricing_orders()` 的 maker 机会门槛，然后才执行自己的 FAK 净 edge 检查。因此：

- 优点：三个路线在同一 maker opportunity 上配对，比较公平；
- 缺点：存在“FAK 自己满足净 edge、但 maker 还没达到 11¢ 门槛”的候选被提前排除。

以 50¢、当前官方 Crypto fee 为例，当前 FAK 的原始概率差约需覆盖 `1.75¢ fee + 0.5¢ slippage + 3¢ uncertainty + 3¢ minimum net edge = 8.25¢`，尚低于 maker 的 11¢门槛；具体值随价格、深度和市场 fee 参数变化。这个约 8.25¢–11¢ 的区间就是值得用独立 taker policy 离线验证的候选区，而不是直接放宽生产阈值。

## 建议的三个冻结 Challenger

### A. 当前共享机会策略（Control）

保留现状，用于 maker / direct FAK / maker→FAK 的严格 paired comparison。

### B. Taker 独立机会策略（最高优先级）

在同一 prediction tick 上直接按延迟后完整 ask depth 计算 FAK 净 edge，不要求先生成 maker plan。只预注册 2¢、3¢、4¢ 三个净 edge 门槛，仍保留两次确认、一次 placement、20%–80% Go 区间和不追单。

比较单位应是共同的 prediction tick / market，而不是共同 maker opportunity；同时报告：候选数、FAK 有深度数、成交份额、净 EV/机会、净 EV/成交份额、markout 与 CI。

### C. 市场残差重估模型（中期主线）

先用 Logistic 建立可解释基线，再让受限 LightGBM 挑战。输入只分三组：

1. `market-relative`：Up/Down midpoint、executable ask、spread、双 token 一致性、price age；
2. `time/option state`：距离开盘价、remaining seconds、remaining-volatility-normalized distance、当前波动状态；
3. `microstructure`：Polymarket L2/OFI，以及可用时的 Binance/Chainlink lag、momentum 与 order flow。

不要立刻训练三个完全独立模型。先用共享模型加 `regime × feature` interaction；只有各阶段都有足够的独立市场数，且分模型在 OOF 的 log loss、Brier、calibration 与净 edge 下界同时更好时再拆分。

## 决策顺序

| 方向 | 当前优先级 | 原因 |
|---|---:|---|
| 策略机会定义与净 edge 诊断 | P0 | 当前收益首先受“是否产生可交易机会”限制；maker 与 taker 不应共享不必要的门槛。 |
| 市场残差/分时段模型 | P1 | 直接针对“模型比市场多知道什么”，比提高裸方向准确率更贴近 PnL。 |
| 有假设的 L2/跨市场因子 | P1 | 有一手微观结构证据支持，但必须在本市场 OOF 验证。 |
| Maker 报价/等待时长优化 | P2 | 要等 forward fill 与 adverse-selection 样本；只追成交率会恶化质量。 |
| VPS/热路径性能 | P2/P3 | 对保存短暂 edge 重要，但不能解释当前零机会；先量化 edge decay。 |
| 大量新增技术指标/模型复杂度 | 不建议 | 最容易扩大搜索空间并产生假阳性。 |

## 最小验收标准

任何“优化”至少同时满足：

- development OOF 的 log loss、Brier 或 calibration 不恶化；
- rebate=0、完整 taker fee/深度、P99 latency 下净 EV/机会为正；
- block-bootstrap CI 与多重比较规则通过；
- 分阶段、价格区间、月份不由单一桶贡献主要 PnL；
- 新特征或新阈值的全部试验次数进入 trial ledger；
- sealed holdout 只在候选与阈值冻结后打开一次。

若上述门槛未通过，应保留 Paper/Shadow，不应通过加仓、依赖 rebate 或进一步反复调参来“提高收益”。
