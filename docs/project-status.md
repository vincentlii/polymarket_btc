# Project Status

## 2026-08-24 1.0 / 2.0 no-order forward comparison

- The early-only runtime now records frozen Legacy 1.0 and market-relative 2.0
  as separate forward-OOS probability streams. An observation-only 2.0
  artifact remains execution-disabled and cannot create simulated or real
  orders.
- Each model may create at most one no-order counterfactual FAK opportunity per
  market. It uses the same admitted dual-token book, visible ask ladder, fee
  snapshot, five-share cap, price band, stage edge floor and slippage stress.
- After settlement the dashboard reports each model's direction mix, accuracy,
  Brier/log loss, hypothetical wins, PnL and EV. Counterfactual results never
  enter order/fill counts, account equity or realized PnL.

## 2026-08-19 BTC 15m profitability research branch

- Created independent branch `codex/btc-v10-profitability-upgrade` from stable
  revision `f8d42a5d0c044b5bca4c2e08ff326887e4910a3f`; the deployed VPS branch and
  the separate pre-open Alpha Master research line are unchanged.
- Current Paper configuration is early-only. Legacy 1x5, later stages and
  repeated 2x/3x confirmations are disabled; their historical ledgers remain
  readable through immutable SQLite snapshot publication. The v2 1x5 route
  remains the primary observer, but a model with a research `No-Go` gate records
  evaluations only and cannot create simulated orders.
- Added common `core`, `flow` and `enriched` feature profiles. Core uses exact
  causal dual-token BBO plus the Legacy probability anchor; larger profiles
  require their actual venue sources and fail closed when evidence is missing.
- Added BTC-only PMXT v2 filtering, causal BBO reconstruction, frozen anchored
  dataset IO, three-stage development OOF and Paper-only artifact tooling.
  Imported historical rows are development-only; the fresh sealed-forward
  boundary is 2026-08-20 UTC.
- The verified PMXT overlap is concentrated in 2026-07-01 through 2026-07-08.
  The explicitly non-promotable quick-development screen therefore uses a
  3-day train, 1-day calibration, 1-day test/step, 4h15m embargo and a 1-day
  untouched tail. It is for candidate screening; the formal
  90/21/14/28-day protocol is not weakened.
- Development selection now evaluates five Logistic and 12 controlled
  LightGBM variants per stage. Every research metric follows the 1x5 contract:
  only the earliest executable opportunity per market can trade, while model
  ranking uses cost-after-execution return per eligible market. Probability
  uncertainty is market-first and tie-safe, so repeated snapshots cannot
  create false confidence.
- Historical execution evidence applies the same one-second dual-token BBO
  freshness gate and configured per-stage net-edge thresholds as Paper. A
  missing later snapshot removes that decision, not an otherwise causal early
  market. Stage fitting and reported point metrics rebalance within each market,
  so uneven snapshot coverage cannot create a direction or calibration bias.
- Model selection distinguishes a statistically promotable champion from a
  Paper-only development leader. The latter still requires non-negative
  log-loss/Brier improvement, positive cost-after-execution return, balanced
  Up/Down opportunities and a valid leaf audit; it never opens the sealed
  holdout or becomes Canary/live eligible.
- The first real overlap run is complete: 689 of 704 PMXT markets pass the
  one-second causal BBO gate and produce 24,220 snapshots. Early Core Logistic
  improves point log loss by 0.00121 and Brier by 0.00297 versus `q_pm`, but 65
  point-probability opportunities earn only +0.76 cents/share and their
  market-block 95% lower bound is -9.39 cents. Only six survive model
  uncertainty and those lose money. The 35--90s result has just 11 point
  opportunities and no independent day/week support; 95--180s degrades
  probability accuracy and selects Down 71 of 74 times. No stage passes.
- The bounded search of five Logistic and 12 LightGBM variants per stage also
  yields no Paper leader. A separate `trade_flow` ablation adds existing causal
  Binance Spot 5/15-second return, volatility and taker-flow features; it
  worsens early point net EV from +0.76 to -0.21 cents and does not rescue the
  later stages. That family is frozen instead of promoted.
