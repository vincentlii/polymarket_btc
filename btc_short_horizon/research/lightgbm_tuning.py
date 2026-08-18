"""Pre-registered, bounded LightGBM challenger grid.

This module only defines the candidate contract.  Model selection remains in
the existing walk-forward and sealed-holdout pipeline; it must not be done by
ranking one shared holdout repeatedly.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from itertools import product
import json

from btc_short_horizon.models import DirectionModelConfig


@dataclass(frozen=True, slots=True)
class LightGBMSearchCandidate:
    name: str
    stage: str
    config: DirectionModelConfig

    @property
    def manifest_hash(self) -> str:
        payload = {
            "name": self.name,
            "stage": self.stage,
            "config": {
                field: getattr(self.config, field) for field in self.config.__dataclass_fields__
            },
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


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


def residual_logistic_grid(*, random_seed: int = 17) -> tuple[LightGBMSearchCandidate, ...]:
    return tuple(
        LightGBMSearchCandidate(
            name=f"residual-logistic-c{value:g}",
            stage="shared_residual",
            config=DirectionModelConfig(
                kind="logistic",
                calibration_method="auto",
                logistic_c=value,
                random_seed=random_seed,
            ),
        )
        for value in (0.01, 0.03, 0.1, 0.3, 1.0)
    )


def residual_lightgbm_grid(*, random_seed: int = 17) -> tuple[LightGBMSearchCandidate, ...]:
    candidates: list[LightGBMSearchCandidate] = []
    for (leaves, depth), min_child, learning_rate, estimators in product(
        ((3, 2), (7, 3), (15, 4), (31, 5)),
        (100, 200, 500, 1_000),
        (0.01, 0.03),
        (1_000, 2_000),
    ):
        candidates.append(
            LightGBMSearchCandidate(
                name=(
                    f"residual-lgbm-d{depth}-l{leaves}-mc{min_child}-"
                    f"lr{learning_rate:g}-n{estimators}"
                ),
                stage="shared_residual",
                config=DirectionModelConfig(
                    kind="lightgbm",
                    calibration_method="auto",
                    random_seed=random_seed,
                    lightgbm_num_leaves=leaves,
                    lightgbm_max_depth=depth,
                    lightgbm_min_child_samples=min_child,
                    lightgbm_learning_rate=learning_rate,
                    lightgbm_n_estimators=estimators,
                    lightgbm_early_stopping_rounds=100,
                ),
            )
        )
    return tuple(candidates)


__all__ = [
    "LightGBMSearchCandidate",
    "controlled_lightgbm_grid",
    "residual_lightgbm_grid",
    "residual_logistic_grid",
]
