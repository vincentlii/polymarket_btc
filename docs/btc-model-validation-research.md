# BTC 模型、概率与研究选择协议

## 结论

方向模型输出只有同时满足因果切分、独立校准、严格数值契约、受控模型选择和可验证 artifact lineage 时，才可被称为“可用于订单比较的概率”。一个模型能够输出分数，或在同一批样本上取得较低 loss，都不构成下单授权。

本项目固定采用以下边界：

- 一个 BTC 15m 市场是一个独立统计单位；同一市场的全部 5 秒快照必须位于同一 partition。
- development OOF test window 不重叠；sealed holdout 只在候选与阈值冻结后使用一次。
- estimator fit、LightGBM early stopping、probability calibration 和 sealed holdout 是互不重叠的时间分区。
- isotonic 的最低样本数按独立市场数计算，不按同一市场的快照行数计算。
- Logistic 默认保留；LightGBM 只有在 log loss、Brier、calibration error 和配对置信下界全部更好时才能替代。
- 概率、标签、权重、时间戳或 lineage 中出现 NaN、Infinity、越界值、布尔伪装整数、重复或不一致时立即失败，不裁剪成“看似合法”的证据。
- `joblib` artifact 只允许从本项目自己生成且受权限保护的 release 目录加载；SHA-256 只能发现损坏，不能把攻击者控制的 pickle 变成可信文件。

## 因果时间切分

`WalkForwardConfig` 的默认正式协议仍为 90 日 train、21 日 calibration、14 日 test/step、4h15m embargo 和 28 日 sealed holdout。`step_duration` 不得小于 `test_duration`，否则同一样本可能产生重复 OOF 预测并被重复计入统计结果。

所有 partition 使用 `feature_ts` 决定归属。train 与 calibration 还要求 `label_available_ts` 在该阶段截止时间前已经可用。跨越边界的市场组整体移到边界的一侧，不拆分快照。

[scikit-learn 的时间序列切分说明](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html)明确指出，时间排序数据不能用会让未来训练、过去测试的普通交叉验证。本项目不直接使用 `TimeSeriesSplit`，因为还需要市场分组、label availability 和按时间长度定义的 embargo，但遵守同一因果原则。

## 校准与独立样本