- The current conclusion is an explicit No-Go, not an implicit promise that
  more tuning will create profit. Collection should continue to add independent
  dates; this development slice must not produce an execution-enabled artifact.
  An explicitly observation-only artifact may emit paired diagnostics, while
  runtime execution remains fail-closed unless a future artifact passes the
  Paper experiment gate or the full sealed promotion contract.
- Full raw-derived Maker/exit L2 replay and Canary remain deliberately deferred
  until the user-approved later phase. No model from this branch has been
  committed, deployed, or promoted.

## 2026-08-18 Collector memory and dynamic-window correction

- Production RSS profiling identified the Collector's 2.3 GiB anonymous-memory
  growth as a Research Paper retention bug, not the durable write buffer or
  PyArrow file cache. The Paper runtime kept up to 20,000 full raw events for
  every stable Binance/OKX/Chainlink instrument even though activation replay
  uses only future Polymarket token evidence.
- Public-feed payloads are now applied and released immediately. Only registered,
  not-yet-activated CLOB tokens enter a count-and-byte-bounded pre-activation
  cache; activation releases them and overflow fails Paper closed. Status now
  exposes the cache count/bytes and their configured ceilings.
- A market's first registered CLOB capture window is now immutable. Disk-policy
  recovery from core `t0+180s` to extended `t0+900s` applies only to a new future
  market and can no longer crash the Collector by re-registering an existing
  token pair with a different end time.
- `max_queue_delay_ms` remains the historical wait to the configured 60-second
  durable flush, not exchange/network latency. It is not used as a trading-path
  health signal.

## 2026-08-18 readiness worker memory and health correction

- Real-candidate verification disproved the earlier in-process external-sort
  design: despite small Arrow batches, DuckDB/Arrow/file-cache lifetime still
  drove the 768 MiB cgroup to its limit and restarted the worker. Production
  readiness now validates immutable raw capture only (inventory, hashes,
  six-source stream manifests, gaps, rule lineage, and bounded CLOB lifecycle).
  It no longer reconstructs model features on the VPS.
- A second real-candidate run proved that per-candidate full-file re-hashing and
  CLOB payload scanning could still fill the cgroup through file cache and an
  unbounded admission-sequence set. The final online path therefore reads only
  the sealed session inventory and manifests. Full payload/hash audits run once
  at archive restore and before local training instead of being repeated for
  every overlapping 15-minute market.
- Full 36-tick feature materialization remains available through the offline
  research pipeline and must emit a separate eligibility receipt before model
  training or promotion. This is a responsibility split, not a reduced data
  contract: the collector continues storing all six raw sources and the full
  Up/Down CLOB lifecycle.

- The superseded one-source-at-a-time and external-sort attempts remain covered
  as offline materializer behavior, but are no longer on the production
  readiness path. Real VPS validation showed that optimizing batch size alone
  could not provide a hard process-memory bound.
- The readiness status includes its worker PID, and its container healthcheck
  requires both a fresh healthy status and a live non-zombie process. A stale
  status file can no longer make an OOM-killed/restarted worker appear healthy.
- This is an execution/memory-safety correction only: feature ordering,
  availability cutoffs, gap handling, source coverage, and No-Go behavior stay
  unchanged. No model artifact or trading threshold is promoted by this fix.

## 2026-08-14 readiness coverage-contract correction

- Readiness now distinguishes durable storage-session continuity from
  event-driven message activity. Normal 60--70ms writer rotation and a quiet
  CLOB/OKX interval no longer masquerade as data loss; explicit persisted gaps
  remain fail-closed.
- Each source has its causal window: BTC/reference lookback, current-market
  CLOB pre-open through `t0+180s`, and full-lifecycle CLOB exit evidence.
- Dashboard readiness aggregates only the newest protocol hash. Prior immutable
  failure receipts remain available for audit but no longer define current
  health after a contract upgrade.

## 2026-08-13 v10 multisource readiness activation

- Execution epoch `paper-v10-multisource-readiness` isolates the new
  rule/model/config identity from v9 while retaining all v9 ledgers as
  read-only history.
- A candidate with a collector-recorded coverage gap now produces an immutable
  `ready=false` receipt once. It no longer enters an endless expensive audit
  loop or makes the readiness worker itself appear crashed.
