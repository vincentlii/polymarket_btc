# prediction-market-backtesting

![GitHub stars](https://img.shields.io/github/stars/evan-kolberg/prediction-market-backtesting?style=social)
![GitHub forks](https://img.shields.io/github/forks/evan-kolberg/prediction-market-backtesting?style=social)
![GitHub watchers](https://img.shields.io/github/watchers/evan-kolberg/prediction-market-backtesting?style=social)

[![Licensing: Mixed](https://img.shields.io/badge/licensing-MIT%20%2B%20LGPL--3.0--or--later-blue.svg)](NOTICE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/charliermarsh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![Rust](https://img.shields.io/badge/rust-1.93.1-CE422B?logo=rust&logoColor=white)
![Rust Edition](https://img.shields.io/badge/edition-2024-CE422B?logo=rust&logoColor=white)
![NautilusTrader](https://img.shields.io/badge/NautilusTrader-1.226.0-1E3A5F)
![GitHub last commit](https://img.shields.io/github/last-commit/evan-kolberg/prediction-market-backtesting)
![GitHub commit activity](https://img.shields.io/github/commit-activity/m/evan-kolberg/prediction-market-backtesting)
![GitHub code size](https://img.shields.io/github/languages/code-size/evan-kolberg/prediction-market-backtesting)
![GitHub top language](https://img.shields.io/github/languages/top/evan-kolberg/prediction-market-backtesting)
![GitHub open issues](https://img.shields.io/github/issues/evan-kolberg/prediction-market-backtesting)
![GitHub contributors](https://img.shields.io/github/contributors/evan-kolberg/prediction-market-backtesting)
![GitHub pull requests](https://img.shields.io/github/issues-pr/evan-kolberg/prediction-market-backtesting)
![GitHub closed issues](https://img.shields.io/github/issues-closed/evan-kolberg/prediction-market-backtesting)
![GitHub closed pull requests](https://img.shields.io/github/issues-pr-closed/evan-kolberg/prediction-market-backtesting)

**New in Version 4.1-alpha:**
- Live sandbox plumbing for Polymarket BTC 5min markets
- Example runner showing how to use live BTC 5min hooks
- (strategy & model not included)

**New in Version 4:**
- Nautilus 1.226.0
- Rust-native data conversion
- Faster staged data loading
- Improved materialized caches
- Unified cache/local/archive/API message bus

**New in Version 3:**
- Telonex vendor support
- Local Telonex download script
- Many bug fixes & accuracy improvements
- Book replay order book deltas with trade ticks

**New in Version 2:**
- Nautilus via PyPI in lieu of a subtree
- Better backtest runner classes via EXPERIMENT objects
- IPython notebook support (.ipynb files)
- Joint portfolio multi replay runners
- Growing support for statistical optimizers
- New aggregate charts
- Massive improvements charting gen speed
- an attempt at a Tree-structured Parzen Estimator via Optuna

Looking for the old version? That was renamed to [Version 1](https://github.com/evan-kolberg/prediction-market-backtesting/tree/v1)

Backtesting framework for prediction market strategies on
[Polymarket](https://polymarket.com), built on top of
[NautilusTrader](https://github.com/nautechsystems/nautilus_trader) with custom
exchange adapters. [Limitless.exchange](https://limitless.exchange) and
[Opinion.trade](https://opinion.trade) are planned next; [Kalshi](https://kalshi.com) support depends on access to L2 historical book data. Current Kalshi components are research and fee-modeling plumbing, not a public runnable backtest path. Plotting inspired by [minitrade](https://github.com/dodid/minitrade). This repo is still in active development.


Fantastic single & multi-market charting. Featuring: equity (total & individual markets), profit / loss ticks, P&L periodic bars, market allocation, YES price (with green buy and red sell fills), drawdown, sharpe (with above/below shading), cash / equity, monthly returns, and cumulative brier advantage.
![Charting preview](https://raw.githubusercontent.com/evan-kolberg/prediction-market-backtesting/main/docs/assets/charting-preview.jpeg)

**If you find any bugs, unexpected behavior, or missing simulation features, PLEASE post an [issue](https://github.com/evan-kolberg/prediction-market-backtesting/issues/new) or [discussion](https://github.com/evan-kolberg/prediction-market-backtesting/discussions/new/choose).**

Detailed guides have been filed away in the [docs index](https://evan-kolberg.github.io/prediction-market-backtesting/) for better organization and long-term sustainability.

## Table of Contents

- [Docs Index](https://evan-kolberg.github.io/prediction-market-backtesting/)
  - [Start Here](https://evan-kolberg.github.io/prediction-market-backtesting/#start-here)
  - [Core Framework](https://evan-kolberg.github.io/prediction-market-backtesting/#core-framework)
  - [Advanced / Experiments](https://evan-kolberg.github.io/prediction-market-backtesting/#advanced-experiments)
  - [Project](https://evan-kolberg.github.io/prediction-market-backtesting/#project)
  - [Acknowledgements](https://evan-kolberg.github.io/prediction-market-backtesting/#acknowledgements)
- [Setup](https://evan-kolberg.github.io/prediction-market-backtesting/setup/)
  - [Prerequisites](https://evan-kolberg.github.io/prediction-market-backtesting/setup/#prerequisites)
  - [Install](https://evan-kolberg.github.io/prediction-market-backtesting/setup/#install)
  - [First Run](https://evan-kolberg.github.io/prediction-market-backtesting/setup/#first-run)
  - [Timing And Cache Defaults](https://evan-kolberg.github.io/prediction-market-backtesting/setup/#timing-and-cache-defaults)
  - [Extension Architecture](https://evan-kolberg.github.io/prediction-market-backtesting/setup/#extension-architecture)
- [Backtests And Runners](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/)
  - [Repo Layout](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#repo-layout)
  - [Runner Contract](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#runner-contract)
  - [HTML And Report Modes](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#html-and-report-modes)
  - [Optimization Runners](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#optimization-runners)
  - [Designing Good Runner Files](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#designing-good-runner-files)
  - [Multi-Market Strategy Configs](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#multi-market-strategy-configs)
  - [Running Backtests](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#running-backtests)
  - [Notebook Runners](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#notebook-runners)
  - [Editing Runner Inputs](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#editing-runner-inputs)
  - [Data Vendor Notes](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#data-vendor-notes)
    - [Native Vendors](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#native-vendors)
    - [PMXT](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#pmxt)
    - [Telonex](https://evan-kolberg.github.io/prediction-market-backtesting/backtests/#telonex)
- [Sandbox And Live Runners](https://evan-kolberg.github.io/prediction-market-backtesting/live/)
  - [Current Scope](https://evan-kolberg.github.io/prediction-market-backtesting/live/#current-scope)
  - [Directory Contract](https://evan-kolberg.github.io/prediction-market-backtesting/live/#directory-contract)
  - [Sandbox Runner Contract](https://evan-kolberg.github.io/prediction-market-backtesting/live/#sandbox-runner-contract)
  - [Shared Live Helpers](https://evan-kolberg.github.io/prediction-market-backtesting/live/#shared-live-helpers)
  - [BTC 5m Sandbox Plumbing](https://evan-kolberg.github.io/prediction-market-backtesting/live/#btc-5m-sandbox-plumbing)
  - [Example BTC Snapshot Runner](https://evan-kolberg.github.io/prediction-market-backtesting/live/#example-btc-snapshot-runner)
  - [Private Strategy Boundary](https://evan-kolberg.github.io/prediction-market-backtesting/live/#private-strategy-boundary)
  - [Model And Parameter Placement](https://evan-kolberg.github.io/prediction-market-backtesting/live/#model-and-parameter-placement)
  - [Running Sandbox](https://evan-kolberg.github.io/prediction-market-backtesting/live/#running-sandbox)
  - [Public Polymarket Data](https://evan-kolberg.github.io/prediction-market-backtesting/live/#public-polymarket-data)
  - [Path To Live Polymarket Trading](https://evan-kolberg.github.io/prediction-market-backtesting/live/#path-to-live-polymarket-trading)
- [Data Loading](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/)
  - [Mental Model](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/#mental-model)
  - [Staged Loading](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/#staged-loading)
  - [PMXT Flow](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/#pmxt-flow)
  - [Telonex Flow](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/#telonex-flow)
  - [Caching](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/#caching)
  - [Downloading Local Data](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/#downloading-local-data)
  - [Progress And Timing](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/#progress-and-timing)
  - [Failure Semantics](https://evan-kolberg.github.io/prediction-market-backtesting/data-loading/#failure-semantics)
- [Polymarket Account Ledger Replay](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/)
  - [Runner And Notebook](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/#runner-and-notebook)
  - [What The Strategy Does](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/#what-the-strategy-does)
  - [Why Exact Reproduction Fails](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/#why-exact-reproduction-fails)
  - [Copy-Trading Interpretation](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/#copy-trading-interpretation)
  - [External Source Check](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/#external-source-check)
  - [Observed Result](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/#observed-result)
  - [Latest Terminal Output](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/#latest-terminal-output)
  - [How To Use This Experiment](https://evan-kolberg.github.io/prediction-market-backtesting/account-ledger-replay/#how-to-use-this-experiment)
- [Research](https://evan-kolberg.github.io/prediction-market-backtesting/research/)
  - [Overview](https://evan-kolberg.github.io/prediction-market-backtesting/research/#overview)
  - [Warm PMXT Cache Before Notebook Runs](https://evan-kolberg.github.io/prediction-market-backtesting/research/#warm-pmxt-cache-before-notebook-runs)
  - [Scoring](https://evan-kolberg.github.io/prediction-market-backtesting/research/#scoring)
  - [Joint-Portfolio Mode](https://evan-kolberg.github.io/prediction-market-backtesting/research/#joint-portfolio-mode)
  - [Samplers](https://evan-kolberg.github.io/prediction-market-backtesting/research/#samplers)
    - [Random Grid (`sampler="random"`)](https://evan-kolberg.github.io/prediction-market-backtesting/research/#random-grid-samplerrandom)
    - [TPE (`sampler="tpe"`)](https://evan-kolberg.github.io/prediction-market-backtesting/research/#tpe-samplertpe)
  - [Caveats](https://evan-kolberg.github.io/prediction-market-backtesting/research/#caveats)
  - [Notebook Output Persistence](https://evan-kolberg.github.io/prediction-market-backtesting/research/#notebook-output-persistence)
- [BTC Short-Horizon Architecture](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/)
  - [Scope](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#scope)
  - [Boundaries](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#boundaries)
  - [Data Flow](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#data-flow)
  - [Runtime Stages](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#runtime-stages)
  - [Core Invariants](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#core-invariants)
  - [Implemented Module Boundaries](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#implemented-module-boundaries)
  - [Configuration](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#configuration)
  - [Data Contracts](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#data-contracts)
  - [Forward Collection](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#forward-collection)
  - [Opening Mispricing Research Protocol](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#opening-mispricing-research-protocol)
    - [Fast Fair-Probability Feasibility Proxy](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#fast-fair-probability-feasibility-proxy)
    - [Opening Market Evidence Audit](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#opening-market-evidence-audit)
    - [Opening Feature Evidence Audit](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#opening-feature-evidence-audit)
  - [Running One Replay](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#running-one-replay)
  - [Dashboard Observability](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#dashboard-observability)
  - [Live Progression](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#live-progression)
  - [Windows Native Build](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#windows-native-build)
  - [Current Evidence Boundary](https://evan-kolberg.github.io/prediction-market-backtesting/btc-short-horizon-architecture/#current-evidence-boundary)
- [BTC 前瞻数据持久性与灾难恢复](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/)
  - [结论](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/#结论)
  - [v9 会话事务](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/#v9-会话事务)
  - [权威读取边界](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/#权威读取边界)
  - [内容寻址备份协议](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/#内容寻址备份协议)
  - [运维命令](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/#运维命令)
  - [推荐节奏与告警](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/#推荐节奏与告警)
  - [安全限制](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/#安全限制)
  - [验收标准](https://evan-kolberg.github.io/prediction-market-backtesting/btc-data-durability-research/#验收标准)
- [BTC Market-Data Protocol Boundaries](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/)
  - [Required collector behavior](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#required-collector-behavior)
  - [1. Binance Spot diff depth](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#1-binance-spot-diff-depth)
    - [Binance Spot official facts](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#binance-spot-official-facts)
    - [Binance Spot implementation rule](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#binance-spot-implementation-rule)
    - [Binance Spot documented ambiguity](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#binance-spot-documented-ambiguity)
  - [2. Binance USD-M Futures diff depth](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#2-binance-usd-m-futures-diff-depth)
    - [Binance Futures official facts](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#binance-futures-official-facts)
    - [Binance Futures implementation rule](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#binance-futures-implementation-rule)
    - [Binance Futures unknown or not guaranteed](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#binance-futures-unknown-or-not-guaranteed)
  - [3. OKX `books`](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#3-okx-books)
    - [OKX official facts](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#okx-official-facts)
    - [OKX implementation rule](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#okx-implementation-rule)
    - [OKX unknown or not guaranteed](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#okx-unknown-or-not-guaranteed)
  - [4. Polymarket CLOB `book` and `price_change`](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#4-polymarket-clob-book-and-price_change)
    - [Polymarket official facts](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#polymarket-official-facts)
    - [Polymarket implementation rule](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#polymarket-implementation-rule)
    - [Polymarket unknown or not guaranteed](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#polymarket-unknown-or-not-guaranteed)
  - [Acceptance fixtures derived from the official boundaries](https://evan-kolberg.github.io/prediction-market-backtesting/btc-market-data-protocol-research/#acceptance-fixtures-derived-from-the-official-boundaries)
- [BTC Bot 本地执行与低延迟研究](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/)
  - [结论](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#结论)
  - [原帖逐项核对](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#原帖逐项核对)
  - [当前 CLOB V2 的签名与 nonce 边界](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#当前-clob-v2-的签名与-nonce-边界)
    - [启动或 credential 管理阶段](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#启动或-credential-管理阶段)
    - [每张订单](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#每张订单)
    - [每次 HTTP 请求](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#每次-http-请求)
  - [WebSocket 与 REST 的正确分工](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#websocket-与-rest-的正确分工)
  - [Batch 不是原子交易](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#batch-不是原子交易)
  - [本项目建议的执行热路径](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#本项目建议的执行热路径)
    - [开盘前/后台预热](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#开盘前后台预热)
    - [正常入场热路径](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#正常入场热路径)
    - [异常与恢复路径](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#异常与恢复路径)
  - [应测量的延迟，而不是只测一个总 RTT](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#应测量的延迟而不是只测一个总-rtt)
  - [部署区域与资格的关键修正](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#部署区域与资格的关键修正)
  - [截至 2026-07-16 的不确定性](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#截至-2026-07-16-的不确定性)
  - [对项目路线的直接建议](https://evan-kolberg.github.io/prediction-market-backtesting/btc-execution-latency-research/#对项目路线的直接建议)
- [BTC VPS Operations](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/)
  - [Scope](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#scope)
  - [Runtime Boundary](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#runtime-boundary)
  - [Dashboard Data Contract](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#dashboard-data-contract)
  - [Local Validation](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#local-validation)
  - [Deployment](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#deployment)
  - [Data Backup And Restore](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#data-backup-and-restore)
  - [Migration](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#migration)
  - [Pre-Purchase Validation Status](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#pre-purchase-validation-status)
  - [Release Gates](https://evan-kolberg.github.io/prediction-market-backtesting/btc-vps-operations/#release-gates)
- [BTC Bot Dashboard Research](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/)
  - [结论](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#结论)
  - [第一手产品与规范中可直接借鉴的做法](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#第一手产品与规范中可直接借鉴的做法)
  - [推荐信息架构](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#推荐信息架构)
    - [全局页头](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#全局页头)
    - [视图一：概览](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#视图一概览)
    - [视图二：交易](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#视图二交易)
    - [视图三：运维](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#视图三运维)
    - [策略生命周期与建议节奏](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#策略生命周期与建议节奏)
  - [告警设计](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#告警设计)
    - [Critical：必须停止新订单或立即人工处理](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#critical必须停止新订单或立即人工处理)
    - [Warning：策略可降级运行，但需要观察](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#warning策略可降级运行但需要观察)
    - [Info：记录但不打扰](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#info记录但不打扰)
  - [不应在主看板展示的内容](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#不应在主看板展示的内容)
  - [刷新与视觉原则](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#刷新与视觉原则)
  - [推荐实施顺序](https://evan-kolberg.github.io/prediction-market-backtesting/btc-dashboard-research/#推荐实施顺序)
- [Execution Modeling](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/)
  - [Fees](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#fees)
    - [Maker Rebates](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#maker-rebates)
  - [Slippage](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#slippage)
  - [Passive Orders And Queue Position](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#passive-orders-and-queue-position)
  - [Latency](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#latency)
  - [Limits](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#limits)
  - [Vendor L2 Behavior](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#vendor-l2-behavior)
    - [PMXT](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#pmxt)
    - [Telonex](https://evan-kolberg.github.io/prediction-market-backtesting/execution-modeling/#telonex)
- [Data Vendors And Local Mirrors](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/)
  - [PMXT](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#pmxt)
    - [Runner Source Modes](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#runner-source-modes)
    - [Lower-Level Loader Env Vars](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#lower-level-loader-env-vars)
    - [What Works Today](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#what-works-today)
    - [Supported Local File Layout](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#supported-local-file-layout)
    - [Required Parquet Columns](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#required-parquet-columns)
    - [Legacy JSON Payload Shape](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#legacy-json-payload-shape)
  - [Telonex](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#telonex)
    - [Download Local Telonex Files](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#download-local-telonex-files)
  - [What Is Not Plug-And-Play Yet](https://evan-kolberg.github.io/prediction-market-backtesting/data-vendors/#what-is-not-plug-and-play-yet)
- [Vendor Fetch Sources And Timing](https://evan-kolberg.github.io/prediction-market-backtesting/vendor-fetch-sources/)
  - [PMXT](https://evan-kolberg.github.io/prediction-market-backtesting/vendor-fetch-sources/#pmxt)
  - [Example Output](https://evan-kolberg.github.io/prediction-market-backtesting/vendor-fetch-sources/#example-output)
  - [Telonex](https://evan-kolberg.github.io/prediction-market-backtesting/vendor-fetch-sources/#telonex)
  - [Timing Expectations By Source](https://evan-kolberg.github.io/prediction-market-backtesting/vendor-fetch-sources/#timing-expectations-by-source)
  - [How To See This Output](https://evan-kolberg.github.io/prediction-market-backtesting/vendor-fetch-sources/#how-to-see-this-output)
- [Plotting](https://evan-kolberg.github.io/prediction-market-backtesting/plotting/)
  - [Scaling Model](https://evan-kolberg.github.io/prediction-market-backtesting/plotting/#scaling-model)
  - [Downsampling](https://evan-kolberg.github.io/prediction-market-backtesting/plotting/#downsampling)
  - [Output Types](https://evan-kolberg.github.io/prediction-market-backtesting/plotting/#output-types)
  - [Output Paths](https://evan-kolberg.github.io/prediction-market-backtesting/plotting/#output-paths)
  - [Example Summary Output](https://evan-kolberg.github.io/prediction-market-backtesting/plotting/#example-summary-output)
  - [Multi-Market References](https://evan-kolberg.github.io/prediction-market-backtesting/plotting/#multi-market-references)
- [Testing](https://evan-kolberg.github.io/prediction-market-backtesting/testing/)
  - [Standard Repo Gate](https://evan-kolberg.github.io/prediction-market-backtesting/testing/#standard-repo-gate)
  - [Useful Smoke Checks](https://evan-kolberg.github.io/prediction-market-backtesting/testing/#useful-smoke-checks)
  - [Docs Validation](https://evan-kolberg.github.io/prediction-market-backtesting/testing/#docs-validation)
- [Project Status](https://evan-kolberg.github.io/prediction-market-backtesting/project-status/)
  - [BTC 15-Minute Short-Horizon Project](https://evan-kolberg.github.io/prediction-market-backtesting/project-status/#btc-15-minute-short-horizon-project)
  - [Roadmap](https://evan-kolberg.github.io/prediction-market-backtesting/project-status/#roadmap)
  - [Known Issues](https://evan-kolberg.github.io/prediction-market-backtesting/project-status/#known-issues)
  - [Recently Fixed](https://evan-kolberg.github.io/prediction-market-backtesting/project-status/#recently-fixed)
- [License Notes](https://evan-kolberg.github.io/prediction-market-backtesting/license/)
  - [Scope](https://evan-kolberg.github.io/prediction-market-backtesting/license/#scope)
  - [NautilusTrader Attribution](https://evan-kolberg.github.io/prediction-market-backtesting/license/#nautilustrader-attribution)
  - [Practical Meaning](https://evan-kolberg.github.io/prediction-market-backtesting/license/#practical-meaning)


## Star History

<a href="https://www.star-history.com/?repos=evan-kolberg%2Fprediction-market-backtesting&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/image?repos=evan-kolberg/prediction-market-backtesting&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/image?repos=evan-kolberg/prediction-market-backtesting&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/image?repos=evan-kolberg/prediction-market-backtesting&type=date&legend=top-left" />
 </picture>
</a>
