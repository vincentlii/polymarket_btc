from __future__ import annotations

from btc_short_horizon.research.lightgbm_tuning import (
    controlled_lightgbm_grid,
    residual_lightgbm_grid,
    residual_logistic_grid,
)


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
