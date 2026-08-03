# BTC Lightweight Factor Challenge Design

## Goal

在不等待新的 Polymarket 历史样本、也不下载新的大批量 1 秒数据的前提下，
使用本机已有的 BTCUSDT 1 秒 opening-proxy 数据训练并比较可解释的新增因子。
只补充体积较小的 Binance Spot 与 USD-M BTCUSDT 1 分钟 Kline，用于 spot/perp
跨市场因子。结果仅作为 development challenger，不替换 VPS champion。

## Fixed Evidence Boundary

- 统计单位仍是一个 BTC 15m market；同市场全部 5 秒快照共享 partition 和总权重 1。
- 所有外部 Kline 必须在 `close_time + 1s <= decision_ts` 后才可使用。
- 当前 protocol-v2 dataset 是 control；challenger 必须使用完全相同的 sample、label、weight 和 fold。
- 本轮只运行 development OOF，不打开 sealed holdout，不生成 runtime model artifact。
- 不从 forward Polymarket 数据补值，不将 Binance proxy 结果解释为可成交收益。
- 不新增 dependency，不启用真实订单，不改动 `paper-v3-independent-fak` VPS champion。
- 本轮仅允许补下载缺失的 Spot 与 USD-M BTCUSDT 1 分钟日归档；必须记录计划/实际
  字节数、SHA-256 与按日覆盖率，且不得下载 1 秒、aggTrades 或 depth 数据。

## Factor Families

固定比较以下家族，不在看到结果后新增候选：

1. `boundary_time`
   - `boundary_abs_z`
   - `boundary_z_elapsed_fraction`
   - `boundary_z_log_remaining`
   - `boundary_probability_margin`
2. `path_state`
   - 5/30、30/180、180/900 秒 return 差
   - 5/60、60/900、300/3600 秒 realized-volatility 比
   - 短长周期 return 同向乘积
3. `flow_value`
   - 5/60、30/300 秒 taker-flow 差
   - 5/300、30/900 秒成交量占比
   - 5/60、60/300、300/3600 秒 VWAP-distance 差
4. `cross_market`
   - 因果可用的 spot/perp 1 分钟 basis
   - 1/5/15 分钟 perp-minus-spot return
   - 1/5/15 分钟 perp-minus-spot taker-flow
   - basis 的 5/15 分钟变化

缺少任一 cross-market 历史窗口时，整个市场从 control 与所有 challenger
共同移除；不得只移除 challenger 行或填 0。

## Frozen Candidate Matrix

- `control_logistic`: 当前 schema，Logistic C=0.1。
- `boundary_logistic`: control + `boundary_time`。
- `state_logistic`: control + `path_state`。
- `flow_logistic`: control + `flow_value`。
- `cross_market_logistic`: control + `cross_market`。
- `all_logistic`: control + 全部家族。
- `all_lightgbm`: 与 `all_logistic` 相同 features，仅使用当前受限 small LightGBM 配置。

所有 6 个 challenger 相对 control 使用同一组 paired UTC-day block bootstrap；
LightGBM 对数据依赖选择的最佳 Logistic 另作第 7 个 replacement comparison。
全部 7 个预注册比较统一使用 Bonferroni `alpha=0.05/7`，确保 development
family-wise error rate 不超过 0.05。LightGBM
只有在 log loss、Brier、calibration error 和 paired confidence lower bounds
全部优于最佳 Logistic 时才可成为 development winner。

## Outputs

研究命令写入新的不可覆盖目录：

- `development_report.json`
- `predictions.parquet`
- `dataset_lineage.json`

报告必须标明 `development_only=true`、`sealed_holdout_evaluated=false`、
原 dataset SHA、spot/perp archive hashes、feature schema hashes、split/model
配置和 Git revision。任何数据、hash、时间或 schema 冲突均非零退出且不留下
看似完整的结果目录。

## Deployment Track

VPS 只部署 clean commit `c1ef4b2` 的 Research Paper、collector 和只读 dashboard。
保留宿主机 `deploy/runtime/data` 与 `deploy/runtime/output`，不迁移或重写旧 raw；
新 `paper-v3-independent-fak` ledger 使用新 epoch。部署必须完成 preflight、Compose
服务检查、`/healthz`、dashboard `/api/status`、ledger freshness 和至少一次完整
15 分钟市场轮换验证。真实订单能力继续禁用。
