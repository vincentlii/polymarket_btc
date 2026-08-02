"""Pure independent taker opportunity planning for BTC outcome tokens."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Mapping

from nautilus_trader.model.enums import LiquiditySide
from prediction_market_extensions.adapters.polymarket.parsing import calculate_commission

from btc_short_horizon.strategy.types import OutcomeBooks, TakerOrderPlan, TokenSide


class TakerRejectionReason(StrEnum):
    SELECTED = "selected"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    BELOW_MINIMUM_SIZE = "below_minimum_size"
    EDGE_BELOW_THRESHOLD = "edge_below_threshold"
    INSUFFICIENT_BALANCE = "insufficient_virtual_balance"


@dataclass(frozen=True, slots=True)
class TakerCandidateEvaluation:
    side: TokenSide
    token_id: str
    fair_probability: float
    requested_size: float
    filled_size: float
    executable_vwap: float | None
    taker_fee_per_share: float | None
    gross_edge: float | None
    total_buffer: float
    net_edge: float | None
    reason: TakerRejectionReason
    limit_price: float | None
    filled_notional: float = 0.0
    taker_fees: float = 0.0


@dataclass(frozen=True, slots=True)
class TakerPlanDecision:
    plan: TakerOrderPlan | None
    reason: TakerRejectionReason
    evaluations: tuple[TakerCandidateEvaluation, ...]


def plan_independent_taker_order(
    *,
    market_slug: str,
    p_boundary_up: float,
    p_up: float,
    books: OutcomeBooks,
    fee_rate_by_side: Mapping[TokenSide, float],
    max_shares: float,
    minimum_net_edge: float,
    slippage_buffer: float,
    model_uncertainty_buffer: float,
    available_balance: float,
    decision_ts_ns: int,
) -> TakerPlanDecision:
    if not market_slug:
        raise ValueError("market_slug is required")
    for name, value in (
        ("p_boundary_up", p_boundary_up),
        ("p_up", p_up),
    ):
        if isinstance(value, bool) or not isfinite(value) or not 0.0 < value < 1.0:
            raise ValueError(f"{name} must be finite and in (0, 1)")
    for name, value in (
        ("max_shares", max_shares),
        ("available_balance", available_balance),
    ):
        if isinstance(value, bool) or not isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be finite and > 0")
    for name, value in (
        ("minimum_net_edge", minimum_net_edge),
        ("slippage_buffer", slippage_buffer),
        ("model_uncertainty_buffer", model_uncertainty_buffer),
    ):
        if isinstance(value, bool) or not isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be finite and >= 0")
    if (
        isinstance(decision_ts_ns, bool)
        or not isinstance(decision_ts_ns, int)
        or decision_ts_ns < 0
    ):
        raise ValueError("decision_ts_ns must be a non-negative integer")
    normalized_rates = MappingProxyType(dict(fee_rate_by_side))
    if set(normalized_rates) != {TokenSide.UP, TokenSide.DOWN}:
        raise ValueError("fee rates must cover exactly Up and Down")

    evaluations = tuple(
        _evaluate_side(
            side=side,
            fair_probability=p_up if side is TokenSide.UP else 1.0 - p_up,
            book=books.for_side(side),
            fee_rate=normalized_rates[side],
            max_shares=max_shares,
            minimum_net_edge=minimum_net_edge,
            slippage_buffer=slippage_buffer,
            model_uncertainty_buffer=model_uncertainty_buffer,
            available_balance=available_balance,
        )
        for side in (TokenSide.UP, TokenSide.DOWN)
    )
    eligible = tuple(item for item in evaluations if item.reason is TakerRejectionReason.SELECTED)
    if not eligible:
        reason = max(
            evaluations,
            key=lambda item: float("-inf") if item.net_edge is None else item.net_edge,
        ).reason
        return TakerPlanDecision(plan=None, reason=reason, evaluations=evaluations)
    selected = max(eligible, key=lambda item: item.net_edge or float("-inf"))
    assert selected.executable_vwap is not None
    assert selected.limit_price is not None
    return TakerPlanDecision(
        plan=TakerOrderPlan(
            market_slug=market_slug,
            token_id=selected.token_id,
            side=selected.side,
            p_boundary=p_boundary_up if selected.side is TokenSide.UP else 1.0 - p_boundary_up,
            p_fair=selected.fair_probability,
            p_market=selected.executable_vwap,
            created_ts_ns=decision_ts_ns,
            total_size=selected.filled_size,
            filled_notional=selected.filled_notional,
            taker_fees=selected.taker_fees,
            slippage_buffer=slippage_buffer,
            model_uncertainty_buffer=model_uncertainty_buffer,
            minimum_edge=minimum_net_edge,
            limit_price=selected.limit_price,
        ),
        reason=TakerRejectionReason.SELECTED,
        evaluations=evaluations,
    )


def _evaluate_side(
    *,
    side: TokenSide,
    fair_probability: float,
    book,
    fee_rate: float,
    max_shares: float,
    minimum_net_edge: float,
    slippage_buffer: float,
    model_uncertainty_buffer: float,
    available_balance: float,
) -> TakerCandidateEvaluation:
    if isinstance(fee_rate, bool) or not isfinite(fee_rate) or not 0.0 <= fee_rate < 1.0:
        raise ValueError("fee rate must be finite and in [0, 1)")
    remaining = max_shares
    filled = notional = fees = 0.0
    limit_price = None
    for level in book.asks:
        quantity = min(remaining, level.size)
        fee = calculate_commission(
            quantity=Decimal(str(quantity)),
            price=Decimal(str(level.price)),
            fee_rate=Decimal(str(fee_rate)),
            liquidity_side=LiquiditySide.TAKER,
        )
        marginal_net_edge = (
            fair_probability
            - level.price
            - fee / quantity
            - slippage_buffer
            - model_uncertainty_buffer
        )
        if marginal_net_edge + 1e-12 < minimum_net_edge:
            break
        filled += quantity
        notional += quantity * level.price
        fees += fee
        remaining -= quantity
        limit_price = level.price
        if remaining <= 1e-12:
            break
    total_buffer = slippage_buffer + model_uncertainty_buffer
    if filled <= 0.0:
        reason = (
            TakerRejectionReason.INSUFFICIENT_DEPTH
            if not book.asks
            else TakerRejectionReason.EDGE_BELOW_THRESHOLD
        )
    elif filled + 1e-12 < book.minimum_order_size:
        reason = TakerRejectionReason.BELOW_MINIMUM_SIZE
    else:
        vwap = notional / filled
        fee_per_share = fees / filled
        net_edge = fair_probability - vwap - fee_per_share - total_buffer
        all_in = notional + fees
        if all_in > available_balance + 1e-12:
            reason = TakerRejectionReason.INSUFFICIENT_BALANCE
        elif net_edge + 1e-12 < minimum_net_edge:
            reason = TakerRejectionReason.EDGE_BELOW_THRESHOLD
        else:
            reason = TakerRejectionReason.SELECTED
        return TakerCandidateEvaluation(
            side=side,
            token_id=book.token_id,
            fair_probability=fair_probability,
            requested_size=max_shares,
            filled_size=filled,
            executable_vwap=vwap,
            taker_fee_per_share=fee_per_share,
            gross_edge=fair_probability - vwap,
            total_buffer=total_buffer,
            net_edge=net_edge,
            reason=reason,
            limit_price=limit_price,
            filled_notional=notional,
            taker_fees=fees,
        )
    return TakerCandidateEvaluation(
        side=side,
        token_id=book.token_id,
        fair_probability=fair_probability,
        requested_size=max_shares,
        filled_size=filled,
        executable_vwap=None,
        taker_fee_per_share=None,
        gross_edge=None,
        total_buffer=total_buffer,
        net_edge=None,
        reason=reason,
        limit_price=limit_price,
    )


__all__ = [
    "TakerCandidateEvaluation",
    "TakerPlanDecision",
    "TakerRejectionReason",
    "plan_independent_taker_order",
]
