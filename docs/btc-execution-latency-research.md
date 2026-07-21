# BTC Bot 本地执行与低延迟研究

> 核对日期：2026-07-16。本文只使用 Polymarket 官方文档、官方 SDK/source 与一手协议资料。由于 CLOB V2 于 2026-04-28 上线，早于该日期的 V1 示例不能直接用于当前生产环境。

## 结论

“把稳定工作移出下单热路径”是正确原则，但“只剩发送请求”“彻底抛弃 REST”“提前估算 nonce”“能省 1 ms 就省 1 ms”都不能照字面实现。

本项目应采用的目标是：**触发交易时不再读取市场参数、余额、allowance 或订单簿；只基于已同步的内存状态生成最终订单，完成一次必要的本地订单签名和一次必要的 L2 HMAC，然后通过一个复用连接提交 `POST /order` 或 `POST /orders`。** 订单响应、User WebSocket、heartbeat、断线恢复和 REST 对账仍然是正确性边界，不能为了少一个请求而删除。

当前策略是 15 分钟 BTC 市场的 post-only maker，决策 cadence 为 5 秒，并要求连续两个信号。它不是微秒级 taker 抢单策略。更近的部署区域、已建立的连接、无数据 gap、正确的队列/撤单状态和稳定的 P99，通常比将 Python 局部代码再缩短 1 ms 更重要；但 maker 入队和撤单竞争仍受延迟影响，因此必须测量而不是忽略。

## 原帖逐项核对

