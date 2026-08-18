# BTC VPS 运行与迁移

## Resource Boundary

Never run `btc_data_archive audit` across the entire raw root while the 4 GB
production collector is active. A 2026-08-13 full hash audit reached roughly
800 MB RSS and caused the kernel OOM killer to restart the collector. Routine
checks use the resource-limited incremental readiness service. Deep archive
verification is an explicit low-traffic maintenance job after memory, swap and
rollback state have been checked.

## Scope

本文件定义当前 VPS 部署边界：运行 BTC 15m 前瞻采集器、实时
`Research Paper` 和只读看板，持续保存原始数据、模拟订单/盈亏、质量计数与
运行状态。它不加载账户凭据，不发送真实订单，也不运行 Canary 或自动扩容。

VPS 是不可变运行环境：本地完成代码、模型、回测与审查后，发布固定的 Git revision 或镜像；服务器只拉取该发布物、挂载数据目录并运行容器。不要在 VPS 上直接修改策略代码或训练模型。

## Runtime Boundary

| Component | Responsibility | Authority |
|---|---|---|
| `btc_forward_runtime.py` | 发现 BTC 15m 当前/下一市场，采集公开 CLOB、Chainlink 与 Binance 证据，写入质量与运行状态 | 无订单网关、无密钥、无交易权力 |
| `ResearchPaperRuntime` | 对采集器已接纳的实时事件执行模型、两次确认、模拟挂单/queue/cancel race、Gamma 结算与虚拟账本 | 只允许内存 `PaperOrderGateway`；无网络下单能力 |
| `btc_runtime_dashboard.py` | 聚合运行健康、绩效快照、逐单盈亏与策略周期 | 只读 HTTP；没有下单、撤单、改参或停机接口 |
| `btc_runtime_control.py` | 写入或清除本地 stop request | 只能停止本项目运行进程；没有订单权限 |
| `btc_opening_proxy_shadow.py` | 对已完成的完整窗口作因果 Shadow 重建 | 不提交或模拟订单；不是实时成交证明 |
| `btc_opening_shadow_scheduler.py` | 可选地调度已落盘窗口的因果 Shadow，并按模型 SHA 保存输出 | 默认不启动；只做事后因果重建 |

`/healthz` 只表示运行器存活、状态新鲜且自身未报告失败。它不等同于交易所每条订阅都已成功送达；状态详情中的质量计数才是后续数据审计的输入。

Paper 使用官方公开 CLOB market-info 验证 token、tick、minimum size、neg-risk
和 fee，但订单网关只在内存中运行。它采用 P99 latency、完整可见 queue 与 50%
卖方主动成交量的悲观 heuristic；结果只证明在线流程和模拟表现，不证明实盘可成交
edge。Canary/实盘仍需独立的账户、地区、时钟、SDK 和 User channel 对账审查。

## Dashboard Data Contract

看板默认每 5 秒读取两个互相隔离的数据源：

- `status/*.json`：采集器、Research Paper、Shadow 和未来执行运行器的存活、状态新鲜度及数据质量。
- `dashboard/snapshot.json`：明确标为模拟的虚拟权益、累计/今日 PnL、回撤、资金曲线、最近市场级订单、连接/延迟健康、活跃告警和策略生命周期。

页面始终只读。快照不存在时，资金与逐单区域显示明确空状态；研究 Proxy、expected edge 和事后 Shadow 不得写成真实 PnL。Shadow 中没有真实订单时，下单延迟显示 `N/A`，不得显示为 `0 ms`。

生命周期使用 `Research → Challenge → Shadow → Canary → Live`，并显示 Champion、Challenger、阶段进度、数据截止时间、下次训练和下次评审。默认治理节奏是每日自动汇总、每 14 天冻结一个 Challenger、每 28 天或达到预注册样本门槛后进行一次人工 Promotion Review；不得自动替换 Champion。规则、fee、tick、schema、数据源、漂移或延迟异常可提前触发 Challenge 或降级。

