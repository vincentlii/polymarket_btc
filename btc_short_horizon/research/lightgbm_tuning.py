"""Pre-registered, bounded LightGBM challenger grid.

This module only defines the candidate contract.  Model selection remains in
the existing walk-forward and sealed-holdout pipeline; it must not be done by
ranking one shared holdout repeatedly.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from btc_short_horizon.models import DirectionModelConfig


@dataclass(frozen=True, slots=True)
class LightGBMSearchCandidate:
    name: str
    stage: str
    config: DirectionModelConfig


def controlled_lightgbm_grid(
    *,
    stage: str,
    random_seed: int = 17,
) -> tuple[LightGBMSearchCandidate, ...]:
    """Return the frozen low-capacity grid for one opening stage."""

    if not stage or not stage.isascii():
        raise ValueError("stage must be a non-empty ASCII identifier")
    leaf_depth_pairs = ((3, 2), (7, 3), (15, 4))
    min_child_samples = (200, 500, 1_000)
    learning_rates = (0.01, 0.03)
    candidates: list[LightGBMSearchCandidate] = []
    for (leaves, depth), min_child, learning_rate in product(
        leaf_depth_pairs,
        min_child_samples,
        learning_rates,
    ):
        config = DirectionModelConfig(
            kind="lightgbm",
            random_seed=random_seed,
            lightgbm_num_leaves=leaves,
            lightgbm_max_depth=depth,
            lightgbm_min_child_samples=min_child,
            lightgbm_learning_rate=learning_rate,
            lightgbm_n_estimators=2_000,
            lightgbm_early_stopping_rounds=100,
        )
        candidates.append(
            LightGBMSearchCandidate(
                name=(f"lgbm-{stage}-d{depth}-l{leaves}-mc{min_child}-lr{learning_rate:g}"),
                stage=stage,
                config=config,
            )
        )
    return tuple(candidates)


__all__ = ["LightGBMSearchCandidate", "controlled_lightgbm_grid"]