| 原帖主张 | 判断 | 当前官方事实与本项目处理 |
|---|---|---|
| 能预处理的全部本地完成 | 正确但有限 | 模型、特征 schema、市场映射、tick、`negRisk`、fee、size ladder、余额/allowance 检查和序列化结构都应预热；方向、价格、edge、data age 和是否下单必须使用决策瞬间的实时状态，不能提前固定。官方要求订单携带正确的市场参数，且 tick 可能变化。[Create Order](https://docs.polymarket.com/trading/orders/create) |
| EIP-712 签名提前生成 | 部分正确 | 官方支持 `createOrder()` 与 `postOrder()` 分离，所以**已确定完整订单参数后**可以先签后发。[Create Order：Two-Step Sign Then Submit](https://docs.polymarket.com/trading/orders/create#two-step-sign-then-submit) 但 V2 签名绑定 token、金额、方向、随机 salt 和毫秒 timestamp；价格或数量变化就要生成另一张订单。官方未公开 V2 signed-order timestamp 的最大可接受年龄，因此不应在很早以前批量预签并假设永远有效。[V2 Order struct source](https://github.com/Polymarket/clob-client-v2/blob/37a6663/src/order-utils/model/orderDataV2.ts)、[V2 builder source](https://github.com/Polymarket/clob-client-v2/blob/37a6663/src/order-utils/exchangeOrderBuilderV2.ts) |
| nonce 预估 | 对 CLOB V2 逐单热路径是错误的 | CLOB V2 order 已移除 `nonce`、`feeRateBps` 和 `taker`，改为 `salt`、毫秒 `timestamp`、`metadata` 和 `builder`。[CLOB V2 changelog](https://docs.polymarket.com/changelog#apr-17-2026) `POLY_NONCE` 只用于 L1 创建/派生 API credentials，默认可为 0；它不是每张 V2 订单要抢先估算的链上 nonce。[Authentication](https://docs.polymarket.com/api-reference/authentication#getting-api-credentials) |
| 价格/数量逻辑预计算 | 部分正确 | rounding、风险上限、固定 size ladder 和候选价阶可预计算；最终 fair probability、passive price、post-only crossing 检查和可用余额必须在当前状态上判断。不能拿旧订单簿价格换取少量 CPU 时间。 |
| batch 打包 | 正确但必须处理部分成功 | `POST /orders` 每次最多 15 张订单，服务端并行处理；官方 OpenAPI 明确返回逐单数组并给出 mixed results 示例，因此 batch **不是原子提交**。[Post multiple orders](https://docs.polymarket.com/api-reference/trade/post-multiple-orders) 本项目的 2–3 层同时挂单可用一个 batch，但必须逐项记录 live/matched/rejected，并在部分成功时正确管理已生效订单。 |
| 交易瞬间只发一个“纯交易请求” | 适合作为正常热路径目标 | 对已经同步且健康的状态，入场应只有一个 `POST /order` 或 `POST /orders`。但最终 payload 的 EIP-712 order signature、序列化和 L2 HMAC 仍要在本地完成；它们不是多余调用。超时、断线或 mixed result 时必须进入对账路径，不能盲目重发。 |
| 不多一次 API 调用/轮询 | 正常热路径正确，作为全系统规则错误 | 官方建议用 WebSocket 获取实时数据而不是轮询；但下单、撤单和 dead-man heartbeat 仍是 REST，启动/恢复时也需要市场参数、余额、open orders/trades 等查询。[Market Maker Trading](https://docs.polymarket.com/market-makers/trading#latency)、[Send heartbeat](https://docs.polymarket.com/api-reference/trade/send-heartbeat) |
| 不多一次签名 | 表述过度 | 每张新的订单 payload 仍需一次 EIP-712 signature；每次 L2 HTTP 请求仍需一个基于当前 timestamp、method、path 和精确 body 的 HMAC。后者无法在最终 body 和提交时间未知时预先完成。[HMAC source](https://github.com/Polymarket/clob-client-v2/blob/37a6663/src/signing/hmac.ts)、[L2 header source](https://github.com/Polymarket/clob-client-v2/blob/37a6663/src/headers/index.ts) |
| 使用官方 TS/Python/Rust SDK | 正确 | 生产必须使用当前 V2 SDK；CLOB V2 自 2026-04-28 起在 `https://clob.polymarket.com` 运行，V1 SDK 和 V1-signed orders 已不受支持。[CLOB V2 production changelog](https://docs.polymarket.com/changelog#apr-28-2026) |
| `http://ws-subscriptions-clob.polymarket.com` | 协议和路径都不完整 | Market channel 是 `wss://ws-subscriptions-clob.polymarket.com/ws/market`；User channel 是 `wss://ws-subscriptions-clob.polymarket.com/ws/user`。[WebSocket Overview](https://docs.polymarket.com/market-data/websocket/overview) |
| `http://ws-live-data.polymarket.com` | 协议错误、host 正确 | RTDS endpoint 是 `wss://ws-live-data.polymarket.com`。BTC Binance/Chainlink feed 使用不同 topic；RTDS 还要求每 5 秒发 `PING`。[RTDS](https://docs.polymarket.com/market-data/websocket/rtds) |
| 彻底抛弃 REST 轮询 | “抛弃热循环轮询”正确，“抛弃 REST”错误 | Market/User WebSocket 用于实时 book、trade、order/fill 更新；CLOB 下单本身仍通过 REST POST。WebSocket 文档没有 durable replay/cursor 保证，所以断线期间的订单/成交状态不能假设完整，恢复后应做 REST reconciliation；这是本文基于接口边界作出的安全推论。[Market Channel](https://docs.polymarket.com/market-data/websocket/market-channel)、[User Channel](https://docs.polymarket.com/market-data/websocket/user-channel)、[Trading Overview](https://docs.polymarket.com/trading/overview) |
| 持久连接/Keep-Alive | 正确，而且当前 Python V2 SDK 已实现基础能力 | 当前 `py-clob-client-v2` 使用模块级 `httpx.Client(http2=True)` 并设置 `Connection: keep-alive`，可复用连接。[Python HTTP helper source](https://github.com/Polymarket/py-clob-client-v2/blob/394ecc1/py_clob_client_v2/http_helpers/helpers.py) 但 `http2=True` 不保证服务端实际协商 HTTP/2，部署后仍需观测连接复用、TLS handshake 和 negotiated protocol。 |
| 减少序列化/反序列化 | 有价值但优先级低于网络和状态正确性 | 最终 body 必须精确序列化后再生成 HMAC，因为 HMAC 覆盖 body。[HMAC source](https://github.com/Polymarket/clob-client-v2/blob/37a6663/src/signing/hmac.ts) 应做到“序列化一次并复用同一 bytes/string 做 HMAC 与发送”，但不要为此绕过官方字段校验。 |
| Rust 重写关键路径 | 只有 profiling 证明 CPU/jitter 是瓶颈后才值得 | 官方 Rust V2 SDK 可用，但语言迁移会增加双实现与状态一致性风险。[Trading Overview](https://docs.polymarket.com/trading/overview) 当前 Python SDK 已有 HTTP/2 persistent client；应先拆分测量 feature/model、order build、EIP-712、JSON、HMAC、socket write 和 venue ack，再决定是否只迁移签名/序列化或整个 execution gateway。 |
| 能省 1 ms 就省 1 ms，否则主动送钱 | 过度概括 | maker 订单更早入队和更快撤单确实有价值，但本策略的 5 秒模型 cadence、两次确认、WAN RTT 和 venue 行为通常是更大项。部分 crypto/finance Up/Down 的 marketable order 还有官方 250 ms taker delay；post-only crossing 则会被拒绝。[Create Order：order status/delay](https://docs.polymarket.com/trading/orders/create#statuses) 是否值得为 1 ms 增加复杂度必须由真实 P99 与成交/markout 数据决定。 |

## 当前 CLOB V2 的签名与 nonce 边界

### 启动或 credential 管理阶段

1. L1 使用 wallet private key 签 `ClobAuth` EIP-712 message。
2. `POLY_NONCE` 用于创建或派生 API credentials；默认值是 0。
3. 得到 `apiKey`、`secret` 和 `passphrase` 后安全持久化，不在每单重新创建。

这是低频 credential 生命周期，不应出现在订单热路径。[Authentication](https://docs.polymarket.com/api-reference/authentication)

### 每张订单

V2 signed order 包含随机 `salt`、maker、signer、tokenId、maker/taker amount、side、signatureType、毫秒 timestamp、metadata 和 builder。V2 order 没有 nonce。[CLOB V2 migration changelog](https://docs.polymarket.com/changelog#apr-17-2026)、[V2 typed data source](https://github.com/Polymarket/clob-client-v2/blob/37a6663/src/order-utils/model/ctfExchangeV2TypedData.ts)

订单参数确定后，客户端在本地生成 EIP-712 signature。官方两阶段 API 允许先调用 `createOrder()`，稍后用 `postOrder()` 提交，但这不是“只准备一个万能签名”：价格、金额、方向或 token 改变时需要另一张 signed order。

### 每次 HTTP 请求

L2 request headers 使用 API secret 做 HMAC-SHA256；message 是：

```text
timestamp + HTTP method + request path + exact serialized body
```

因此可以预热 key/client、复用 payload buffer 和减少 allocation，但无法在 timestamp 与最终 body 未确定时生成最终 HMAC。[Official HMAC implementation](https://github.com/Polymarket/clob-client-v2/blob/37a6663/src/signing/hmac.ts)

## WebSocket 与 REST 的正确分工

| 功能 | 正确通道 | 是否在订单热路径 |
|---|---|---|
| Polymarket L2 book、price change、last trade | Market WebSocket | 是，只读内存状态 |
| 自己的 order/trade/fill 更新 | User WebSocket | 是，异步状态机 |
| Binance/Chainlink BTC price | RTDS 或直接 venue feed | 是，只读内存状态 |
| 下单 | REST `POST /order` 或 `POST /orders` | 是，正常情况一次 |
| 撤单 | REST cancel endpoint | 按风险触发 |
| Dead-man switch | authenticated REST heartbeat | 独立安全循环 |
| 市场 metadata、tick、negRisk、fee、min size | REST/Gamma/CLOB metadata | 开盘前预取；变更事件使订单 fail closed |
| balance/allowance、open orders/trades 对账 | authenticated REST | 启动、定期低频和恢复路径，不做每次决策轮询 |

Market/User WebSocket 要每 10 秒发 `PING`；RTDS 要每 5 秒发 `PING`。这只是维持 socket 连接。[WebSocket Overview：Heartbeats](https://docs.polymarket.com/market-data/websocket/overview#heartbeats)、[RTDS](https://docs.polymarket.com/market-data/websocket/rtds)

CLOB authenticated heartbeat 是不同机制：若不持续发送，官方会自动撤销该用户所有 open orders，应作为 dead-man switch 独立运行。[Send heartbeat](https://docs.polymarket.com/api-reference/trade/send-heartbeat)

## Batch 不是原子交易

`POST /orders` 对本项目有实际价值：2–3 层 post-only order 可以在一个 HTTP request 中并行提交，减少 WAN round trips。它同时带来三个必要约束：

1. 每次最多 15 张。
2. 服务端并行处理，不保证全部成功或全部失败。
3. HTTP 200 返回逐单结果，可以同时包含 live、matched 和 failed。

因此 batch lifecycle 必须按 order ID 独立落账。若三层中两层成功、一层失败，Bot 不能把整个 placement cycle 标成失败后再次提交三层；否则可能重复敞口。[Post multiple orders](https://docs.polymarket.com/api-reference/trade/post-multiple-orders)

## 本项目建议的执行热路径

### 开盘前/后台预热

- 发现并验证 current + next BTC 15m market、condition/token IDs 和规则 hash。
- 缓存并持续监听 tick size、`negRisk`、fee、min size；变化即停止新下单并重新验证。
- 建立 Market WebSocket、User WebSocket、RTDS/Binance/Chainlink connections，完成 snapshot/delta 同步。
- 在内存加载 frozen model、calibrator、feature state、threshold 和 size ladder。
- 预先完成 geo eligibility、余额、allowance、funder、credentials 和 server/local clock 检查。
- 建立并验证 HTTP connection reuse；不要等触发信号才首次 DNS/TLS/connect。
- 启动 CLOB dead-man heartbeat 与各 WebSocket PING/PONG。

### 正常入场热路径

```text
WebSocket event / 5s decision tick
  -> 更新内存 feature/book state
  -> 计算 p_fair、双方 edge、data age、risk guard
  -> 选择最早合格的一侧和已预注册 price/size layers
  -> 生成并签署精确 V2 order(s)
  -> 序列化一次
  -> 生成当前 L2 HMAC
  -> 一个 POST /order 或 POST /orders
  -> 逐单应用 response
  -> User WebSocket 驱动后续 order/fill state
```

不允许在 `p_fair` 已触发后再调用 `getTickSize()`、`getNegRisk()`、`getBook()`、`getBalance()` 或 `getAllowance()`。这些状态必须已经同步且仍在 freshness/risk contract 内；否则放弃本次订单，而不是临时读取后再追单。

### 异常与恢复路径

- POST timeout 的结果是 ambiguous：订单可能已被 venue 接收。暂停同一 placement cycle 的重提，先通过 User WebSocket 和 authenticated REST 对账。
- User WebSocket gap/reconnect 后，不假设断线期间无成交；停止新单，查询 open orders/trades 并重建 ledger。
- Market WebSocket gap 后丢弃旧 book state，等新的完整 snapshot；期间不交易。
- Batch mixed results 必须逐张管理；已成功订单不能随失败项被“回滚”。
- heartbeat、User channel 或 cancel path 失效时进入 cancel-only/fail-closed，而不是继续以低延迟名义挂单。

## 应测量的延迟，而不是只测一个总 RTT

每笔订单和撤单至少记录 monotonic timestamp，并输出 P50/P95/P99：

- `market_source_ts -> collector_receive_ts`（只在 source timestamp 可比较时）。
- `collector_receive -> feature/model decision`。
- `decision -> signed_order_ready`。
- `signed_order_ready -> serialized_and_hmac_ready`。
- `socket_write_start -> HTTP response`。
- `HTTP response -> User WebSocket placement/update`。
- `cancel_decision -> cancel_request -> cancel_ack`。
- `fill match_time -> local User WebSocket receive`。
- DNS/TCP/TLS/HTTP connection reuse 命中率、reconnect duration、event-loop lag 和 clock offset。

只有当本地 build/sign/serialize/HMAC 的 P99 对总延迟或 jitter 有实质贡献，才评估 Rust。否则应优先处理 VPS region、连接复用、event-loop blocking、意外 REST 读取、GC/CPU contention、WebSocket gap 和日志/磁盘阻塞。

## 部署区域与资格的关键修正

官方当前说明 matching engine primary servers 位于 `eu-west-2`；最近的非受限区域是 `eu-west-1`。通过 KYC/KYB 后才可能获得 `eu-west-2` direct co-location。[Trading Overview：Server Infrastructure](https://docs.polymarket.com/trading/overview#server-infrastructure)

截至 2026-07-21 核对，官方 geoblock 文档将德国 `DE` 与英国 `GB` 都列为 `Blocked`。**因此法兰克福和伦敦 VPS 都不能作为开仓服务器**；订单提交前必须从候选 VPS IP 调用官方 `GET https://polymarket.com/api/geoblock` 并 fail closed。地区限制和法律资格优先于延迟优化。[Geographic Restrictions](https://docs.polymarket.com/api-reference/geoblock)

## 截至 2026-07-16 的不确定性

- 当前 OpenAPI heartbeat 页面展示 `POST /heartbeats`，而官方 TS/Python V2 SDK source 使用 `/v1/heartbeats`；SDK 还携带 `heartbeat_id`，但页面没有说明 cadence 或 body。生产应调用当前官方 SDK method，并在 staging/最小 canary 验证“多久不发会撤单”、返回 ID 续传和断线行为，不能凭旧文档硬编码。[Heartbeat API page](https://docs.polymarket.com/api-reference/trade/send-heartbeat)、[Python endpoint source](https://github.com/Polymarket/py-clob-client-v2/blob/394ecc1/py_clob_client_v2/endpoints.py)、[Python client source](https://github.com/Polymarket/py-clob-client-v2/blob/394ecc1/py_clob_client_v2/client.py)
- 官方公开资料没有给出 V2 signed-order timestamp 的最大接受年龄。远期预签策略必须先做最小规模实测；在此之前只允许在完整订单参数确定后临近提交签名。
- `httpx.Client(http2=True)` 表示客户端启用 HTTP/2，但实际 negotiated protocol、连接是否跨请求复用、服务端 idle timeout 和 TLS resume 只能在目标 VPS 上测量。
- `eu-west-2` co-location 需要官方 KYC/KYB 批准。项目首次使用用户选定的首尔 VPS 做 Shadow 观测，但必须以该 IP 的 geoblock 结果和实测 CLOB/RTDS/Binance P50/P95/P99 为准；不能把首尔预先认定为长期最优执行区域。
- Polymarket API、fees、tick、market rules 和 geographic restrictions 都会变化；每次 SDK/规则 epoch 变化必须重新跑签名、post-only、batch mixed-result、heartbeat 和 cancel-race integration tests。

## 对项目路线的直接建议

1. 保留 Python V2 SDK，先完成分段 P99 telemetry 和连接复用验证；不要现在整体迁移 Rust。
2. 将所有 market/risk reads 移到预热和异步更新层，保证正常下单路径没有 REST read-before-write。
3. 对 2–3 层订单加入 V2 batch gateway，但把 mixed results、partial success 和 ambiguous timeout 作为验收测试。
4. 不实现“order nonce 预估”；明确区分 L1 API-key nonce、V2 order salt/timestamp 和 L2 request timestamp。
5. 只在订单参数确定后签名。若以后要维护短寿命 pre-signed price grid，必须先取得 timestamp-age 实测证据并评估泄漏/错误提交风险。
6. 保留 REST heartbeat、启动检查和重连对账；删除的只能是实时行情与订单状态的 REST 轮询。
7. 首尔 VPS 首次只运行 collector、Shadow 与 dashboard，并记录至少一周网络分布；未来是否迁往更靠近 `eu-west-2` 的合规区域由实测决定。法兰克福与伦敦在当前官方规则下都不可作为开仓部署默认值。

### 已实现的控制面边界

`LiveOperationsController` 已将 authenticated heartbeat、完整账户 ledger
刷新、REST reconciliation、User-channel gap admission、状态和 dashboard
投影组成独立 cadence 的有界 supervisor。官方 heartbeat 当前可能返回
历史 `heartbeat_id`，也可能只返回 `{"status":"ok"}`；gateway 对两种明确
成功响应兼容，其他响应 fail closed。

WAL checkpoint/rotation 只允许在服务已 halt、无未终态 order/trade、无
reserved notional 且市场状态无歧义时执行。segment 之间保留全局 sequence
与 hash 连续性，完整读取仍审计全部历史 segment。目标 VPS 必须先运行
`scripts/btc_vps_preflight.py`，把 release revision、rule epoch、官方
geoblock、NTP、CLOB clock offset 与 CLOB/Gamma/Binance 延迟分布写入不可变
receipt；任何失败都不能启用后续服务。

## 2026-07-20 回放执行契约补充

历史 maker 结论不再依赖单一的“悲观场景”。正式证据必须同时包含四个
P99 组件：完整/50% `TradeTick` 成交量，分别配合
`book_before_trade` 与 `trade_before_book` 的同时间戳排序。四个组件必须使用
相同的 insert/update/cancel latency，开启 Nautilus queue position，并关闭
maker rebate。50% 成交量只是政策压力，不代表能够从 L2 恢复真实 FIFO。

当前 P99 参数（base 150 ms、insert 50 ms、update 25 ms、cancel 100 ms）仍是
部署前压力值，不是实测分布。Minimum-size Canary 必须重新测量完整链路并替换
这些值；在此之前，任何单一组件都带有
`standalone_strategy_conclusion=false`，不能生成 Maker Go。

正式回放还必须满足以下失败关闭条件：

- 所有订单均为 `post_only`，所有实际 fill 均为 maker；
- maker commission 与 rebate 都为零；
- `cancel_rejected`、提前终止或未运行到 `label_available_ts` 均使结果无效；
- Up/Down 两个结果必须互补，event-level partial fills 必须与 Nautilus
  order-level 汇总在数量、加权价格和 commission 上一致；
- 1/3/10/30/60 秒 markout 必须记录其盘口时间与 book age，不能把旧盘口静默
  当成新报价；
- 真实引擎 fixture 必须持续覆盖 insert-latency post-only race、cancel-latency
  partial fill race、multi-layer rejection 和 GTC 最大工作时间。

当前官方费用边界为：maker 不收 trading fee；若做诊断性 rebate 估算，crypto、
sports 和其他合格类别当前分别使用 20%、15% 和 25% 分成。正式 BTC 结论始终
禁用 rebate，因为日级 payout、最低累计金额与其他 maker 的钱包级状态无法由
逐笔历史回放精确恢复。参考 [Fees](https://docs.polymarket.com/trading/fees) 与
[Maker Rebates](https://docs.polymarket.com/market-makers/maker-rebates)。

## 2026-07-21 Live 执行安全契约

当前实现将“尽量少做热路径工作”约束为一组可恢复、可对账的不变量，而不是以
牺牲订单身份或持久化证据来换取较小的本地耗时：

1. 只接受精确锁定的 `py-clob-client-v2==1.0.2`、官方
   `https://clob.polymarket.com` origin、显式提供的 L2 credentials 与已验证的
   signer/funder/signature type。程序不会在运行中派生或创建 credentials，也不会
   将 secret、passphrase、private key 或完整认证 payload 写入日志/WAL。
2. `prepare` 阶段完成 V2 order build/sign，并用官方 builder 本地计算 EIP-712
   order hash。该 hash 就是 venue order ID；在任何 POST 前先与完整订单意图一起
   写入 hash-chained WAL 并 `fsync`。
3. 同一 placement cycle 的多层订单只发一次 `POST /orders`。响应按每张订单独立
   解析；成功项的 `orderID` 必须等于本地预计算 hash，重复 ID、字段缺失、错误
   tick/size/market/token 或 mixed-result 语义不一致都会 fail closed。Batch 仍不是
   原子提交。[Post multiple orders](https://docs.polymarket.com/api-reference/trade/post-multiple-orders)
4. POST 返回后把每张结果作为第二个 WAL transaction 持久化。正常热路径因此有
   两次必要的耐久化边界：网络前保存可恢复身份，网络后保存 venue 结果。成功
   heartbeat 不逐次落 WAL，避免无界写放大；失败与状态变化仍必须持久化。
5. POST 不自动重试。若连接中断或响应丢失，预计算 hash 允许启动恢复直接查询
   open orders 与 `GET /order/{orderID}`；可证明的 LIVE、MATCHED、CANCELED、
   INVALID 等终态会重建本地状态。既不在 open orders，也无法取得终态证明的订单
   保持 unknown，并立即关闭交易闸门、执行 cancel-all，禁止猜测后重提。
   [Get single order by ID](https://docs.polymarket.com/api-reference/trade/get-single-order-by-id)
6. User channel 固定使用
   `wss://ws-subscriptions-clob.polymarket.com/ws/user`。初次连接可省略 `markets`
   以订阅账户事件；动态过滤使用 condition IDs。任何 reconnect 或订阅替换都立即
   标记 gap，在新的 REST reconciliation 完成前不能重新开放下单。Order/trade
   更新分别按累计数量与 `(trade_id, client_order_id)` 幂等，只有 CONFIRMED fill
   计入 canary 晋级计数。[User Channel](https://docs.polymarket.com/api-reference/wss/user)
7. REST order amount 使用官方 6-decimal fixed-math；User WebSocket 的 price/size
   使用 decimal 字符串。两者在各自边界解析，禁止用同一个隐式缩放规则混读。
8. 撤单请求、撤单响应、cancel-before/after-fill race 与 heartbeat failure 都是
   独立状态边界。任何未知提交、User channel gap、terminal trade failure、规则
   变化或对账失败都会进入只撤单/停机状态，而不是继续接收新 placement cycle。

这个实现缩短了正常请求链，但不宣称已经得到真实 VPS P99。订单 build/sign、两次
WAL `fsync`、socket/HTTP、venue ack、User WebSocket 与 cancel ack 仍需在 Shadow
及 minimum-size Canary 中分别测量。若磁盘同步成为 P99 瓶颈，应先选择可靠低延迟
磁盘并做 WAL checkpoint/rotation；不得删除网络前持久化边界。