[scikit-learn probability calibration 文档](https://scikit-learn.org/stable/modules/calibration.html)要求 calibrator 理想情况下使用独立于 classifier fit 的数据，否则训练内预测会使校准结果偏向过度自信。项目因此禁止用 train 内概率拟合 calibrator。

同一市场的 36 个快照共享一个最终 Up/Down 标签，不能被当作 36 个独立校准样本。isotonic 的门槛由 calibration partition 的唯一 `group_id` 数量决定。官方文档同时指出 isotonic 在小样本下更容易过拟合，并通常需要约 1,000 个以上样本；本项目保留更保守的 2,000 个独立市场门槛。

LightGBM 的 early-stopping partition 按完整市场分组从 train 尾部按时间切出，不能随机抽行，也不能将同一市场同时放进 fit 与 validation。[LightGBM 官方说明](https://lightgbm.readthedocs.io/en/v4.6.0/pythonapi/lightgbm.early_stopping.html)要求至少一个 validation set 与 metric，并用 validation score 决定停止轮次。

## 相关时间数据的置信区间

相邻 UTC 日的波动、流动性与市场状态相关，不能把日期当成独立同分布点随机打散。模型配对 loss 与价格 edge 使用完整 UTC 日的 circular moving-block bootstrap，保留相邻日期结构，并用每日总 loss/收益分子和总权重计算重采样均值。

这一选择来自 Künsch 对平稳相关序列 block bootstrap 的原始工作：[The Jackknife and the Bootstrap for General Stationary Observations](https://doi.org/10.1214/aos/1176347265)。block bootstrap 不能消除非平稳性，因此报告仍必须分月、regime 与连续 test 周检查稳定性。

## 多重选择控制

价格研究在三个 regime 中各检查八个预注册阈值，共 24 个有限比较。若对每个阈值都使用普通 95% 下界后再挑最好的，会提高假阳性概率。

正式阈值选择使用 Bonferroni-adjusted bootstrap 下界，普通 95% CI 仅用于解释。CLI 默认至少 10,000 次重采样，使极端调整分位数不只由一两个 bootstrap draw 决定。[NIST Bonferroni 说明](https://www.itl.nist.gov/div898/handbook/prc/section4/prc463.htm)给出的原则是：对有限的 `g` 个预先指定比较，将每个区间的 alpha 调整为 `alpha/g`，以维持整体置信水平。

多个 LightGBM 候选相对最佳 Logistic 的 replacement CI 同样按实际 LightGBM 比较数调整。sealed holdout 不参与候选、calibrator 或 threshold 选择。

## Artifact 原子性与信任边界

模型保存先写同卷 sibling staging directory，刷新 model 与 metadata 文件，计算 SHA-256，验证 schema/config 一致后再原子 rename 为最终 immutable directory。失败会清除 staging，最终路径不会留下只有 model 或只有 metadata 的半成品。

metadata 必须记录完整模型 config、feature schema hash、train/calibration 时间范围、model hash，以及最终 fit、early-stopping、calibration 的 sample/market 数。load 时再次验证 hash、schema 和 config。
正式 Gate 还要求 clean Git provenance；dirty 或无法识别的工作区可以生成调试 artifact，但必须以 `dirty_or_unknown_code_provenance` 失败，不能伪装成某个 commit 的正式结果。

[joblib 官方文档](https://joblib.readthedocs.io/en/stable/generated/joblib.load.html)明确说明 `joblib.load` 基于 pickle，可能执行任意 Python 代码。因此：

- 不从网页、对象存储公共桶、聊天附件或未知主机直接加载 model artifact。
- VPS 只加载由本地 release 流程生成、上传后重新核对 hash、且目录不可由 bot 运行用户之外主体写入的 artifact。
- 若未来模型需要跨不可信边界分发，应迁移到可审计的安全格式，而不是依赖同目录 SHA 文件。

## Protocol 版本与迁移

`opening_proxy_protocol` 已升级为 version 2，并记录实际 cadence 对应的 regime 首末决策。例如 10 秒 cadence 的三个区间从 10、40、100 秒开始，而不是伪称从 3、35、95 秒开始。

version 1 artifact 不得静默用于 version 2 shadow/live。它仍可保留为历史研究证据，但部署前必须用当前代码重新训练、重新生成 OOF/holdout、重新执行 price-edge study，并产生新的 immutable model directory。

materialized dataset 的市场序列必须与请求 catalog 的精确、按时间排序 slug 序列一致；仅有相同 market count 不足以证明使用了相同日期或市场。

实时 `Research Paper` 只允许加载通过上述 schema hash 与 protocol v2 检查的固定
artifact，并使用与训练/回放相同的五秒 cadence 和两次连续信号确认。它不会重新
训练、调参或自动晋级模型。当前 artifact 仍标为 `Research Proxy`：Paper 的虚拟
PnL 可以产生新的 forward OOS 诊断证据，但不能补足少于 2,500 个 sealed holdout
市场、缺失的因果 Polymarket baseline，也不能替代正式 queue/latency BookReplay。

## Market-relative challenger protocol

The deployed Binance/Gamma Logistic model remains the unchanged control. The
market-relative path is a research-only challenger built on the exact subset of
markets with complete causal dual-token CLOB observations at every frozen
decision timestamp. Missing, future, stale, gapped or structurally invalid
observations remove the whole market from both control and challenger; values
are never filled with 0.5 or forward-filled trades.

Four Logistic candidates are pre-registered: paired control, market-logit
anchor, anchor plus boundary-market logit residual, and all available
time/quality interactions. Market logit is a feature rather than a claimed
fixed offset because the current sklearn/artifact boundary has no offset
contract. All candidates share identical grouped walk-forward partitions and
weights. Selection uses development OOF log loss, Brier, calibration error,
calibration slope and paired UTC-day block-bootstrap confidence intervals.
Sealed holdout is callable only after one challenger passes the development
gate and still requires at least 2,500 markets by default.

No runtime model is switched by this implementation. A research contract can
be produced only from the accepted typed sealed-holdout result associated with
the accepted development run. Development freezes deterministic hashes for the
complete paired dataset, control/challenger schemas and split/model protocol;
sealed evaluation rejects any replacement dataset or protocol. The contract
also binds the CLOB source hash, rule epoch, ingest version, factor families,
cadence and opening protocol.

This MVP cannot publish a runtime-loadable model. `OpeningMarketObservation`
does not yet carry a pair-level collector-session identity, and numeric epoch
IDs from independently reconstructed token streams are not comparable. The
contract is therefore marked `runtime_promotion_eligible=false` with
`pair_session_identity_not_proven`. P2b must add a causal pair-session contract
before runtime promotion; it must not require unrelated epoch integers to be
equal and call that synchronization evidence.

PM spread, imbalance and full-depth factors remain a later P2b extension
because the current `OpeningMarketObservation` does not causally persist those
fields.

## Lightweight opening-factor challenger

The lightweight challenger reuses the frozen protocol-v2 materialized dataset
and adds four causal families: boundary/time interactions, multi-horizon path
state, flow/value interactions, and Spot/Perpetual 1-minute cross-market state.
External minute bars become usable only after `close_time + 1s <= decision_ts`.
Spot and Perpetual must have the same latest `open_ts`, and each 15-minute
lookback must be independently contiguous; otherwise the whole market is
removed from control and every challenger exactly once.

The pre-registered development family contains six comparisons against
`control_logistic` plus one LightGBM replacement comparison against the
data-selected best Logistic. All seven use the conservative Bonferroni budget
`alpha=0.05/7`. The second comparison is calculated from the actual paired OOF
predictions; a LightGBM interval against control is never reused as evidence
against the best Logistic. Candidate progress is published atomically, but the
statistics and selection protocol remain unchanged by progress reporting.

The 2026-04-27 through 2026-07-12 development run retained no challenger. It
covered all 7,295 markets and 48,348 OOF predictions per candidate. Control
log loss/Brier/ECE were `0.648506/0.228951/0.016447`. `state_logistic` had the
best Logistic point loss (`0.648390/0.228871`) but worse ECE and both adjusted
loss intervals crossed zero. `all_lightgbm` had the best point loss
(`0.647788/0.228298`), but its adjusted log-loss/Brier intervals crossed zero
both versus control and versus `state_logistic`. This is a development No-Go:
sealed holdout was not opened, no runtime artifact was produced, and the VPS
champion remains unchanged.

## 验收标准

- 浮点/布尔/非有限标签无法进入训练。
- estimator 或 calibrator 返回非有限、越界、错误 shape 或不归一概率时失败。
- 同一市场不会跨 train、early stopping、calibration、test 或 sealed holdout。
- 每个 OOF test sample 恰好预测一次；holdout 预测与 holdout index 精确一致。
- isotonic 门槛使用 calibration 唯一市场数。
- model artifact 写入故障不留下最终目录；load 会复核 hash、schema 与 config。
- materialized 数据的 slug 顺序、sample timestamp、整数 label/elapsed 与 catalog 完全一致。
- price cache 不把“请求过但失败”当成证据；同 token/timestamp 的冲突价格失败。
- threshold selection 使用 24-comparison adjusted lower bound；sealed holdout 不参与选择。
- 分段阈值下任何中间失败信号都会重置连续信号计数，不能删除失败行后跨行拼成“连续”。
- 价格加压后达到或超过 1 的订单被删除，不能截断成 0.999999 来制造可成交结果。
- price-edge artifact 记录 prediction、catalog、price cache 与 coverage manifest 的 SHA-256。
