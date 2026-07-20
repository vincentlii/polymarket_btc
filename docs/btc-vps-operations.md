# BTC VPS 运行与迁移

## Scope

本文件定义首次 VPS 部署的边界：运行 BTC 15m 前瞻采集器和只读状态看板，持续保存原始数据、质量计数与运行状态。它不运行实盘下单、Canary 或自动扩容。

VPS 是不可变运行环境：本地完成代码、模型、回测与审查后，发布固定的 Git revision 或镜像；服务器只拉取该发布物、挂载数据目录并运行容器。不要在 VPS 上直接修改策略代码或训练模型。

## Runtime Boundary

| Component | Responsibility | Authority |
|---|---|---|
| `btc_forward_runtime.py` | 发现 BTC 15m 当前/下一市场，采集公开 CLOB、Chainlink 与 Binance 证据，写入质量与运行状态 | 无订单网关、无密钥、无交易权力 |
| `btc_runtime_dashboard.py` | 聚合运行健康、绩效快照、逐单盈亏与策略周期 | 只读 HTTP；没有下单、撤单、改参或停机接口 |
| `btc_runtime_control.py` | 写入或清除本地 stop request | 只能停止本项目运行进程；没有订单权限 |
| `btc_opening_proxy_shadow.py` | 对已完成的完整窗口作因果 Shadow 重建 | 不提交或模拟订单；不是实时成交证明 |
| `btc_opening_shadow_scheduler.py` | 可选地调度已落盘窗口的因果 Shadow，并按模型 SHA 保存输出 | 默认不启动；只做事后因果重建 |

`/healthz` 只表示运行器存活、状态新鲜且自身未报告失败。它不等同于交易所每条订阅都已成功送达；状态详情中的质量计数才是后续数据审计的输入。

Paper 网关只能在本地内存中使用。Canary/实盘路径仍需要独立的市场规则、账户、地区、时钟、SDK、手续费和用户频道对账验证，不能随 VPS 一起启用。

## Dashboard Data Contract

看板默认每 5 秒读取两个互相隔离的数据源：

- `status/*.json`：采集器、Shadow 和未来执行运行器的存活、状态新鲜度及数据质量。
- `dashboard/snapshot.json`：账户权益、累计/今日 PnL、回撤、资金曲线、最近市场级交易、连接/延迟健康、活跃告警和策略生命周期。

页面始终只读。快照不存在时，资金与逐单区域显示明确空状态；研究 Proxy、expected edge 和事后 Shadow 不得写成真实 PnL。Shadow 中没有真实订单时，下单延迟显示 `N/A`，不得显示为 `0 ms`。

生命周期使用 `Research → Challenge → Shadow → Canary → Live`，并显示 Champion、Challenger、阶段进度、数据截止时间、下次训练和下次评审。默认治理节奏是每日自动汇总、每 14 天冻结一个 Challenger、每 28 天或达到预注册样本门槛后进行一次人工 Promotion Review；不得自动替换 Champion。规则、fee、tick、schema、数据源、漂移或延迟异常可提前触发 Challenge 或降级。

界面与字段取舍依据记录在 [BTC Bot Dashboard Research](btc-dashboard-research.md)。真实 Paper/Canary/Live 运行器只有在账户 ledger、订单状态和 User channel 对账完成后，才允许写入绩效快照。

## Local Validation

在本地工程目录中，先确认当前模型、配置与数据版本，再启动采集器：

```powershell
uv run python scripts/btc_forward_runtime.py `
  --rule-epoch <verified-current-rule-epoch>

uv run python scripts/btc_runtime_dashboard.py
```

默认运行目录是 `output/btc_short_horizon/runtime`。看板默认地址是 `http://127.0.0.1:8080`。

安全停机只使用控制脚本：

```powershell
uv run python scripts/btc_runtime_control.py --request-stop operator_request
uv run python scripts/btc_runtime_control.py --show
uv run python scripts/btc_runtime_control.py --clear-stop
```

在发布前至少通过当前改动相关测试、格式检查和完整 BTC 测试组。模型训练、参数选择和 Replay 继续在本地完成；VPS 不下载大规模历史 K 线，也不做调参。

## Deployment

首次部署只需要 Docker Engine 与 Compose。将 `.env.example` 复制为部署目录中的 `.env`，填入经过当期 Gamma/市场规则核实的 `BTC_RULE_EPOCH`。不要从旧市场或旧文档盲目复制该值。

```bash
cd /path/to/polymarket_btc
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env，填写 BTC_RULE_EPOCH
mkdir -p deploy/runtime/data deploy/runtime/output
sudo chown -R 10001:10001 deploy/runtime

docker compose --env-file deploy/.env -f deploy/compose.yaml up --build -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs -f forward_collector
```

容器镜像不包含 `data/` 或 `output/`。它们被挂载到宿主机：模型、原始数据和运行状态都在 `deploy/runtime/` 下，替换 VPS 时只需迁移这个目录与发布代码。

当且仅当本地已经验证过模型 artefact 后，可在 `.env` 设置
`BTC_MODEL_DIRECTORY` 并启用可选 Shadow profile。它会在市场窗口和数据
handoff 完成后调用既有 Shadow pass；不会提交任何订单：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile shadow up -d
```

看板端口默认绑定到 VPS 的 `127.0.0.1`，不应直接暴露到公网。通过 SSH 隧道查看：

```bash
ssh -N -L 8080:127.0.0.1:8080 <user>@<vps-host>
```

随后在本机浏览器打开 `http://127.0.0.1:8080`。私有 overlay 网络可在后续单独审查后加入；首次部署不开放公网管理面。

