from __future__ import annotations

from btc_short_horizon.research.lightgbm_tuning import controlled_lightgbm_grid


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
