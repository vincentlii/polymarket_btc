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
only how quickly a newly opened Gamma market is retried. CLI overrides are
available for a deliberately bounded operator run; do not lower these defaults
without measuring resulting file counts.

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

## Forward Collection

The collector is deliberately BTC-only. First discover the current 15m Gamma
metadata into a validated catalog. The collector then resolves the active
Up/Down token IDs from that catalog; it never reuses a token ID from a prior
window.

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
window from the configured family, asks Gamma for that exact slug, writes a
rule-hash-specific catalog, and only then starts a collector for its two token
IDs. At the validated window end it flushes and switches to the next market;
Gamma publication delays are retried rather than reusing a previous token pair.

```powershell
uv run python scripts/btc_forward_collector.py `
  --follow-current `
  --family 15m `
  --rule-epoch chainlink-btc-usd-v1 `
  --catalog-directory data/metadata/forward_catalogs
```

Choose and monitor an explicit storage budget before leaving this append-only
process running. It is a public data collector only and never submits orders.

It records the Polymarket market channel, Chainlink BTC/USD RTDS, Binance spot
`btcusdt@trade`, `btcusdt@depth@100ms`, and `btcusdt@bookTicker`, Binance
perpetual `btcusdt@aggTrade`, `btcusdt@depth@100ms`, and `btcusdt@bookTicker`,
and OKX BTC-USDT / BTC-USDT-SWAP public trades and books by default. The swap
contract quantity is only normalized after the collector fetches the current
OKX instrument metadata; it is never hard-coded. Each Binance spot/perpetual
depth sequence has an independent synchronizer.
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
with both passive outcome-token books. No pre-open prediction, MAE trigger, or
separate time-bucket model participates in the decision.

Features update every 250 ms and inference occurs every second. The model is
one continuous, time-aware model; elapsed/remaining time are features. Time
bins are used only for calibration and reporting. A candidate must persist for
two seconds, then gets one placement cycle at pre-registered passive price
levels. It never lowers an order price merely to manufacture apparent edge.

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

## Live Progression

`LiveMode.SHADOW` is the default and never calls a gateway. `PAPER` and
`CANARY` require a gateway and an explicit `trading_enabled` risk setting.
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

The implementation supplies infrastructure and a provisional fair-probability
result, not a maker-profitability or deployment claim. The Binance proxy is
allowed to establish only that the outcome model merits the next data stage.
It cannot establish Polymarket mispricing, passive fills, or latency-adjusted
P&L. No market should advance beyond shadow until synchronized CLOB/Chainlink
data passes the configured data-coverage and pessimistic execution gates using
reproducible raw manifests.
