"""Validated, credential-free projection consumed by the read-only dashboard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
from math import isfinite
import os
from pathlib import Path
from uuid import uuid4


_DASHBOARD_SCHEMA_VERSION = 2


class HealthState(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


class StrategyStage(StrEnum):
    RESEARCH = "research"
    CHALLENGE = "challenge"
    SHADOW = "shadow"
    CANARY = "canary"
    LIVE = "live"


class GateState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    GO = "go"
    NO_GO = "no_go"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class HealthIndicator:
    key: str
    label: str
    state: HealthState
    detail: str
    updated_at: datetime
    latency_ms: float | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.key, "health key")
        _require_text(self.label, "health label")
        _require_text(self.detail, "health detail")
        if not isinstance(self.state, HealthState):
            object.__setattr__(self, "state", HealthState(self.state))
        object.__setattr__(self, "updated_at", _as_utc(self.updated_at, "updated_at"))
        _optional_nonnegative(self.latency_ms, "latency_ms")

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state.value,
            "detail": self.detail,
            "updated_at": self.updated_at.isoformat(),
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_json(cls, raw: object) -> HealthIndicator:
        value = _mapping(raw, "health indicator")
        return cls(
            key=_text(value.get("key"), "health key"),
            label=_text(value.get("label"), "health label"),
            state=HealthState(_text(value.get("state"), "health state")),
            detail=_text(value.get("detail"), "health detail"),
            updated_at=_timestamp(value.get("updated_at"), "updated_at"),
            latency_ms=_optional_float(value.get("latency_ms"), "latency_ms"),
        )


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _as_utc(self.timestamp, "timestamp"))
        _finite(self.equity, "equity")
        if self.equity < 0.0:
            raise ValueError("equity must be >= 0")

    def to_json(self) -> dict[str, object]:
        return {"timestamp": self.timestamp.isoformat(), "equity": self.equity}

    @classmethod
    def from_json(cls, raw: object) -> EquityPoint:
        value = _mapping(raw, "equity point")
        return cls(
            timestamp=_timestamp(value.get("timestamp"), "timestamp"),
            equity=_float(value.get("equity"), "equity"),
        )


@dataclass(frozen=True, slots=True)
class OrderPerformance:
    variant_id: str
    order_id: str
    market_slug: str
    side: str
    execution_status: str
    settlement_status: str
    placed_at: datetime
    shares: float
    filled_shares: float
    entry_price: float | None = None
    p_fair: float | None = None
    market_price: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    order_latency_ms: float | None = None
    terminal_reason: str | None = None
    execution_route: str = "maker"
    taker_fees: float = 0.0
    initial_queue_ahead: float = 0.0
    remaining_queue_ahead: float = 0.0
    opportunity_id: str | None = None
    entry_regime: str | None = None
    price_bucket: str | None = None
    go_eligible: bool | None = None
    decision_best_ask: float | None = None
    signal_edge_decay: float | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.variant_id, "variant_id")
        _require_text(self.order_id, "order_id")
        _require_text(self.market_slug, "market_slug")
        if self.side not in {"up", "down"}:
            raise ValueError("side must be 'up' or 'down'")
        _require_text(self.execution_status, "execution_status")
        _require_text(self.settlement_status, "settlement_status")
        _require_text(self.execution_route, "execution_route")
        if self.terminal_reason is not None:
            _require_text(self.terminal_reason, "terminal_reason")
        object.__setattr__(self, "placed_at", _as_utc(self.placed_at, "placed_at"))
        _nonnegative(self.shares, "shares")
        _nonnegative(self.filled_shares, "filled_shares")
        if self.filled_shares > self.shares:
            raise ValueError("filled_shares must not exceed shares")
        for name, value in (
            ("entry_price", self.entry_price),
            ("p_fair", self.p_fair),
            ("market_price", self.market_price),
        ):
            if value is not None and (not isfinite(value) or not 0.0 < value < 1.0):
                raise ValueError(f"{name} must be in (0, 1) when provided")
        _optional_finite(self.realized_pnl, "realized_pnl")
        _optional_finite(self.unrealized_pnl, "unrealized_pnl")
        _optional_nonnegative(self.order_latency_ms, "order_latency_ms")
        _nonnegative(self.taker_fees, "taker_fees")
        _nonnegative(self.initial_queue_ahead, "initial_queue_ahead")
        _nonnegative(self.remaining_queue_ahead, "remaining_queue_ahead")
        for name in ("opportunity_id", "entry_regime", "price_bucket"):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)
        if self.go_eligible is not None and not isinstance(self.go_eligible, bool):
            raise ValueError("go_eligible must be bool when provided")
        if self.decision_best_ask is not None and (
            not isfinite(self.decision_best_ask) or not 0.0 < self.decision_best_ask < 1.0
        ):
            raise ValueError("decision_best_ask must be in (0, 1) when provided")
        _optional_finite(self.signal_edge_decay, "signal_edge_decay")

    def to_json(self) -> dict[str, object]:
        return {
            "variant_id": self.variant_id,
            "order_id": self.order_id,
            "market_slug": self.market_slug,
            "side": self.side,
            "execution_status": self.execution_status,
            "settlement_status": self.settlement_status,
            "placed_at": self.placed_at.isoformat(),
            "shares": self.shares,
            "filled_shares": self.filled_shares,
            "entry_price": self.entry_price,
            "p_fair": self.p_fair,
            "market_price": self.market_price,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "order_latency_ms": self.order_latency_ms,
            "terminal_reason": self.terminal_reason,
            "execution_route": self.execution_route,
            "taker_fees": self.taker_fees,
            "initial_queue_ahead": self.initial_queue_ahead,
            "remaining_queue_ahead": self.remaining_queue_ahead,
            "opportunity_id": self.opportunity_id,
            "entry_regime": self.entry_regime,
            "price_bucket": self.price_bucket,
            "go_eligible": self.go_eligible,
            "decision_best_ask": self.decision_best_ask,
            "signal_edge_decay": self.signal_edge_decay,
        }

    @classmethod
    def from_json(cls, raw: object) -> OrderPerformance:
        value = _mapping(raw, "order performance")
        return cls(
            variant_id=_text(value.get("variant_id"), "variant_id"),
            order_id=_text(value.get("order_id"), "order_id"),
            market_slug=_text(value.get("market_slug"), "market_slug"),
            side=_text(value.get("side"), "side"),
            execution_status=_text(value.get("execution_status"), "execution_status"),
            settlement_status=_text(value.get("settlement_status"), "settlement_status"),
            placed_at=_timestamp(value.get("placed_at"), "placed_at"),
            shares=_float(value.get("shares"), "shares"),
            filled_shares=_float(value.get("filled_shares"), "filled_shares"),
            entry_price=_optional_float(value.get("entry_price"), "entry_price"),
            p_fair=_optional_float(value.get("p_fair"), "p_fair"),
            market_price=_optional_float(value.get("market_price"), "market_price"),
            realized_pnl=_optional_float(value.get("realized_pnl"), "realized_pnl"),
            unrealized_pnl=_optional_float(value.get("unrealized_pnl"), "unrealized_pnl"),
            order_latency_ms=_optional_float(value.get("order_latency_ms"), "order_latency_ms"),
            terminal_reason=_optional_text(value.get("terminal_reason"), "terminal_reason"),
            execution_route=_text(value.get("execution_route"), "execution_route"),
            taker_fees=_float(value.get("taker_fees", 0.0), "taker_fees"),
            initial_queue_ahead=_float(
                value.get("initial_queue_ahead", 0.0), "initial_queue_ahead"
            ),
            remaining_queue_ahead=_float(
                value.get("remaining_queue_ahead", 0.0), "remaining_queue_ahead"
            ),
            opportunity_id=_optional_text(value.get("opportunity_id"), "opportunity_id"),
            entry_regime=_optional_text(value.get("entry_regime"), "entry_regime"),
            price_bucket=_optional_text(value.get("price_bucket"), "price_bucket"),
            go_eligible=_optional_bool(value.get("go_eligible"), "go_eligible"),
            decision_best_ask=_optional_float(value.get("decision_best_ask"), "decision_best_ask"),
            signal_edge_decay=_optional_float(value.get("signal_edge_decay"), "signal_edge_decay"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionSegmentPerformance:
    dimension: str
    key: str
    opportunity_count: int
    resolved_count: int
    fill_count: int
    realized_pnl: float
    resolved_ev_per_opportunity: float | None

    def __post_init__(self) -> None:
        _require_identifier(self.dimension, "segment dimension")
        _require_identifier(self.key, "segment key")
        for name in ("opportunity_count", "resolved_count", "fill_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        _finite(self.realized_pnl, "segment realized_pnl")
        _optional_finite(self.resolved_ev_per_opportunity, "resolved_ev_per_opportunity")

    def to_json(self) -> dict[str, object]:
        return {
            "dimension": self.dimension,
            "key": self.key,
            "opportunity_count": self.opportunity_count,
            "resolved_count": self.resolved_count,
            "fill_count": self.fill_count,
            "realized_pnl": self.realized_pnl,
            "resolved_ev_per_opportunity": self.resolved_ev_per_opportunity,
        }

    @classmethod
    def from_json(cls, raw: object) -> ExecutionSegmentPerformance:
        value = _mapping(raw, "execution segment performance")
        return cls(
            dimension=_text(value.get("dimension"), "segment dimension"),
            key=_text(value.get("key"), "segment key"),
            opportunity_count=_integer(value.get("opportunity_count"), "opportunity_count"),
            resolved_count=_integer(value.get("resolved_count"), "resolved_count"),
            fill_count=_integer(value.get("fill_count"), "fill_count"),
            realized_pnl=_float(value.get("realized_pnl"), "segment realized_pnl"),
            resolved_ev_per_opportunity=_optional_float(
                value.get("resolved_ev_per_opportunity"), "resolved_ev_per_opportunity"
            ),
        )


@dataclass(frozen=True, slots=True)
class DirectionStageSummary:
    stage: str
    paired_market_count: int
    actual_up_count: int
    actual_down_count: int
    predicted_up_count: int
    predicted_down_count: int
    mean_p_up: float | None
    calibration_z: float | None
    bias_state: str

    def __post_init__(self) -> None:
        _require_identifier(self.stage, "direction stage")
        _require_identifier(self.bias_state, "direction bias state")
        for name in (
            "paired_market_count",
            "actual_up_count",
            "actual_down_count",
            "predicted_up_count",
            "predicted_down_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.actual_up_count + self.actual_down_count != self.paired_market_count:
            raise ValueError("actual direction counts must equal paired markets")
        if self.predicted_up_count + self.predicted_down_count != self.paired_market_count:
            raise ValueError("predicted direction counts must equal paired markets")
        _optional_probability(self.mean_p_up, "mean_p_up")
        _optional_finite(self.calibration_z, "calibration_z")

    def to_json(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_json(cls, raw: object) -> DirectionStageSummary:
        value = _mapping(raw, "direction stage summary")
        return cls(
            stage=_text(value.get("stage"), "direction stage"),
            paired_market_count=_integer(value.get("paired_market_count"), "paired_market_count"),
            actual_up_count=_integer(value.get("actual_up_count"), "actual_up_count"),
            actual_down_count=_integer(value.get("actual_down_count"), "actual_down_count"),
            predicted_up_count=_integer(value.get("predicted_up_count"), "predicted_up_count"),
            predicted_down_count=_integer(
                value.get("predicted_down_count"), "predicted_down_count"
            ),
            mean_p_up=_optional_float(value.get("mean_p_up"), "mean_p_up"),
            calibration_z=_optional_float(value.get("calibration_z"), "calibration_z"),
            bias_state=_text(value.get("bias_state"), "direction bias state"),
        )


@dataclass(frozen=True, slots=True)
class DirectionHealthSnapshot:
    scope: str
    coverage_started_at: datetime
    activated_market_count: int
    resolved_market_count: int
    paired_market_count: int
    prediction_count: int
    actual_up_count: int
    actual_down_count: int
    predicted_up_count: int
    predicted_down_count: int
    mean_p_up: float | None
    calibration_z: float | None
    bias_state: str
    stage_summaries: tuple[DirectionStageSummary, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.scope, "direction health scope")
        _require_identifier(self.bias_state, "direction bias state")
        object.__setattr__(
            self,
            "coverage_started_at",
            _as_utc(self.coverage_started_at, "coverage_started_at"),
        )
        for name in (
            "activated_market_count",
            "resolved_market_count",
            "paired_market_count",
            "prediction_count",
            "actual_up_count",
            "actual_down_count",
            "predicted_up_count",
            "predicted_down_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.resolved_market_count > self.activated_market_count:
            raise ValueError("resolved markets cannot exceed activated markets")
        if self.paired_market_count > self.resolved_market_count:
            raise ValueError("paired markets cannot exceed resolved markets")
        if self.actual_up_count + self.actual_down_count != self.paired_market_count:
            raise ValueError("actual direction counts must equal paired markets")
        if self.predicted_up_count + self.predicted_down_count != self.paired_market_count:
            raise ValueError("predicted direction counts must equal paired markets")
        _optional_probability(self.mean_p_up, "mean_p_up")
        _optional_finite(self.calibration_z, "calibration_z")
        object.__setattr__(self, "stage_summaries", tuple(self.stage_summaries))

    def to_json(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "coverage_started_at": self.coverage_started_at.isoformat(),
            "activated_market_count": self.activated_market_count,
            "resolved_market_count": self.resolved_market_count,
            "paired_market_count": self.paired_market_count,
            "prediction_count": self.prediction_count,
            "actual_up_count": self.actual_up_count,
            "actual_down_count": self.actual_down_count,
            "predicted_up_count": self.predicted_up_count,
            "predicted_down_count": self.predicted_down_count,
            "mean_p_up": self.mean_p_up,
            "calibration_z": self.calibration_z,
            "bias_state": self.bias_state,
            "stage_summaries": [item.to_json() for item in self.stage_summaries],
        }

    @classmethod
    def from_json(cls, raw: object) -> DirectionHealthSnapshot:
        value = _mapping(raw, "direction health snapshot")
        return cls(
            scope=_text(value.get("scope"), "direction health scope"),
            coverage_started_at=_timestamp(value.get("coverage_started_at"), "coverage_started_at"),
            activated_market_count=_integer(
                value.get("activated_market_count"), "activated_market_count"
            ),
            resolved_market_count=_integer(
                value.get("resolved_market_count"), "resolved_market_count"
            ),
            paired_market_count=_integer(value.get("paired_market_count"), "paired_market_count"),
            prediction_count=_integer(value.get("prediction_count"), "prediction_count"),
            actual_up_count=_integer(value.get("actual_up_count"), "actual_up_count"),
            actual_down_count=_integer(value.get("actual_down_count"), "actual_down_count"),
            predicted_up_count=_integer(value.get("predicted_up_count"), "predicted_up_count"),
            predicted_down_count=_integer(
                value.get("predicted_down_count"), "predicted_down_count"
            ),
            mean_p_up=_optional_float(value.get("mean_p_up"), "mean_p_up"),
            calibration_z=_optional_float(value.get("calibration_z"), "calibration_z"),
            bias_state=_text(value.get("bias_state"), "direction bias state"),
            stage_summaries=tuple(
                DirectionStageSummary.from_json(item)
                for item in _sequence(value.get("stage_summaries", ()), "stage_summaries")
            ),
        )


@dataclass(frozen=True, slots=True)
class DirectionExecutionPerformance:
    side: str
    qualified_signal_count: int
    opportunity_count: int
    fill_count: int
    resolved_opportunity_count: int
    realized_pnl: float
    mean_fair_probability: float | None
    realized_accuracy: float | None
    calibration_gap: float | None
    resolved_ev_per_opportunity: float | None

    def __post_init__(self) -> None:
        if self.side not in {"up", "down"}:
            raise ValueError("direction side must be 'up' or 'down'")
        for name in (
            "qualified_signal_count",
            "opportunity_count",
            "fill_count",
            "resolved_opportunity_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.fill_count > self.opportunity_count:
            raise ValueError("direction fills cannot exceed opportunities")
        if self.resolved_opportunity_count > self.opportunity_count:
            raise ValueError("resolved direction opportunities cannot exceed opportunities")
        _finite(self.realized_pnl, "direction realized_pnl")
        _optional_probability(self.mean_fair_probability, "mean_fair_probability")
        _optional_probability(self.realized_accuracy, "realized_accuracy")
        _optional_finite(self.calibration_gap, "calibration_gap")
        _optional_finite(self.resolved_ev_per_opportunity, "resolved_ev_per_opportunity")

    def to_json(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_json(cls, raw: object) -> DirectionExecutionPerformance:
        value = _mapping(raw, "direction execution performance")
        return cls(
            side=_text(value.get("side"), "direction side"),
            qualified_signal_count=_integer(
                value.get("qualified_signal_count"), "qualified_signal_count"
            ),
            opportunity_count=_integer(value.get("opportunity_count"), "opportunity_count"),
            fill_count=_integer(value.get("fill_count"), "fill_count"),
            resolved_opportunity_count=_integer(
                value.get("resolved_opportunity_count"), "resolved_opportunity_count"
            ),
            realized_pnl=_float(value.get("realized_pnl"), "direction realized_pnl"),
            mean_fair_probability=_optional_float(
                value.get("mean_fair_probability"), "mean_fair_probability"
            ),
            realized_accuracy=_optional_float(value.get("realized_accuracy"), "realized_accuracy"),
            calibration_gap=_optional_float(value.get("calibration_gap"), "calibration_gap"),
            resolved_ev_per_opportunity=_optional_float(
                value.get("resolved_ev_per_opportunity"), "resolved_ev_per_opportunity"
            ),
        )


@dataclass(frozen=True, slots=True)
class ExecutionVariantPerformance:
    variant_id: str
    label: str
    policy: str
    primary: bool
    starting_balance: float
    equity: float
    realized_pnl: float
    order_count: int
    fill_count: int
    taker_fees: float
    enabled: bool = True
    opportunity_count: int = 0
    evaluation_count: int = 0
    qualified_signal_count: int = 0
    resolved_opportunity_count: int = 0
    core_resolved_opportunity_count: int = 0
    tail_resolved_opportunity_count: int = 0
    resolved_ev_per_opportunity: float | None = None
    core_resolved_ev_per_opportunity: float | None = None
    tail_resolved_ev_per_opportunity: float | None = None
    conditional_ev_per_filled_share: float | None = None
    segment_summaries: tuple[ExecutionSegmentPerformance, ...] = ()
    direction_summaries: tuple[DirectionExecutionPerformance, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.variant_id, "variant_id")
        _require_text(self.label, "variant label")
        _require_identifier(self.policy, "variant policy")
        if not isinstance(self.primary, bool):
            raise ValueError("variant primary must be bool")
        if not isinstance(self.enabled, bool):
            raise ValueError("variant enabled must be bool")
        for name in ("opportunity_count", "evaluation_count", "qualified_signal_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"variant {name} must be non-negative")
        if self.qualified_signal_count > self.evaluation_count:
            raise ValueError("qualified signals cannot exceed evaluations")
        _nonnegative(self.starting_balance, "starting_balance")
        _nonnegative(self.equity, "equity")
        _finite(self.realized_pnl, "realized_pnl")
        _nonnegative(self.taker_fees, "taker_fees")
        for name, value in (("order_count", self.order_count), ("fill_count", self.fill_count)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "resolved_opportunity_count",
            "core_resolved_opportunity_count",
            "tail_resolved_opportunity_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        _optional_finite(self.resolved_ev_per_opportunity, "resolved_ev_per_opportunity")
        _optional_finite(
            self.core_resolved_ev_per_opportunity,
            "core_resolved_ev_per_opportunity",
        )
        _optional_finite(
            self.tail_resolved_ev_per_opportunity,
            "tail_resolved_ev_per_opportunity",
        )
        _optional_finite(
            self.conditional_ev_per_filled_share,
            "conditional_ev_per_filled_share",
        )
        object.__setattr__(self, "segment_summaries", tuple(self.segment_summaries))
        direction_summaries = tuple(self.direction_summaries)
        if len({item.side for item in direction_summaries}) != len(direction_summaries):
            raise ValueError("direction summaries must have unique sides")
        object.__setattr__(self, "direction_summaries", direction_summaries)

    @property
    def fill_rate(self) -> float | None:
        return None if self.order_count == 0 else self.fill_count / self.order_count

    def to_json(self) -> dict[str, object]:
        return {
            "variant_id": self.variant_id,
            "label": self.label,
            "policy": self.policy,
            "primary": self.primary,
            "enabled": self.enabled,
            "opportunity_count": self.opportunity_count,
            "evaluation_count": self.evaluation_count,
            "qualified_signal_count": self.qualified_signal_count,
            "starting_balance": self.starting_balance,
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "order_count": self.order_count,
            "fill_count": self.fill_count,
            "fill_rate": self.fill_rate,
            "taker_fees": self.taker_fees,
            "resolved_opportunity_count": self.resolved_opportunity_count,
            "core_resolved_opportunity_count": self.core_resolved_opportunity_count,
            "tail_resolved_opportunity_count": self.tail_resolved_opportunity_count,
            "resolved_ev_per_opportunity": self.resolved_ev_per_opportunity,
            "core_resolved_ev_per_opportunity": self.core_resolved_ev_per_opportunity,
            "tail_resolved_ev_per_opportunity": self.tail_resolved_ev_per_opportunity,
            "conditional_ev_per_filled_share": self.conditional_ev_per_filled_share,
            "segment_summaries": [item.to_json() for item in self.segment_summaries],
            "direction_summaries": [item.to_json() for item in self.direction_summaries],
        }

    @classmethod
    def from_json(cls, raw: object) -> ExecutionVariantPerformance:
        value = _mapping(raw, "execution variant performance")
        return cls(
            variant_id=_text(value.get("variant_id"), "variant_id"),
            label=_text(value.get("label"), "variant label"),
            policy=_text(value.get("policy"), "variant policy"),
            primary=value.get("primary"),
            starting_balance=_float(value.get("starting_balance"), "starting_balance"),
            equity=_float(value.get("equity"), "equity"),
            realized_pnl=_float(value.get("realized_pnl"), "realized_pnl"),
            order_count=_integer(value.get("order_count"), "order_count"),
            fill_count=_integer(value.get("fill_count"), "fill_count"),
            taker_fees=_float(value.get("taker_fees", 0.0), "taker_fees"),
            enabled=(
                _optional_bool(value.get("enabled"), "variant enabled")
                if value.get("enabled") is not None
                else True
            ),
            opportunity_count=_integer(value.get("opportunity_count", 0), "opportunity_count"),
            evaluation_count=_integer(value.get("evaluation_count", 0), "evaluation_count"),
            qualified_signal_count=_integer(
                value.get("qualified_signal_count", 0), "qualified_signal_count"
            ),
            resolved_opportunity_count=_integer(
                value.get("resolved_opportunity_count", 0), "resolved_opportunity_count"
            ),
            core_resolved_opportunity_count=_integer(
                value.get("core_resolved_opportunity_count", 0),
                "core_resolved_opportunity_count",
            ),
            tail_resolved_opportunity_count=_integer(
                value.get("tail_resolved_opportunity_count", 0),
                "tail_resolved_opportunity_count",
            ),
            resolved_ev_per_opportunity=_optional_float(
                value.get("resolved_ev_per_opportunity"), "resolved_ev_per_opportunity"
            ),
            core_resolved_ev_per_opportunity=_optional_float(
                value.get("core_resolved_ev_per_opportunity"),
                "core_resolved_ev_per_opportunity",
            ),
            tail_resolved_ev_per_opportunity=_optional_float(
                value.get("tail_resolved_ev_per_opportunity"),
                "tail_resolved_ev_per_opportunity",
            ),
            conditional_ev_per_filled_share=_optional_float(
                value.get("conditional_ev_per_filled_share"),
                "conditional_ev_per_filled_share",
            ),
            segment_summaries=tuple(
                ExecutionSegmentPerformance.from_json(item)
                for item in _sequence(value.get("segment_summaries", ()), "segment_summaries")
            ),
            direction_summaries=tuple(
                DirectionExecutionPerformance.from_json(item)
                for item in _sequence(value.get("direction_summaries", ()), "direction_summaries")
            ),
        )


@dataclass(frozen=True, slots=True)
class DecisionFunnelSnapshot:
    scope: str
    decision_ticks: int
    predictions: int
    evaluations: int
    qualified_signals: int
    confirmation_pending: int
    opportunities: int
    placements: int
    working: int
    rejected: int
    canceled: int
    fills: int
    resolved: int

    def __post_init__(self) -> None:
        _require_identifier(self.scope, "funnel scope")
        for name in (
            "decision_ticks",
            "predictions",
            "evaluations",
            "qualified_signals",
            "confirmation_pending",
            "opportunities",
            "placements",
            "working",
            "rejected",
            "canceled",
            "fills",
            "resolved",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not (
            self.decision_ticks
            >= self.predictions
            >= self.evaluations
            >= self.qualified_signals
            >= self.opportunities
            >= self.placements
            >= self.fills
        ):
            raise ValueError("decision funnel conversion counts must be monotonic")
        if self.resolved > self.opportunities:
            raise ValueError("resolved opportunities cannot exceed confirmed opportunities")

    def to_json(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_json(cls, raw: object) -> DecisionFunnelSnapshot:
        value = _mapping(raw, "decision funnel")
        return cls(
            scope=_text(value.get("scope"), "funnel scope"),
            **{
                name: _integer(value.get(name), name)
                for name in (
                    "decision_ticks",
                    "predictions",
                    "evaluations",
                    "qualified_signals",
                    "confirmation_pending",
                    "opportunities",
                    "placements",
                    "working",
                    "rejected",
                    "canceled",
                    "fills",
                    "resolved",
                )
            },
        )


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    currency: str = "USDC"
    starting_balance: float | None = None
    equity: float | None = None
    available_balance: float | None = None
    open_exposure: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    today_pnl: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    order_count: int = 0
    fill_count: int = 0
    equity_curve: tuple[EquityPoint, ...] = ()
    primary_variant_id: str | None = None
    variant_summaries: tuple[ExecutionVariantPerformance, ...] = ()
    recent_orders: tuple[OrderPerformance, ...] = ()
    paper_execution_epoch: str | None = None
    decision_funnel: DecisionFunnelSnapshot | None = None
    direction_health: DirectionHealthSnapshot | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.currency, "currency")
        for name, value in (
            ("starting_balance", self.starting_balance),
            ("equity", self.equity),
            ("available_balance", self.available_balance),
            ("open_exposure", self.open_exposure),
        ):
            _optional_nonnegative(value, name)
        for name, value in (
            ("realized_pnl", self.realized_pnl),
            ("unrealized_pnl", self.unrealized_pnl),
            ("today_pnl", self.today_pnl),
        ):
            _optional_finite(value, name)
        _optional_nonnegative(self.max_drawdown, "max_drawdown")
        if self.win_rate is not None and (
            not isfinite(self.win_rate) or not 0.0 <= self.win_rate <= 1.0
        ):
            raise ValueError("win_rate must be in [0, 1] when provided")
        for name, value in (("order_count", self.order_count), ("fill_count", self.fill_count)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        points = tuple(self.equity_curve)
        if self.primary_variant_id is not None:
            _require_identifier(self.primary_variant_id, "primary_variant_id")
        summaries = tuple(self.variant_summaries)
        orders = tuple(self.recent_orders)
        if self.paper_execution_epoch is not None:
            _require_identifier(self.paper_execution_epoch, "paper_execution_epoch")
        if summaries:
            primary_ids = {item.variant_id for item in summaries if item.primary}
            if primary_ids != {self.primary_variant_id}:
                raise ValueError("variant summaries must identify the configured primary variant")
        if any(right.timestamp < left.timestamp for left, right in zip(points, points[1:])):
            raise ValueError("equity_curve must be ordered by timestamp")
        object.__setattr__(self, "equity_curve", points)
        object.__setattr__(self, "variant_summaries", summaries)
        object.__setattr__(self, "recent_orders", orders)

    def to_json(self) -> dict[str, object]:
        return {
            "currency": self.currency,
            "starting_balance": self.starting_balance,
            "equity": self.equity,
            "available_balance": self.available_balance,
            "open_exposure": self.open_exposure,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "today_pnl": self.today_pnl,
            "max_drawdown": self.max_drawdown,
            "win_rate": self.win_rate,
            "order_count": self.order_count,
            "fill_count": self.fill_count,
            "equity_curve": [point.to_json() for point in self.equity_curve],
            "primary_variant_id": self.primary_variant_id,
            "variant_summaries": [item.to_json() for item in self.variant_summaries],
            "recent_orders": [order.to_json() for order in self.recent_orders],
            "paper_execution_epoch": self.paper_execution_epoch,
            "decision_funnel": (
                None if self.decision_funnel is None else self.decision_funnel.to_json()
            ),
            "direction_health": (
                None if self.direction_health is None else self.direction_health.to_json()
            ),
        }

    @classmethod
    def from_json(cls, raw: object) -> PerformanceSnapshot:
        value = _mapping(raw, "performance snapshot")
        return cls(
            currency=_text(value.get("currency", "USDC"), "currency"),
            starting_balance=_optional_float(value.get("starting_balance"), "starting_balance"),
            equity=_optional_float(value.get("equity"), "equity"),
            available_balance=_optional_float(value.get("available_balance"), "available_balance"),
            open_exposure=_optional_float(value.get("open_exposure"), "open_exposure"),
            realized_pnl=_optional_float(value.get("realized_pnl"), "realized_pnl"),
            unrealized_pnl=_optional_float(value.get("unrealized_pnl"), "unrealized_pnl"),
            today_pnl=_optional_float(value.get("today_pnl"), "today_pnl"),
            max_drawdown=_optional_float(value.get("max_drawdown"), "max_drawdown"),
            win_rate=_optional_float(value.get("win_rate"), "win_rate"),
            order_count=_integer(value.get("order_count", 0), "order_count"),
            fill_count=_integer(value.get("fill_count", 0), "fill_count"),
            equity_curve=tuple(
                EquityPoint.from_json(item)
                for item in _sequence(value.get("equity_curve", ()), "equity_curve")
            ),
            primary_variant_id=_optional_text(
                value.get("primary_variant_id"), "primary_variant_id"
            ),
            variant_summaries=tuple(
                ExecutionVariantPerformance.from_json(item)
                for item in _sequence(value.get("variant_summaries", ()), "variant_summaries")
            ),
            recent_orders=tuple(
                OrderPerformance.from_json(item)
                for item in _sequence(value.get("recent_orders", ()), "recent_orders")
            ),
            paper_execution_epoch=_optional_text(
                value.get("paper_execution_epoch"), "paper_execution_epoch"
            ),
            decision_funnel=(
                None
                if value.get("decision_funnel") is None
                else DecisionFunnelSnapshot.from_json(value.get("decision_funnel"))
            ),
            direction_health=(
                None
                if value.get("direction_health") is None
                else DirectionHealthSnapshot.from_json(value.get("direction_health"))
            ),
        )


@dataclass(frozen=True, slots=True)
class StrategyCycle:
    stage: StrategyStage
    gate_state: GateState
    next_action: str
    model_id: str | None = None
    challenger_model_id: str | None = None
    stage_started_at: datetime | None = None
    data_cutoff_at: datetime | None = None
    next_challenge_at: datetime | None = None
    next_review_at: datetime | None = None
    challenger_interval_days: int | None = None
    review_interval_days: int | None = None
    progress_label: str | None = None
    progress_current: float | None = None
    progress_target: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, StrategyStage):
            object.__setattr__(self, "stage", StrategyStage(self.stage))
        if not isinstance(self.gate_state, GateState):
            object.__setattr__(self, "gate_state", GateState(self.gate_state))
        _require_text(self.next_action, "next_action")
        for name in ("model_id", "challenger_model_id", "progress_label"):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)
        for name in (
            "stage_started_at",
            "data_cutoff_at",
            "next_challenge_at",
            "next_review_at",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _as_utc(value, name))
        for name in ("challenger_interval_days", "review_interval_days"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer when provided")
        _optional_nonnegative(self.progress_current, "progress_current")
        _optional_nonnegative(self.progress_target, "progress_target")
        if (self.progress_current is None) != (self.progress_target is None):
            raise ValueError("progress_current and progress_target must be provided together")
        if self.progress_target == 0.0:
            raise ValueError("progress_target must be > 0")

    def to_json(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "gate_state": self.gate_state.value,
            "next_action": self.next_action,
            "model_id": self.model_id,
            "challenger_model_id": self.challenger_model_id,
            "stage_started_at": _optional_iso(self.stage_started_at),
            "data_cutoff_at": _optional_iso(self.data_cutoff_at),
            "next_challenge_at": _optional_iso(self.next_challenge_at),
            "next_review_at": _optional_iso(self.next_review_at),
            "challenger_interval_days": self.challenger_interval_days,
            "review_interval_days": self.review_interval_days,
            "progress_label": self.progress_label,
            "progress_current": self.progress_current,
            "progress_target": self.progress_target,
        }

    @classmethod
    def from_json(cls, raw: object) -> StrategyCycle:
        value = _mapping(raw, "strategy cycle")
        return cls(
            stage=StrategyStage(_text(value.get("stage"), "stage")),
            gate_state=GateState(_text(value.get("gate_state"), "gate_state")),
            next_action=_text(value.get("next_action"), "next_action"),
            model_id=_optional_text(value.get("model_id"), "model_id"),
            challenger_model_id=_optional_text(
                value.get("challenger_model_id"), "challenger_model_id"
            ),
            stage_started_at=_optional_timestamp(value.get("stage_started_at"), "stage_started_at"),
            data_cutoff_at=_optional_timestamp(value.get("data_cutoff_at"), "data_cutoff_at"),
            next_challenge_at=_optional_timestamp(
                value.get("next_challenge_at"), "next_challenge_at"
            ),
            next_review_at=_optional_timestamp(value.get("next_review_at"), "next_review_at"),
            challenger_interval_days=_optional_integer(
                value.get("challenger_interval_days"), "challenger_interval_days"
            ),
            review_interval_days=_optional_integer(
                value.get("review_interval_days"), "review_interval_days"
            ),
            progress_label=_optional_text(value.get("progress_label"), "progress_label"),
            progress_current=_optional_float(value.get("progress_current"), "progress_current"),
            progress_target=_optional_float(value.get("progress_target"), "progress_target"),
        )


@dataclass(frozen=True, slots=True)
class DashboardAlert:
    state: HealthState
    message: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.state, HealthState):
            object.__setattr__(self, "state", HealthState(self.state))
        if self.state is HealthState.OK:
            raise ValueError("dashboard alerts must not use the ok state")
        _require_text(self.message, "alert message")
        object.__setattr__(self, "created_at", _as_utc(self.created_at, "created_at"))

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "message": self.message,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: object) -> DashboardAlert:
        value = _mapping(raw, "dashboard alert")
        return cls(
            state=HealthState(_text(value.get("state"), "alert state")),
            message=_text(value.get("message"), "alert message"),
            created_at=_timestamp(value.get("created_at"), "created_at"),
        )


@dataclass(frozen=True, slots=True)
class BotDashboardSnapshot:
    generated_at: datetime
    run_mode: str
    strategy: StrategyCycle
    performance: PerformanceSnapshot | None = None
    health: tuple[HealthIndicator, ...] = ()
    alerts: tuple[DashboardAlert, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", _as_utc(self.generated_at, "generated_at"))
        _require_identifier(self.run_mode, "run_mode")
        object.__setattr__(self, "health", tuple(self.health))
        object.__setattr__(self, "alerts", tuple(self.alerts))
        health_keys = [item.key for item in self.health]
        if len(set(health_keys)) != len(health_keys):
            raise ValueError("health indicator keys must be unique")

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": _DASHBOARD_SCHEMA_VERSION,
            "generated_at": self.generated_at.isoformat(),
            "run_mode": self.run_mode,
            "strategy": self.strategy.to_json(),
            "performance": None if self.performance is None else self.performance.to_json(),
            "health": [item.to_json() for item in self.health],
            "alerts": [item.to_json() for item in self.alerts],
        }

    @classmethod
    def from_json(cls, raw: object) -> BotDashboardSnapshot:
        value = _mapping(raw, "dashboard snapshot")
        if value.get("schema_version") != _DASHBOARD_SCHEMA_VERSION:
            raise ValueError("unsupported dashboard snapshot schema")
        performance = value.get("performance")
        return cls(
            generated_at=_timestamp(value.get("generated_at"), "generated_at"),
            run_mode=_text(value.get("run_mode"), "run_mode"),
            strategy=StrategyCycle.from_json(value.get("strategy")),
            performance=(
                None if performance is None else PerformanceSnapshot.from_json(performance)
            ),
            health=tuple(
                HealthIndicator.from_json(item)
                for item in _sequence(value.get("health", ()), "health")
            ),
            alerts=tuple(
                DashboardAlert.from_json(item)
                for item in _sequence(value.get("alerts", ()), "alerts")
            ),
        )


class DashboardSnapshotStore:
    """One atomic projection file shared by all read-only dashboard clients."""

    def __init__(self, runtime_root: Path) -> None:
        self.path = runtime_root / "dashboard" / "snapshot.json"

    def write(self, snapshot: BotDashboardSnapshot) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        encoded = (
            json.dumps(snapshot.to_json(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.path

    def read(self) -> BotDashboardSnapshot | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid dashboard snapshot JSON: {self.path}") from exc
        return BotDashboardSnapshot.from_json(raw)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _require_identifier(value: object, name: str) -> None:
    text = _text(value, name)
    if any(character in text for character in "/\\") or text in {".", ".."}:
        raise ValueError(f"{name} must be a simple identifier")


def _require_text(value: object, name: str) -> None:
    _text(value, name)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _optional_bool(value: object, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")


def _float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _optional_float(value: object, name: str) -> float | None:
    return None if value is None else _float(value, name)


def _nonnegative(value: float, name: str) -> None:
    _finite(value, name)
    if value < 0.0:
        raise ValueError(f"{name} must be >= 0")


def _optional_finite(value: float | None, name: str) -> None:
    if value is not None:
        _finite(value, name)


def _optional_probability(value: float | None, name: str) -> None:
    if value is not None:
        _finite(value, name)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0, 1]")


def _optional_nonnegative(value: float | None, name: str) -> None:
    if value is not None:
        _nonnegative(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _as_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    return _as_utc(parsed, name)


def _optional_timestamp(value: object, name: str) -> datetime | None:
    return None if value is None else _timestamp(value, name)


def _optional_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
