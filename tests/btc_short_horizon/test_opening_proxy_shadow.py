from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow
from btc_short_horizon.models import (
    DirectionModelConfig,
    FittedDirectionModel,
    ModelArtifactMetadata,
)
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_evidence import (
    ForwardBookEventLoad,
    OpeningMarketObservation,
)
from btc_short_horizon.research.opening_proxy import opening_proxy_feature_schema
import scripts.btc_opening_proxy_shadow as shadow
from scripts.btc_opening_proxy_shadow import (
    _shadow_regime_coverage,
    build_shadow_predictions,
    shadow_bootstrap_window,
)


_SECOND = 1_000_000_000


class _ConstantEstimator:
    def predict_proba(self, matrix: np.ndarray) -> np.ndarray:
        return np.tile(np.asarray([[0.35, 0.65]]), (len(matrix), 1))


class _IdentityCalibrator:
    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        return probabilities


def _market(start: datetime) -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(start),
        condition_id="condition",
        up_token_id="up",
        down_token_id="down",
        t0=start,
        t1=start + timedelta(minutes=15),
        rule_epoch="chainlink-btc-usd-v1",
        rule_hash="a" * 64,
    )


def _history(start: datetime) -> BinanceKlineHistory:
    opens = np.arange(
        int((start - timedelta(hours=1)).timestamp() * _SECOND),
        int((start + timedelta(minutes=1)).timestamp() * _SECOND),
        _SECOND,
        dtype=np.int64,
    )
    close = 100_000.0 + np.arange(len(opens), dtype=float)
    return BinanceKlineHistory(
        open_ts_ns=opens,
        close=close,
        volume=np.ones(len(opens)),
        quote_volume=close,
        taker_buy_volume=np.full(len(opens), 0.6),
    )


def _model() -> tuple[FittedDirectionModel, ModelArtifactMetadata]:
    schema = opening_proxy_feature_schema(1)
    model = FittedDirectionModel(
        schema=schema,
        config=DirectionModelConfig(),
        estimator=_ConstantEstimator(),
        calibrator=_IdentityCalibrator(),
    )
    return model, ModelArtifactMetadata(
        model_id="opening-shadow-test",
        feature_schema_hash=schema.hash,
        training_start_ns=1,
        training_end_ns=2,
        calibration_start_ns=3,
        calibration_end_ns=4,
        data_hash="data-hash",
        code_revision="revision",
        config=model.config_dict,
    )


def test_shadow_builds_replay_ready_signals_without_submitting_orders() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    market = _market(start)
    observations = tuple(
        OpeningMarketObservation(
            market_slug=market.slug,
            decision_ts_ns=int((start + timedelta(seconds=elapsed)).timestamp() * _SECOND),
            p_market_mid_up=0.55,
            data_age_seconds=0.1,
            up_available_ts_ns=int(
                (start + timedelta(seconds=elapsed, milliseconds=-100)).timestamp() * _SECOND
            ),
            down_available_ts_ns=int(
                (start + timedelta(seconds=elapsed, milliseconds=-100)).timestamp() * _SECOND
            ),
            up_epoch_id=0,
            down_epoch_id=0,
            has_data_gap=False,
            structure_valid=True,
            tick_unchanged=True,
        )
        for elapsed in (5, 10)
    )
    model, metadata = _model()

    predictions, signals = build_shadow_predictions(
        model=model,
        metadata=metadata,
        market=market,
        klines=_history(start),
        observations=observations,
    )

    assert [prediction.p_up for prediction in predictions] == pytest.approx([0.65, 0.65])
    assert [signal.ts_event for signal in signals] == [
        observation.decision_ts_ns for observation in observations
    ]
    assert all(signal.model_version == metadata.model_id for signal in signals)

    coverage = _shadow_regime_coverage(
        market=market,
        decisions=[item.decision_ts_ns for item in observations],
        observations=observations,
        predictions=predictions,
        stale_after_seconds=1.0,
    )
    assert sum(item["predictions"] for item in coverage.values()) == 2


def test_shadow_bootstrap_window_stays_inside_four_rest_pages() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)

    bootstrap_start, bootstrap_end = shadow_bootstrap_window(
        market_start=start,
        last_decision_time=start + timedelta(seconds=180),
        max_lookback_seconds=3_600,
        availability_delay=timedelta(seconds=1),
    )

    assert bootstrap_start == start - timedelta(hours=1)
    assert bootstrap_end == start + timedelta(seconds=182)
    assert bootstrap_end - bootstrap_start < timedelta(seconds=4_000)


def test_shadow_rejects_mismatched_up_down_timestamp_tolerances(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    market = _market(start)
    model, metadata = _model()
    expected_tolerances: list[object] = []

    monkeypatch.setattr(
        shadow,
        "read_market_catalog",
        lambda _path: SimpleNamespace(require=lambda _slug: market),
    )
    monkeypatch.setattr(
        shadow.ModelArtifactStore,
        "load",
        lambda **_kwargs: (model, metadata),
    )
    monkeypatch.setattr(shadow, "validate_opening_proxy_protocol", lambda *_args, **_kwargs: None)

    def load_book(**kwargs: object) -> ForwardBookEventLoad:
        expected_tolerances.append(
            kwargs.get("expected_source_timestamp_regression_tolerance_seconds")
        )
        token_id = str(kwargs["token_id"])
        tolerance = 0.25 if token_id == market.up_token_id else 0.5
        return ForwardBookEventLoad(
            token_id=token_id,
            events=(),
            raw_part_count=1,
            raw_row_count=1,
            duplicate_row_count=0,
            awaiting_snapshot_count=0,
            polymarket_source_timestamp_regression_tolerance_seconds=tolerance,
        )

    monkeypatch.setattr(shadow, "load_forward_polymarket_book_events", load_book)
    args = argparse.Namespace(
        config=Path("configs/btc_short_horizon/baseline.toml"),
        market_catalog=tmp_path / "catalog.parquet",
        market_slug=market.slug,
        model_directory=tmp_path / "model",
        output_directory=tmp_path / "output",
        raw_data_root=tmp_path / "raw",
        book_lookback_seconds=300,
        availability_delay_seconds=1.0,
        as_of=start + timedelta(minutes=5),
    )

    with pytest.raises(ValueError, match="different Polymarket timestamp tolerances"):
        asyncio.run(shadow.run_async(args))

    assert expected_tolerances == [1.0, 1.0]
