# Project Status

## BTC 15-Minute Short-Horizon Project

This independent BTC-only project is implemented under `btc_short_horizon/`.
It reuses the framework's L2 BookReplay, matching, fees, latency, settlement,
and reporting plumbing without importing archived BTC strategy logic.

- Complete code paths: causal data contracts, storage/manifests, multi-venue
  opening features, fair-probability models, post-only dual-side maker planning,
  dual-token replay, required artifacts, shadow/canary safety, and Go/No-Go gates.
- Pre-VPS runtime: a public forward-collector supervisor now persists atomic
  status/control files, stops safely on a local kill request, exposes a
  loopback-only read-only dashboard, and has Docker Compose assets with host
  data/output mounts. The dashboard now has a validated projection seam for
  account equity/PnL, an equity curve, recent market-level trades, connection
  and order-latency health, active alerts, and the Research-to-Live lifecycle.
  Missing projections stay visibly empty; research Proxy and Shadow output are
  never rendered as real profit. An optional post-window Shadow scheduler
  writes one SHA-versioned causal pass per completed window. This is
  collection/observability plumbing only; Docker build verification still
  requires a Docker Engine, and it does not authorize Paper, Canary, or live
  orders.
- Data boundary: 15m is the only trading family; 5m is collection-only.
- Forward collection: `btc_forward_collector.py --follow-current` now discovers
  current and next exact Gamma slugs, persists separate rule-hash catalogs, and
  pre-subscribes both token pairs before the next opening window. It retains the
  old pair through `t0+180s` so the entire research window has one connection.
- Evidence boundary: no profitability or deployability claim exists until real
  data passes the documented holdout and the complete queue-enabled P99
  trade-volume/timestamp-order robustness grid.
- Model/research hardening: model inputs and probabilities now reject coercion,
  non-finite values, invalid shapes, and inconsistent lineage. Walk-forward
  tests are non-overlapping and group-safe; LightGBM early stopping, calibration,
  OOF, and holdout remain disjoint. Isotonic counts unique markets. Candidate
  and price-threshold selection use contiguous UTC-day block bootstrap with
  multiple-comparison control. Model artifacts publish atomically and bind
  schema/config/source/code hashes. Price-proxy persistence resets on an
  intervening failed signal, and stress tests discard prices at or above one
  instead of fabricating an executable price.
- Replay/execution hardening: the BTC runner now consumes current dual-token L2
  books, requires exchange-valid post-only prices and minimum sizes, caps each
  layer to 5% of displayed same-side depth, and confirms two exact five-second
  signals. Formal evidence is a four-component P99 grid covering full/half
  execution-trade volume and both same-timestamp book/trade orderings; each
  component keeps queue modelling on and rebates off, and no component is a
  standalone strategy conclusion. Real Nautilus fixtures cover post-only
  insert races, partial fills during cancel latency, synchronous multi-layer
  rejection, and the GTC maximum-work timer. Event-level fills reconcile to
  Nautilus order summaries and carry 1/3/10/30/60-second markouts plus source
  book age. Placement and working orders now fail closed when either execution
  L2 book is missing, future-dated, invalid, or older than the configured
  freshness limit. Formal ledgers require explicit finite settlement fields and
  the complete exact-horizon markout grid. Run artifacts publish as an immutable
  whole-directory transaction
  and preserve the union of heterogeneous lifecycle fields in Parquet. Signal
  and coverage manifests replace operator-entered provenance hashes; PMXT
  coverage now binds canonical per-token replay-record SHA-256 values which the
  loader recomputes before engine start.
