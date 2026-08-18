from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from btc_short_horizon.backtest.signal_io import (
    read_opening_mispricing_signals,
    write_opening_mispricing_signals,
)
from btc_short_horizon.backtest.signals import (
    opening_signal_data_age_seconds,
    opening_signal_entry_rejection_reason,
    to_opening_mispricing_signal,
    validate_opening_mispricing_signal,
)
from btc_short_horizon.models import OpeningMispricingPrediction


def _prediction() -> OpeningMispricingPrediction:
    return OpeningMispricingPrediction(
        market_slug="btc-updown-15m-1776038400",
        model_version="opening-mispricing-logistic-2026-07-14",
        feature_schema_hash="feature-hash",
        market_window_start_ts_ns=1_000,
        trigger_ts_ns=2_000,
        p_up=0.64,
        p_boundary_up=0.62,
        p_market_mid_up=0.60,
        data_age_seconds=0.25,
    )


def test_opening_prediction_becomes_timestamp_preserving_custom_data() -> None:
    signal = to_opening_mispricing_signal(_prediction())

    validate_opening_mispricing_signal(signal)
    assert signal.market_slug == "btc-updown-15m-1776038400"
    assert signal.p_up == pytest.approx(0.64)
    assert signal.p_boundary_up == pytest.approx(0.62)
    assert signal.p_market_mid_up == pytest.approx(0.60)
    assert signal.ts_event == 2_000
    assert signal.ts_init == 2_000


def test_signal_contract_rejects_invalid_probability_and_bad_signal_fields() -> None:
    with pytest.raises(ValueError, match="p_up"):
        OpeningMispricingPrediction(
            market_slug="market",
            model_version="model",
            feature_schema_hash="schema",
            market_window_start_ts_ns=0,
            trigger_ts_ns=1,
            p_up=1.0,
            p_boundary_up=0.5,
            p_market_mid_up=0.5,
            data_age_seconds=0.0,
        )

    signal = to_opening_mispricing_signal(_prediction())
    signal.p_up = 1.0
    with pytest.raises(ValueError, match="p_up"):
        validate_opening_mispricing_signal(signal)


def test_market_relative_interval_has_independent_point_and_lineage() -> None:
    prediction = replace(
        _prediction(),
        market_relative_model_version="market-relative-v1",
        market_relative_feature_schema_hash="a" * 64,
        market_relative_p_up=0.49,
        market_relative_p_up_lower=0.45,
        market_relative_p_up_upper=0.53,
    )

    assert prediction.p_up == pytest.approx(0.64)
    assert prediction.market_relative_p_up == pytest.approx(0.49)

    with pytest.raises(ValueError, match="market-relative probability fields"):
        replace(prediction, market_relative_p_up_lower=None)


def test_signal_parquet_round_trip_is_validated_and_timestamp_ordered(tmp_path: Path) -> None:
    earlier = to_opening_mispricing_signal(replace(_prediction(), trigger_ts_ns=1_000))
    later = to_opening_mispricing_signal(replace(_prediction(), trigger_ts_ns=2_000))
    path = tmp_path / "opening-mispricing-signals.parquet"

    write_opening_mispricing_signals(path, (later, earlier))
    loaded = read_opening_mispricing_signals(path, market_slug=earlier.market_slug)

    assert [signal.ts_init for signal in loaded] == [1_000, 2_000]
    assert [signal.p_up for signal in loaded] == pytest.approx([0.64, 0.64])


def test_signal_parquet_reader_rejects_boolean_string_coercion(tmp_path: Path) -> None:
    path = tmp_path / "opening-mispricing-signals.parquet"
    write_opening_mispricing_signals(path, (to_opening_mispricing_signal(_prediction()),))
    record = pq.read_table(path).to_pylist()[0]
    record["has_data_gap"] = "false"
    pq.write_table(pa.Table.from_pylist([record]), path)

    with pytest.raises(TypeError, match="has_data_gap"):
        read_opening_mispricing_signals(path)


def test_signal_entry_rejects_quality_failure_and_ages_during_replay() -> None:
    signal = to_opening_mispricing_signal(_prediction())

    assert opening_signal_data_age_seconds(signal, now_ts_ns=500_002_000) == pytest.approx(0.75)
    assert (
        opening_signal_entry_rejection_reason(
            signal,
            now_ts_ns=1_100_002_000,
            stale_after_seconds=1.0,
        )
        == "data_stale"
    )

    signal.has_data_gap = True
    assert (
        opening_signal_entry_rejection_reason(
            signal,
            now_ts_ns=2_000,
            stale_after_seconds=1.0,
        )
        == "data_gap"
    )


def test_signal_entry_validates_types_before_interpreting_quality_flags() -> None:
    signal = to_opening_mispricing_signal(_prediction())
    signal.has_data_gap = "false"  # type: ignore[assignment]

    with pytest.raises(TypeError, match="has_data_gap"):
        opening_signal_entry_rejection_reason(
            signal,
            now_ts_ns=2_000,
            stale_after_seconds=1.0,
        )

    signal.has_data_gap = False
    with pytest.raises(ValueError, match="stale_after_seconds"):
        opening_signal_entry_rejection_reason(
            signal,
            now_ts_ns=2_000,
            stale_after_seconds=True,
        )
