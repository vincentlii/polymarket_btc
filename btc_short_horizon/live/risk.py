"""Projected exposure and liveness admission for maker order submission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite


@dataclass(frozen=True, slots=True)
class TradingSafetyConfig:
    trading_enabled: bool = False
    max_working_markets: int = 1
    max_open_orders: int = 3
    max_unresolved_notional: float = 25.0
    max_balance_fraction: float = 0.05
    daily_loss_limit: float = 10.0
    max_account_snapshot_age_seconds: float = 5.0
    max_future_clock_skew_seconds: float = 0.25
    require_healthy_feeds: bool = True
    require_market_channel: bool = True
    require_user_channel: bool = True
    require_heartbeat: bool = True
    require_healthy_clock: bool = True
    require_geo_eligible: bool = True
    require_account_reconciled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "trading_enabled",
            "require_healthy_feeds",
            "require_market_channel",
            "require_user_channel",
            "require_heartbeat",
            "require_healthy_clock",
            "require_geo_eligible",
            "require_account_reconciled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        for name in ("max_working_markets", "max_open_orders"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an integer >= 1")
        for name in (
            "max_unresolved_notional",
            "daily_loss_limit",
            "max_account_snapshot_age_seconds",
            "max_future_clock_skew_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name} must be a finite number > 0")
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be a finite number > 0")
        if (
            isinstance(self.max_balance_fraction, bool)
            or not isinstance(self.max_balance_fraction, int | float)
            or not isfinite(self.max_balance_fraction)
            or not 0.0 < self.max_balance_fraction <= 1.0
        ):
            raise ValueError("max_balance_fraction must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """One asynchronously refreshed account and connectivity projection."""

    observed_at_ns: int
    collateral_balance: float
    collateral_allowance: float
    account_equity: float
    unresolved_position_cost: float
    open_order_notional: float
    daily_realized_pnl: float
    unrealized_pnl: float
    daily_pnl_day: date
    working_market_ids: frozenset[str]
    open_orders: int
    feeds_healthy: bool
    market_channel_healthy: bool
    user_channel_healthy: bool
    heartbeat_healthy: bool
    clock_healthy: bool
    geo_eligible: bool
    account_reconciled: bool

    def __post_init__(self) -> None:
        if (
            isinstance(self.observed_at_ns, bool)
            or not isinstance(self.observed_at_ns, int)
            or self.observed_at_ns < 0
        ):
            raise ValueError("observed_at_ns must be a non-negative integer")
        for name in (
            "collateral_balance",
            "collateral_allowance",
            "account_equity",
            "unresolved_position_cost",
            "open_order_notional",
            "daily_realized_pnl",
            "unrealized_pnl",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if (
            min(
                self.collateral_balance,
                self.collateral_allowance,
                self.account_equity,
                self.unresolved_position_cost,
                self.open_order_notional,
            )
            < 0.0
        ):
            raise ValueError("balances, equity, and notionals must be non-negative")
        if not isinstance(self.daily_pnl_day, date) or isinstance(self.daily_pnl_day, datetime):
            raise ValueError("daily_pnl_day must be a date")
        if isinstance(self.working_market_ids, str):
            raise ValueError("working_market_ids must be a set of market IDs")
        market_ids = frozenset(self.working_market_ids)
        if any(not isinstance(value, str) or not value.strip() for value in market_ids):
            raise ValueError("working_market_ids must contain non-empty strings")
        if isinstance(self.open_orders, bool) or not isinstance(self.open_orders, int):
            raise ValueError("open_orders must be a non-negative integer")
        if self.open_orders < 0:
            raise ValueError("open_orders must be a non-negative integer")
        for name in (
            "feeds_healthy",
            "market_channel_healthy",
            "user_channel_healthy",
            "heartbeat_healthy",
            "clock_healthy",
            "geo_eligible",
            "account_reconciled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        object.__setattr__(self, "working_market_ids", market_ids)


@dataclass(frozen=True, slots=True)
class RiskReservations:
    """Exposure reserved locally after admission and before a final venue outcome."""

    notional: float = 0.0
    order_count: int = 0
    market_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if (
            isinstance(self.notional, bool)
            or not isinstance(self.notional, int | float)
            or not isfinite(self.notional)
            or self.notional < 0.0
        ):
            raise ValueError("reservation notional must be finite and >= 0")
        if (
            isinstance(self.order_count, bool)
            or not isinstance(self.order_count, int)
            or self.order_count < 0
        ):
            raise ValueError("reservation order_count must be a non-negative integer")
        market_ids = frozenset(self.market_ids)
        if any(not isinstance(value, str) or not value.strip() for value in market_ids):
            raise ValueError("reservation market_ids must contain non-empty strings")
        if self.order_count == 0 and (self.notional > 0.0 or market_ids):
            raise ValueError("non-empty reservations require order_count > 0")
        object.__setattr__(self, "market_ids", market_ids)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str
    projected_exposure: float | None = None
    projected_open_orders: int | None = None


def evaluate_order_risk(
    *,
    config: TradingSafetyConfig,
    account: AccountSnapshot,
    order_notional: float,
    market_id: str,
    now_ts_ns: int,
    reservations: RiskReservations = RiskReservations(),
) -> RiskDecision:
    if (
        isinstance(order_notional, bool)
        or not isinstance(order_notional, int | float)
        or not isfinite(order_notional)
        or order_notional <= 0.0
    ):
        return RiskDecision(False, "invalid_order_notional")
    if not isinstance(market_id, str) or not market_id.strip():
        return RiskDecision(False, "invalid_market_id")
    if isinstance(now_ts_ns, bool) or not isinstance(now_ts_ns, int) or now_ts_ns < 0:
        return RiskDecision(False, "invalid_current_time")
    if not config.trading_enabled:
        return RiskDecision(False, "trading_disabled")

    age_ns = now_ts_ns - account.observed_at_ns
    future_tolerance_ns = int(config.max_future_clock_skew_seconds * 1_000_000_000)
    if age_ns < -future_tolerance_ns:
        return RiskDecision(False, "account_snapshot_in_future")
    if age_ns > int(config.max_account_snapshot_age_seconds * 1_000_000_000):
        return RiskDecision(False, "account_snapshot_stale")
    if config.require_healthy_feeds and not account.feeds_healthy:
        return RiskDecision(False, "feeds_unhealthy")
    if config.require_market_channel and not account.market_channel_healthy:
        return RiskDecision(False, "market_channel_unhealthy")
    if config.require_user_channel and not account.user_channel_healthy:
        return RiskDecision(False, "user_channel_unhealthy")
    if config.require_heartbeat and not account.heartbeat_healthy:
        return RiskDecision(False, "heartbeat_unhealthy")
    if config.require_healthy_clock and not account.clock_healthy:
        return RiskDecision(False, "clock_unhealthy")
    if config.require_geo_eligible and not account.geo_eligible:
        return RiskDecision(False, "geo_ineligible")
    if config.require_account_reconciled and not account.account_reconciled:
        return RiskDecision(False, "account_not_reconciled")
    current_day = datetime.fromtimestamp(now_ts_ns / 1_000_000_000, tz=UTC).date()
    if account.daily_pnl_day != current_day:
        return RiskDecision(False, "daily_pnl_epoch_mismatch")

    projected_markets = account.working_market_ids | reservations.market_ids | {market_id}
    if len(projected_markets) > config.max_working_markets:
        return RiskDecision(False, "working_market_limit")
    projected_orders = account.open_orders + reservations.order_count + 1
    if projected_orders > config.max_open_orders:
        return RiskDecision(False, "open_order_limit")
    risk_pnl = account.daily_realized_pnl + min(account.unrealized_pnl, 0.0)
    if risk_pnl <= -config.daily_loss_limit:
        return RiskDecision(False, "daily_loss_limit")

    spendable = (
        min(account.collateral_balance, account.collateral_allowance)
        - account.open_order_notional
        - reservations.notional
    )
    if order_notional > spendable + 1e-12:
        return RiskDecision(False, "insufficient_collateral_or_allowance")
    projected_exposure = (
        account.unresolved_position_cost
        + account.open_order_notional
        + reservations.notional
        + order_notional
    )
    max_exposure = min(
        config.max_unresolved_notional,
        account.account_equity * config.max_balance_fraction,
    )
    if projected_exposure > max_exposure + 1e-12:
        return RiskDecision(
            False,
            "unresolved_notional_limit",
            projected_exposure=projected_exposure,
            projected_open_orders=projected_orders,
        )
    return RiskDecision(
        True,
        "allowed",
        projected_exposure=projected_exposure,
        projected_open_orders=projected_orders,
    )