- Live execution hardening: the CLOB V2 gateway is pinned to the exact official
  SDK release and official origin, accepts only explicit credentials, validates
  signer/funder/signature type, and prepares post-only GTC orders without a
  REST read-before-write. It computes the venue order hash locally, persists
  the expected identity before POST, requires the response ID to match, and
  handles mixed batch results per order. The hash-chained, locked, `fsync` WAL
  rejects secret-bearing payloads and supports crash recovery from open orders
  or `GET /order/{hash}` terminal evidence. Unknown submissions, reconciliation
  failures, User WebSocket gaps/subscription changes, heartbeat failures, and
  failed terminal trades close the trading gate and trigger cancel-all instead
  of retrying. User order/trade updates are independently idempotent, and only
  confirmed fills advance canary counts. A bounded operations supervisor now
  schedules heartbeat, exact daily account-ledger refresh, authenticated REST
  reconciliation, User-channel gap admission, runtime status and real dashboard
  publication. Immutable WAL segments support safe halted-state checkpoint and
  rotation without resetting the global hash/sequence chain; restart restores
  market-cycle guards and canary counts from the latest verified checkpoint.
  Operator recovery is durable and cannot override unresolved venue evidence.
  This is a fail-closed control plane, not a trading decision runner or VPS
  approval.
- Deployment hardening: the target-host preflight binds the exact release Git
  revision and rule epoch, verifies non-symlink durable mounts, capacity, NTP,
  official geoblock, CLOB clock offset and CLOB/Gamma/Binance latency, and
  persists a content-addressed receipt. Compose services run read-only as an
  unprivileged user with all capabilities dropped, `no-new-privileges`, bounded
  PIDs and explicit pre-created bind mounts. Live secrets support strict
  mutually exclusive `POLY_*_FILE` injection and are excluded from Git and the
  image context. The dashboard health endpoint fails when the real ledger
  projection is missing, future-dated or stale.
- Runtime durability: `btc_runtime_archive.py` creates release/rule-bound
  content-addressed snapshots for model, WAL, exact ledger and report roots,
  uploads through the existing immutable local/rclone transport, full-download
  verifies every object, restores only into an isolated no-overwrite tree and
  records a freshness-audited restore drill. Source overlaps, symlinks,
  partial/secret-like files, hash changes and conflicting targets fail closed.
  Critical runtime evidence has an explicit retain-local policy; only the much
  larger raw sessions use verified-receipt local reclamation.
- Current three-minute fair-probability proxy: the reproducible protocol v2 exact
  `stride=1` run used all 7,295 resolved markets and 262,620 causal five-second snapshots.
  Paired daily-block candidate selection retained `logistic-c0.1` because
  neither LightGBM candidate improved both log loss and Brier with positive
  95% confidence lower bounds. Its 1,345-market holdout achieved log
  loss/Brier 0.65249/0.23008 versus 0.69341/0.25013 for the frozen training prior;
  calibration slope was 0.993. The formal direction gate remains No-Go because
  the holdout has fewer than 2,500 markets, the available 76-day history cannot
  run the full 90/21/14/28-day protocol, and no causally available Polymarket
  implied-probability baseline exists for this period. Gamma coverage is also
  one market short of the requested 7,296. Confidence-band sample and
  calibration checks passed. The clean artifact was generated at revision
  `87e14c28b84632e5fdeb3bf349f6685a94b8b1eb` under
  `output/btc_short_horizon/research/opening-proxy-protocol-v2-clean-20260428-20260713/`.
  It is acceptable for research Shadow only; the failed direction gate blocks
  Canary and Live promotion.
- Current sparse Polymarket price-history proxy: frozen development thresholds,
  a 1c entry-price buffer and a 15-second maximum price age produced 945 holdout
  entries at 2.53c/share (95% CI 0.70c to 4.39c). Adding another 1c cost and
  reapplying the frozen rules leaves 782 entries at 2.65c/share (95% CI 0.07c
  to 5.23c). The 3--30 second regime is positive; 35--90 seconds is negative and
  95--180 seconds crosses zero. This clean rerun includes corrected persistence,
  non-tradeable-price, family-wise selection and cache-schema rules. One-minute
  price history is still not BBO, queue, fill, fee, rebate, or latency evidence,
  so it cannot authorize maker deployment.
- Legacy pre-open/revalidation strategy, historical script, runner, generic
  feature state, and their tests were removed. The only BTC 15m strategy path
  is Opening Mispricing with 36 decisions at `t0+5/10/.../180s`, two
  consecutive signals, per-regime research reporting, and a conservative
  10c maker minimum edge plus a separate 1c safety buffer.
