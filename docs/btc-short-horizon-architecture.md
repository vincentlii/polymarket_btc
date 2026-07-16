# BTC Short-Horizon Architecture

## Scope

This project develops only BTC Up/Down markets. The first complete product is
the 15-minute family; 5-minute markets are collected for later research only.
The project reuses the upstream replay engine and implements its own data,
features, models, strategy, reports, and live safety layer.

## Boundaries

- The upstream remote is read-only. The local repository is an independent
  project and must never push to `upstream`.
- Archived BTC strategies and models are not imported or used as research
  inputs.
- The primary path is passive, post-only buying and hold-to-resolution.
- Rebate-free, pessimistic queue, and latency-stressed results are the only
  results eligible for a strategy conclusion.

## Data Flow

```text
raw market/BTC feeds
  -> immutable Parquet + manifests
  -> causal feature snapshots
  -> frozen model artifacts
  -> PMXT L2 + feature auxiliary replay
  -> Nautilus joint backtest
  -> ledger, statistics, and report artifacts
```

## Runtime Stages

1. Local historical research and a provisional edge gate.
2. AWS forward collector and shadow only after the provisional gate.
3. Minimum-size canary remains disabled until an operator explicitly enables it.

## Core Invariants

- A feature may only read events with `available_ts <= decision_ts`.
- Labels have an explicit `label_available_ts`.
- `timestamp` and `timestamp_received` remain distinct in PMXT replay.
- All final strategy decisions are reproducible from a run manifest.

## Implemented Module Boundaries

| Area | Package | Responsibility |
|---|---|---|
| Market contracts | `btc_short_horizon.data` | Validated 15m/5m family slugs, Gamma metadata, labels, immutable raw parts, CLOB/RTDS/Binance/OKX normalizers, and data-quality epochs. |
| Features | `btc_short_horizon.features` | One causal multi-venue feature state shared by batch and streaming opening snapshots. |
| Models | `btc_short_horizon.models` | Logistic/LightGBM fair-probability models, calibration, versioned model artifacts, and causal Opening Mispricing predictions. |
| Research | `btc_short_horizon.research` | Group-safe walk-forward splits, OOF output, a quick Binance feasibility proxy, and machine-evaluated fair-probability/opening-mispricing/maker Go/No-Go gates. |
| Strategy | `btc_short_horizon.strategy` | Pure post-only maker planning, cancellation decisions, and one-placement lifecycle accounting. |
| Replay | `btc_short_horizon.backtest` | A dual-token `BookReplay` with causal `BtcOpeningMispricingSignal` auxiliary data; it reuses the upstream engine rather than creating another simulator. |
| Reporting | `btc_short_horizon.reporting` | Atomic required artifact bundle and probability/fill attribution metrics. |
| Live | `btc_short_horizon.live` | Shadow-default service, append-only WAL, current CLOB V2 gateway, heartbeat cancellation, reconciliation, and canary caps. |

## Configuration

`configs/btc_short_horizon/baseline.toml` is the single baseline configuration.
It contains the 15m primary family, the 5m collection-only family, feature
cadence, maker lifecycle settings, relative data/output roots, PMXT source
priority, and latency/queue scenarios. Relative `local:` paths are resolved
from the configuration file, so runners do not depend on the shell working
directory.

Its `[collection]` section controls raw-data durability and file granularity:
`flush_size` caps in-memory events, `flush_interval_seconds` bounds the time
to the next append-only Parquet write, and `rotation_poll_seconds` controls
only how quickly a newly opened Gamma market is retried.
`opening_handoff_delay_seconds` keeps the old connection alive through the
configured opening interval after the next market starts. CLI overrides are
available for a deliberately bounded operator run; do not lower these defaults
without measuring resulting file counts.

`collection.ingest_version` is a mandatory reader/writer boundary for collector
semantics. A behavior change that can alter causal reconstruction must use a
new value; the market-evidence reader selects exactly that value and never
silently mixes versions from the same token directory. Old raw parts remain
immutable for provenance and are not migrated.

