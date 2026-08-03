from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_factor_challenge import (
    OpeningFactorFamily,
    build_opening_factor_dataset,
    opening_factor_feature_schema,
)
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample


def _control() -> DirectionDataset:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    schema = FeatureSchema(
        version="control",
        names=(
            "elapsed_seconds",
            "remaining_seconds",
            "boundary_z",
            "p_boundary_up",
            "binance_spot_return_5s",
            "binance_spot_return_30s",
            "binance_spot_return_180s",
            "binance_spot_return_900s",
            "binance_spot_rv_5s",
            "binance_spot_rv_60s",
            "binance_spot_rv_300s",
            "binance_spot_rv_900s",
            "binance_spot_rv_3600s",
            "binance_spot_flow_5s",
            "binance_spot_flow_30s",
            "binance_spot_flow_60s",
            "binance_spot_flow_300s",
            "binance_spot_volume_5s",
            "binance_spot_volume_30s",
            "binance_spot_volume_300s",
            "binance_spot_volume_900s",
            "binance_spot_vwap_distance_5s",
            "binance_spot_vwap_distance_60s",
            "binance_spot_vwap_distance_300s",
            "binance_spot_vwap_distance_3600s",
        ),
    )
    samples = tuple(
        ResearchSample(
            sample_id=f"m{market}@{int((start + timedelta(minutes=15 * market, seconds=65)).timestamp() * 1e9)}",
            feature_ts=start + timedelta(minutes=15 * market, seconds=65),
            label_available_ts=start + timedelta(minutes=15 * market + 16),
            label=market % 2,
            group_id=f"m{market}",
        )
        for market in range(2)
    )
    return DirectionDataset(
        samples=samples,
        vectors=np.full((2, len(schema.names)), 0.1),
        schema=schema,
        sample_weights=np.full(2, 1.0),
    )


def _history(start: datetime, *, future_shift: float = 0.0) -> BinanceKlineHistory:
    opens = np.arange(
        int((start - timedelta(minutes=30)).timestamp() * 1e9),
        int((start + timedelta(minutes=40)).timestamp() * 1e9),
        60_000_000_000,
        dtype=np.int64,
    )
    values = 100.0 + np.arange(len(opens), dtype=float)
    values[-5:] += future_shift
    return BinanceKlineHistory(
        open_ts_ns=opens,
        close=values,
        volume=np.full(len(opens), 2.0),
        quote_volume=values * 2,
        taker_buy_volume=np.full(len(opens), 1.2),
        interval_seconds=60,
    )


def test_factor_schema_is_append_only_and_family_order_is_frozen() -> None:
    control = _control()
    schema = opening_factor_feature_schema(
        base_schema=control.schema, families=reversed(tuple(OpeningFactorFamily))
    )

    assert schema.names[: len(control.schema.names)] == control.schema.names
    assert schema.names[len(control.schema.names) : len(control.schema.names) + 4] == (
        "boundary_abs_z",
        "boundary_z_elapsed_fraction",
        "boundary_z_log_remaining",
        "boundary_probability_margin",
    )


def test_factor_build_is_causal_paired_and_excludes_incomplete_market() -> None:
    control = _control()
    start = control.samples[0].feature_ts
    baseline = build_opening_factor_dataset(
        control=control, spot_klines=_history(start), perp_klines=_history(start)
    )
    future = build_opening_factor_dataset(
        control=control,
        spot_klines=_history(start, future_shift=1_000_000),
        perp_klines=_history(start, future_shift=1_000_000),
    )

    assert baseline.paired_control_dataset.samples == baseline.factor_dataset.samples
    assert baseline.paired_control_dataset.sample_weights == pytest.approx(
        baseline.factor_dataset.sample_weights
    )
    assert baseline.factor_dataset.vectors == pytest.approx(future.factor_dataset.vectors)
    assert baseline.eligible_market_count == 2
    assert np.isfinite(baseline.factor_dataset.vectors).all()


def test_cross_market_requires_matching_latest_endpoint_and_counts_exclusion_once_per_market() -> (
    None
):
    control = _control()
    repeated = replace(
        control.samples[0],
        sample_id=control.samples[0].sample_id.replace("@", "-repeat@"),
        feature_ts=control.samples[0].feature_ts + timedelta(seconds=5),
    )
    repeated = replace(
        repeated,
        sample_id=f"m0-repeat@{int(repeated.feature_ts.timestamp() * 1e9)}",
        group_id="m0",
    )
    surviving = replace(
        control.samples[1],
        feature_ts=control.samples[0].feature_ts + timedelta(minutes=30),
        label_available_ts=control.samples[0].label_available_ts + timedelta(minutes=30),
    )
    surviving = replace(
        surviving,
        sample_id=f"m1@{int(surviving.feature_ts.timestamp() * 1e9)}",
    )
    control = DirectionDataset(
        samples=(control.samples[0], repeated, surviving),
        vectors=np.vstack((control.vectors[0], control.vectors[0], control.vectors[1])),
        schema=control.schema,
        sample_weights=np.asarray((0.5, 0.5, 1.0)),
    )
    spot = _history(control.samples[0].feature_ts)
    perp = _history(control.samples[0].feature_ts)
    decision_ns = int(control.samples[0].feature_ts.timestamp() * 1e9)
    available = spot.open_ts_ns + 61_000_000_000 <= decision_ns
    first_market_latest_open = int(spot.open_ts_ns[np.flatnonzero(available)[-1]])
    keep = spot.open_ts_ns != first_market_latest_open
    spot_with_endpoint_gap = BinanceKlineHistory(
        open_ts_ns=spot.open_ts_ns[keep],
        close=spot.close[keep],
        volume=spot.volume[keep],
        quote_volume=spot.quote_volume[keep],
        taker_buy_volume=spot.taker_buy_volume[keep],
        interval_seconds=spot.interval_seconds,
    )

    build = build_opening_factor_dataset(
        control=control,
        spot_klines=spot_with_endpoint_gap,
        perp_klines=perp,
    )

    assert {sample.group_id for sample in build.factor_dataset.samples} == {"m1"}
    assert build.eligible_market_count == 1
    assert build.excluded_incomplete_cross_market == 1