- PMXT native extension has now been built locally and its focused native suite
  passed (21 tests). A bounded v2 audit found zero rows for all four
  Gamma-verified BTC 15m conditions in the downloaded 2026-07-13T04 raw hour.
  Empty PMXT coverage is therefore a hard data failure, not a replay result.
- Forward raw collection writes canonical JSON and readers fail closed on
  malformed payloads. Version v5 made closed Binance Spot `kline_1s` the
  lightweight default. Version v6 pre-subscribed the next CLOB pair. Version
  v7 added the collector-session, immutable JSON, atomic multi-token CLOB, and
  bounded-buffer contracts. Version v8 persists every continuity loss as an
  immediate causal raw event, invalidates stale books offline even when no
  later message arrives, separates the official Spot/Futures depth bridge
  rules, and forces OKX/Polymarket resubscription after invalid state. Ingress
  across simultaneous feeds is admission-atomic under backpressure. The shared
  shutdown deadline covers producer quiescence and durable flush, while the CLI
  event loop also exits boundedly if a task resists cancellation. Readers are
  manifest-first: they verify hashes, identities, row/time statistics, reject
  orphan or missing parts and mixed ingest versions, and replay the persisted
  Polymarket timestamp tolerance. Every v8+ row also carries a session-monotonic
  admission sequence, so identical timestamps preserve live order across parts;
  source events and their gap boundaries reserve capacity and commit together.
  Follow catalogs preserve their first pre-open `collected_at` timestamp and
  reject same-path metadata conflicts instead of overwriting provenance.
  Current v9 adds a durable prepared/committed session inventory, process-level
  single-writer lease, interrupted-session recovery, and content-addressed
  object backup with full-download verification. Readers now detect deletion of
  both a part and its adjacent manifest; archive is dry-run by default and
  remote-only evidence must be restored before research.
- The historical v6 collector completed a fresh 180-second audit on
  `btc-updown-15m-1784214900`. The dual-token Shadow reconstructed all 36
  decisions with zero submitted orders. The enhanced multi-venue audit observed
  all 36 decisions; 32 were quality-eligible and four correctly failed closed
  because Binance Spot trade/BBO evidence was older than one second. The
  enhanced audit explicitly opted into the current Binance Futures `/public`
  route for trade and Book Ticker. The deploy default remains Spot closed
  `kline_1s` only, and the runtime reports the look-ahead market as active after
  handoff. This run does not validate the v9 storage/session boundary; fresh v9
  evidence is required before promotion.
- The runtime model path uses the same feature schema as training and bootstraps
  at most four Binance REST pages. A full three-minute shadow needs 3,782 closed
  one-second bars instead of downloading another historical archive.
- The resolved-label dataset seam now enforces whole-market quality exclusion,
  a five-second training cadence, one total sample weight per market, one rule
  epoch per dataset, and the existing group-safe `DirectionDataset` pipeline.

See [BTC Short-Horizon Architecture](btc-short-horizon-architecture.md) for
the input/output contract and operational commands.

## Pre-VPS Final Audit (2026-07-22)

- Local implementation is complete for the first deployment scope: current
  forward collection, post-window causal Shadow, read-only dashboard, target
  preflight, runtime control, immutable evidence, backup and isolated restore.
- Model evidence is internally reproducible and protocol v2 clean, but the
  formal direction gate remains No-Go: 1,345 holdout markets are below 2,500,
  the full 90/21/14/28 walk-forward protocol cannot fit the available history,
  the causal Polymarket implied-probability baseline is missing, and Gamma is
  one market short.
- Price evidence can prioritize 3--30 second Shadow research, but the maker gate
  remains No-Go until fresh synchronized L2/TradeTick replay passes pessimistic
  queue, P99 insert/cancel latency, same-timestamp ordering, fee and capacity
  stress. No production decision runner connects model output to live placement.
