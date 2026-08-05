# BTC 15m Stage-Aware Taker Challenger v2

## Release boundary

This release is a new Research Paper epoch: `paper-v4-stage-aware-taker-v2`. It
does not reuse or mutate the `paper-v3-independent-fak` ledgers. The three
maker-related variants remain in the configuration for historical comparison,
but are disabled for new decisions. The active primary is
`independent_fak_stable_3x5s`; `independent_fak_2x5s` is its active challenger.

## Decision contract

Every valid post-open decision is assigned to one frozen stage:

| Stage | Window | Minimum taker net edge | Confirmation | Price band |
| --- | ---: | ---: | ---: | ---: |
| Early | 3–30 s | 4.0¢ | 2 signals | 20–80% |
| Price discovery | 35–90 s | 3.5¢ | 2 signals | 20–80% |
| Mid-early | 95–180 s | 3.0¢ | 3 signals | 20–80% |

The configured variant threshold and the stage threshold are combined with
`max`. A tail price is rejected before FAK planning; it is never counted as a
valid core trade. Stage policy is a pure strategy module, separate from the
book planner and execution simulator.

## Comparable opportunity set

The portfolio writes `opportunities.json` under the new epoch. It records both
selected and rejected independent-taker evaluations using the common identity
`market_slug:decision_ts_ns`. Variant cards therefore expose an opportunity
denominator instead of only counting submitted orders. Writes are batched to
avoid synchronous full-file I/O on every five-second tick.

## Stage model seam

`select_stage_dataset` and `run_stage_walk_forward_models` provide one causal
walk-forward model/calibrator run per stage. The live model adapter loads an
optional artifact directory named after a stage; if it is absent, it fails
back to the already validated proxy artifact and keeps the fallback explicit
in the runtime code. A stage artifact must pass the same feature-schema and
opening-protocol checks as the default artifact.

## LightGBM challenger

`controlled_lightgbm_grid` is the only registered v2 grid: 18 deterministic
low-capacity candidates over three `(num_leaves, max_depth)` pairs, three
`min_child_samples` values, and two learning rates. It uses early stopping and
must be evaluated through the existing walk-forward/sealed-holdout pipeline;
no candidate may be selected by repeatedly ranking the sealed holdout.

## Rollback

The VPS pre-release anchor is recorded in
`docs/btc-v2-rollback-baseline.json`. The existing remote commit
`2d1be58902ae3d0ca9a7595e04c3a9d9fd215b99` and
`deploy/compose.yaml.pre-depth` must remain available. The v2 deployment uses a
separate epoch and never removes prior ledgers or raw collector data.
