# BTC Polymarket Bot 简约看板研究与信息架构

## 结论

建议将看板做成只读、三视图结构：

1. **概览**：默认页，只回答四个问题——Bot 正常吗、当前赚亏多少、资金风险多大、策略处于什么阶段。
2. **交易**：完整逐单列表；一行代表一个市场的一次 placement cycle，展开后查看各层订单、partial fill 和结算明细。
3. **运维**：网络、数据、订单链路延迟、数据质量和告警的诊断页。

主页面保持一屏可读，不把原始 JSON、完整日志、模型特征和 L2 深度图塞入首页。Grafana 官方建议看板围绕明确问题、按“整体到细节”的顺序组织，并降低认知负担；Google SRE 同样强调监控与告警链路应简单、可理解，而不是要求人持续盯屏。[Grafana dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/)、[Google SRE: Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/)

当前 VPS 阶段允许 `Research Paper` 写入一套严格标为“模拟”的绩效投影：
虚拟余额、模拟成交、逐市场盈亏和资金曲线均来自独立 Paper ledger，不得显示为
真实账户或实盘成交。首页必须同时显示其固定假设（P99 latency、完整可见 queue、
50% seller-initiated trade volume）以及 `Research Proxy / Maker Gate No-Go`；这样可
观察策略与运行链路，但不会把 L2 heuristic 误包装成可实现利润。

## 第一手产品与规范中可直接借鉴的做法