- Collector sessions reject late prepare/commit callbacks after either terminal
  state, preventing shutdown races from producing contradictory terminal
  timestamps.

## 2026-08-13 v9 full-lifecycle Research Paper upgrade

- Execution epoch `paper-v9-full-lifecycle-research` preserves v8 and all older
  epochs as read-only history. It keeps exactly the Legacy and 2.0 one-signal
  FAK variants and expands causal CLOB capture through market end.
- Qualified-opportunity Edge is now distinct from rejected-candidate Edge;
  tail quarantine, evidence target and the five-share execution cap are explicit.
- Forward ingest v16 adds the lightweight Binance/OKX feeds, rule-contract
  fingerprint and disk-pressure observability needed for the 28-day study.

## 2026-08-13 Paper dashboard and ledger correctness (v8 history)

- Research Paper now reports a distinct `paper` lifecycle stage. The dashboard
  renders a flat current-epoch equity line even before the first settlement and
  states that no settled trade exists instead of hiding the chart.
- Execution epoch `paper-v8-dashboard-ledger-identity` preserves v7 as
  read-only history and binds new ledgers to both model hashes, rule epochs,
  variant configuration and execution stress assumptions.
- The current primary curve remains isolated to the primary variant and current
  execution epoch. Legacy/control and older-epoch PnL are not spliced into it.
- Dashboard health now evaluates only services declared active by the deployed
  compose profile; stale status files from disabled services remain historical
  evidence but no longer create a false runtime outage.
- Market-relative 2.0 orders and lifecycle lineage now record 2.0 as the
  decision model and Legacy as its base dependency. Rejected opportunities are
  no longer counted as submitted orders.
- Paper ledger restore rejects misplaced variants, duplicate placement IDs and
  invalid fill/settlement invariants. Dashboard equity payloads retain exact
  endpoints while bounding long-running curve size to 2,000 points.
- VPS preflight rejects abbreviated or malformed release revisions. Deployment
  identity must use the exact full 40-character Git SHA in Git, compose, image
  labels and runtime status.

## 2026-08-12 Market-relative 1x5s 2.0 development result

- The point-rule Shadow archive now contains 1,079 markets. Strict causal pair
  freshness, quality and complete-stage filtering retained 1,033 markets and
  34,080 snapshots; the 493 post-boundary TWAP markets were not mixed into the
  point-rule study.
- Legacy and 2.0 probabilities now have separate runtime fields and model
  lineage. Loading a 2.0 artifact cannot overwrite Legacy `p_up`; a missing or
  mismatched 2.0 artifact keeps only the challenger abstained.
- Five Logistic residual candidates all failed against the causal Polymarket
  probability baseline. The bounded 64-candidate residual LightGBM grid found a
  best OOS candidate at log loss/Brier 0.652535/0.230936 versus
  0.654680/0.231826 for Polymarket across 640 markets and seven UTC days.
- The nominal paired interval was positive, but after the pre-registered
  64-comparison correction the Brier improvement interval crossed zero. The
  development gate therefore remains No-Go and the sealed holdout stays closed.
  This blocks Canary/live promotion, but no longer blocks a clearly labelled
  Paper-only challenger.
- Shared stage interactions beat three separately fitted stage models on the
  same OOS markets. No individual stage independently cleared its probability
  gate. Trading-threshold tuning remains blocked because retained Shadow output
  has midpoint probabilities but no executable ask ladders, fee-rounded VWAP,
  latency-stressed fills or outcomes.
- The frozen `market_relative_lightgbm_v1` Paper artifact was fitted from all
  1,033 eligible markets, with the final UTC day isolated for early stopping.
  It stores a single 3-cent probability uncertainty radius and is explicitly
  marked ineligible for runtime promotion. Execution epoch
  `paper-v7-market-relative-lightgbm` makes 2.0 the Paper primary and runs
  Legacy 1x5s simultaneously as the control; v6 remains read-only history.

## 2026-08-12 Chainlink 60-second TWAP rule boundary

- Added fail-closed Gamma rule-metadata classification for the point-price and
  official 60-second TWAP epochs, plus config/artifact/runtime mismatch checks.
- Forward collection now stores point-price and 60-second TWAP RTDS evidence as
  separate streams and separate quality keys. TWAP remains evidence only; there
  is no TWAP-trained or promoted probability model.
