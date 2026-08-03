# BTC Lightweight Factor Challenge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a development-only paired factor challenge from existing BTC 1-second data plus lightweight Binance USD-M 1-minute data.

**Architecture:** Extract the existing materialized-dataset reader into a shared research boundary, append deterministic factor-family matrices without changing sample lineage, and run a frozen paired walk-forward candidate matrix. Cross-market observations use only closed spot/perp minute bars available before each decision; incomplete markets are removed from every candidate.

**Tech Stack:** Python 3.13, NumPy, PyArrow, scikit-learn, LightGBM, existing BTC research pipeline.

## Global Constraints

- BTC 15m only; no other Polymarket families.
- No new dependency and no new bulk 1-second download.
- One market is one statistical group and has total sample weight 1.
- `available_ts <= decision_ts` for every factor; historical Klines remain event-time-only.
- Exact paired samples, labels, weights and folds across all candidates.
- Development OOF only; sealed holdout remains unopened and no runtime artifact is emitted.
- Existing VPS champion and `paper-v3-independent-fak` runtime are unchanged.

---

### Task 1: Shared materialized dataset reader

**Files:**
- Create: `btc_short_horizon/research/materialized_dataset.py`
- Modify: `scripts/btc_opening_mispricing_proxy.py`
- Modify: `btc_short_horizon/research/__init__.py`
- Test: `tests/btc_short_horizon/test_materialized_dataset.py`

**Interfaces:**
- Produces: `read_materialized_direction_dataset(path: Path, *, expected_schema: FeatureSchema | None = None, market_stride: int = 1) -> DirectionDataset`.
- Preserves the current CLI reader's strict integer, finite, chronological, group and weight checks.

- [ ] Write tests for valid roundtrip, schema mismatch, duplicate/out-of-order samples, non-finite values, and whole-market stride.
- [ ] Run the focused tests and confirm the new API is absent/failing.
- [ ] Move the existing strict reader logic into the shared module and make the old CLI call it without behavior changes.
- [ ] Run materialized reader and existing opening-proxy tests.

### Task 2: Deterministic factor augmentation

**Files:**
- Create: `btc_short_horizon/research/opening_factor_challenge.py`
- Modify: `btc_short_horizon/research/__init__.py`
- Test: `tests/btc_short_horizon/test_opening_factor_challenge.py`

**Interfaces:**
- Produces: `OpeningFactorFamily`, `opening_factor_feature_schema(...)`, and `build_opening_factor_dataset(...)`.
- Consumes the exact control `DirectionDataset`; optional spot/perp `BinanceKlineHistory` adds the fixed `cross_market` family.

- [ ] Write synthetic tests for exact feature values, causal minute availability, future-data mutation invariance, finite validation, identical weights/samples, and whole-market exclusion for incomplete cross-market history.
- [ ] Run focused tests and confirm failure before implementation.
- [ ] Implement the four frozen factor families and append-only schema construction.
- [ ] Run focused tests and existing opening-proxy tests.

### Task 3: Frozen development challenge and CLI

**Files:**
- Create: `btc_short_horizon/research/opening_factor_research.py`
- Create: `scripts/btc_opening_factor_challenge.py`
- Test: `tests/btc_short_horizon/test_opening_factor_research.py`
- Test: `tests/btc_short_horizon/test_btc_opening_factor_challenge_script.py`

**Interfaces:**
- Produces the seven fixed candidates from the design, paired metrics, seven pre-registered Bonferroni-adjusted comparisons at `alpha=0.05/7`, and a development-only verdict.
- CLI consumes one materialized protocol-v2 dataset plus optional spot/perp minute archive directories and writes atomic report artifacts.

- [ ] Write tests for candidate immutability, paired lineage, adjusted alpha, Logistic-first selection, LightGBM replacement rules, no sealed-holdout call, provenance and atomic output failure.
- [ ] Run focused tests and confirm failure before implementation.
- [ ] Implement the development runner and direct-script/import-safe CLI.
- [ ] Run focused tests and `--help` smoke.

### Task 4: Execute the local challenge and document evidence

**Files:**
- Modify: `docs/btc-model-validation-research.md`
- Modify: `docs/project-status.md`
- Modify: `README.md` table of contents only if a new documentation heading requires it.
- Refresh: `CODEBASE_UML.md`

**Interfaces:**
- Uses `D:/polymarket_btc/output/btc_short_horizon/research/opening-proxy-protocol-v2-clean-20260428-20260713/dataset.parquet` as the frozen control dataset.
- Uses existing local archives and downloads only missing Binance Spot and USD-M BTCUSDT 1-minute daily archives for 2026-04-27 through 2026-07-12; records planned/actual bytes, SHA-256, and daily coverage. No 1-second, aggTrades, or depth archive is downloaded.

- [ ] Run the challenge into a new timestamped local output directory; never overwrite an earlier result.
- [ ] Record actual coverage, candidate metrics, adjusted intervals and development verdict without claiming profit.
- [ ] Update long-lived docs with the new factor protocol and evidence boundary.
- [ ] Refresh UML, run Ruff, complete BTC tests, full tests, MkDocs strict build and diff check.
