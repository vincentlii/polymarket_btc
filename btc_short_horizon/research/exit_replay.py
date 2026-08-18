"""Deterministic offline Hold/Sell-FAK/Pair-lock replay over visible L2 depth."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from math import isfinite
from typing import Sequence

from nautilus_trader.model.enums import LiquiditySide
from prediction_market_extensions.adapters.polymarket.parsing import calculate_commission

from btc_short_horizon.research.exit_policy import (
    ExitAction,
    ExitObservation,
    evaluate_exit_policies,
)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: float
    size: float

    def __post_init__(self) -> None:
        if not isfinite(self.price) or not 0.0 <= self.price <= 1.0:
            raise ValueError("level price must be finite and in [0, 1]")
        if not isfinite(self.size) or self.size <= 0.0:
            raise ValueError("level size must be finite and > 0")


@dataclass(frozen=True, slots=True)
class ExitReplayInput:
    market_slug: str
    side: str
    position_size: float
    entry_vwap: float
    side_probability_lower: float
    side_probability_upper: float
    side_bids: tuple[BookLevel, ...]
    opposite_asks: tuple[BookLevel, ...]
    fee_rate: float
    fee_exponent: int
    fee_rule_hash: str
    safety_buffer_per_share: float
    slippage_buffer_per_share: float
    minimum_order_size: float
    remaining_seconds: float
    outcome: int

    def __post_init__(self) -> None:
        if not self.market_slug or self.market_slug.strip() != self.market_slug:
            raise ValueError("market_slug must be non-empty and trimmed")
        if self.side not in {"up", "down"}:
            raise ValueError("side must be 'up' or 'down'")
        if self.outcome not in {0, 1}:
            raise ValueError("outcome must be binary")
        if not isfinite(self.position_size) or self.position_size <= 0.0:
            raise ValueError("position_size must be finite and > 0")
        if not isfinite(self.minimum_order_size) or self.minimum_order_size <= 0.0:
            raise ValueError("minimum_order_size must be finite and > 0")
        if not isfinite(self.fee_rate) or self.fee_rate < 0.0:
            raise ValueError("fee_rate must be finite and non-negative")
        if isinstance(self.fee_exponent, bool) or self.fee_exponent != 1:
            raise ValueError("offline replay only supports the verified exponent-1 fee curve")
        if self.fee_rule_hash != fee_rule_sha256(self.fee_rate, self.fee_exponent):
            raise ValueError("fee_rule_hash does not match the supplied fee schedule")
        _validate_ladder(self.side_bids, descending=True)
        _validate_ladder(self.opposite_asks, descending=False)


@dataclass(frozen=True, slots=True)
class ExitReplayReport:
    market_slug: str
    action: ExitAction
    position_size: float
    filled_size: float
    unfilled_size: float
    execution_vwap: float | None
    fee_paid: float
    locked_pair_size: float
    realized_pnl: float
    reason: str
    fee_rule_hash: str


def replay_exit_policies(payload: ExitReplayInput) -> ExitReplayReport:
    """Replay one exit decision; unfilled FAK quantity is canceled and then settles."""

    sell = _walk_ladder(payload.side_bids, payload.position_size)
    pair = _walk_ladder(payload.opposite_asks, payload.position_size)
    sell_eligible = sell.size >= payload.minimum_order_size
    pair_eligible = pair.size >= payload.minimum_order_size
    if not sell_eligible and not pair_eligible:
        return _hold_report(payload, "no_executable_minimum_order")
    decision = evaluate_exit_policies(
        ExitObservation(
            side=payload.side,
            entry_cost_per_share=payload.entry_vwap,
            side_probability_lower=payload.side_probability_lower,
            side_probability_upper=payload.side_probability_upper,
            executable_bid=sell.vwap if sell_eligible else 0.0,
            opposite_executable_ask=pair.vwap if pair_eligible else 1.0,
            exit_fee_per_share=_fee_per_share(payload, sell.vwap),
            hedge_fee_per_share=_fee_per_share(payload, pair.vwap),
            slippage_buffer_per_share=payload.slippage_buffer_per_share,
            safety_buffer_per_share=payload.safety_buffer_per_share,
            remaining_seconds=payload.remaining_seconds,
        )
    )
    if decision.action is ExitAction.SELL_FAK and sell_eligible:
        fee = _fee(payload, sell.fills)
        remaining = payload.position_size - sell.size
        pnl = (
            sell.notional
            - fee
            + remaining * _side_payout(payload)
            - payload.position_size * payload.entry_vwap
        )
        return ExitReplayReport(
            market_slug=payload.market_slug,
            action=decision.action,
            position_size=payload.position_size,
            filled_size=sell.size,
            unfilled_size=remaining,
            execution_vwap=sell.vwap,
            fee_paid=fee,
            locked_pair_size=0.0,
            realized_pnl=pnl,
            reason=decision.reason,
            fee_rule_hash=payload.fee_rule_hash,
        )
    if decision.action is ExitAction.BUY_OPPOSITE_FAK and pair_eligible:
        fee = _fee(payload, pair.fills)
        remaining = payload.position_size - pair.size
        pnl = (
            pair.size
            - pair.notional
            - fee
            + remaining * _side_payout(payload)
            - payload.position_size * payload.entry_vwap
        )
        return ExitReplayReport(
            market_slug=payload.market_slug,
            action=decision.action,
            position_size=payload.position_size,
            filled_size=pair.size,
            unfilled_size=remaining,
            execution_vwap=pair.vwap,
            fee_paid=fee,
            locked_pair_size=pair.size,
            realized_pnl=pnl,
            reason=decision.reason,
            fee_rule_hash=payload.fee_rule_hash,
        )
    return _hold_report(payload, decision.reason)


@dataclass(frozen=True, slots=True)
class _Walk:
    size: float
    notional: float
    fills: tuple[tuple[float, float], ...]

    @property
    def vwap(self) -> float:
        return self.notional / self.size if self.size > 0.0 else 0.0


def _walk_ladder(levels: Sequence[BookLevel], requested: float) -> _Walk:
    remaining = requested
    fills: list[tuple[float, float]] = []
    for level in levels:
        if remaining <= 0.0:
            break
        size = min(remaining, level.size)
        fills.append((level.price, size))
        remaining -= size
    return _Walk(
        size=sum(size for _price, size in fills),
        notional=sum(price * size for price, size in fills),
        fills=tuple(fills),
    )


def fee_rule_sha256(rate: float, exponent: int) -> str:
    encoded = json.dumps(
        {"curve": "polymarket_taker_v1", "rate": rate, "exponent": exponent},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _fee(payload: ExitReplayInput, fills: Sequence[tuple[float, float]]) -> float:
    return sum(_commission(payload, price=price, quantity=size) for price, size in fills)


def _fee_per_share(payload: ExitReplayInput, price: float) -> float:
    return _commission(payload, price=price, quantity=1.0)


def _commission(payload: ExitReplayInput, *, price: float, quantity: float) -> float:
    return calculate_commission(
        quantity=Decimal(str(quantity)),
        price=Decimal(str(price)),
        fee_rate=Decimal(str(payload.fee_rate)),
        liquidity_side=LiquiditySide.TAKER,
    )


def _side_payout(payload: ExitReplayInput) -> float:
    return float(payload.outcome == (1 if payload.side == "up" else 0))


def _hold_report(payload: ExitReplayInput, reason: str) -> ExitReplayReport:
    return ExitReplayReport(
        market_slug=payload.market_slug,
        action=ExitAction.HOLD,
        position_size=payload.position_size,
        filled_size=0.0,
        unfilled_size=payload.position_size,
        execution_vwap=None,
        fee_paid=0.0,
        locked_pair_size=0.0,
        realized_pnl=(payload.position_size * (_side_payout(payload) - payload.entry_vwap)),
        reason=reason,
        fee_rule_hash=payload.fee_rule_hash,
    )


def _validate_ladder(levels: Sequence[BookLevel], *, descending: bool) -> None:
    values = tuple(levels)
    if any(not isinstance(level, BookLevel) for level in values):
        raise ValueError("book ladders must contain BookLevel values")
    prices = tuple(level.price for level in values)
    expected = tuple(sorted(prices, reverse=descending))
    if prices != expected or len(set(prices)) != len(prices):
        raise ValueError("book ladder must be unique and price sorted")


__all__ = [
    "BookLevel",
    "ExitReplayInput",
    "ExitReplayReport",
    "fee_rule_sha256",
    "replay_exit_policies",
]