- Cross-epoch artifacts reject by default. The configured point-to-TWAP
  exception is a visibly labelled, non-promotable Research Paper transition
  proxy only; Shadow, Canary, and live paths remain strict. Epoch-less legacy
  artifacts fail closed and must not be silently relabelled.

## 2026-08-12 Paper v6 one-signal boundary

- Opened execution epoch `paper-v6-1x5s-v2` without mutating v5 or older
  ledgers, evaluations, frozen rules, or direction evidence.
- The live Paper portfolio now instantiates only `independent_fak_1x5s` as the
  current primary and `independent_fak_1x5s_2_0` as the fail-closed challenger.
  Both use one five-second cadence and isolated ledgers. The challenger records
  why it abstains but cannot become primary before an artifact passes the
  development gate.
- Maker, 2x5, and 3x5 variants remain declared but disabled rollback/history
  code. They do not receive current events or appear in the active dashboard
  projection. Historical order API reads remain read-only and epoch-filterable.
- The local Binance 1-second history is materialized as 302 verified daily
  Parquet partitions from 2025-10-09 through 2026-08-06: 26,092,800 rows and
  zero manifest gaps. Training can read those parts directly without restoring
  the source ZIPs. A full development run keeps the sealed holdout closed unless
  the explicit one-shot command and eligibility checks pass.
- The completed full-profile development run used 27,979 resolved markets and
  1,007,244 five-second snapshots. OOF evidence covered 13,439 effective market
  weights. Logistic C=0.1 remained selected (log loss 0.661348, Brier 0.234655,
  weighted calibration error 0.005196, threshold accuracy 59.55%). Both bounded
  LightGBM candidates had worse log loss/Brier and failed their paired
  replacement rule, so no model was promoted. The 28-day sealed holdout remains
  unopened.
- Direction samples now give equal total weight to early, price-discovery and
  mid-early stages. Calibration compares guarded identity/sigmoid/temperature/
  beta/isotonic candidates and cannot worsen either weighted log loss or Brier.
- The 2.0 challenger validates one causal dual-token collector session, uses a
  market-logit residual probability with uncertainty bounds, walks full ask
  depth, and deducts fee, slippage and latency exactly once. Evaluation storage
  and the dashboard expose Gross edge, each deduction, robust Net edge and the
  leading rejection reasons. It remains non-primary until its development gate
  passes and a compatible artifact is published.

## 2026-08-11 Paper recovery and market-end maker control

- Fixed the live `t0+30.058s` failure by making direction evidence persist the
  exact tolerance-aware stage selected by the execution policy. Research Paper
  now closes and recreates a failed runtime after a bounded delay while raw
  collection and immutable ledgers continue.
- Added an isolated `independent_maker_1x5s_market_end` control for the prior
  v5 epoch. It remains rollback/history code after v6 disabled it.
- Kept the bounded CLOB window at `t0+215s`. The market-end maker control
  defers its remaining fill assessment until resolution, then persists public
  seller-initiated trades and consumes the remaining queue under the same 50%
  volume stress. Missing or ambiguous evidence cannot create a simulated fill.

## 2026-08-10 Direction health and release identity

- Added durable market-level direction evidence for every activated BTC 15m
  market, shared `p_up` prediction and final outcome. Dashboard direction ratios
  use only paired predicted/resolved markets, de-correlate five-second samples at
  market/stage level, and expose coverage start rather than fabricating history.
- Added per-variant Up/Down splits for qualified signals, opportunities, fills,
  accuracy, PnL and calibration diagnostics.
- Corrected a deployment provenance incident: the v5 image and code were
  `47980b78`, while a stale Compose runtime override reported `2d1be589` in status.
  Historical files are retained unchanged. Runtime revision now comes only from
  the immutable image, and preflight rejects a stale Compose release revision.

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
  never rendered as real profit. A credential-free real-time Research Paper
  runtime now consumes only collector-admitted events and publishes an
  explicitly simulated virtual ledger, per-market orders/PnL and equity curve.
  It uses a zero-network `PaperOrderGateway`, P99 insert/cancel latency, full
  visible queue ahead and 50% seller-initiated trade volume. It cannot authorize
  Canary or live orders. An optional post-window Shadow scheduler still writes
  one SHA-versioned causal pass per completed window.