界面与字段取舍依据记录在 [BTC Bot Dashboard Research](btc-dashboard-research.md)。
Research Paper 只能从独立 `paper/ledger.json` 写入带模拟标签的投影；Canary/Live
只有在真实账户 ledger、订单状态和 User channel 对账完成后才能写账户绩效。

## Local Validation

在本地工程目录中，先确认当前模型、配置与数据版本，再启动采集器：

```powershell
uv run python scripts/btc_forward_runtime.py `
  --rule-epoch <verified-current-rule-epoch> `
  --paper-model-directory <validated-model-artifact-directory>

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

### 目标主机强制预检

先在目标 VPS 创建两个彼此独立、不会被容器镜像替换的目录，并确保它们是
普通目录而不是 symlink。预检必须针对将要发布的 clean Git revision 和当前
已核实的 rule epoch 执行；不能使用 `latest`、dirty worktree 或占位值：

```bash
mkdir -p deploy/runtime/data deploy/runtime/output deploy/runtime/preflight
test -f deploy/.env || cp deploy/.env.example deploy/.env
# 先填写并核对 deploy/.env，再执行预检。
sudo chown -R 10001:10001 deploy/runtime

uv run python scripts/btc_vps_preflight.py \
  --code-revision "$(git rev-parse HEAD)" \
  --rule-epoch "$BTC_RULE_EPOCH" \
  --compose-env-file deploy/.env \
  --data-root deploy/runtime/data \
  --output-root deploy/runtime/output \
  --runtime-root deploy/runtime/output/btc_short_horizon/runtime
```

命令会检查 release identity、目录耐久性/可写性/容量、host NTP、官方
Geo-block、CLOB clock offset，以及 CLOB、Gamma、Binance 的
P50/P95/P99。任何一项失败都会返回非零并写入不可变、content-addressed
receipt。报告不保存公网 IP 或凭据。当前官方限制同时把 `GB` 与 `DE` 列为
blocked，因此伦敦和法兰克福不能用于开仓；首尔也必须以目标 IP 的实际返回
为准，不能根据城市名称推断资格。

首次部署不创建 `deploy/secrets/`，也不配置任何 `POLY_*` 凭据。未来只有
独立 Canary 发布审查通过后，才允许由宿主机 secret manager 创建权限最小的
secret files，并通过 mutually exclusive `POLY_*_FILE` 变量注入。文件必须是
absolute、regular、non-symlink、UTF-8；不得进入 Git、镜像 build context、
日志、WAL 或状态报告。

第一次部署必须使用全新的空 `deploy/runtime/data` 和
`deploy/runtime/output`，不得复制当前本地 `data/btc_short_horizon`。本地目录
混有历史 v2--v8 epoch 与未关闭的旧 session；它们只保留作不可变研究 provenance，
不能修补或迁移成 v13 证据。部署前只复制已验证的 protocol v2
模型目录
`output/btc_short_horizon/research/opening-proxy-protocol-v2-clean-20260428-20260713/model/`
到 `deploy/runtime/data/btc_short_horizon/models/<model-id>/`，不要复制同级的
`dataset.parquet`、旧 raw 或旧 Shadow 输出。VPS 必须重新采集 fresh v13 数据。

首次部署只需要 Docker Engine 与 Compose。将 `.env.example` 复制为部署目录中的
`.env`，填入经过当期 Gamma/市场规则核实的 `BTC_RULE_EPOCH`、测试过的完整
`BTC_CODE_REVISION`，以及容器内的 `BTC_MODEL_DIRECTORY`。不要从旧市场或旧文档
盲目复制 rule epoch。

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

For an upgrade, preserve the currently deployed Compose file as an untracked
operational artifact before switching to the candidate revision. The release command takes
that file explicitly; a failed deployment uses it with the prior image and
`--remove-orphans`, restoring the old service topology as well as the old code:

```bash
cp deploy/compose.yaml deploy/compose.previous.yaml
uv run python scripts/btc_release.py \
  --release-sha "$(git rev-parse HEAD)" \
  --env-file deploy/.env \
  --previous-compose-file deploy/compose.previous.yaml \
  --receipt-root deploy/runtime/output/btc_short_horizon/releases \
  --rule-epoch "$BTC_RULE_EPOCH" \
  --data-root deploy/runtime/data \
  --output-root deploy/runtime/output \
  --runtime-root deploy/runtime/output/btc_short_horizon/runtime
