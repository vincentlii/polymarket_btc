from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.features import OpeningFeatureObservation, opening_feature_schema
from btc_short_horizon.research.opening_dataset import (
    build_opening_direction_dataset,
    market_stage_sample_weights,
)
from btc_short_horizon.strategy import OpeningStage


T0 = datetime(2026, 4, 13, tzinfo=UTC)
_SECOND = 1_000_000_000


def _market(start: datetime, *, outcome: MarketOutcome | None) -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(start),
        condition_id=f"condition-{int(start.timestamp())}",
        up_token_id=f"up-{int(start.timestamp())}",
        down_token_id=f"down-{int(start.timestamp())}",
        t0=start,
        t1=start + timedelta(minutes=15),
        rule_epoch="chainlink-v1",
        rule_hash="a" * 64,
        resolution=outcome,
        label_available_ts=(None if outcome is None else start + timedelta(minutes=16)),
    )


def _observations(
    market: MarketWindow,
    *,
    missing_offset: int | None = None,
    flagged_offset: int | None = None,
) -> tuple[OpeningFeatureObservation, ...]:
    schema = opening_feature_schema()
    start_ns = int(market.t0.timestamp() * _SECOND)
    values = schema.vector_from({name: 0.0 for name in schema.names})
    return tuple(
        OpeningFeatureObservation(
            market_window_start_ns=start_ns,
            ts_event=start_ns + offset * _SECOND,
            ts_init=start_ns + offset * _SECOND,
            feature_schema_hash=schema.hash,
            values=values,
            p_boundary_up=0.5,
            p_market_mid_up=0.5,
            quality_flags=(frozenset({"stale"}) if offset == flagged_offset else frozenset()),
        )
        for offset in range(5, 181, 5)
        if offset != missing_offset
    )


def test_opening_direction_dataset_weights_each_market_once() -> None:
    up = _market(T0, outcome=MarketOutcome.UP)
    down = _market(T0 + timedelta(minutes=15), outcome=MarketOutcome.DOWN)

    build = build_opening_direction_dataset(
        markets=(up, down),
        observations_by_market={
            up.slug: _observations(up),
            down.slug: _observations(down),
        },
    )

    assert build.included_markets == 2
    assert build.snapshots_per_market == 36
    assert len(build.dataset.samples) == 72
    for market in (up, down):
        indices = [
            index
            for index, sample in enumerate(build.dataset.samples)
            if sample.group_id == market.slug
        ]
        assert np.sum(build.dataset.sample_weights[indices]) == pytest.approx(1.0)
        elapsed = [
            (build.dataset.samples[index].feature_ts - market.t0).total_seconds()
            for index in indices
        ]
        for lower, upper in ((3, 30), (35, 90), (95, 180)):
            stage_indices = [
                index
                for index, seconds in zip(indices, elapsed, strict=True)
                if lower <= seconds <= upper
            ]
            assert np.sum(build.dataset.sample_weights[stage_indices]) == pytest.approx(1.0 / 3.0)


def test_market_stage_weights_are_equal_with_unequal_snapshot_counts() -> None:
    stages = (
        OpeningStage.EARLY,
        OpeningStage.PRICE_DISCOVERY,
        OpeningStage.PRICE_DISCOVERY,
        OpeningStage.MID_EARLY,
        OpeningStage.MID_EARLY,
        OpeningStage.MID_EARLY,
    )

    weights = market_stage_sample_weights(stages)

    assert sum(weights) == pytest.approx(1.0)
    for stage in OpeningStage:
        assert sum(
            weight for weight, item in zip(weights, stages, strict=True) if item is stage
        ) == (pytest.approx(1.0 / 3.0))


def test_market_stage_weights_reject_missing_stage_instead_of_renormalizing() -> None:
    with pytest.raises(ValueError, match="missing required opening stages"):
        market_stage_sample_weights((OpeningStage.EARLY, OpeningStage.MID_EARLY))


def test_opening_direction_dataset_excludes_whole_incomplete_or_ineligible_markets() -> None:
    complete = _market(T0, outcome=MarketOutcome.UP)
    incomplete = _market(T0 + timedelta(minutes=15), outcome=MarketOutcome.DOWN)
    ineligible = _market(T0 + timedelta(minutes=30), outcome=MarketOutcome.UP)

    build = build_opening_direction_dataset(
        markets=(complete, incomplete, ineligible),
        observations_by_market={
            complete.slug: _observations(complete),
            incomplete.slug: _observations(incomplete, missing_offset=25),
            ineligible.slug: _observations(ineligible, flagged_offset=25),
        },
    )

    assert build.included_markets == 1
    assert build.excluded_incomplete_markets == 1
    assert build.excluded_ineligible_markets == 1
    assert {sample.group_id for sample in build.dataset.samples} == {complete.slug}


def test_opening_direction_dataset_keeps_unresolved_and_void_labels_out() -> None:
    complete = _market(T0, outcome=MarketOutcome.UP)
    unresolved = _market(T0 + timedelta(minutes=15), outcome=None)
    void = _market(T0 + timedelta(minutes=30), outcome=MarketOutcome.VOID)

    build = build_opening_direction_dataset(
        markets=(complete, unresolved, void),
        observations_by_market={complete.slug: _observations(complete)},
    )

    assert build.excluded_unresolved_markets == 1
    assert build.excluded_void_markets == 1
    assert set(build.dataset.labels) == {1}