- Data boundary: 15m is the only trading family; 5m is collection-only.
- Forward collection: `btc_forward_collector.py --follow-current` now discovers
  current and next exact Gamma slugs and persists separate rule-hash catalogs.
  In ingest v13, each isolated token pair connects only during `t0-90s` through
  `t0+200s`; every interval requests a fresh official full-book snapshot.
  Binance and Chainlink now remain on the same live transport across market
  boundaries; raw prepared/committed sessions rotate independently after the
  current `t0+200s` CLOB handoff and a complete flush. This removes the previously observed deterministic 8--13s
  `kline_1s` gap every 15 minutes without creating an unbounded inventory.
  Planned CLOB sleep is excluded from required-feed health. This targets the
  measured dominant disk source
  while covering the complete 3-180s decision window, a final order's 15-second
  work period and P99 cancel race.
  Shadow now records a terminal, explicit skip for pre-epoch windows with no
  causal observations instead of retrying them forever. Its container health
  and dashboard freshness thresholds cover the declared 60-second scheduler
  cadence instead of intermittently reporting a 30-second stale failure.
- Evidence boundary: no profitability or deployability claim exists until real
  data passes the documented holdout and the complete queue-enabled P99
  trade-volume/timestamp-order robustness grid.
- Research Paper hardening: a bounded non-blocking admitted-event seam cannot
  backpressure durable collection; overflow fails Paper only. The runtime pins
  public CLOB token/tick/min-size/neg-risk/zero-maker-fee rules, preserves one
  placement cycle across restart, continuously revalues working orders, permits
  cancel-race fills, rejects insufficient virtual balance and settles only from
  explicit Gamma outcomes. Unfilled resolved orders do not distort win rate.
  Active prediction failures now make Paper unhealthy with the exact market,
  timestamp and reason until a later decision succeeds; a cumulative error
  counter can no longer coexist with a misleading healthy status.
  Execution epoch `paper-v3-independent-fak` retains the v2 controls and adds
  independent 2x5s and edge-stable 3x5s FAK challengers. They evaluate both
  outcome-token ask ladders without requiring a maker opportunity. Frozen CLOB
  `itode` rules add the versioned 250 ms venue delay separately from client
  latency; post-submit gaps become invalid execution evidence instead of a
  fabricated cancellation. The existing controls compare 15-second maker, immediate
  one-shot FAK and 5-second maker-to-FAK on the same confirmed opportunity.
  Direct and hybrid FAK use complete ask depth, P99 latency, per-level taker
  fees and frozen safety buffers with no retry. Maker may improve one tick only
  inside a two-tick spread while keeping correct zero queue at the empty level.
  Paired EV counts every resolved opportunity, including rejected/unfilled
  routes as zero, while filled-share EV, time regime and core/tail price buckets
  remain separate. New ledgers are isolated from legacy evidence; per-market
  rules are frozen atomically for deterministic raw replay through the same live
  portfolio. A restart in the same market reloads that validated immutable
  snapshot rather than attempting to replace it with a new observation timestamp;
  malformed, schema, identity, and hash conflicts remain fail-closed. Latency
  transitions now execute before later events can reprice them. The read-only
  dashboard exposes the primary-session funnel, execution
  epoch, segment metrics and paginated full schema-validated order history.
  Causal Binance feature-window shortages are recorded as one recoverable
  prediction-unavailable episode with decision/tail evidence; model or invariant
  failures remain fail-closed rather than being misclassified as stale data.
