"""Nautilus custom-data bridge for causal Opening Mispricing predictions."""

from math import isfinite

from nautilus_trader.core import Data
from nautilus_trader.model.custom import customdataclass

from btc_short_horizon.models import OpeningMispricingPrediction


@customdataclass
class BtcOpeningMispricingSignal(Data):
    """One causal opening-window estimate delivered to the joint L2 strategy."""

    market_slug: str = ""
    model_version: str = ""
    feature_schema_hash: str = ""
    market_window_start_ts_ns: int = 0
    p_up: float = 0.5
    p_boundary_up: float = 0.5
    p_market_mid_up: float = 0.5
    data_age_seconds: float = 0.0
    has_data_gap: bool = False
    structure_valid: bool = True
    tick_unchanged: bool = True
    fee_unchanged: bool = True
    latency_healthy: bool = True


def to_opening_mispricing_signal(
    prediction: OpeningMispricingPrediction,
) -> BtcOpeningMispricingSignal:
    return BtcOpeningMispricingSignal(
        market_slug=prediction.market_slug,
        model_version=prediction.model_version,
        feature_schema_hash=prediction.feature_schema_hash,
        market_window_start_ts_ns=prediction.market_window_start_ts_ns,
        p_up=prediction.p_up,
        p_boundary_up=prediction.p_boundary_up,
        p_market_mid_up=prediction.p_market_mid_up,
        data_age_seconds=prediction.data_age_seconds,
        has_data_gap=prediction.has_data_gap,
        structure_valid=prediction.structure_valid,
        tick_unchanged=prediction.tick_unchanged,
        fee_unchanged=prediction.fee_unchanged,
        latency_healthy=prediction.latency_healthy,
        ts_event=prediction.trigger_ts_ns,
        ts_init=prediction.trigger_ts_ns,
    )


def validate_opening_mispricing_signal(signal: BtcOpeningMispricingSignal) -> None:
    if not signal.market_slug or not signal.model_version or not signal.feature_schema_hash:
        raise ValueError("market_slug, model_version, and feature_schema_hash are required")
    if signal.market_window_start_ts_ns < 0 or signal.ts_init < signal.market_window_start_ts_ns:
        raise ValueError("signal timestamps must be ordered and non-negative")
    for name in ("p_up", "p_boundary_up", "p_market_mid_up"):
        value = float(getattr(signal, name))
        if not isfinite(value) or not 0.0 < value < 1.0:
            raise ValueError(f"{name} must be finite and in (0, 1)")
    if not isfinite(float(signal.data_age_seconds)) or float(signal.data_age_seconds) < 0.0:
        raise ValueError("data_age_seconds must be finite and >= 0")
    for name in (
        "has_data_gap",
        "structure_valid",
        "tick_unchanged",
        "fee_unchanged",
        "latency_healthy",
    ):
        if not isinstance(getattr(signal, name), bool):
            raise TypeError(f"{name} must be bool")


def opening_signal_data_age_seconds(signal: BtcOpeningMispricingSignal, *, now_ts_ns: int) -> float:
    """Return the signal's age at the decision time, including elapsed replay time."""

    validate_opening_mispricing_signal(signal)
    if now_ts_ns < signal.ts_init:
        raise ValueError("now_ts_ns cannot precede signal ts_init")
    return float(signal.data_age_seconds) + (now_ts_ns - int(signal.ts_init)) / 1_000_000_000


def opening_signal_entry_rejection_reason(
    signal: BtcOpeningMispricingSignal,
    *,
    now_ts_ns: int,
    stale_after_seconds: float,
) -> str | None:
    """Reject unsafe input before a one-cycle passive order can be planned."""

    if stale_after_seconds <= 0.0 or not isfinite(stale_after_seconds):
        raise ValueError("stale_after_seconds must be finite and > 0")
    if signal.has_data_gap:
        return "data_gap"
    if not signal.structure_valid:
        return "structure_invalid"
    if not signal.tick_unchanged:
        return "tick_changed"
    if not signal.fee_unchanged:
        return "fee_changed"
    if not signal.latency_healthy:
        return "latency_unhealthy"
    if opening_signal_data_age_seconds(signal, now_ts_ns=now_ts_ns) > stale_after_seconds:
        return "data_stale"
    return None
