# BTC 15m P0-P2 优化实施计划

> 实施必须遵守 `docs/btc-short-horizon-architecture.md`、execution latency、market-data protocol、model validation 文档中的因果与 fail-closed 边界。

## Task 1：P0 规则与 server delay

**Files:** `btc_short_horizon/live/paper_execution.py`、`paper_runtime.py`、`paper_replay.py` 及对应 tests。

1. 先写规则 JSON/hash、`itode` 解析和 FAK 到达时间失败测试。
2. 为规则增加不可变的 `taker_server_delay_ms`，未知协议 fail closed。
3. 将 client/server/total latency 写入执行证据与 replay。
4. 运行 focused tests。

## Task 2：P0 结构化执行诊断

**Files:** `paper_execution.py`、`research_paper.py`、dashboard projection 及对应 tests。

1. 先写每个拒绝分支和 edge 分解测试。
2. 增加封闭 rejection reason 与 taker evaluation 数据结构。
3. 持久化到 ledger，并在 dashboard funnel/variant 指标聚合。
4. 验证旧 ledger 不被新 epoch 读取。

## Task 3：P1 独立 taker planner

**Files:** 新增 `strategy/taker.py`，修改 strategy exports、config、research runtime/tests。

1. 先写 maker 无计划仍成交、双侧选择、跨档深度、fee、minimum size、balance、stale book 测试。
2. 实现纯函数 planner，不依赖 maker `OrderPlan`。
3. 新增 independent mode 并接入 portfolio。
4. 保留原 variants 作为 control。

## Task 4：P1 edge stability

**Files:** `strategy/confirmation.py`、config、baseline TOML、runtime/tests。

1. 先写同侧但 edge 衰减、cadence reset、稳定窗口通过测试。
2. 扩展确认器或新增专用确认器，避免改变 maker control 语义。
3. 配置 `independent_fak_2x5s` 与 `independent_fak_stable_3x5s`。
4. 升级 paper epoch 并验证 variant IDs/primary 唯一性。

## Task 5：P2 数据集与 schema

**Files:** `research/opening_dataset.py`、`opening_evidence.py`、`features/opening.py`、新 residual 模块及 tests。

1. 写同步市场价格、price age、dual-token parity、available timestamp 和缺失组失败测试。
2. 定义 factor group schema 与 causal row builder。
3. 只接受明确同步且通过 freshness/parity 的 CLOB evidence。

## Task 6：P2 residual model 与 OOF ablation

**Files:** 新 `models/market_residual.py`、`research/market_residual.py`、artifact/CLI/tests。

1. 写 offset 概率、数值边界、合成信号拟合、grouped split、sealed holdout 不参与选择测试。
2. 实现 Logistic offset/residual estimator。
3. 实现 factor-group ablation、paired metrics 和 block-bootstrap CI。
4. artifact metadata 绑定 schema/source/protocol；数据不足不发布。

## Task 7：文档、UML 与验证

1. 更新 architecture、latency、model validation、project status 和研究说明。
2. 运行 `scripts/generate_codebase_uml.py`。
3. 运行 focused tests、`ruff check`、`ruff format --check`、全部 BTC tests。
4. 做规格审查与代码质量审查，修复后复验。
5. 本轮不 commit、push 或部署；向用户报告本地结果与待上线门槛。