- Model/research hardening: model inputs and probabilities now reject coercion,
  non-finite values, invalid shapes, and inconsistent lineage. Walk-forward
  tests are non-overlapping and group-safe; LightGBM early stopping, calibration,
  OOF, and holdout remain disjoint. Isotonic counts unique markets. Candidate
  and price-threshold selection use contiguous UTC-day block bootstrap with
  multiple-comparison control. Model artifacts publish atomically and bind
  schema/config/source/code hashes. Price-proxy persistence resets on an
  intervening failed signal, and stress tests discard prices at or above one
  instead of fabricating an executable price.
  A research-only market-relative pipeline builds exact paired control and
  challenger datasets, freezes four Logistic factor ablations, runs grouped OOF
  comparisons with paired block-bootstrap intervals, and keeps sealed holdout
  unavailable until the development gate passes. Insufficient causal CLOB
  coverage returns a structured No-Go and cannot publish or activate a model.
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
  Version v9 introduced a durable prepared/committed session inventory, process-level
  single-writer lease, interrupted-session recovery, and content-addressed
  object backup with full-download verification. Readers now detect deletion of
  both a part and its adjacent manifest; archive is dry-run by default and
  remote-only evidence must be restored before research. Version v13 separates
  public-feed transport lifetime from storage-session lifetime: session
  rotation after CLOB handoff no longer reconnects Binance/Chainlink, while dynamic CLOB windows
  still start from a fresh official snapshot and retire after handoff.
- The historical v6 collector completed a fresh 180-second audit on
  `btc-updown-15m-1784214900`. The dual-token Shadow reconstructed all 36
  decisions with zero submitted orders. The enhanced multi-venue audit observed
  all 36 decisions; 32 were quality-eligible and four correctly failed closed
  because Binance Spot trade/BBO evidence was older than one second. The
  enhanced audit explicitly opted into the current Binance Futures `/public`
  route for trade and Book Ticker. The deploy default remains Spot closed
  `kline_1s` only, and the runtime reports the look-ahead market as active after
  handoff. This historical run does not validate the current v13 collection;
  fresh v13 evidence is required before promotion.
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
  roots and collects only current v13 evidence.
- Target-only evidence cannot be manufactured locally: actual-IP geoblock and
  legal eligibility, NTP and endpoint latency, Linux Docker build, fresh v13
  collection, off-VPS full verification/restore and long-running recovery
  must all pass on the purchased host.
- The approved VPS scope is collector, credential-free Research Paper, optional
  post-window Shadow and loopback dashboard. Canary and real orders remain
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

- The interrupted first Research Paper hotfix deployment left the forward
  services stopped from 2026-07-26 19:18:54 UTC until 2026-07-27 15:29:45 UTC.
  The restart created a new collector session, so durability provenance remains
  honest, but every market overlapping that interval must be excluded from
  continuous forward evidence and cannot be backfilled as live-parity data.
- Runtime and raw-data backup tools are implemented. The selected operational
  path is periodic transfer to a local staging root and content-addressed
  `--transport local` snapshots on a removable drive, with full-download
  verification and isolated restore drill. Until the first verified off-VPS
  snapshot exists, the VPS remains a single point of data loss.
- The operations supervisor is intentionally not exposed as a real-order
  Compose service. There is no production decision runner connecting the
  opening model to live placement, and the direction/maker evidence gates are
  still No-Go. Research Paper is a credential-free heuristic projection, not a
  production decision runner and not Maker-Go evidence.
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
  schema; current readers select one ingest version and apply its matching
  row contract without mixing epochs.
- Kalshi is not currently exposed as a public runnable backtest path. The repo
  still contains Kalshi instrument, trade/candlestick loader, fee-model, and
  research helper components, but the built-in replay adapter registry only
  exposes Polymarket PMXT and Telonex book replay adapters. Users should treat
  Kalshi as experimental adapter plumbing until real Kalshi L2 historical book
  data, a Kalshi replay adapter, and a public runner are added.

## Recently Fixed

- [x] Forward ingest v15 now detects a half-open Chainlink RTDS or Binance
  `kline_1s` socket from accepted business-payload inactivity rather than
  trusting ping/pong health. Each timeout writes exactly one explicit
  `continuity_gap`, advances the affected logical-stream epoch, and reconnects
  with bounded exponential backoff. Low-frequency feeds remain exempt unless
  their protocol guarantees a safe event cadence.
- [x] Research Paper bootstrap now waits for the first collector-admitted closed
  Binance kline, anchors the bounded REST history immediately before that bar,
  and causally replays the events deferred during the fetch. This removes the
  deterministic 3--5 second REST/WebSocket startup gap without weakening gap
  rejection; missing anchors and event-buffer overflow remain fail-closed.
