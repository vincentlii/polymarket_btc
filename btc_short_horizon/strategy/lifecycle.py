"""Explicit order lifecycle with cancel/fill race preservation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite

from btc_short_horizon.strategy.types import OrderPlan


class StrategyPhase(StrEnum):
    DISCOVERED = "discovered"
    MONITORING = "monitoring"
    ORDER_WORKING = "order_working"
    CANCEL_REQUESTED = "cancel_requested"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    RESOLVED = "resolved"
    REDEEMED = "redeemed"


@dataclass(frozen=True, slots=True)
class MarketExecution:
    """One market's permitted placement cycle and immutable fill accounting."""

    market_slug: str
    phase: StrategyPhase = StrategyPhase.DISCOVERED
    plan: OrderPlan | None = None
    placement_used: bool = False
    filled_size: float = 0.0
    resolved: bool = False

    def __post_init__(self) -> None:
        if not self.market_slug:
            raise ValueError("market_slug is required")
        if not isfinite(self.filled_size) or self.filled_size < 0.0:
            raise ValueError("filled_size must be finite and >= 0")
        if self.plan is not None and self.plan.market_slug != self.market_slug:
            raise ValueError("plan market_slug must match execution market_slug")
        if self.plan is None and self.filled_size != 0.0:
            raise ValueError("filled_size requires an order plan")
        if self.plan is not None and self.filled_size > self.plan.total_size + 1e-12:
            raise ValueError("filled_size cannot exceed plan total_size")

    @property
    def remaining_size(self) -> float:
        return 0.0 if self.plan is None else self.plan.total_size - self.filled_size

    def begin_monitoring(self) -> MarketExecution:
        return self._transition(StrategyPhase.DISCOVERED, StrategyPhase.MONITORING)

    def submit_plan(self, plan: OrderPlan) -> MarketExecution:
        if self.placement_used:
            raise ValueError("each market permits only one placement cycle")
        if self.phase is not StrategyPhase.MONITORING:
            raise ValueError("a plan can only be submitted from monitoring")
        if plan.market_slug != self.market_slug:
            raise ValueError("plan market_slug must match execution market_slug")
        return replace(self, phase=StrategyPhase.ORDER_WORKING, plan=plan, placement_used=True)

    def request_cancel(self) -> MarketExecution:
        return self._transition(StrategyPhase.ORDER_WORKING, StrategyPhase.CANCEL_REQUESTED)

    def reject_cancel(self) -> MarketExecution:
        return self._transition(StrategyPhase.CANCEL_REQUESTED, StrategyPhase.ORDER_WORKING)

    def record_fill(self, size: float) -> MarketExecution:
        if self.phase not in {StrategyPhase.ORDER_WORKING, StrategyPhase.CANCEL_REQUESTED}:
            raise ValueError(
                "fills are accepted only while an order is working or cancel is pending"
            )
        if not isfinite(size) or size <= 0.0:
            raise ValueError("fill size must be finite and > 0")
        if size > self.remaining_size + 1e-12:
            raise ValueError("fill exceeds remaining planned size")
        filled_size = min(self.plan.total_size, self.filled_size + size) if self.plan else 0.0
        if filled_size >= self.plan.total_size - 1e-12:
            phase = StrategyPhase.FILLED
        elif self.phase is StrategyPhase.CANCEL_REQUESTED:
            phase = StrategyPhase.CANCEL_REQUESTED
        else:
            phase = StrategyPhase.ORDER_WORKING
        return replace(self, phase=phase, filled_size=filled_size)

    def acknowledge_cancel(self) -> MarketExecution:
        if self.phase is not StrategyPhase.CANCEL_REQUESTED:
            raise ValueError("cancel acknowledgement requires a pending cancellation")
        phase = StrategyPhase.PARTIALLY_FILLED if self.filled_size > 0.0 else StrategyPhase.CANCELED
        return replace(self, phase=phase)

    def reject_order(self) -> MarketExecution:
        if self.phase is not StrategyPhase.ORDER_WORKING:
            raise ValueError("order rejection requires a working order")
        return replace(self, phase=StrategyPhase.REJECTED)

    def complete_working_orders(self) -> MarketExecution:
        """Close unfilled working orders while preserving any partial fill."""
        if self.phase is not StrategyPhase.ORDER_WORKING:
            raise ValueError("working-order completion requires an active order")
        phase = StrategyPhase.PARTIALLY_FILLED if self.filled_size > 0.0 else StrategyPhase.CANCELED
        return replace(self, phase=phase)

    def record_resolution(self) -> MarketExecution:
        if self.phase not in {
            StrategyPhase.CANCELED,
            StrategyPhase.PARTIALLY_FILLED,
            StrategyPhase.FILLED,
            StrategyPhase.REJECTED,
        }:
            raise ValueError("resolution requires a terminal execution phase")
        return replace(self, phase=StrategyPhase.RESOLVED, resolved=True)

    def redeem(self) -> MarketExecution:
        if self.phase is not StrategyPhase.RESOLVED:
            raise ValueError("redemption requires a resolved market")
        if self.filled_size <= 0.0:
            raise ValueError("cannot redeem a market with no fills")
        return replace(self, phase=StrategyPhase.REDEEMED)

    def _transition(self, source: StrategyPhase, target: StrategyPhase) -> MarketExecution:
        if self.phase is not source:
            raise ValueError(f"invalid lifecycle transition: {self.phase} -> {target}")
        return replace(self, phase=target)
