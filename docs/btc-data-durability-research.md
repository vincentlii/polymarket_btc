# BTC 前瞻数据持久性与灾难恢复

## 结论

`btc-short-horizon-v9` 把前瞻原始数据从“目录里还剩哪些文件”升级为可审计的会话账本。每个 Parquet part 必须经历 `prepared → committed`，采集会话必须经历 `open → complete|failed`；研究读取器以会话清单为权威，因此 part 与相邻 manifest 同时丢失也会被发现。

异地备份采用内容寻址对象、不可变 snapshot 和全量回读校验。只有生成了与 snapshot 精确绑定的 verification receipt，才允许清理本地 part/manifest；清理默认 dry-run。恢复与实际清理都必须取得采集器同一把单写者锁，不能与采集进程并行修改原始数据目录。

这套机制证明的是“指定 snapshot 中的字节可以从指定 remote 完整取回”，不是云账户绝对可信。生产环境仍要使用独立备份权限、bucket versioning、可选 Object Lock，并把 snapshot ID 与告警/运维记录同步到 VPS 之外。

## v9 会话事务

每次采集启动会创建：

```text
inventory/
  session-registry/<session-id>.json
  sessions/<session-id>.json
raw/<source>/<instrument>/date=YYYY-MM-DD/hour=HH/
  part-<content-prefix>.parquet
  manifest-<content-prefix>.json
```

写入顺序固定为：

1. 在同一目标文件系统写临时 Parquet、`fsync` 并计算 SHA-256。
2. 向 session inventory 写入 `prepared` 记录。
3. 原子安装 part，再原子写 manifest。
4. 重新核对 part、manifest、完整 SHA-256 与 manifest payload。
5. 把 inventory 记录更新为 `committed`。
6. 所有 part 均 committed 且通过完整性检查后，session 才能标为 `complete`。

崩溃语义：

| 崩溃点 | 重启后的处理 | 是否允许研究读取 |
|---|---|---|
| registry 写完、inventory 未写 | registry/inventory 不成对，启动失败并要求人工检查 | 否 |
| `prepared` 后、part/manifest 未完成 | 会话标记 failed，缺失证据保留在账本 | 否 |
| part/manifest 已耐久、`commit` 未记录 | recovery 复核字节后补记 committed，再把中断会话标记 failed | 仅不与 failed 会话相交的窗口 |
| 正常关闭前仍有 prepared part | session 不能 complete | 否 |
| part 与 manifest 后续同时丢失 | inventory 仍引用它们，audit/reader 明确失败 | 否 |

采集器、restore 和实际 archive 共用 `inventory/collector.lock`。该锁是进程级互斥，不是网络分布式锁；同一个 raw root 只能由一台主机上的一个写入进程挂载。不要把同一可写目录同时挂给两台 VPS。

## 权威读取边界

v9 manifest 必须声明 session inventory schema，且必须在 inventory 中有完全一致的 payload 与 manifest hash。读取器同时验证：

- source、instrument、schema、ingest version 与 collector session；
- part/manifest 文件名、完整 SHA-256、row count 与时间范围；
- session 状态、part 提交状态和请求时间窗口；
- 远端归档标记与 snapshot/file hash 的精确对应关系。

与 failed session、prepared part、缺失本地文件或 remote-only evidence 相交的查询均 fail closed。remote-only 不是“没有数据”，而是必须先 restore 的显式状态。v8 仍可按旧 manifest 合同读取以保留 provenance，但新采集只能写 v9，不能把 v8/v9 混为一个研究样本。

## 内容寻址备份协议

一个 backup snapshot 包含完整 session registry、session inventory、part、manifest 和 instrument identity。对象键由完整内容 hash 决定：

```text
objects/<sha256-prefix>/<full-sha256>
snapshots/<snapshot-id>.json
```