`epoch_id` is scoped to one logical stream, not merely `(source, instrument)`.
For example, Binance trade, diff-depth, and Book Ticker streams have independent
ordering validators. A shared WebSocket disconnect advances each subscribed
stream explicitly; ordinary cross-stream timestamp interleaving is not a gap.
Version `btc-short-horizon-v3` introduced this rule. Version v4 split Binance
Futures `aggTrade` onto `/market/stream` and depth/Book Ticker onto
`/public/stream`, matching the official channel layout. Version v5 made closed
Binance Spot `kline_1s` the lightweight default and left trade/depth,
perpetual, and OKX streams as explicit opt-ins. The current v6 boundary adds
look-ahead CLOB subscription and namespaces epoch IDs by market-window start,
so overlapping handoff connections cannot silently reuse the same epoch.

`paths.raw_data_root` is the root of the project's immutable collector store;
it contains `raw/<source>/<encoded-instrument>/...` parts and manifests. The
manifest retains the original instrument string, while the directory component
uses reversible URL encoding (for example, `btc%2Fusd`). It is distinct from
the first `data_sources` entry, which is a PMXT vendor-archive mirror at
`data/pmxt_raw` for BookReplay. Do not point both settings at the same directory.

The `zero` scenario is diagnostic only. Strategy conclusions must use
`p99_pessimistic`, which keeps queue modelling enabled and explicitly stresses
insert, update, and cancellation latency.

## Data Contracts

Every stored event carries `source_ts`, optional collector receive time,
`available_ts`, source/instrument identity, schema/ingest versions, and an
epoch identifier. Historical event-time-only data is allowed for direction
research but is not evidence about network latency. A duplicate, ordering
regression, or declared continuity gap starts a new epoch rather than silently
mixing state.

PMXT v2 records retain source time as `ts_event` and exporter receive time as
`ts_init`. Its `last_trade_price` rows are converted to `TradeTick` evidence
for L2 matching. L2 data does not recover L3 FIFO order position; queue results
remain a stressed heuristic, not a claim of exact fills.

The forward collector stores accepted, normalized public messages as append-only
Parquet. Each part manifest carries duplicate and gap counts for its partition;
a declared disconnect or ordering regression is attached to the first accepted
post-gap partition and creates a new epoch. Rejected events that have no
accepted event in their partition remain visible in the collector's quality
summary and are never fabricated into a raw-data row.

Part and manifest filenames use a 128-bit content-hash prefix to stay below
Windows path limits for CLOB token IDs. The manifest retains the full SHA-256;
a prefix collision fails explicitly rather than reusing the wrong data. The
collector expands batch envelopes, ignores documented `PONG` replies and empty
subscription control frames, and only persists messages that pass a source
normalizer. In particular, an RTDS history envelope is not treated as a
real-time Chainlink update without its documented message fields.

`payload_json` is canonical, syntactically valid JSON. Any reader must reject
a malformed payload rather than infer or repair missing delimiters. The
2026-07-13 forward raw parts written before this invariant was fixed are kept
only for provenance; they are excluded from research and will not be migrated
or silently mixed with valid collection epochs.

For CLOB events, a small source-clock timestamp regression is conservatively
held at the prior available time, while a regression of one second or more
starts a new epoch. This prevents millisecond source-clock jitter from being
misreported as a transport disconnect without allowing a material ordering
regression to reuse the prior L2 state.

## Forward Collection

The collector is deliberately BTC-only. Follow mode discovers both the current
and next 15m Gamma slugs into separate validated catalogs, then subscribes one
market-channel connection to both Up/Down pairs. It never guesses or reuses a
token ID from a prior window. The old connection remains active through the
first 180 seconds of the next market before rotation; this removes Gamma
discovery and WebSocket setup from the complete `t0+3s` to `t0+180s` decision
path.
The first catalog written for a slug/rule hash is immutable so its pre-open
`collected_at` evidence is preserved; identical rediscovery reuses it and
conflicting metadata fails closed.

The catalog discovery uses Gamma keyset pagination. `--closed all` deliberately
scans both API states; it does not rely on Gamma's mutable default for the
`closed` filter.

```powershell
uv run python scripts/btc_gamma_catalog.py `
  --family 15m `
  --closed open `
  --market-slug <current-15m-slug> `
  --rule-epoch chainlink-btc-usd-v1 `
  --output data/metadata/btc-15m-open.json