```

Do not derive the previous Compose file after checking out the candidate: it
must be the exact topology that produced the currently running containers.

容器镜像不包含 `data/` 或 `output/`。它们被挂载到宿主机：模型、原始数据和运行状态都在 `deploy/runtime/` 下。后续替换 VPS 时只能迁移通过当前 audit 与远端全量校验的 runtime snapshot；首次部署不迁移历史本地 raw。

基础 Compose 会启动实时 Research Paper；它的模型目录因此是必填项。可选
`shadow` profile 复用同一 artifact，在市场窗口和数据 handoff 完成后再运行一遍
事后因果重建；两者都不会提交真实订单：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile shadow up -d
```

`BTC_CODE_REVISION` 只在 image build 时注入，并同时写入 image ENV 与 OCI revision label。
Compose service 不得再次用 runtime environment 覆盖它，否则旧 `.env` 会让看板声明的版本与
容器实际代码分离。每次发布先让 `deploy/.env` 的完整 SHA 与待测 release 一致；preflight 会在
任何网络检查和容器替换前拒绝不一致。部署后必须核对 image label、三个容器的 image ID 与
容器内 `BTC_CODE_REVISION` 完全一致。

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

前瞻原始数据沿用 v9 引入的 session inventory（当前写入 epoch 为 v13），而不是
按目录中文件数量判断完整性。每次安全停机后先运行 audit。若按当前决定使用移动
硬盘而非 COS，应每几天安全停机，把 complete sessions 拉到本机暂存目录，再在
连接移动硬盘的本机使用 `--transport local` 创建内容寻址 snapshot 并全量回读验证；
VPS 上未验证的普通文件复制不算备份。

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

移动硬盘在本机挂载后可替换为：

```bash
uv run python scripts/btc_data_archive.py \
  --raw-data-root /local/staging/btc_short_horizon \
  backup \
  --transport local \
  --remote /media/polymarket-btc/forward \
  --temporary-root /local/staging/tmp
```

`/local/staging` 必须来自一次完整、安全停机后的 VPS 同步，并在备份前通过
`audit --require-closed`。移动硬盘不在线期间 VPS 仍是单点故障，因此同步间隔就是
可接受的数据损失窗口；空间接近阈值时必须提前同步，不能边采集边删。

备份命令只有在重新下载并核对 snapshot 中每个对象后才写 verification receipt。snapshot ID 与 receipt 路径必须同步到本地电脑或独立运维记录。对象存储至少开启 versioning，并使用与主 VPS 隔离、无历史版本删除权限的凭据。

本地空间清理默认只输出计划。实际增加 `--apply` 前必须停掉 collector、核对 cutoff/session/file/byte count，并确认最近一次恢复演练成功。restore 和实际 archive 会取得 collector 同一把单写者锁；拿不到锁即退出，不允许边采集边删除或恢复。

完整命令、崩溃语义、bucket 权限与恢复验收见 [BTC 前瞻数据持久性与灾难恢复](btc-data-durability-research.md)。

### Runtime Evidence Backup

Raw session backup 完成后，再保护 model、WAL、exact ledger 和 report。执行前
必须先通过 runtime control 安全停止 collector，并确认 Compose 中相关服务均已
退出；未来 Live 服务还必须先 halt、完成 reconciliation 与 WAL checkpoint/
rotation。四个 root 必须是实际配置路径，不能把整个 `deploy/runtime` 当成一个
root，也不能包含 `deploy/secrets`：