上传顺序是 data objects 在前、snapshot 在后。snapshot ID 是其 canonical payload 的 SHA-256；任何路径、大小、hash 或 session 集合变化都会得到不同 ID。远端验证必须重新下载 snapshot 和它引用的每个对象，再逐字节核对大小与 SHA-256，最后在本地写 verification receipt。仅列目录、HEAD/ETag 或“上传命令返回成功”都不算通过。

`rclone copyto` 通过参数列表执行，不经过 shell。上传启用 `--immutable --checksum`；rclone 官方说明 `--immutable` 会拒绝修改已有文件，而 `copy` 不会删除 destination 上的文件。[rclone copy](https://rclone.org/commands/rclone_copy/) [rclone immutable](https://rclone.org/docs/#immutable)

对象存储建议：

- 开启 versioning，避免误覆盖后只剩最后一个对象版本；AWS 文档说明 versioning 会为同一 key 保留多个版本。[S3 Versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
- 需要更强 WORM 保护时启用 Object Lock；它依赖 versioned bucket，可用 retention/legal hold 阻止对象版本被删除或覆盖。[S3 Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
- lifecycle 只用于转低频存储或清理旧版本，规则必须先在测试 bucket 验证；生命周期操作是异步的，不能作为即时磁盘容量控制。[S3 lifecycle](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html)
- VPS 使用只允许写入目标 prefix、读取验证对象且无 bucket 管理权限的凭据；删除权限与 collector 凭据隔离。

## 运维命令

先检查会话完整性：

```bash
uv run python scripts/btc_data_archive.py \
  --raw-data-root deploy/runtime/data/btc_short_horizon \
  audit --require-closed
```

上传并全量回读验证。`--remote` 是已配置的 `rclone remote:path`：

```bash
uv run python scripts/btc_data_archive.py \
  --raw-data-root deploy/runtime/data/btc_short_horizon \
  backup \
  --transport rclone \
  --remote btc-archive:polymarket-btc/forward \
  --temporary-root deploy/runtime/tmp
```

命令输出 `snapshot_id` 与 `receipt_path`。两者应复制到本地电脑或独立运维记录。需要重新验证既有远端 snapshot 时：

```bash
uv run python scripts/btc_data_archive.py \
  --raw-data-root deploy/runtime/data/btc_short_horizon \
  verify \
  --snapshot-id <sha256> \
  --transport rclone \
  --remote btc-archive:polymarket-btc/forward \
  --temporary-root deploy/runtime/tmp
```

释放本地空间前必须安全停掉 collector。先 dry-run：

```bash
uv run python scripts/btc_data_archive.py \
  --raw-data-root deploy/runtime/data/btc_short_horizon \
  archive \
  --receipt-path <verified-receipt.json> \
  --completed-before 2026-08-01T00:00:00+00:00
```

核对 session、file count 和 byte count 后才增加 `--apply`。程序先写 durable archive marker，再删除已由 receipt 覆盖的本地 part/manifest；registry 与 inventory 保留，因此任何读取都会明确要求恢复。

恢复到空目录或原 raw root：

```bash
uv run python scripts/btc_data_archive.py \
  restore \
  --snapshot-id <sha256> \
  --destination-root deploy/runtime/data/btc_short_horizon \
  --transport rclone \
  --remote btc-archive:polymarket-btc/forward \
  --temporary-root deploy/runtime/tmp
```

restore 会下载并验证全部对象，通过目标文件系统内的临时文件原子安装，最后运行 session audit。现有同路径文件只有 hash/size 完全一致才会复用；内容不同会停止，不会覆盖。

## 推荐节奏与告警

- 每次 collector 正常关闭后运行 `audit --require-closed`。
- 至少每日创建并验证一个只含 complete sessions 的异地 snapshot。
- 每周在独立临时目录执行一次 restore drill，并对恢复目录运行 audit。
- 只有当 VPS 剩余空间接近预设阈值时才 archive；至少保留最近数日的本地热数据，具体天数由实测日增量与磁盘预算决定。
- `open_session_count > 0`（collector 已停止时）、`failed_session_count > 0`、`prepared_part_count > 0`、backup/verify 失败或 receipt 长时间未更新均触发告警。
- 清理作业若拿不到 collector lock 必须退出；不得强杀锁持有者后继续删除。

## 安全限制

- verification receipt 是本机完整性记录，不是数字签名。取得 VPS 写权限的攻击者可能同时篡改本地 snapshot 与 receipt；远端 versioning/Object Lock、最小权限和外部保存 snapshot ID 才是独立信任锚。
- 内容寻址防止无声覆盖，不防止攻击者删除全部对象。remote 凭据不应有删除历史版本的权限。
- 原始数据工具只归档 complete sessions 的 immutable part/manifest；模型、WAL、ledger 与报告由独立 runtime snapshot 保护。dashboard/status 等可重建 projection 不作为恢复权威。PMXT vendor mirror 仍按供应商缓存策略独立管理。
- 文件系统 lock 不适用于多主机共享写入。未来如需 active/passive collector，必须先设计显式 leader election 与 fencing token，不能复用当前单机锁假装分布式安全。

## 验收标准

首次 VPS 部署前至少证明：

1. 正常 session 可完成 `snapshot → upload → full-download verify → archive dry-run → apply → restore → audit`。
2. part 与 manifest 同时删除、远端对象被篡改、receipt count 被修改、archive marker 冲突和双 writer 均自动失败。
3. 从与目标 raw root 不同磁盘的 temporary root 恢复成功。
4. 生产 bucket 已启用 versioning；Object Lock 是否启用、retention 和 lifecycle 已形成书面配置。
5. snapshot ID/receipt 已保存到 VPS 之外，并完成一次人工恢复演练。

## Runtime Evidence Backup And Restore

`scripts/btc_runtime_archive.py` 只保护 raw-session 工具未覆盖的四类关键证据：
`model`、`wal`、`ledger`、`report`。每个 source root 都是显式参数；root 不得
重叠、不得是 symlink，也不得包含 symlink、partial file、`.env`、常见
private-key 文件或 text artifact 中的凭据字段。快照绑定 release Git revision、rule epoch、相对路径、大小
与 SHA-256，并复用相同的 immutable local/rclone object transport。上传完成不
代表成功；工具会重新下载 manifest 和每个 object，逐字节验证后才写 receipt。

创建 runtime backup 前必须先安全停止相关服务；Live 阶段还必须先进入 halted
状态、完成 REST/User-channel reconciliation，并在无未终态订单、trade 和
reserved notional 时执行 WAL checkpoint/rotation。文件在 hash 期间发生变化会
直接失败。恢复只允许写入隔离目录：写入前会预检全部目标，任何不同文件或
任意父目录 symlink 都会使整个安装开始前失败；同 hash 文件才允许复用。

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

每周恢复到新的隔离目录，并要求最近七天内存在恢复 receipt：

```bash
uv run python scripts/btc_runtime_archive.py \
  --repository-root deploy/runtime/output/btc_short_horizon/recovery \
  restore \
  --snapshot-id <sha256> \
  --destination-root deploy/runtime/restore-drill/<sha256> \
  --transport rclone \
  --remote btc-archive:polymarket-btc/runtime \
  --temporary-root deploy/runtime/tmp

uv run python scripts/btc_runtime_archive.py \
  --repository-root deploy/runtime/output/btc_short_horizon/recovery \
  audit \
  --snapshot-id <sha256> \
  --max-receipt-age-hours 48 \
  --require-restore-drill \
  --max-restore-age-hours 168
```

Retention 采用按风险分层的保守默认值：raw data 是主要容量来源，继续使用已
验证 receipt 后的 `btc_data_archive.py archive`；model、WAL、ledger 与恢复
receipt 本地不自动删除；report 在实际容量数据不足前也不自动删除。远端使用
versioning，并建议 Object Lock；生命周期只能转冷存储，不能删除仍处于治理、
对账、争议或模型复现实验窗口内的版本。这样不会为了 60G 本地磁盘而破坏订单
恢复链或资金账本。清理策略只有在观察到真实日增量后，才能作为独立变更加入。
