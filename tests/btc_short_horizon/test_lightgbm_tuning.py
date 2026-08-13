from __future__ import annotations

from btc_short_horizon.research.lightgbm_tuning import (
    controlled_lightgbm_grid,
    residual_lightgbm_grid,
    residual_logistic_grid,
)
from btc_short_horizon.models.market_relative_lightgbm import (
    fit_market_relative_lightgbm,
    minimum_leaf_unique_market_count,
    require_minimum_leaf_unique_markets,
)

import numpy as np
import pytest

from btc_short_horizon.features.schema import FeatureSchema


def test_controlled_lightgbm_grid_is_bounded_and_deterministic() -> None:
    candidates = controlled_lightgbm_grid(stage="early_3s_to_30s")

    assert len(candidates) == 18
    assert len({candidate.name for candidate in candidates}) == len(candidates)
    assert all(candidate.config.kind == "lightgbm" for candidate in candidates)
    assert all(candidate.config.lightgbm_n_estimators == 2_000 for candidate in candidates)
    assert all(candidate.config.lightgbm_early_stopping_rounds == 100 for candidate in candidates)
    assert [candidate.name for candidate in candidates] == [
        candidate.name for candidate in controlled_lightgbm_grid(stage="early_3s_to_30s")
    ]


def test_residual_grids_are_exact_deterministic_and_do_not_mutate_historical_grid() -> None:
    logistic = residual_logistic_grid()
    lightgbm = residual_lightgbm_grid()

    assert len(logistic) == 5
    assert [item.config.logistic_c for item in logistic] == [0.01, 0.03, 0.1, 0.3, 1.0]
    assert len(lightgbm) == 64
    assert len({item.name for item in lightgbm}) == 64
    assert all(item.config.lightgbm_early_stopping_rounds == 100 for item in lightgbm)
    assert [item.name for item in lightgbm] == [item.name for item in residual_lightgbm_grid()]
    assert len(controlled_lightgbm_grid(stage="early_3s_to_30s")) == 18


def test_residual_leaf_audit_counts_unique_markets_not_snapshot_rows() -> None:
    leaves = np.asarray(
        [
            [0, 0],
            [0, 0],
            [0, 1],
            [1, 1],
        ]
    )

    assert (
        minimum_leaf_unique_market_count(
            leaf_indices=leaves,
            market_slugs=("a", "a", "b", "c"),
        )
        == 1
    )


def test_artifact_leaf_gate_rejects_a_model_with_too_few_independent_markets() -> None:
    leaves = np.asarray([[0], [0], [1], [1]])

    with pytest.raises(ValueError, match="independent markets per leaf"):
        require_minimum_leaf_unique_markets(
            leaf_indices=leaves,
            market_slugs=("m1", "m1", "m2", "m2"),
            minimum_markets_per_leaf=2,
        )

    assert (
        require_minimum_leaf_unique_markets(
            leaf_indices=leaves,
            market_slugs=("m1", "m2", "m3", "m4"),
            minimum_markets_per_leaf=2,
        )
        == 2
    )


def test_market_relative_lightgbm_zero_tree_prediction_keeps_market_anchor() -> None:
    vectors = np.asarray([[-1.0], [-0.5], [0.5], [1.0]])
    labels = np.asarray([0, 0, 1, 1])
    anchors = np.asarray([0.20, 0.40, 0.60, 0.80])
    model = fit_market_relative_lightgbm(
        train_vectors=vectors,
        train_labels=labels,
        train_market_up_probability=anchors,
        validation_vectors=vectors,
        validation_labels=labels,
        validation_market_up_probability=anchors,
        schema=FeatureSchema(version="test-v1", names=("momentum",)),
        probability_uncertainty_radius=0.03,
        num_leaves=3,
        max_depth=2,
        min_child_samples=100,
        learning_rate=0.03,
        n_estimators=5,
        early_stopping_rounds=2,
        random_seed=17,
    )

    assert model.predict_up_probability(vectors, anchors) == pytest.approx(anchors)