```

`--market-slug` uses Gamma's exact server-side filter and is the normal live
path; repeat it for a bounded set of historical windows. Omitting it deliberately
performs the complete, slower family scan.

```powershell
uv run python scripts/btc_forward_collector.py `
  --market-catalog data/metadata/btc-15m-open.json `
  --market-slug <validated-current-slug>
```

`--token-id` remains available only for a deliberately explicit operator run.
It must be repeated for both validated Up and Down IDs; it is not a discovery
mechanism.

For continuous public collection, use the follow mode. It derives the current
and next windows from the configured family, asks Gamma for both exact slugs,
writes rule-hash-specific catalogs, and only then starts a collector for the
four token IDs. If the look-ahead market is not published yet, it collects the
current pair and retries at the normal boundary without adding a handoff delay.

```powershell
uv run python scripts/btc_forward_collector.py `
  --follow-current `
  --family 15m `
  --rule-epoch chainlink-btc-usd-v1 `
  --catalog-directory data/metadata/forward_catalogs
```

Choose and monitor an explicit storage budget before leaving this append-only
process running. It is a public data collector only and never submits orders.

The lightweight default records the Polymarket market channel for both market
pairs, Chainlink BTC/USD RTDS, and closed Binance Spot `btcusdt@kline_1s` bars.
Unclosed bars are rejected. Binance spot trade/depth/Book Ticker and Binance
perpetual streams remain disabled unless repeated through `--binance-stream`,
`--binance-futures-market-stream`, or `--binance-futures-public-stream`.
OKX trades/books remain library-level research opt-ins and are not enabled by
the public VPS runner. The higher-frequency streams must not be enabled on a
bounded VPS until their measured daily storage rate fits an explicit budget.
The swap contract quantity is normalized only after fetching current OKX
instrument metadata; it is never hard-coded. Each enabled Binance
spot/perpetual depth sequence has an independent synchronizer.
For the subscribed BTC token pair, it also persists `new_market` and
`market_resolved` metadata events as raw evidence; outcome handling remains in
the isolated label pipeline rather than the strategy event stream.
It fetches a public depth snapshot, buffers updates until the snapshot overlaps
their update IDs, and resets the local depth epoch on a discontinuity. The Book
Ticker and REST snapshot do not carry a venue event time, so their schema marks
them as receive-time-only evidence. Add a stream only when it is part of the
pre-registered collection protocol, for example `--binance-stream
btcusdt@aggTrade`. Stop with Ctrl+C; the collector flushes accepted buffered
records before it exits.

## Opening Mispricing Research Protocol

The first strategy is deliberately one problem: estimate the fair probability
of the final Up outcome only during `t0+3s` through `t0+180s`, then compare it
with both passive outcome-token books. No pre-open prediction or MAE trigger
participates in the decision.

Features may update every 250 ms, but the frozen model cadence is five seconds,
anchored to market open. The 36 decisions are exactly `t0+5/10/.../180s`.
The model is one continuous, time-aware model; elapsed/remaining time are
features. It does not train a separate model per time bucket. Development-only
calibration and threshold selection are reported separately for
`3–30s`, `35–90s`, and `95–180s`. A regime is disabled unless its selected
development threshold has a strictly positive 95% net-edge confidence lower
bound. The live/replay candidate remains the earliest signal across all enabled
regimes, so a later regime never replaces an earlier qualifying entry. A
candidate must appear in two consecutive model signals, five seconds apart,
then gets one placement cycle at pre-registered passive price levels. It never
lowers an order price merely to manufacture apparent edge. The current maker
configuration remains a conservative 10c model-edge requirement plus a
separate 1c safety buffer; lower research-proxy thresholds are not execution
authorization.

At each decision point the strategy evaluates both sides:

```text
edge(up) = p_fair(up) - passive_up_price - maker_fee - safety_buffer
edge(down) = (1-p_fair(up)) - passive_down_price - maker_fee - safety_buffer
```

`p_market` is an observed execution comparison, not a feature in the current
fair-probability model. This prevents a Binance-only historical proxy from
quietly learning a feature unavailable in that proxy. A later residual model
may use synchronized CLOB history only after its own held-out validation.

The walk-forward protocol keeps every snapshot of one market in the same
group. A train/calibration/test/holdout boundary may never split a market,
because doing so would leak the shared final label across partitions. Each
market's snapshots have total sample weight one. `btc_short_horizon.research.gates`
encodes acceptance checks; an opening-mispricing gate requires a positive
paired held-out net-edge confidence lower bound, and the maker gate additionally
requires real replay fills under the pessimistic queue/P99 latency scenario.

### Fast Fair-Probability Feasibility Proxy

`scripts/btc_opening_mispricing_proxy.py` is a fast research path, not a
maker-backtest substitute. It uses compact Binance klines and final Gamma
labels to test whether a causal opening-window fair-probability model is useful.
It deliberately excludes Polymarket price/depth, Chainlink opening reference,
queue, fills, fees, rebates, and network latency. The script trains only a
small pre-registered Logistic/LightGBM candidate set on development data, then
uses the sealed holdout once for the selected candidate.

```powershell
uv run python scripts/btc_opening_mispricing_proxy.py `
  --start-date <YYYY-MM-DD> `
  --end-date <YYYY-MM-DD> `
  --rule-epoch <verified-rule-epoch> `
  --market-catalog <prior-catalog.json> `
  --binance-directory <local-kline-archive-directory> `
  --interval 1s `
  --profile quick `
  --output-directory <new-artifact-directory>
