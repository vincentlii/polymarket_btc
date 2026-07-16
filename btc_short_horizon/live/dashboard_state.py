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


_DASHBOARD_SCHEMA_VERSION = 1


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
class TradePerformance:
    order_id: str
    market_slug: str
    side: str
    status: str
    placed_at: datetime
    shares: float
    filled_shares: float
    entry_price: float | None = None
    p_fair: float | None = None
    market_price: float | None = None
    realized_pnl: float | None = None
    unrealized_pnl: float | None = None
    order_latency_ms: float | None = None

    def __post_init__(self) -> None:
        _require_text(self.order_id, "order_id")
        _require_text(self.market_slug, "market_slug")
        if self.side not in {"up", "down"}:
            raise ValueError("side must be 'up' or 'down'")
        _require_text(self.status, "status")
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

    def to_json(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "market_slug": self.market_slug,
            "side": self.side,
            "status": self.status,
            "placed_at": self.placed_at.isoformat(),
            "shares": self.shares,
            "filled_shares": self.filled_shares,
            "entry_price": self.entry_price,
            "p_fair": self.p_fair,
            "market_price": self.market_price,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "order_latency_ms": self.order_latency_ms,
        }

    @classmethod
    def from_json(cls, raw: object) -> TradePerformance:
        value = _mapping(raw, "trade performance")
        return cls(
            order_id=_text(value.get("order_id"), "order_id"),
            market_slug=_text(value.get("market_slug"), "market_slug"),
            side=_text(value.get("side"), "side"),
            status=_text(value.get("status"), "status"),
            placed_at=_timestamp(value.get("placed_at"), "placed_at"),
            shares=_float(value.get("shares"), "shares"),
            filled_shares=_float(value.get("filled_shares"), "filled_shares"),
            entry_price=_optional_float(value.get("entry_price"), "entry_price"),
            p_fair=_optional_float(value.get("p_fair"), "p_fair"),
            market_price=_optional_float(value.get("market_price"), "market_price"),
            realized_pnl=_optional_float(value.get("realized_pnl"), "realized_pnl"),
            unrealized_pnl=_optional_float(value.get("unrealized_pnl"), "unrealized_pnl"),
            order_latency_ms=_optional_float(value.get("order_latency_ms"), "order_latency_ms"),
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
    recent_trades: tuple[TradePerformance, ...] = ()

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
        trades = tuple(self.recent_trades)
        if any(right.timestamp < left.timestamp for left, right in zip(points, points[1:])):
            raise ValueError("equity_curve must be ordered by timestamp")
        object.__setattr__(self, "equity_curve", points)
        object.__setattr__(self, "recent_trades", trades)

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
            "recent_trades": [trade.to_json() for trade in self.recent_trades],
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
            recent_trades=tuple(
                TradePerformance.from_json(item)
                for item in _sequence(value.get("recent_trades", ()), "recent_trades")
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