停止采集器时，先写入 stop request。由于采集器使用 `on-failure` 重启策略，正常停机不会被自动拉起：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml exec forward_collector \
  python scripts/btc_runtime_control.py \
  --runtime-root /app/output/btc_short_horizon/runtime \
  --request-stop operator_request
```

需要重新启动时，在一次性容器中清除 stop request，再启动采集器：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml run --rm --no-deps \
  --entrypoint python forward_collector \
  scripts/btc_runtime_control.py \
  --runtime-root /app/output/btc_short_horizon/runtime \
  --clear-stop
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d forward_collector
```

## Data Backup And Restore

前瞻原始数据使用 v9 session inventory，而不是按目录中文件数量判断完整性。每次安全停机后先运行 audit；至少每天把 complete sessions 上传到异地对象存储并执行全量下载校验。当前容器镜像不包含 `rclone`，首次部署由宿主机安装并配置 rclone，再从固定代码 revision 运行 `scripts/btc_data_archive.py`。

```bash
uv run python scripts/btc_data_archive.py \
  --raw-data-root deploy/runtime/data/btc_short_horizon \
  audit --require-closed

uv run python scripts/btc_data_archive.py \
  --raw-data-root deploy/runtime/data/btc_short_horizon \
  backup \
  --transport rclone \
  --remote btc-archive:polymarket-btc/forward \
  --temporary-root deploy/runtime/tmp
```

备份命令只有在重新下载并核对 snapshot 中每个对象后才写 verification receipt。snapshot ID 与 receipt 路径必须同步到本地电脑或独立运维记录。对象存储至少开启 versioning，并使用与主 VPS 隔离、无历史版本删除权限的凭据。

本地空间清理默认只输出计划。实际增加 `--apply` 前必须停掉 collector、核对 cutoff/session/file/byte count，并确认最近一次恢复演练成功。restore 和实际 archive 会取得 collector 同一把单写者锁；拿不到锁即退出，不允许边采集边删除或恢复。

完整命令、崩溃语义、bucket 权限与恢复验收见 [BTC 前瞻数据持久性与灾难恢复](btc-data-durability-research.md)。

## Migration

迁移前先写入 stop request，确认采集器已退出，运行 `audit --require-closed`，并完成最新 snapshot 的远端全量校验。复制以下内容到新 VPS：

1. 固定的代码 revision 与 `deploy/.env`（通过安全渠道传输，绝不提交）；
2. `deploy/runtime/data/`，其中包括原始前瞻数据与模型 artefact；
3. `deploy/runtime/output/`，其中包括 WAL、状态、Shadow 输出和报告。

优先从已验证 snapshot 恢复 `deploy/runtime/data/btc_short_horizon`，再复制其余 runtime 输出。在新 VPS 创建同样的目录权限后先运行 data audit，最后执行 `docker compose ... up --build -d`。迁移不需要重新训练模型或修改策略；只有新代码、配置、规则 epoch 或模型版本经过本地验证后才发布新的不可变版本。

## Pre-Purchase Validation Status

本地购买前验收已经覆盖完整的 180 秒窗口：双 token CLOB Shadow 的 36 个决策点全部完成，零订单提交；增强的 Binance Spot/Perpetual、Chainlink 与 CLOB 特征审计观察到 36/36 个决策点，其中 32 个满足严格的一秒数据新鲜度，另外 4 个按设计 fail closed。运行器已验证市场 handoff、stop request、pending event 清空、feed health、磁盘状态和只读看板状态投影。

购买首尔 VPS 后仍必须在目标 IP 上完成、且不能由本地替代的检查包括：Geo-block/法律资格、到 CLOB/RTDS/Binance 的实际 P50/P95/P99 RTT、NTP 偏差、Docker 镜像构建与重启策略、长期断线恢复、真实账户 user channel 对账，以及后续最小规模 Canary。首次部署只启用 collector、Shadow scheduler 与 dashboard，不启用真实订单网关。

## Release Gates

购买或启用 VPS 不会改变以下上线门槛：

- 完整 180 秒、36 个决策点的前瞻窗口必须先通过因果 Shadow 覆盖审计。
- 可选 Shadow scheduler 是窗口结束后的因果重建，不是在线订单延迟或 passive fill 的证明。
- 当前方向/价格 proxy 只说明研究优先级，不能证明可成交 maker edge。
- 未取得 L2/TradeTick 证据、悲观 queue、P99 延迟、费用和容量测试前，不得把 Shadow/Paper 结果解释为实盘盈利能力。
- 账户、地区与法律资格必须由操作者独立确认；服务器区域不能用于规避 Polymarket 的资格限制。
- 实盘前必须增加一份专门的 Canary 发布审查；本 Compose 文件不包含密钥或真实订单客户端。

相关市场资格、订单与费用行为以 Polymarket 的当期官方文档为准：[Geo-block API](https://docs.polymarket.com/api-reference/geoblock)、[订单创建](https://docs.polymarket.com/trading/orders/create)、[费用](https://docs.polymarket.com/trading/fees)。