```

The 1-second archives are a bounded diagnostic. Use the 1-minute interval when
only minute-resolvable features are sufficient; neither archive has observed
receive timestamps, so neither provides latency evidence.

For a memory-bounded feasibility run, `--materialized-market-stride N` reads
one of every `N` chronologically ordered markets from an existing materialized
dataset. The default `N=1` is the exact path. Any `N>1` result is explicitly
tagged approximate and may prioritize research, but cannot satisfy a Go/No-Go
gate or authorize a model change. The loader streams Parquet batches rather
than materializing the source table in Pandas.

Runtime bootstrap does not download another archive. It requests at most four
1,000-row pages from Binance Spot `/api/v3/klines`, accepts closed 1-second bars
only, verifies continuity, and builds the exact training feature schema. The
one-hour baseline plus the 180-second decision horizon currently needs 3,782
rows. See the
official [Binance Spot REST market-data documentation](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints).

`scripts/btc_opening_price_edge_proxy.py` is the next lightweight gate. It
joins OOF/holdout probabilities to sparse one-minute Polymarket token price
history, applies frozen development-selected thresholds independently by
regime, chooses the earliest candidate with two consecutive five-second
signals, and permits at most one entry per market. Those prices are not BBO,
queue, latency, or fill evidence, so the output is a market-relative edge proxy
only and never maker P&L.

### Opening Market Evidence Audit

`scripts/btc_opening_market_audit.py` rebuilds both token books from the
forward raw CLOB events through the same L2 normalizer used by the collector,
then joins them at causal decision timestamps. It reports observed market
probability, book age, epoch gaps, and tick changes. It does not fabricate a
fair probability or submit a replay order.

```powershell
uv run python scripts/btc_opening_market_audit.py `
  --market-catalog <forward-catalog.json> `
  --market-slug <validated-slug> `
  --output-directory <new-audit-directory>
```

`scripts/btc_pmxt_coverage_audit.py` performs the corresponding historical
availability check. It verifies the immutable Gamma token mapping, rebuilds
each PMXT token through Nautilus `OrderBook`, and requires both L2 and
`TradeTick` evidence before a joint replay may be considered data-eligible.
An empty PMXT result is a coverage failure, never evidence that the market had
no liquidity.

### Opening Feature Evidence Audit

`scripts/btc_opening_feature_audit.py` replays the same versioned forward raw
streams into the shared `OpeningFeatureState`: both Polymarket token books,
Chainlink BTC/USD, and Binance spot/perpetual trades plus usable BBOs. It
emits causal feature snapshots and source/quality coverage for one complete
opening window. A raw epoch boundary, CLOB tick change, stale required source,
or missing opening reference remains an explicit ineligible flag; the script
does not train a model, emit a probability, or place an order.