- Existing local raw roots contain historical v2--v8 epochs and an open legacy
  session. They remain immutable research provenance and must not be copied to
  the first VPS or repaired in place. The VPS starts from empty data/output
  roots and collects only current v9 evidence.
- Target-only evidence cannot be manufactured locally: actual-IP geoblock and
  legal eligibility, NTP and endpoint latency, Linux Docker build, fresh v9
  collection, object-store full verification/restore and long-running recovery
  must all pass on the purchased host.
- The approved first VPS scope is therefore collector, optional post-window
  Shadow and loopback dashboard only. Paper, Canary and real orders remain
  disabled until their independent promotion gates pass.

## Roadmap

- [x] multi-market support within strategies [PR#30](https://github.com/evan-kolberg/prediction-market-backtesting/pull/30), [PR#53](https://github.com/evan-kolberg/prediction-market-backtesting/pull/53), [PR#54](https://github.com/evan-kolberg/prediction-market-backtesting/pull/54), [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64)
- [x] better position sizing [PR#8](https://github.com/evan-kolberg/prediction-market-backtesting/pull/8)
- [x] fee modeling [PR#4](https://github.com/ben-gramling/nautilus_pm/pull/4), [PR#42](https://github.com/evan-kolberg/prediction-market-backtesting/pull/42)
- [ ] fuller slippage modeling for maker realism still needs L3 data [PR#6](https://github.com/ben-gramling/nautilus_pm/pull/6), [PR#9](https://github.com/evan-kolberg/prediction-market-backtesting/pull/9), [PR#50](https://github.com/evan-kolberg/prediction-market-backtesting/pull/50)
- [x] Polymarket L2 order-book backtests [PR#10](https://github.com/evan-kolberg/prediction-market-backtesting/pull/10), [PR#45](https://github.com/evan-kolberg/prediction-market-backtesting/pull/45), [PR#57](https://github.com/evan-kolberg/prediction-market-backtesting/pull/57)
- [x] PMXT raw archive workflow: local raw mirrors, direct archive fallback,
  filtered replay cache, and incremental downloader reruns [PR#17](https://github.com/evan-kolberg/prediction-market-backtesting/pull/17), [PR#22](https://github.com/evan-kolberg/prediction-market-backtesting/pull/22), [PR#40](https://github.com/evan-kolberg/prediction-market-backtesting/pull/40), [PR#47](https://github.com/evan-kolberg/prediction-market-backtesting/pull/47), [PR#56](https://github.com/evan-kolberg/prediction-market-backtesting/pull/56), [PR#61](https://github.com/evan-kolberg/prediction-market-backtesting/pull/61), [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64)
- [x] Rust-native staged data loading for PMXT and Telonex with unified
  cache/local/archive/API progress output [PR#132](https://github.com/evan-kolberg/prediction-market-backtesting/pull/132)
- [ ] Kalshi L2 order-book backtests need L2 historical book data we do not have
  yet. The next exchange expansion targets are
  [Limitless.exchange](https://limitless.exchange) and
  [Opinion.trade](https://opinion.trade) after the Polymarket loading path
  stays stable.
- [x] richer charting and honest multi-run HTML/report outputs [PR#5](https://github.com/ben-gramling/nautilus_pm/pull/5), [PR#52](https://github.com/evan-kolberg/prediction-market-backtesting/pull/52), [PR#68](https://github.com/evan-kolberg/prediction-market-backtesting/pull/68), [PR#74](https://github.com/evan-kolberg/prediction-market-backtesting/pull/74), [PR#80](https://github.com/evan-kolberg/prediction-market-backtesting/pull/80), [PR#83](https://github.com/evan-kolberg/prediction-market-backtesting/pull/83)
- [x] manifest-based runner architecture and repo-level optimizer surface [PR#67](https://github.com/evan-kolberg/prediction-market-backtesting/pull/67)
- [x] repo-level runner/report contracts, docs validation, and launcher/docs hardening [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64), [PR#65](https://github.com/evan-kolberg/prediction-market-backtesting/pull/65), [PR#68](https://github.com/evan-kolberg/prediction-market-backtesting/pull/68), [PR#69](https://github.com/evan-kolberg/prediction-market-backtesting/pull/69), [PR#71](https://github.com/evan-kolberg/prediction-market-backtesting/pull/71), [PR#76](https://github.com/evan-kolberg/prediction-market-backtesting/pull/76), [PR#77](https://github.com/evan-kolberg/prediction-market-backtesting/pull/77), [PR#78](https://github.com/evan-kolberg/prediction-market-backtesting/pull/78), [PR#80](https://github.com/evan-kolberg/prediction-market-backtesting/pull/80), [PR#81](https://github.com/evan-kolberg/prediction-market-backtesting/pull/81)

## Known Issues

- Runtime and raw-data backup tools are implemented, but a real target bucket
  still needs versioning/Object Lock policy, least-privilege credentials and an
  off-VPS copy of snapshot IDs. One successful target-host full backup and
  isolated restore drill remains deployment evidence; local deterministic tests
  cannot prove the user's future cloud account or storage policy.
- The operations supervisor is intentionally not exposed as a real-order
  Compose service. There is no production decision runner connecting the
  opening model to live placement, and the direction/maker evidence gates are
  still No-Go. First deployment remains collector, optional post-window Shadow
  and read-only dashboard only.
- A single fresh 36-decision Shadow window proves runtime compatibility, not
  statistical stability. Promotion still requires accumulated forward windows,
  target holdout counts, synchronized executable L2/TradeTick evidence, and the
  complete queue-enabled P99 execution robustness gate.
- The forward raw parts written on 2026-07-13 contain malformed `payload_json`
  because their JSON field delimiter was incorrect. They must not be used for
  feature generation, market evidence, model fitting, or replay. The reader
  fails closed on these parts, and later valid collection is a separate epoch.
- CLOB data collected before the `btc-short-horizon-v2` cutover on 2026-07-14
  is excluded from opening-market research because small source-timestamp
  jitter produced false epoch gaps. The raw parts are retained, not migrated;
  only a window passing the current versioned coverage audit can establish
  synchronized evidence.
- Forward data collected under `btc-short-horizon-v2` is excluded from joined
  feature research because Binance trade, depth, and Book Ticker events shared
  one ordering validator. Normal cross-stream interleaving therefore created
  false epoch transitions. Version v3 scopes epochs per logical stream; v2 raw
  remains immutable and is not repaired in place.
- Forward data collected under `btc-short-horizon-v3` is excluded from joined
  feature research because Binance moved Futures aggregate trades to
  `/market/stream` while depth and Book Ticker use `/public/stream`. The old
  single endpoint produced no perpetual `aggTrade` rows. Version v4 uses both
  official connections; v3 raw remains immutable.
- Forward data through `btc-short-horizon-v6` predates the explicit
  collector-session column and hard queued/in-flight buffer contract. It
  remains immutable provenance and can be audited only with its matching
  schema; current v9 readers select one ingest version and apply its matching
  row contract without mixing epochs.
- Kalshi is not currently exposed as a public runnable backtest path. The repo
  still contains Kalshi instrument, trade/candlestick loader, fee-model, and
  research helper components, but the built-in replay adapter registry only
  exposes Polymarket PMXT and Telonex book replay adapters. Users should treat
  Kalshi as experimental adapter plumbing until real Kalshi L2 historical book
  data, a Kalshi replay adapter, and a public runner are added.

## Recently Fixed

- [x] BTC forward storage v9 now records every immutable part in a transactional
  session inventory, reconciles durable prepared pairs after interruption,
  excludes concurrent writers with one storage lease, detects adjacent
  part/manifest deletion, and provides content-addressed rclone/local backup,
  full-download verification receipts, fail-closed local archival, and atomic
  restore across filesystems. Operational rules are documented in
  [BTC 前瞻数据持久性与灾难恢复](btc-data-durability-research.md).

- [x] v4 staged replay loading now prepares Polymarket metadata first, loads all
  book data second, and loads execution trade ticks last. PMXT filtered-cache
  misses are grouped by raw archive hour, Telonex API/file work has separate
  worker caps, and warm 100-market Telonex loads avoid redundant full-week book
  sorting/count scans [PR#132](https://github.com/evan-kolberg/prediction-market-backtesting/pull/132).
- [x] PR#119 upgrades public Polymarket runners to L2-native `BookReplay`
  semantics with `OrderBookDeltas` plus real `TradeTick` execution evidence,
  keeps Nautilus on `BookType.L2_MBP` with `trade_execution=True`, removes
  standalone quote/trade replay framing, speeds Telonex API-day cache reads with
  `.fast.parquet` sidecars, prunes local Telonex mirror scans through the DuckDB
  manifest, bounds the Telonex downloader writer queue, periodically closes
  open Telonex part writers, and makes PMXT raw downloads incremental by
  skipping existing local files
  [PR#119](https://github.com/evan-kolberg/prediction-market-backtesting/pull/119)
- [x] PR#119 now also materializes Telonex `OrderBookDeltas` under
  `book-deltas-v2`, prints richer terminal statistics from per-market result
  payloads plus portfolio-level Nautilus `BacktestResult` stats, and keeps
  public Python runner inputs inline inside `run()` instead of module-level
  constants [PR#119](https://github.com/evan-kolberg/prediction-market-backtesting/pull/119)
- [x] prediction-market backtests now use settlement-aware result assembly,
  finite synthetic taker depth, lower default touched-limit fill probability,
  fill-time Kalshi fee waivers, zero-fee Polymarket maker modeling, safer
  trade-tick liquidity caps, and corrected Telonex outcome selection; docs,
  UML, tests, and public runner configs were updated alongside the execution
  honesty fixes [PR#118](https://github.com/evan-kolberg/prediction-market-backtesting/pull/118)
- [x] Telonex source loading now falls through to the next source when one
  fails (so a missing `local:` mirror cleanly hands off to `api:`) instead of
  aborting the whole replay [PR#105](https://github.com/evan-kolberg/prediction-market-backtesting/pull/105)
- [x] v3 adds a Telonex joint-portfolio runner, local Telonex daily-Parquet
  downloader, Hive-partitioned Parquet output with a DuckDB resume manifest,
  and daily-file timing output for Telonex `local:` / `api:` sources [PR#104](https://github.com/evan-kolberg/prediction-market-backtesting/pull/104)
- [x] v3 removes the active PMXT relay path, relay badges, and relay service
  package. PMXT runners now use local raw files plus direct `r2v2.pmxt.dev` /
  `r2.pmxt.dev` archive fallback, and Telonex is available as a separate
  Polymarket vendor [PR#103](https://github.com/evan-kolberg/prediction-market-backtesting/pull/103)
- [x] relay schema-bootstrap now commits its `UPDATE` so a second writer can
  take the WAL lock instead of deadlocking on first start [PR#102](https://github.com/evan-kolberg/prediction-market-backtesting/pull/102)
- [x] PMXT relay latest-hour badge now prints the mirrored filename
  (`polymarket_orderbook_YYYY-MM-DDTHH`) instead of only the naked hour, the
  mirror worker validates the local file size against upstream before reusing
  an existing raw file (so stale placeholder downloads can no longer survive
  as `ready`), `count_raw_dump_files` excludes undersized parquet files from
  the public coverage denominator, and startup adoption purges orphan raw
  files under the nonempty byte threshold that aren't tracked as ready in the
  index DB [PR#101](https://github.com/evan-kolberg/prediction-market-backtesting/pull/101)
- [x] PMXT relay archive-coverage redesign exposes per-source priority
  metrics, mirrored-vs-archive accounting, and clearer empty-hour handling
  [PR#100](https://github.com/evan-kolberg/prediction-market-backtesting/pull/100), [PR#95](https://github.com/evan-kolberg/prediction-market-backtesting/pull/95), [PR#94](https://github.com/evan-kolberg/prediction-market-backtesting/pull/94), [PR#93](https://github.com/evan-kolberg/prediction-market-backtesting/pull/93), [PR#90](https://github.com/evan-kolberg/prediction-market-backtesting/pull/90), [PR#89](https://github.com/evan-kolberg/prediction-market-backtesting/pull/89)
- [x] PMXT runners support full per-entry source ordering and split raw-archive
  sources, so `MarketDataConfig.sources` can interleave `local:` and `archive:`
  entries in any order the runner needs
  [PR#98](https://github.com/evan-kolberg/prediction-market-backtesting/pull/98), [PR#92](https://github.com/evan-kolberg/prediction-market-backtesting/pull/92)
- [x] public runner validation harness covers every flat runner under
  `backtests/` to keep direct script paths and the menu deterministic
  [PR#91](https://github.com/evan-kolberg/prediction-market-backtesting/pull/91)
- [x] ruff-driven cleanups across relay, adapters, and plotting; docs now
  include an embedded-HTML example, acknowledgements, and a fixed index
  anchor URL [PR#88](https://github.com/evan-kolberg/prediction-market-backtesting/pull/88), [PR#87](https://github.com/evan-kolberg/prediction-market-backtesting/pull/87)
- [x] multi-market runners now default to `EMIT_HTML=False` and the artifact
  pipeline downsamples price points to 5 000 before building dense equity
  curves, cutting wall time from ~320s to ~26s on an 8-market basket
  [PR#84](https://github.com/evan-kolberg/prediction-market-backtesting/pull/84)
- [x] HTML chart files are now downsampled to ~5 000 points before Bokeh
  serialization, reducing a 446 K-bar chart from 31 MB to under 1 MB;
  redundant ColumnDataSource columns and intermediate DataFrames were also
  deduplicated, and new regression tests enforce that 100 K-bar backtests
  produce HTML under 5 MB
  [PR#83](https://github.com/evan-kolberg/prediction-market-backtesting/pull/83)
- [x] aggregate summary report builders now skip serializing unused per-market
  price series, fill events, and overlay curves when the selected summary
  panels do not render them
  [PR#83](https://github.com/evan-kolberg/prediction-market-backtesting/pull/83)
- [x] docs deploy workflow now triggers on the active `v2` branch instead of the
  removed `main` branch, and the GitHub Pages environment allows `v2` deploys
  [PR#81](https://github.com/evan-kolberg/prediction-market-backtesting/pull/81)
- [x] plotting docs rewritten around a clearer detail-vs-summary mental model,
  stale `blob/main` GitHub links fixed across all docs, and a regression test
  guards against stale branch links returning
  [PR#80](https://github.com/evan-kolberg/prediction-market-backtesting/pull/80)
- [x] backtest runner examples refreshed to match current runner contracts
  [PR#78](https://github.com/evan-kolberg/prediction-market-backtesting/pull/78)
- [x] legacy Kalshi trade-tick runners pinned `end_time` to a known-good close
  window so direct script paths and the repo pytest gate stay deterministic,
  the docs/examples now point at current runnable entrypoints, and shared
  startup reporting no longer understates factory-backed runs [PR#76](https://github.com/evan-kolberg/prediction-market-backtesting/pull/76), [PR#77](https://github.com/evan-kolberg/prediction-market-backtesting/pull/77)
- [x] public runners now use typed replay specs plus one experiment builder,
  adapter-owned replay loading, and a repo-layer optimizer surface instead of
  the older shared `SIMS` / `BACKTEST` contract
  [PR#67](https://github.com/evan-kolberg/prediction-market-backtesting/pull/67)
- [x] plotting now scales as one detailed HTML per loaded sim plus one aggregate summary HTML per basket, the repo no longer relies on concatenated mega-pages, and the prediction-market runner internals are split into clearer execution, artifact, reporting, and data-source seams [Issue #73](https://github.com/evan-kolberg/prediction-market-backtesting/issues/73), [PR#74](https://github.com/evan-kolberg/prediction-market-backtesting/pull/74)
- [x] direct script HTML outputs now resolve from the repo root, fixed-basket multi-market runners emit aggregate reports again, and the repo runner/report surface stays explicit about per-sim detail charts versus aggregate multi-market reports [PR#68](https://github.com/evan-kolberg/prediction-market-backtesting/pull/68)
- [x] setup/backtest/fetch-source docs now match the unified `main.py` launcher
  and current PMXT terminal/reporting output, and the orphaned `_trade_tick_ui.py`
  helper is gone [PR#69](https://github.com/evan-kolberg/prediction-market-backtesting/pull/69)
- [x] root README scope and agent guidance now keep detailed operational docs out of the README body and in `docs/` instead [PR#70](https://github.com/evan-kolberg/prediction-market-backtesting/pull/70), [PR#71](https://github.com/evan-kolberg/prediction-market-backtesting/pull/71)
- [x] PMXT L2 replay now orders book updates deterministically so longer
  windows do not lose book state [PR#26](https://github.com/evan-kolberg/prediction-market-backtesting/pull/26)
- [x] relay misses fall back client-side to `r2.pmxt.dev`, trusted proxy clients keep distinct rate-limit buckets, and stale buckets are pruned instead of accumulating forever [PR#22](https://github.com/evan-kolberg/prediction-market-backtesting/pull/22), [PR#25](https://github.com/evan-kolberg/prediction-market-backtesting/pull/25), [PR#42](https://github.com/evan-kolberg/prediction-market-backtesting/pull/42)
- [x] relay observability and survivability improved with progress badges, ClickHouse ingest, retry handling around transient lock contention, mirror pruning, and incremental raw-hour adoption [PR#34](https://github.com/evan-kolberg/prediction-market-backtesting/pull/34), [PR#35](https://github.com/evan-kolberg/prediction-market-backtesting/pull/35), [PR#36](https://github.com/evan-kolberg/prediction-market-backtesting/pull/36), [PR#40](https://github.com/evan-kolberg/prediction-market-backtesting/pull/40), [PR#56](https://github.com/evan-kolberg/prediction-market-backtesting/pull/56), [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64)
- [x] PMXT public workflows are now raw-first: local raw mirrors, archive fallback, mirror-only relay behavior, and downloader output all line up across runners and docs [PR#45](https://github.com/evan-kolberg/prediction-market-backtesting/pull/45), [PR#47](https://github.com/evan-kolberg/prediction-market-backtesting/pull/47), [PR#57](https://github.com/evan-kolberg/prediction-market-backtesting/pull/57), [PR#60](https://github.com/evan-kolberg/prediction-market-backtesting/pull/60), [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64)
- [x] public runners now model queue position and static latency where this repo uses them, reducing dependence on zero-latency assumptions [PR#50](https://github.com/evan-kolberg/prediction-market-backtesting/pull/50), [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64)
- [x] replay/report outputs now distinguish requested windows from loaded windows and keep honesty-focused defaults visible in normal runs [PR#52](https://github.com/evan-kolberg/prediction-market-backtesting/pull/52), [PR#56](https://github.com/evan-kolberg/prediction-market-backtesting/pull/56), [PR#63](https://github.com/evan-kolberg/prediction-market-backtesting/pull/63), [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64)
- [x] the interactive menu again shows full runner contents, direct runner imports work in both script and package modes, and the root `_script_helpers.py` shim is gone [PR#53](https://github.com/evan-kolberg/prediction-market-backtesting/pull/53), [PR#62](https://github.com/evan-kolberg/prediction-market-backtesting/pull/62), [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64)
- [x] PMXT timing output, source labels, and raw-hour progress reporting are clearer and better aligned with the actual runner behavior [PR#55](https://github.com/evan-kolberg/prediction-market-backtesting/pull/55), [PR#59](https://github.com/evan-kolberg/prediction-market-backtesting/pull/59), [PR#60](https://github.com/evan-kolberg/prediction-market-backtesting/pull/60)
- [x] repo CI and docs validation now match the documented local gate, and PR docs builds validate without trying to deploy Pages [PR#58](https://github.com/evan-kolberg/prediction-market-backtesting/pull/58), [PR#64](https://github.com/evan-kolberg/prediction-market-backtesting/pull/64)
