"""Pre-registered Go/No-Go gates for fair probability, opening mispricing, and maker research."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Integral


_DIRECTION_MIN_HOLDOUT_MARKETS = 2_500
_DIRECTION_MIN_LOG_LOSS_IMPROVEMENT = 0.002
_DIRECTION_MIN_BRIER_IMPROVEMENT = 0.001
_DIRECTION_CALIBRATION_SLOPE_MIN = 0.8
_DIRECTION_CALIBRATION_SLOPE_MAX = 1.2
_DIRECTION_BIN_MIN_SAMPLES = 300
_DIRECTION_BIN_MAX_ERROR = 0.03

_MAKER_MIN_TOTAL_FILLS = 500
_MAKER_MIN_SIDE_FILLS = 150
_MAKER_MIN_NET_EV_PER_SHARE = 0.005
_MAKER_MIN_POSITIVE_WEEK_RATIO = 0.70
_MAKER_MAX_SINGLE_MONTH_PNL_SHARE = 0.50


@dataclass(frozen=True, slots=True)
class GateDecision:
    accepted: bool
    failed_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.failed_conditions, (str, bytes)):
            raise ValueError("failed conditions must be an iterable of strings")
        try:
            object.__setattr__(self, "failed_conditions", tuple(self.failed_conditions))
        except TypeError as exc:
            raise ValueError("failed conditions must be iterable") from exc
        if not isinstance(self.accepted, bool):
            raise ValueError("accepted must be boolean")
        if any(
            not isinstance(value, str) or not value or value.strip() != value
            for value in self.failed_conditions
        ):
            raise ValueError("failed conditions must be non-empty trimmed strings")
        if len(self.failed_conditions) != len(set(self.failed_conditions)):
            raise ValueError("failed conditions must be unique")
        if self.accepted == bool(self.failed_conditions):
            raise ValueError("accepted must be true exactly when there are no failed conditions")


@dataclass(frozen=True, slots=True)
class CalibrationBinEvidence:
    sample_count: int
    calibration_error: float

    def __post_init__(self) -> None:
        _require_nonnegative_integer(self.sample_count, "sample_count")
        _require_finite_nonnegative(self.calibration_error, "calibration_error")
        if self.calibration_error > 1.0:
            raise ValueError("calibration_error must lie in [0, 1]")


@dataclass(frozen=True, slots=True)
class DirectionGateEvidence:
    sealed_holdout_markets: int
    log_loss_improvement: float
    log_loss_ci_lower: float
    brier_improvement: float
    brier_ci_lower: float
    calibration_slope: float
    target_bins: tuple[CalibrationBinEvidence, ...]

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "target_bins", tuple(self.target_bins))
        except TypeError as exc:
            raise ValueError("target_bins must be iterable") from exc
        _require_nonnegative_integer(self.sealed_holdout_markets, "sealed_holdout_markets")
        if len(self.target_bins) != 4:
            raise ValueError("target_bins must contain the four pre-registered confidence bands")
        if any(not isinstance(value, CalibrationBinEvidence) for value in self.target_bins):
            raise ValueError("target_bins must contain CalibrationBinEvidence values")
        for name in (
            "log_loss_improvement",
            "log_loss_ci_lower",
            "brier_improvement",
            "brier_ci_lower",
            "calibration_slope",
        ):
            _require_finite(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class OpeningMispricingGateEvidence:
    paired_net_edge_ci_lower: float

    def __post_init__(self) -> None:
        _require_finite(self.paired_net_edge_ci_lower, "paired_net_edge_ci_lower")


@dataclass(frozen=True, slots=True)
class MakerGateEvidence:
    total_fills: int
    up_fills: int
    down_fills: int
    net_ev_per_filled_share: float
    net_ev_ci_lower: float
    pnl_without_top_one_percent: float
    positive_nonoverlap_week_ratio: float
    largest_month_pnl_share: float
    capacity_net_edge_ci_lower: float
    rebate_free: bool
    pessimistic_queue: bool
    p99_latency: bool
    trade_order_robust: bool

    def __post_init__(self) -> None:
        for name in ("total_fills", "up_fills", "down_fills"):
            _require_nonnegative_integer(getattr(self, name), name)
        for name in (
            "net_ev_per_filled_share",
            "net_ev_ci_lower",
            "pnl_without_top_one_percent",
            "positive_nonoverlap_week_ratio",
            "largest_month_pnl_share",
            "capacity_net_edge_ci_lower",
        ):
            _require_finite(getattr(self, name), name)
        for name in ("positive_nonoverlap_week_ratio", "largest_month_pnl_share"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must lie in [0, 1]")


def evaluate_direction_gate(evidence: DirectionGateEvidence) -> GateDecision:
    failures: list[str] = []
    if evidence.sealed_holdout_markets < _DIRECTION_MIN_HOLDOUT_MARKETS:
        failures.append("insufficient_sealed_holdout_markets")
    if evidence.log_loss_improvement < _DIRECTION_MIN_LOG_LOSS_IMPROVEMENT:
        failures.append("log_loss_improvement_below_threshold")
    if evidence.log_loss_ci_lower <= 0.0:
        failures.append("log_loss_ci_lower_not_positive")
    if evidence.brier_improvement < _DIRECTION_MIN_BRIER_IMPROVEMENT:
        failures.append("brier_improvement_below_threshold")
    if evidence.brier_ci_lower <= 0.0:
        failures.append("brier_ci_lower_not_positive")
    if (
        not _DIRECTION_CALIBRATION_SLOPE_MIN
        <= evidence.calibration_slope
        <= _DIRECTION_CALIBRATION_SLOPE_MAX
    ):
        failures.append("calibration_slope_out_of_range")
    for bin_evidence in evidence.target_bins:
        if bin_evidence.sample_count < _DIRECTION_BIN_MIN_SAMPLES:
            failures.append("target_bin_insufficient_samples")
        if bin_evidence.calibration_error > _DIRECTION_BIN_MAX_ERROR:
            failures.append("target_bin_calibration_error_exceeds_limit")
    return _decision(failures)


def evaluate_opening_mispricing_gate(evidence: OpeningMispricingGateEvidence) -> GateDecision:
    return _decision(
        []
        if evidence.paired_net_edge_ci_lower > 0.0
        else ["paired_net_edge_ci_lower_must_be_positive"]
    )


def evaluate_maker_gate(evidence: MakerGateEvidence) -> GateDecision:
    failures: list[str] = []
    if evidence.total_fills < _MAKER_MIN_TOTAL_FILLS:
        failures.append("insufficient_total_fills")
    if evidence.up_fills < _MAKER_MIN_SIDE_FILLS:
        failures.append("insufficient_up_fills")
    if evidence.down_fills < _MAKER_MIN_SIDE_FILLS:
        failures.append("insufficient_down_fills")
    if evidence.net_ev_per_filled_share < _MAKER_MIN_NET_EV_PER_SHARE:
        failures.append("net_ev_per_share_below_threshold")
    if evidence.net_ev_ci_lower <= 0.0:
        failures.append("net_ev_ci_lower_not_positive")
    if evidence.pnl_without_top_one_percent <= 0.0:
        failures.append("top_one_percent_removal_not_positive")
    if evidence.positive_nonoverlap_week_ratio < _MAKER_MIN_POSITIVE_WEEK_RATIO:
        failures.append("insufficient_positive_test_weeks")
    if evidence.largest_month_pnl_share > _MAKER_MAX_SINGLE_MONTH_PNL_SHARE:
        failures.append("single_month_pnl_concentration")
    if evidence.capacity_net_edge_ci_lower <= 0.0:
        failures.append("capacity_ci_lower_not_positive")
    if not evidence.rebate_free:
        failures.append("rebate_free_requirement_failed")
    if not evidence.pessimistic_queue:
        failures.append("pessimistic_queue_requirement_failed")
    if not evidence.p99_latency:
        failures.append("p99_latency_requirement_failed")
    if not evidence.trade_order_robust:
        failures.append("trade_ordering_not_robust")
    return _decision(failures)


def _decision(failures: list[str]) -> GateDecision:
    unique_failures = tuple(dict.fromkeys(failures))
    return GateDecision(accepted=not unique_failures, failed_conditions=unique_failures)


def _require_finite(value: float, name: str) -> None:
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")


def _require_finite_nonnegative(value: float, name: str) -> None:
    _require_finite(value, name)
    if value < 0.0:
        raise ValueError(f"{name} must be >= 0")


def _require_nonnegative_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


__all__ = [
    "CalibrationBinEvidence",
    "DirectionGateEvidence",
    "GateDecision",
    "MakerGateEvidence",
    "OpeningMispricingGateEvidence",
    "evaluate_direction_gate",
    "evaluate_maker_gate",
    "evaluate_opening_mispricing_gate",
]
