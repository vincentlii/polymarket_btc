from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class TradingSafetyConfig:
    trading_enabled: bool = False
    max_working_markets: int = 1
    max_open_orders: int = 3
    max_unresolved_notional: float = 25.0
    max_balance_fraction: float = 0.05
    daily_loss_limit: float = 10.0
    require_healthy_feeds: bool = True
    require_geo_eligible: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("max_working_markets", self.max_working_markets),
            ("max_open_orders", self.max_open_orders),
        ):
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
        for name, value in (
            ("max_unresolved_notional", self.max_unresolved_notional),
            ("daily_loss_limit", self.daily_loss_limit),
        ):
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and > 0")
        if not isfinite(self.max_balance_fraction) or not 0.0 < self.max_balance_fraction <= 1.0:
            raise ValueError("max_balance_fraction must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    available_balance: float
    unresolved_notional: float
    daily_realized_pnl: float
    working_markets: int
    open_orders: int
    feeds_healthy: bool
    geo_eligible: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("available_balance", self.available_balance),
            ("unresolved_notional", self.unresolved_notional),
            ("daily_realized_pnl", self.daily_realized_pnl),
        ):
            if not isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.available_balance < 0.0 or self.unresolved_notional < 0.0:
            raise ValueError("balances and notional must be non-negative")
        if self.working_markets < 0 or self.open_orders < 0:
            raise ValueError("working_markets and open_orders must be non-negative")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    reason: str


def evaluate_order_risk(
    *,
    config: TradingSafetyConfig,
    account: AccountSnapshot,
    order_notional: float,
) -> RiskDecision:
    if not isfinite(order_notional) or order_notional <= 0.0:
        return RiskDecision(False, "invalid_order_notional")
    if not config.trading_enabled:
        return RiskDecision(False, "trading_disabled")
    if config.require_healthy_feeds and not account.feeds_healthy:
        return RiskDecision(False, "feeds_unhealthy")
    if config.require_geo_eligible and not account.geo_eligible:
        return RiskDecision(False, "geo_ineligible")
    if account.working_markets >= config.max_working_markets:
        return RiskDecision(False, "working_market_limit")
    if account.open_orders >= config.max_open_orders:
        return RiskDecision(False, "open_order_limit")
    if account.daily_realized_pnl <= -config.daily_loss_limit:
        return RiskDecision(False, "daily_loss_limit")
    max_notional = min(
        config.max_unresolved_notional,
        account.available_balance * config.max_balance_fraction,
    )
    if account.unresolved_notional + order_notional > max_notional:
        return RiskDecision(False, "unresolved_notional_limit")
    return RiskDecision(True, "allowed")