```bash
uv run python scripts/btc_runtime_archive.py \
  --repository-root deploy/runtime/output/btc_short_horizon/recovery \
  backup \
  --release-revision "$(git rev-parse HEAD)" \
  --rule-epoch "$BTC_RULE_EPOCH" \
  --source model="$MODEL_ROOT" \
  --source wal="$WAL_ROOT" \
  --source ledger="$LEDGER_ROOT" \
  --source report="$REPORT_ROOT" \
  --transport rclone \
  --remote btc-archive:polymarket-btc/runtime \
  --temporary-root deploy/runtime/tmp
```

至少每周把同一 snapshot 恢复到新的隔离目录，再运行带
`--require-restore-drill --max-restore-age-hours 168` 的 `audit`。恢复成功不
会自动替换生产目录；先核对 release revision、rule epoch、WAL 全链、ledger
pointer、模型 manifest 与报告 hash，再由人工原子切换。model、WAL、ledger 和
receipt 默认全部保留在本地；60G 空间首先通过已验证的 raw-session archive
释放，不能删除 WAL segment 或历史 ledger 来腾空间。

## Migration

迁移前先写入 stop request，确认采集器已退出，运行 `audit --require-closed`，并完成最新 snapshot 的远端全量校验。复制以下内容到新 VPS：

1. 固定的代码 revision 与 `deploy/.env`（通过安全渠道传输，绝不提交）；
2. `deploy/runtime/data/`，其中包括原始前瞻数据与模型 artefact；
3. `deploy/runtime/output/`，其中包括 WAL、状态、Shadow 输出和报告。

优先从已验证 snapshot 恢复 `deploy/runtime/data/btc_short_horizon`，再复制其余 runtime 输出。在新 VPS 创建同样的目录权限后先运行 data audit，最后执行 `docker compose ... up --build -d`。迁移不需要重新训练模型或修改策略；只有新代码、配置、规则 epoch 或模型版本经过本地验证后才发布新的不可变版本。

## Pre-Purchase Validation Status

本地购买前验收已覆盖完整 180 秒决策窗口。Polymarket CLOB 原始采集仍限制为
`t0-90s` 至 `t0+215s`。market-end maker 的剩余 SELL 成交证据在结算后通过公开
Data API 一次性补取，并与 Paper ledger 一起持久化；不会为每个市场保留完整
15 分钟的原始 CLOB。证据缺失时该结果保持未结算且运行状态异常，不得推定成交。
handoff 后只轮换 durable session、不重连连续 BTC feeds。实时 Research Paper
复用相同模型/确认规则，模拟 ledger 与看板明确隔离于真实账户。

首尔 VPS 仍必须在目标 IP 上完成、且不能由本地替代的检查包括：Geo-block/法律
资格、到 CLOB/RTDS/Binance 的实际 P50/P95/P99 RTT、NTP 偏差、Docker 镜像构建
与重启策略、长期断线恢复，以及后续真实账户 User channel 与最小规模 Canary。
当前部署启用 collector、Research Paper、可选 Shadow scheduler 与 dashboard，
不启用真实订单网关。

## Release Gates

购买或启用 VPS 不会改变以下上线门槛：

- 完整 180 秒、36 个决策点的前瞻窗口必须先通过因果 Shadow 覆盖审计。
- 可选 Shadow scheduler 是窗口结束后的因果重建，不是在线订单延迟或 passive fill 的证明。
- 当前方向/价格 proxy 只说明研究优先级，不能证明可成交 maker edge。
- 未取得 L2/TradeTick 证据、悲观 queue、P99 延迟、费用和容量测试前，不得把 Shadow/Paper 结果解释为实盘盈利能力。
- 账户、地区与法律资格必须由操作者独立确认；服务器区域不能用于规避 Polymarket 的资格限制。
- 实盘前必须增加一份专门的 Canary 发布审查；本 Compose 文件不包含密钥或真实订单客户端。

相关市场资格、订单与费用行为以 Polymarket 的当期官方文档为准：[Geo-block API](https://docs.polymarket.com/api-reference/geoblock)、[订单创建](https://docs.polymarket.com/trading/orders/create)、[费用](https://docs.polymarket.com/trading/fees)。
