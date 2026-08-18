# BTC 15m Stage-Aware Taker Challenger v2

## v5 integrity correction

The corrected runtime uses the new `paper-v5-stage-integrity` epoch. It keeps
the v3 and v4 ledgers immutable and visible through the dashboard history API.
The direction model, stage thresholds, price band, size, FAK latency, and
matching assumptions are unchanged from v4.

Confirmation state is now scoped to one stage and its required count is owned
only by the execution variant. A signal observed at 30 seconds cannot
contribute to confirmation at 35 seconds, and a 90-second signal cannot
contribute at 95 seconds. Stage rules own timing, price and edge gates; they do
not silently increase a variant's confirmation count.

Dashboard terminology is fixed and intentionally non-paired:

- **evaluation**: one planner evaluation, selected or rejected;
- **qualified signal**: the planner returned a side after all entry gates;
- **opportunity**: confirmation completed and an immutable trade record was created;
- **fill**: the simulated execution filled positive shares.

Evaluation diagnostics are stored incrementally in SQLite at
`evaluations.sqlite3`. The runtime no longer retains all evaluations in memory,
performs linear duplicate scans, or rewrites one ever-growing JSON document.
The journal is diagnostic evidence and is not a paired-EV denominator.

## Release boundary

This release is a new Research Paper epoch: `paper-v4-stage-aware-taker-v2`. It
does not reuse or mutate the `paper-v3-independent-fak` ledgers. The three
maker-related variants remain in the configuration for historical comparison,
but are disabled for new decisions. The active primary is
`independent_fak_2x5s`. `independent_fak_1x5s` is the immediate first-signal
control, while `independent_fak_stable_3x5s` is the conservative control. The
stable control also limits net-edge decay, so it is intentionally labeled as a
policy control rather than a confirmation-count-only experiment.

## Decision contract

Every valid post-open decision is assigned to one frozen stage:

| Stage | Window | Minimum taker net edge | Confirmation | Price band |
| --- | ---: | ---: | ---: | ---: |
| Early | 3–30 s | 4.0¢ | Variant-defined: 1 / 2 / 3 | 20–80% |
| Price discovery | 35–90 s | 3.5¢ | Variant-defined: 1 / 2 / 3 | 20–80% |
| Mid-early | 95–180 s | 3.0¢ | Variant-defined: 1 / 2 / 3 | 20–80% |

The configured variant threshold and the stage threshold are combined with
`max`. A tail price is rejected before FAK planning; it is never counted as a
valid core trade. Stage policy is a pure strategy module, separate from the
book planner and execution simulator.

## v4 evaluation log (superseded)

The v4 portfolio wrote `opportunities.json` and called every selected or
rejected five-second evaluation an opportunity. This made the dashboard ratio
look like `180 / 1` even though only one confirmed trade opportunity existed.
The v4 file remains immutable historical evidence, but v5 does not use its
terminology or storage design.

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
