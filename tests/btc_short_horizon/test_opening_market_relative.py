from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from btc_short_horizon.research.opening_evidence import OpeningMarketObservation
from btc_short_horizon.research.opening_market_relative import (
    MarketRelativeFeatureFamily,
    build_opening_market_relative_datasets,
    opening_market_relative_feature_schema,
)
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.opening_proxy import (
    OpeningProxyDatasetBuild,
    opening_proxy_feature_schema,
)
from btc_short_horizon.research.walk_forward import ResearchSample


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _control_build() -> OpeningProxyDatasetBuild:
    schema = opening_proxy_feature_schema(1)
    names = {name: index for index, name in enumerate(schema.names)}
    vectors = np.zeros((4, len(schema.names)), dtype=float)
    for row, remaining in enumerate((895.0, 890.0, 895.0, 890.0)):
        vectors[row, names["p_boundary_up"]] = 0.60
        vectors[row, names["remaining_seconds"]] = remaining
    samples = tuple(
        ResearchSample(
            sample_id=f"m{market}@{second}",
            feature_ts=T0 + timedelta(minutes=15 * market, seconds=second),
            label_available_ts=T0 + timedelta(minutes=15 * (market + 1)),
            label=market % 2,
            group_id=f"m{market}",
        )
        for market in range(2)
        for second in (5, 10)
    )
    dataset = DirectionDataset(
        samples=samples,
        vectors=vectors,
        schema=schema,
        sample_weights=np.full(4, 0.5),
    )
    return OpeningProxyDatasetBuild(
        dataset=dataset,
        requested_markets=2,
        excluded_void_markets=0,
        excluded_insufficient_history=0,
        excluded_kline_gaps=0,
        snapshots_per_market=2,
    )


def _observation(market: int, second: int, *, probability: float = 0.55):
    decision = T0 + timedelta(minutes=15 * market, seconds=second)
    decision_ns = int(decision.timestamp() * 1_000_000_000)
    return OpeningMarketObservation(
        market_slug=f"m{market}",
        decision_ts_ns=decision_ns,
        p_market_mid_up=probability,
        data_age_seconds=0.2,
        up_available_ts_ns=decision_ns - 100_000_000,
        down_available_ts_ns=decision_ns - 200_000_000,
        up_epoch_id=1,
        down_epoch_id=1,
        has_data_gap=False,
        structure_valid=True,
        tick_unchanged=True,
    )


def test_market_relative_dataset_is_paired_and_appends_frozen_factors() -> None:
    control = _control_build()
    observations = {
        f"m{market}": tuple(_observation(market, second) for second in (5, 10))
        for market in range(2)
    }
    families = tuple(MarketRelativeFeatureFamily)

    build = build_opening_market_relative_datasets(
        control_build=control,
        observations_by_market=observations,
        families=families,
    )

    assert build.eligible_market_count == 2
    assert build.excluded_missing_market_observation == 0
    assert build.paired_control_dataset.schema == control.dataset.schema
    assert build.paired_control_dataset.samples == build.challenger_dataset.samples
    assert np.array_equal(
        build.paired_control_dataset.sample_weights,
        build.challenger_dataset.sample_weights,
    )
    assert build.challenger_dataset.schema == opening_market_relative_feature_schema(
        base_schema=control.dataset.schema,
        families=families,
    )
    appended = build.challenger_dataset.vectors[:, len(control.dataset.schema.names) :]
    market_logit = np.log(0.55 / 0.45)
    boundary_gap = np.log(0.60 / 0.40) - market_logit
    assert appended[0, 0] == pytest.approx(market_logit)
    assert appended[0, 1] == pytest.approx(boundary_gap)
    assert appended[0, 2] == pytest.approx(market_logit * (895.0 / 900.0))
    assert appended[0, 3] == pytest.approx(boundary_gap * (895.0 / 900.0))
    assert appended[0, 4] == pytest.approx(0.2)


def test_market_relative_dataset_excludes_whole_market_on_missing_or_future_evidence() -> None:
    control = _control_build()
    future = _observation(1, 5)
    future = replace(future, up_available_ts_ns=future.decision_ts_ns + 1)
    observations = {
        "m0": (_observation(0, 5),),
        "m1": (future, _observation(1, 10)),
    }

    build = build_opening_market_relative_datasets(
        control_build=control,
        observations_by_market=observations,
        families=(MarketRelativeFeatureFamily.MARKET_ANCHOR,),
    )

    assert build.eligible_market_count == 0
    assert build.excluded_missing_market_observation == 1
    assert build.excluded_invalid_market_observation == 1
    assert build.paired_control_dataset is None
    assert build.challenger_dataset is None


def test_market_relative_schema_is_order_independent_and_control_schema_is_unchanged() -> None:
    base = opening_proxy_feature_schema(1)
    original_hash = base.hash

    first = opening_market_relative_feature_schema(
        base_schema=base,
        families=(
            MarketRelativeFeatureFamily.TIME_QUALITY,
            MarketRelativeFeatureFamily.MARKET_ANCHOR,
        ),
    )
    second = opening_market_relative_feature_schema(
        base_schema=base,
        families=(
            MarketRelativeFeatureFamily.MARKET_ANCHOR,
            MarketRelativeFeatureFamily.TIME_QUALITY,
        ),
    )

    assert first == second
    assert base.hash == original_hash
