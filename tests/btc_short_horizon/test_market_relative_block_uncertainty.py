from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from btc_short_horizon.research.market_relative_uncertainty import (
    BlockUnit,
    paired_block_lower_bound,
)


def test_block_lower_bound_aggregates_snapshots_to_independent_markets() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    result = paired_block_lower_bound(
        candidate=np.asarray([0.3, 0.3, -0.1, -0.1]),
        baseline=np.zeros(4),
        market_ids=("a", "a", "b", "b"),
        market_times=(start, start, start + timedelta(days=1), start + timedelta(days=1)),
        unit=BlockUnit.MARKET,
        confidence=0.95,
        resamples=200,
        seed=17,
    )

    assert result.independent_market_count == 2
    assert result.observation_count == 4
    assert result.point_estimate == pytest.approx(0.1)
    assert 0.0 < result.p_value <= 1.0


@pytest.mark.parametrize("unit", [BlockUnit.DAY, BlockUnit.WEEK])
def test_calendar_block_lower_bound_reports_the_actual_block_count(unit: BlockUnit) -> None:
    start = datetime(2026, 7, 6, tzinfo=UTC)
    result = paired_block_lower_bound(
        candidate=np.asarray([0.1, 0.2, 0.3]),
        baseline=np.zeros(3),
        market_ids=("a", "b", "c"),
        market_times=(start, start + timedelta(days=1), start + timedelta(days=8)),
        unit=unit,
        confidence=0.95,
        resamples=100,
        seed=3,
    )

    assert result.block_count == (3 if unit is BlockUnit.DAY else 2)
    assert 0.0 < result.p_value <= 1.0


def test_block_lower_bound_rejects_one_market_repeated_as_fake_evidence() -> None:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="independent markets"):
        paired_block_lower_bound(
            candidate=np.asarray([0.1, 0.2]),
            baseline=np.zeros(2),
            market_ids=("same", "same"),
            market_times=(now, now),
            unit=BlockUnit.DAY,
            confidence=0.95,
            resamples=100,
            seed=1,
        )


def test_one_sided_p_value_uses_block_sign_flip_null() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    values = np.full(20, 0.1)
    result = paired_block_lower_bound(
        candidate=values,
        baseline=np.zeros(20),
        market_ids=tuple(f"m{index}" for index in range(20)),
        market_times=tuple(start + timedelta(days=index) for index in range(20)),
        unit=BlockUnit.DAY,
        confidence=0.95,
        resamples=2_000,
        seed=7,
    )

    assert result.p_value < 0.01
