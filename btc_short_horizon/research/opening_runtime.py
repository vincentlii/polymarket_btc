"""Runtime bridge from the frozen opening proxy to replay/live predictions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from btc_short_horizon.data import MarketWindow
from btc_short_horizon.models import (
    FittedDirectionModel,
    FittedOpeningMispricingModel,
    ModelArtifactMetadata,
    OpeningMispricingPrediction,
)
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_evidence import OpeningMarketObservation
from btc_short_horizon.research.opening_proxy import opening_proxy_feature_values_at


def build_opening_proxy_prediction(
    *,
    model: FittedDirectionModel,
    metadata: ModelArtifactMetadata,
    market: MarketWindow,
    klines: BinanceKlineHistory,
    market_observation: OpeningMarketObservation,
    availability_delay: timedelta = timedelta(seconds=1),
    fee_unchanged: bool = True,
    latency_healthy: bool = True,
) -> OpeningMispricingPrediction:
    """Create one causal prediction; order planning remains in the strategy."""

    if market_observation.market_slug != market.slug:
        raise ValueError("market observation market slug does not match the market window")
    if model.schema.hash != metadata.feature_schema_hash:
        raise ValueError("model artifact feature schema does not match the loaded model")
    values = opening_proxy_feature_values_at(
        klines=klines,
        market_start=market.t0,
        decision_time=datetime.fromtimestamp(
            market_observation.decision_ts_ns / 1_000_000_000,
            tz=UTC,
        ),
        availability_delay=availability_delay,
    )
    fair_model = FittedOpeningMispricingModel(model=model)
    return fair_model.predict(
        market_slug=market.slug,
        model_version=metadata.model_id,
        market_window_start_ts_ns=int(market.t0.timestamp() * 1_000_000_000),
        trigger_ts_ns=market_observation.decision_ts_ns,
        feature_values=values,
        p_boundary_up=values["p_boundary_up"],
        p_market_mid_up=market_observation.p_market_mid_up,
        data_age_seconds=max(values["data_age_seconds"], market_observation.data_age_seconds),
        has_data_gap=market_observation.has_data_gap,
        structure_valid=market_observation.structure_valid,
        tick_unchanged=market_observation.tick_unchanged,
        fee_unchanged=fee_unchanged,
        latency_healthy=latency_healthy,
    )


__all__ = ["build_opening_proxy_prediction"]