`build_opening_direction_dataset` is the only seam from these snapshots into
the existing `DirectionDataset` model pipeline. It accepts final Gamma labels
only after `label_available_ts`, selects the pre-registered five-second
training cadence, excludes an entire market if any selected snapshot is
missing or ineligible, and gives every included market total sample weight one.
The observed Polymarket probability remains outside the feature vector.

```powershell
uv run python scripts/btc_opening_feature_audit.py `
  --market-catalog <forward-catalog.json> `
  --market-slug <validated-slug> `
  --output-directory <new-audit-directory>
```

`scripts/btc_opening_proxy_shadow.py` performs one bounded model-to-signal pass
without an execution gateway. It loads the versioned model, reconstructs the
configured causal dual-token observations through `t0+180s`, bootstraps only
the bounded Binance REST window, and writes `predictions.parquet`,
`signals.parquet`, and `metrics.json`. It validates the model artifact's timing
protocol and reports coverage by regime; a legacy 30-second artifact cannot be
used as a 180-second shadow model.

```powershell
uv run python scripts/btc_opening_proxy_shadow.py `
  --market-catalog <forward-catalog.json> `
  --market-slug <validated-slug> `
  --model-directory <model-artifact-directory> `
  --output-directory <new-shadow-directory>
```

## Running One Replay

The runner takes real inputs only; it does not embed a stale market slug or a
synthetic prediction:

```bash
uv run python backtests/polymarket_btc_15m_opening_mispricing_maker.py \
  --market-catalog /path/to/btc-15m-open-catalog.json \
  --market-slug <validated-current-slug> \
  --signals /path/to/opening-mispricing-signals.parquet \
  --rule-epoch chainlink-btc-usd-v1 \
  --start-time 2026-04-13T00:00:00Z \
  --end-time 2026-04-13T00:01:00Z \
  --scenario p99_pessimistic \
  --artifact-directory /path/to/output/run-001 \
  --code-revision "$(git rev-parse HEAD)" \
  --upstream-revision 8d694143836f486e053e157a31484e8ad471fe6f \
  --model-hash <model-sha256> \
  --raw-data-hash <raw-manifest-sha256>
