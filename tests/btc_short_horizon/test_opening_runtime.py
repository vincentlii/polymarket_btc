from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from btc_short_horizon.backtest import to_opening_mispricing_signal
from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow
from btc_short_horizon.models import (
    DirectionModelConfig,
    FittedDirectionModel,
    ModelArtifactMetadata,
)
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_evidence import OpeningMarketObservation
from btc_short_horizon.research.opening_proxy import opening_proxy_feature_schema
from btc_short_horizon.research.opening_runtime import build_opening_proxy_prediction


_SECOND = 1_000_000_000


class _ConstantEstimator:
    def predict_proba(self, matrix: np.ndarray) -> np.ndarray:
        return np.tile(np.asarray([[0.27, 0.73]]), (len(matrix), 1))


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
        int((start - timedelta(hours=2)).timestamp() * _SECOND),
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


def _model_and_metadata() -> tuple[FittedDirectionModel, ModelArtifactMetadata]:
    schema = opening_proxy_feature_schema(1)
    model = FittedDirectionModel(
        schema=schema,
        config=DirectionModelConfig(),
        estimator=_ConstantEstimator(),
        calibrator=_IdentityCalibrator(),
    )
    metadata = ModelArtifactMetadata(
        model_id="btc-opening-early30-v1",
        feature_schema_hash=schema.hash,
        training_start_ns=1,
        training_end_ns=2,
        calibration_start_ns=3,
        calibration_end_ns=4,
        data_hash="d" * 64,
        code_revision="code-revision",
        config=model.config_dict,
    )
    return model, metadata


def test_runtime_prediction_uses_proxy_features_and_preserves_market_quality() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    decision_ts_ns = int((start + timedelta(seconds=5)).timestamp() * _SECOND)
    market = _market(start)
    observation = OpeningMarketObservation(
        market_slug=market.slug,
        decision_ts_ns=decision_ts_ns,
        p_market_mid_up=0.58,
        data_age_seconds=0.25,
        up_available_ts_ns=decision_ts_ns - 250_000_000,
        down_available_ts_ns=decision_ts_ns - 200_000_000,
        up_epoch_id=1,
        down_epoch_id=1,
        has_data_gap=True,
        structure_valid=False,
        tick_unchanged=False,
    )
    model, metadata = _model_and_metadata()

    prediction = build_opening_proxy_prediction(
        model=model,
        metadata=metadata,
        market=market,
        klines=_history(start),
        market_observation=observation,
    )
    signal = to_opening_mispricing_signal(prediction)

    assert signal.p_up == pytest.approx(0.73)
    assert signal.p_boundary_up > 0.5
    assert signal.p_market_mid_up == pytest.approx(0.58)
    assert signal.market_slug == market.slug
    assert signal.model_version == metadata.model_id
    assert signal.ts_event == decision_ts_ns
    assert signal.has_data_gap
    assert not signal.structure_valid
    assert not signal.tick_unchanged


def test_runtime_prediction_rejects_schema_or_market_mismatch() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    market = _market(start)
    decision_ts_ns = int((start + timedelta(seconds=5)).timestamp() * _SECOND)
    observation = OpeningMarketObservation(
        market_slug="different-market",
        decision_ts_ns=decision_ts_ns,
        p_market_mid_up=0.5,
        data_age_seconds=0.0,
        up_available_ts_ns=decision_ts_ns,
        down_available_ts_ns=decision_ts_ns,
        up_epoch_id=0,
        down_epoch_id=0,
        has_data_gap=False,
        structure_valid=True,
        tick_unchanged=True,
    )
    model, metadata = _model_and_metadata()

    with pytest.raises(ValueError, match="market slug"):
        build_opening_proxy_prediction(
            model=model,
            metadata=metadata,
            market=market,
            klines=_history(start),
            market_observation=observation,
        )

    bad_metadata = ModelArtifactMetadata(
        model_id=metadata.model_id,
        feature_schema_hash="f" * 64,
        training_start_ns=metadata.training_start_ns,
        training_end_ns=metadata.training_end_ns,
        calibration_start_ns=metadata.calibration_start_ns,
        calibration_end_ns=metadata.calibration_end_ns,
        data_hash=metadata.data_hash,
        code_revision=metadata.code_revision,
        config=metadata.config,
    )
    matching_observation = OpeningMarketObservation(
        market_slug=market.slug,
        decision_ts_ns=decision_ts_ns,
        p_market_mid_up=0.5,
        data_age_seconds=0.0,
        up_available_ts_ns=decision_ts_ns,
        down_available_ts_ns=decision_ts_ns,
        up_epoch_id=0,
        down_epoch_id=0,
        has_data_gap=False,
        structure_valid=True,
        tick_unchanged=True,
    )
    with pytest.raises(ValueError, match="schema"):
        build_opening_proxy_prediction(
            model=model,
            metadata=bad_metadata,
            market=market,
            klines=_history(start),
            market_observation=matching_observation,
        )