| 官方来源 | 可直接借鉴 | 本项目的定制推断 |
|---|---|---|
| [Grafana dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/) | 看板应回答明确问题；图表保持简单；减少认知负担；避免无意义的高频刷新；使用阈值与说明文字；避免看板膨胀。 | 只保留一个主概览、一个交易页和一个运维页，不建设“每个模型/市场一张看板”。 |
| [Google SRE: Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/) | 服务监控优先看 latency、traffic、errors、saturation；尾部延迟比平均延迟更能暴露问题；告警应低噪声且代表明确失败。 | 首页显示健康结论和 P95/P99，平均值与底层资源细节下沉到运维页。 |
| [FreqUI](https://www.freqtrade.io/en/stable/freq-ui/) | 同时提供 Bot 状态、累计收益与 wallet balance；实际余额曲线包含未实现盈亏、出入金；重建历史应标出可信起点。 | 本项目分别画“账户权益”和“累计已实现净 PnL”，并标注 capture start、资金变动和模型发布事件。 |
| [Freqtrade REST API](https://www.freqtrade.io/en/stable/rest-api/) | 成熟 Bot 接口分别提供 health、trades、profit、daily/weekly/monthly、balance、system info；官方不建议将可控 Bot 的 API 直接暴露公网。 | 前端保持只读；运行控制、密钥和 kill switch 不进入普通看板，远程访问走 VPN/SSH tunnel 或受保护的反向代理。 |
| [Hummingbot Dashboard: Managing Instances](https://hummingbot.org/dashboard/instances/) | 实例页展示运行状态、启动时间、net/realized/unrealized PnL、成交量和错误日志。 | 概览展示收益与状态，错误日志只显示可执行摘要，完整日志下沉。 |
| [Hummingbot Dashboard: Viewing Portfolio](https://hummingbot.org/dashboard/portfolio/) | 总资产、持仓明细和资产随时间变化是独立视图。 | BTC 15m 项目只显示 USDC、未结算 token、可用资金和锁定资金，不做通用多币种资产分配图。 |
| [Hummingbot Dashboard: Backtesting](https://hummingbot.org/dashboard/backtest/) | 常用绩效包括 net PnL、max drawdown、volume、Sharpe、profit factor；配置 tag 用于追踪策略版本。 | 首页只保留 net PnL 与 drawdown；Sharpe、profit factor、fill rate 和版本 lineage 放在详情或生命周期卡。 |
| [Polymarket WebSocket overview](https://docs.polymarket.com/market-data/websocket/overview) | Market/User channels 要每 10 秒发送 `PING` 并接收 `PONG`；Market channel 提供 book/trade，User channel提供 order/trade 更新。 | 连接状态用最近 PONG、最近有效消息、重连次数判断；不能只看 TCP 仍连接。PONG 往返时间可作为连接 RTT 代理，但不是交易所承诺的下单延迟。 |
| [Polymarket User Channel](https://docs.polymarket.com/market-data/websocket/user-channel) | User channel 给出 `PLACEMENT`、`UPDATE`、`CANCELLATION`，以及 `MATCHED`、`MINED`、`CONFIRMED`、`RETRYING`、`FAILED`；API credentials 不得暴露在客户端。 | 用这些事件构建订单状态机和逐单审计；浏览器只接收脱敏后的只读汇总。 |
| [MLflow Model Registry workflow](https://mlflow.org/docs/latest/ml/model-registry/workflow/) | aliases 与 tags 可表达 champion/challenger、validation status 和部署版本。 | 看板显示 champion、challenger、验证结论、模型 hash、配置 hash、代码 revision；不要求引入 MLflow 本身。 |
| [FreqAI periodic retraining](https://www.freqtrade.io/en/stable/freqai-running/) | 周期重训、模型过期和滑动训练窗口是成熟做法；重训周期与模型可用期限应显式配置。 | 定时任务只创建 challenger；是否晋级必须通过冻结门槛，不能定期自动替换 champion。 |

## 推荐信息架构

### 全局页头

所有页面共用一条紧凑页头：

- `BTC 15m Bot`
- 模式徽标：`RESEARCH / CHALLENGE / SHADOW / CANARY / LIVE`
- 总状态：`正常 / 降级 / 已停止`
- 当前 champion：短版本号，例如 `opening-v12`
- 全局时间范围：`24h / 7d / 30d / 全部`
- 数据截至时间与页面最后刷新时间

总状态不使用模糊的“健康分 92%”。它取所有关键依赖中的最高严重级别：任一必需 feed 或订单对账链路失败，整体就是“降级/故障”，不能被其他正常项目平均掉。这是针对本项目的推断，依据是 Google SRE 对症状告警和明确失败的要求。[Google SRE: Symptoms Versus Causes](https://sre.google/sre-book/monitoring-distributed-systems/#symptoms-versus-causes)

### 视图一：概览

#### 第一行：五个核心数值

| 卡片 | 定义 | 注意事项 |
|---|---|---|
| 当前账户权益 | 可用 USDC + working orders 保留的 USDC + 按明确价格源标记的未结算头寸 | 必须显示 mark 来源和 `as_of`；无可靠 mark 时显示“不可用”，不能用模型概率估值。 |
| 累计净 PnL | 已结算 gross PnL + 未实现 mark PnL - fees + rebates | fees、rebates 另有 attribution；主值默认是实际账户口径。 |
| 今日净 PnL | UTC 日内净 PnL | 点击或 hover 显示 realized/unrealized 拆分。 |
| 最大回撤 | 当前筛选周期内的账户权益 peak-to-trough | 显示金额和百分比。 |
| 当前风险资金 | 未结算成本 + working order 最大可能占用 | 同时显示占账户权益比例。 |

FreqUI 官方明确区分 cumulative profit 与实际 wallet balance，并提醒重建余额可能遗漏出入金；因此本项目也必须把余额、权益和 PnL 的口径拆开，并标记 capture start。[FreqUI Wallet Balance](https://www.freqtrade.io/en/stable/freq-ui/#wallet-balance)

#### 第二行：资金曲线 + Bot 健康

左侧约占三分之二：

- 主线：账户权益。
- 次线：累计已实现净 PnL，可开关，不与权益强行共用含义不明的刻度。
- 下方窄带：drawdown。
- 事件标注：入金/出金、模型 promotion、模式切换、停机和重大数据 gap。
- Shadow 阶段没有真钱时，标题明确写 `Shadow simulated equity`，不能与 live equity 混合。

右侧约占三分之一，仅显示关键链路：

| 链路 | 首页字段 |
|---|---|
| Polymarket Market WS | 状态、最近有效消息 age、最近 PONG age、24h 重连次数 |
| Polymarket User WS | 状态、最近 order/trade event age；Shadow 时显示 `N/A` |
| Chainlink RTDS | 状态、数据 age、24h stale/gap |
| Binance BTC feed | 状态、数据 age、24h stale/gap |
| Order gateway | 最近提交结果、24h reject/timeout；非交易模式显示 `disabled by mode` |
| Collector/storage | 最近成功 flush、待写事件、磁盘剩余预计天数 |
| Clock | 本机 UTC offset 与最近校时 |

绿色只代表通过明确阈值；黄色代表仍可运行但证据降级；红色代表策略必须停止新订单。Polymarket 官方要求 Market/User WebSocket 每 10 秒 `PING`，并通过 User channel报告订单与成交状态，所以这两条连接必须分别监控，不能合成一个“API 在线”。[Polymarket WebSocket overview](https://docs.polymarket.com/market-data/websocket/overview)

#### 第三行：策略生命周期 + 活跃告警

生命周期使用一条短时间线：

```text
Research → Challenge → Shadow → Canary → Live
                ↑ 当前阶段高亮
```

同时只显示：

- champion / challenger 版本；
- 当前阶段开始时间；
- 最近一次训练、challenge 和 promotion；
- 下一次计划 challenge；
- 当前 gate 结论，例如 `等待 1,240 / 2,500 个 forward markets`；
- 模型、配置和代码 revision 的短 hash；
- `on schedule / overdue / blocked`。

活跃告警区域在无告警时压缩成一行“无活跃告警”；有告警时按 Critical、Warning 排列，不展示大量已恢复事件。Google SRE 强调告警必须高信号、低噪声，且代表需要人采取行动的明确故障。[Google SRE: Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/)

#### 第四行：最近交易

首页只展示最近 8–10 个市场级交易：时间、市场、方向、状态、成本、净 PnL。点击后打开交易抽屉；完整列表进入“交易”页。

在 `research_paper` 模式中，状态使用 `simulated_*` 或明确的 Paper 生命周期；
未成交但已结算的订单不计入胜负交易，partial fill 只按实际模拟成交份额计 PnL，
working notional 按各层真实挂单价冻结，而不是按模型 `p_fair` 估算。看板聚合任一
runtime service 的失败：Paper 失败会显示故障，但不会把仍健康的原始采集器停掉。

### 视图二：交易

#### 主表粒度

本策略每个市场最多一次 placement cycle，但可能包含 1–3 层订单和 partial fills。因此主表一行代表“一个市场的一次策略交易”，展开行后再显示订单和 fills。这能同时满足整体可读性与逐单审计，不把 partial fill误当成多次独立策略决策。

主表建议字段：

| 分组 | 字段 |
|---|---|
| 身份 | entry time、market window、Up/Down、运行模式、model version |
| 决策 | `p_fair`、被动价格、模型 edge、安全 buffer、触发 regime（3–30s / 35–90s / 95–180s） |
| 执行 | requested/filled shares、平均成交价、fill rate、首单 ack latency、cancel latency、partial-fill 标记 |
| 资金 | invested capital、gross PnL、fees、rebates、net PnL、return % |
| 结果 | working / partial / filled / cancelled / resolved / failed、最终 outcome、settlement time |

展开后的订单审计字段：

- client order ID 的短格式、价格层、shares；
- decision、submit、ack/live、cancel request、cancel ack、fill 时间；
- 每次 fill 的 price、shares、maker/taker、fee、trade status；
- User channel 的 `MATCHED → MINED → CONFIRMED` 或失败路径；
- cancel-race fill 和 ledger reconciliation 状态。

Polymarket User channel 官方分别定义 placement/update/cancellation 与 trade finality 状态，因此逐单详情应保留这些真实状态，而不是把“HTTP 请求成功”直接显示成“已成交”。[Polymarket User Channel](https://docs.polymarket.com/market-data/websocket/user-channel)

默认筛选仅保留：时间范围、状态、方向、模式、模型版本。CSV/Parquet 导出可以有，但不在首页增加更多筛选器。Freqtrade 官方 API 同样把 trades history、单笔 trade、profit、daily/weekly/monthly 分成独立查询，而不是一张表承担所有分析。[Freqtrade REST API](https://www.freqtrade.io/en/stable/rest-api/#available-endpoints)

### 视图三：运维

#### 连接与服务健康

运维页按“用户可感知症状 → 可能原因”组织：

1. **交易症状**：订单超时、reject、对账缺口、撤单未确认。
2. **数据症状**：必需 feed stale/gap、下一市场未发现、opening window 不完整。
3. **依赖原因**：WebSocket reconnect、PONG timeout、REST error、collector backlog、disk/CPU/memory saturation、clock skew。

这直接采用 Google SRE 的 latency/traffic/errors/saturation 和“what is broken / why”分层；首页看症状，运维页再看原因。[Google SRE: Four Golden Signals](https://sre.google/sre-book/monitoring-distributed-systems/#the-four-golden-signals)

#### 延迟

只展示对策略有意义的阶段：

| 延迟 | 计算边界 | 首页/运维页 |
|---|---|---|
| WebSocket heartbeat RTT proxy | `PING sent → PONG received` | 首页状态；运维页 P50/P95/P99 |
| Market data age | `collector_receive_ts - source_ts` | 首页 P95/当前 age；运维页 P50/P95/P99 |
| Feature availability | `available_ts - source_ts` | 运维页 |
| Decision compute | `decision end - decision start` | 运维页 |
| Submit ack | `HTTP/order submit → response/order id` | 首页 P95/P99；运维页分布 |
| Exchange placement observation | `submit → User WS PLACEMENT` | 运维页 |
| Cancel ack | `cancel request → User WS CANCELLATION/terminal state` | 首页 P95/P99；运维页分布 |
| Fill notification | `exchange match time → collector receive` | 运维页 |

`collector_receive_ts - source_ts` 同时包含网络传输与两端时钟误差，必须标为 data age/observed lag，不能命名成纯网络 latency。Google SRE建议关注尾部延迟，特别是 P99，而不只看平均值；成功和失败请求也应分开统计。[Google SRE: Worrying About Your Tail](https://sre.google/sre-book/monitoring-distributed-systems/#worrying-about-your-tail-or-instrumentation-and-performance)

#### 数据质量

展示可验证事实，不合成不透明分数：

- 24h expected / observed / eligible market windows；
- 因 ingest epoch 切换或缺少因果观察而跳过的 Shadow window 数量与明确原因；
- 36 个 opening decision 的完整覆盖率；
- stale、gap、duplicate、out-of-order、invalid payload 数；
- 每个 required source 的最新 `available_ts`；
- 最新完整 raw manifest、最近 flush、pending events；
- tick/fee/rule/schema epoch 是否变化；
- ledger 与 authenticated account reconciliation 是否一致。

### 策略生命周期与建议节奏

以下节奏是**针对本项目的建议**，不是上述产品的固定规则：

| 周期 | 动作 | 是否自动发布 |
|---|---|---|
| 每日 | 汇总数据质量、PnL、风险、延迟和模型输入漂移；只生成状态与告警。 | 否 |
| 每 14 天 | 用新增合格数据训练 challenger，冻结 artifact 后运行既定 challenge；与现有 14 日 walk-forward 步长一致。 | 否 |
| 每 28 天或达到预注册样本/成交门槛 | 做 promotion review：统计效果、执行效果、回撤、容量和稳定性均通过才允许替换 champion。 | 否，需明确 promotion 记录 |
| 事件触发 | fee/tick/rule/schema 变化、必需 feed 改变、模型过期、持续漂移或重大延迟异常时，提前 challenge 或降级。 | 否 |

初次正式 Go 使用的 sealed holdout 是一次性的锁定终考，不应在日常调参中反复查看。上线后的 14/28 日循环使用新的 forward OOS 数据做 champion/challenger 比较，不能继续把同一个 sealed holdout 当作调参集。

FreqAI 官方提供 periodic retraining、sliding training window 与 model expiration，但并不意味着新模型应自动替换旧模型；MLflow 官方使用 champion/challenger aliases 和 validation tags 表达验证与部署状态。基于这两类做法，本项目应“自动产出候选，门槛化晋级”。[FreqAI periodic retraining](https://www.freqtrade.io/en/stable/freqai-running/)、[MLflow Model Registry workflow](https://mlflow.org/docs/latest/ml/model-registry/workflow/)

生命周期卡建议保存并显示：

- `stage`、`stage_started_at`、`stage_gate_status`；
- champion/challenger `model_id`、artifact hash、feature schema hash；
- training/calibration/OOS end time 与样本数；
- `last_trained_at`、`last_challenged_at`、`next_challenge_at`；
- challenge verdict、失败 gate 和 promotion operator/time；
- deployed code revision、config hash、data ingest version；
- model age/expiration 与当前数据 epoch。

## 告警设计

### Critical：必须停止新订单或立即人工处理

- Live/Canary 中 Polymarket User WS 失联或订单无法对账；
- 必需市场数据 stale/gap，或下一 15m 市场未正确发现；
- cancel 超过风险阈值仍无 terminal state；
- ledger 与账户余额/持仓不一致；
- clock offset 超过既定上限；
- 风险限额、可用余额或 allowance 不满足；
- fee/tick/rule/schema 未验证即变化。

### Warning：策略可降级运行，但需要观察

- reconnect 次数、P95/P99 submit/cancel latency 显著超过基线；
- collector backlog、磁盘剩余、CPU/memory saturation 接近阈值；
- challenge overdue、模型接近 expiration；
- eligible window coverage 下降但未触发硬停止；
- fill rate、adverse-selection 或 drawdown 触及预警线。

### Info：记录但不打扰

- challenger 训练完成；
- challenge/promotion 完成；
- 模式切换、部署版本变化；
- 市场正常结算和日终汇总。

每条告警只包含 severity、开始时间、持续时间、受影响链路、明确阈值、当前值、自动动作与 runbook 链接。首页只显示 active Critical/Warning；恢复事件进入历史。Google SRE明确指出过多告警会导致忽略真正故障，告警规则应简单且对应清晰失败。[Google SRE: Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/)

## 不应在主看板展示的内容

- 原始 JSON、完整日志或 stack trace；只给错误摘要和诊断链接。
- 全量模型特征、feature importance、LightGBM 训练曲线、每个阈值扫描结果。
- L2 全深度热力图、全部 token/condition ID、每条 WebSocket 消息。
- CPU/RAM/磁盘的常态曲线；只有接近 saturation 或进入运维页时才显示。
- 十几个收益比率或装饰性 gauge；首页仅保留权益、PnL、回撤、风险资金。
- rebate-inclusive 的“乐观 PnL”作为主收益；rebate 只做单独 attribution。
- expected edge、研究 Proxy PnL 与真实 realized PnL 混合累计。
- 未结算头寸使用模型概率伪装成可兑现价值。
- 所有 feeds 的平均延迟；平均值会隐藏尾部问题，主监控应使用 P95/P99。
- 直接下单、强平、改参数、promote model 或显示 API credentials 的按钮。
- 永久常驻的绿色卡片和无告警历史；正常项应压缩，让异常更醒目。

Grafana 官方建议避免不必要刷新、堆叠误导与无目标图表；Freqtrade 官方警告可控制 Bot 的 API 不应直接暴露公网；Polymarket 官方明确禁止在客户端暴露 User channel credentials。因此“简约只读”同时是 UX 与安全边界。[Grafana dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/)、[Freqtrade REST API security](https://www.freqtrade.io/en/stable/rest-api/#security)、[Polymarket User Channel](https://docs.polymarket.com/market-data/websocket/user-channel)

## 刷新与视觉原则

- 运维状态、活动订单：5 秒刷新；WebSocket 原始 heartbeat 继续按协议独立运行。
- PnL、权益、交易表：15–30 秒刷新；结算后事件驱动更新。
- 生命周期与模型治理：5 分钟或事件驱动刷新。
- 服务新鲜度不能统一套用一个固定秒数。生产者必须声明
  `expected_status_interval_seconds`；看板只对该服务使用带余量的阈值，未声明的
  collector/Paper 继续使用严格默认值，避免 60 秒 Shadow 调度器被 30 秒阈值周期性
  误报为故障。
- 深色与浅色均可，但主色保持中性；绿色只用于正常，橙色用于降级，红色只用于需要行动的故障/亏损。
- 金额统一 USDC，概率统一百分比或 0–1 其中一种，延迟统一毫秒。
- 所有数值显示 `as_of`、口径和数据状态：`observed / reconstructed / proxy`。
- 空状态明确写“当前阶段无此数据”，例如 Shadow 的真实下单延迟为 `N/A`，不能显示 0 ms。

Grafana建议刷新频率应匹配数据变化速度，并为 panel 添加说明；FreqUI支持时区设置且对余额历史标注可信起点。这里的具体 5 秒/15–30 秒是针对本 Bot 决策速度和低复杂度目标的工程推断。[Grafana dashboard best practices](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/)、[FreqUI](https://www.freqtrade.io/en/stable/freq-ui/)

## 推荐实施顺序

1. 先完成概览：真实权益/PnL、资金曲线、关键链路健康、生命周期、最近交易、活跃告警。
2. 再完成交易页：市场级主表、订单/fill 展开、实际与 Proxy 口径隔离。
3. 最后完成运维页：分阶段延迟、数据质量、告警历史和诊断链接。
4. 所有运行阶段共用同一 UI；缺失能力显示 `N/A by mode`，不维护多套看板。

这一顺序优先满足“打开后几秒内知道是否正常、是否赚钱、是否该行动”，同时为后续 Shadow、Canary 和 Live 逐步补齐真实数据留出稳定接口。

Research Paper 的首页默认只显示启用中的四张策略卡：首次合格信号立即提交的 1x5
FAK 对照、复用同一 1x5 候选筛选但挂至市场结束的 maker 对照、两次五秒确认的 2x5
主策略，以及带 edge 稳定约束的 Stable 3x5 对照。三个
暂停的 maker-gated 历史策略仍保留在状态与账本中，但只有用户切换到“全部”筛选后
才显示。筛选只影响前端展示，不删除历史数据，也不改变 runtime 决策。各变体资金、
订单、成交与 PnL 账本完全隔离；maker 对照的有效期为市场不可变的 `t1`，但数据异常、
规则变化等安全撤单仍然生效。主资金曲线只采用标记为 primary 的 2x5。卡片显示
全样本 PnL、核心与全样本已结算机会 EV、按已成交份额计算的 conditional EV、
成交率、评估/合格信号、核心/尾部样本和 taker fee，并按 3--30 秒、35--90 秒、
95--180 秒及价格区间分层。Promotion 只读取核心样本 EV；尾部研究样本不能靠较大
的偶然 PnL 抬高 Go 指标。

策略卡下方显示主策略本进程的决策漏斗：decision tick、有效预测、候选信号、独立
机会、提交、拒绝、成交、结算。该漏斗只用于解释为什么没有订单或没有成交，不能跨重启
拼接为伪精确转化率。首页订单仍固定为最近 15 条；完整历史通过只读、分页、可按
variant 筛选的 `/api/orders` 显式加载。历史读取递归发现各 execution epoch 的账本，
但任一账本结构、路径身份或 schema 校验失败时整次请求 fail closed，不返回看似完整
的残缺结果，也不暴露本地路径或 token ID。

订单表必须显示策略、执行状态与结算状态，未成交订单的 PnL 显示 `—`，不能把
“已结算但未成交”显示成一笔收益为零的交易。完整历史保留 opportunity、执行阶段、
core/tail、Go eligibility、decision ask 以及最多三次 fair/VWAP/fee/net-edge 观察；
终止原因、queue ahead、可消费卖盘量、执行路径和 taker fee 继续保留供逐单诊断。
看板必须区分四个不同口径：`evaluation` 是一次 planner 计算，`qualified signal`
是通过 edge/价格/数据 gate 的信号，`opportunity` 是完成确认并创建不可变交易记录，
`fill` 是正成交份额。用户可见的“机会 / 成交”只能使用后两者；被拒绝的五秒评估
不得进入机会分母。跨 execution epoch 的账本可在完整历史中筛选和汇总，但不同
策略版本的 starting balance 与资金曲线不得拼接为同一账户曲线。

方向健康使用独立的市场级证据口径，自首次部署该能力起记录所有激活市场、共享模型的
`p_up` 以及最终结果。模型预测不会按并行 execution variant 重复计数，也不会把每个 5 秒
tick 当成独立市场样本：先在每个 `3--30s`、`35--90s`、`95--180s` 阶段内求均值，再按市场
聚合。实际 UP/DOWN 比例只与“有预测且已结算”的同一批市场配对；没有机会的市场仍须保存
结果。旧账本缺少无机会市场与完整 `p_up`，不得推测回填，页面必须显示 coverage start。

方向偏向不要求接近 50/50。首页同时显示实际 UP/DOWN、模型硬方向、平均 `p_up`、配对样本数
和分阶段摘要；只有配对市场至少 100 个且 calibration-in-the-large 的标准化残差
`|z| > 1.96` 时提示检查。样本不足只显示积累中。各 variant 另显示合格信号、确认机会、成交、
准确率和 PnL 的 UP/DOWN 拆分，用于区分模型偏向、筛选偏向与执行偏向，但不替代 Brier、
calibration、净 EV 或 sealed holdout。