```

The token-index flags default to `0` for Up and `1` for Down only because the
operator is required to verify that mapping against the specific PMXT market
metadata during the framework audit. Override `--up-token-index` and
`--down-token-index` when the archive order differs.

The output directory contains `run_manifest.json`, `data_quality.json`, six
Parquet tables, `metrics.json`, and a summary `report.html`. A missing model or
raw data hash is an error; the runner never invents lineage.

## Dashboard Observability

The dashboard is a credential-free, read-only observer. It combines two
persisted inputs without reading exchange credentials or mutating Bot state:

- `status/*.json` contains short-lived service health and data-quality counters.
- `dashboard/snapshot.json` is one validated projection for performance,
  recent market-level trades, connection/latency health, alerts, and the
  strategy lifecycle.

`DashboardSnapshotStore` is the seam between producers and the page. Paper,
Canary, and future live runners may publish the same `BotDashboardSnapshot`;
the HTTP dashboard only calls `read()` and renders an explicit empty state when
the projection is absent or invalid. One recent-trade row represents one
market placement cycle, even when it contains multiple order layers or partial
fills.

Performance values must be based on an account ledger or an explicitly marked
Paper/Canary projection. Direction-model scores, sparse price-history edge,
expected edge, and post-window Shadow signals must never populate realized
PnL or the account-equity curve. Missing order latency in Shadow is `N/A`, not
zero milliseconds.

Health producers own their thresholds and publish `ok`, `warning`, `error`, or
`unknown`; the page never averages these into a misleading health score. A
required failed feed or order-reconciliation path therefore makes the overall
status fail closed.

The recommended governance cadence is operational review every day, a frozen
challenger candidate every 14 days, and a manual promotion review every 28
days or when the pre-registered evidence target is reached. A fee, tick, rule,
schema, feed, drift, or latency event may trigger an earlier challenge. No
schedule automatically replaces the champion model.

## Live Progression

`LiveMode.SHADOW` is the default and never calls a gateway. `PAPER` and
`CANARY` require a gateway and an explicit `trading_enabled` risk setting.
`PAPER` accepts only the in-memory `PaperOrderGateway`, so a paper test cannot
accidentally reach the CLOB client. The separate forward-collector runtime has
no gateway at all: it writes atomic, credential-free status snapshots and a
filesystem stop request for a read-only local dashboard. See
[BTC VPS Operations](btc-vps-operations.md) for the deployment and migration
contract.
Canary orders are bounded by `canary_max_shares`; the service tracks 200
orders/50 fills before extended-canary eligibility and 2,000 orders/300 fills
before scale review. User-channel trade reconciliation is idempotent per
`(trade_id, client_order_id)`, so later status updates do not double-count a
fill.

Current Polymarket documentation states that makers are not charged trading
fees. The baseline therefore uses maker fee and rebate equal to zero; maker
rebates stay a separate attribution only. Before any non-shadow run, query the
current CLOB market info for the actual condition and set `fee_unchanged=false`
on any rule change. See the official [fee documentation](https://docs.polymarket.com/trading/fees).

No credential, funder, API key, or region eligibility result belongs in this
repository. Production remains disabled until account, jurisdiction, market
rules, tick size, fee schedule, clock, and current SDK behavior are verified
by the operator.

## Windows Native Build

The PMXT native extension requires Rust plus the Visual Studio C++ build tools
and a Windows SDK. After the C++ workload is installed, run:

```powershell
cmd /d /s /c "call \"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat\" -arch=x64 -host_arch=x64 && set PATH=%USERPROFILE%\.cargo\bin;%PATH% && uv run --with maturin maturin develop --release --manifest-path crates/python/Cargo.toml --uv"
```

Run the PMXT/native test group immediately afterward. Until that build passes,
the Python-level contracts are tested but a full PMXT replay is not considered
verified.

## Current Evidence Boundary

The current exact three-minute directional artifact covers all 7,295 resolved
markets from 2026-04-28 through 2026-07-13 and 262,620 five-second snapshots.
Paired daily-block candidate selection retained `logistic-c0.1`; LightGBM did
not show a jointly positive held-out improvement in log loss and Brier. The
1,345-market holdout has log loss 0.65249 and Brier 0.23008, versus 0.69341 and
0.25013 for the frozen training prior, with calibration slope 0.993. The formal gate is
still No-Go: fewer than 2,500 holdout markets are available, the 76-day source
cannot satisfy the full 90/21/14/28-day protocol, and the period lacks a causal
Polymarket implied-probability baseline. Gamma coverage is also one market
short of the requested 7,296. This is useful direction evidence, not a
profitability approval.

The corresponding sparse price proxy applies frozen per-regime thresholds and
caps observed price age at 15 seconds. The retained holdout has 1,098 entries at
2.56c/share (95% CI 0.38c to 4.75c). With an additional 1c cost it has 935
entries at 1.67c/share, but the 95% CI crosses zero (-0.59c to 4.17c). These
one-minute observations cannot establish executable prices, passive fills,
queue position, fees, or latency-adjusted maker P&L.

The v6 look-ahead collector completed a full opening on
`btc-updown-15m-1784214900`. The bounded dual-token Shadow reconstructed all 36
five-second decisions through `t0+180s`, produced all 36 predictions, and
submitted zero orders. The enhanced spot/perpetual/Chainlink/CLOB audit observed
all 36 decisions; 32 were quality-eligible and four failed closed because
Binance Spot trade or BBO evidence was older than one second. There were no gap
flags. This validates causal runtime compatibility while preserving the data
freshness boundary; one window does not establish statistical stability.

These results still cannot establish passive fills, queue position, or
latency-adjusted maker P&L. No market advances beyond shadow until synchronized
BookReplay evidence passes the pessimistic queue/P99 latency gates using
reproducible raw manifests.

A bounded PMXT v2 audit of the 2026-07-13T04 UTC raw hour found zero rows for
all four Gamma-verified BTC 15m conditions in the local archive file. The
archive may become usable for other windows only after the per-market coverage
audit succeeds; this project does not substitute PMXT v1 or treat an empty
hour as a tradable historical replay.