- [x] The first Research Paper VPS start exposed two real-wire mismatches that
  idealized fixtures had missed. Collector-admitted Binance kline decimal
  fields are now parsed from their documented string representation, and CLOB
  V2 fee admission now follows `fd.to=true` plus validated rate/exponent rather
  than rejecting a nonzero `mbf`; `nr=null` is normalized to the documented
  false/default state. Captured payload-shaped regression tests keep all three
  boundaries fail-closed without weakening maker-fee assumptions.
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
# BTC Stage-Aware Taker Challenger v2

## v9 Upgrade Status

- [x] Qualified-opportunity Edge is separated from all evaluated candidates;
  the 0.35 tail quarantine and Paper evidence target are explicit config.
- [x] FAK may select a smaller positive-EV executable quantity, and disabled
  variants are absent from the active baseline while historical ledgers remain.
- [x] Forward ingest v16 freezes `t0-90s` through `t0+900s`, lightweight
  Binance/OKX feeds, rule-contract fingerprints and disk protection thresholds.
- [x] Dual-token v2 features, an independent-market LightGBM leaf gate and
  Hold/Sell-FAK/Pair-lock offline exit seams are implemented without silently
  replacing the deployed v1 artifact.

## v10 Upgrade Status

- [x] The research path materializes causal dual-token, Binance Spot/Perpetual,
  OKX Spot/Swap and cross-venue features and runs cumulative factor ablations.
- [x] Three independent stage models learn a residual around `q_pm`; paired
  market/day/week lower bounds cover log loss, Brier and cost-after-execution
  opportunity EV.
- [x] Development tuning is capped at five Logistic and 64 LightGBM candidates
  per stage, ranked by median fold execution score with an actual Bonferroni
  gate and a unique-market leaf audit.
- [x] Hold, Sell-FAK and opposite-token lock replay support visible-depth
  partial fills, VWAP, official fee math and immutable evidence receipts.
- [ ] The current operator-supplied ladder exit replay is non-promotable; a
  raw-derived full-depth producer with session/part lineage remains required.
- [x] Paper ledgers use append-only SQLite/WAL while preserving legacy epochs as
  read-only migration evidence.
- [x] Rule-contract fingerprints are bound to catalog, raw session/part,
  readiness, artifact and Paper epoch identity.
- [x] Legacy v1 and strict v2 follow-current catalogs use separate immutable
  filenames. v10 never overwrites evidence that a v9 rollback still needs;
  conflicting v2 metadata remains fail-closed.
- [x] Immutable releases require the previously deployed Compose file as an
  explicit rollback artifact, so a failed topology-changing deployment
  restores both the prior image and its exact service set.
- [ ] A market-relative artifact remains blocked until enough independent
  eligible markets, a full-depth exit receipt and sealed OOS evidence satisfy
  the promotion contract.
- [ ] Production deployment and one clean post-release market rotation must be
  recorded before v10 is called operationally complete.

On 2026-08-13, a manual whole-archive hash audit on the 4 GB VPS triggered the
Linux OOM killer and restarted the collector container. The collector recovered
automatically, but this confirmed that archive-wide audits must never run in
the collector process or release path. v10 therefore uses a resource-limited,
incremental per-market readiness service; deep archive audits remain an
explicit low-traffic maintenance operation.

- The v2 implementation is isolated from the VPS `2d1be` release and uses the
  new `paper-v4-stage-aware-taker-v2` epoch.

- The follow-up `paper-v5-stage-integrity` epoch corrects stage-boundary
  confirmation, separates evaluations/qualified signals/opportunities/fills,
  records actual model provenance, stores evaluation diagnostics in SQLite,
  and keeps all older epoch ledgers available through dashboard history.
- The v6 active portfolio contains only two one-signal immediate-FAK variants:
  `independent_fak_1x5s` is the legacy control and
  `independent_fak_1x5s_2_0` is the non-primary challenger. Maker, 2x5, and 3x5 variants remain
  disabled rollback/history code; previous epochs remain readable through the
  dashboard history filter.
- Stage rules, common opportunity logging, optional stage-specific artifacts,
  core price-band protection, and the bounded LightGBM grid are implemented.
- This remains a Research Paper challenger. It is not a real-money Go decision
  and does not establish profitability until the OOS opportunity denominator,
  fill evidence, and confidence gates are met.
