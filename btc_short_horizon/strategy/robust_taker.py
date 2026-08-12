"""Central robust executable-cost contract and one-signal taker policy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from math import isfinite
from typing import Mapping

from nautilus_trader.model.enums import LiquiditySide
from prediction_market_extensions.adapters.polymarket.parsing import calculate_commission

from btc_short_horizon.models.market_relative import ProbabilityInterval
from btc_short_horizon.strategy.causal_pair import CausalPairSession, ValidatedCausalPair
from btc_short_horizon.strategy.types import SideBook, TokenSide


class RobustTakerRejection(StrEnum):
    SELECTED = "selected"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    BELOW_MINIMUM_SIZE = "below_minimum_size"
    EDGE_BELOW_THRESHOLD = "edge_below_threshold"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    OUTSIDE_PRICE_BAND = "outside_price_band"
    ALREADY_CONSUMED = "already_consumed"
    INVALID_CADENCE = "invalid_cadence"


@dataclass(frozen=True, slots=True)
class RobustExecutableCost:
    side: TokenSide
    requested_shares: float
    executable_shares: float
    point_fair: float
    robust_fair: float
    executable_vwap: float | None
    fee_per_share: float | None
    slippage_stress: float
    latency_stress: float
    gross_edge: float | None
    robust_net_edge: float | None
    limit_price: float | None
    filled_notional: float
    taker_fees: float
    reason: RobustTakerRejection


@dataclass(frozen=True, slots=True)
class RobustTakerPlan:
    market_slug: str
    token_id: str
    side: TokenSide
    decision_ts_ns: int
    evaluation: RobustExecutableCost


@dataclass(frozen=True, slots=True)
class RobustTakerDecision:
    plan: RobustTakerPlan | None
    reason: RobustTakerRejection
    evaluations: tuple[RobustExecutableCost, ...]


def evaluate_robust_executable_cost(
    *,
    side: TokenSide | str,
    interval: ProbabilityInterval,
    book: SideBook,
    requested_shares: float,
    fee_rate: float,
    slippage_stress: float,
    latency_stress: float,
    minimum_net_edge: float,
    available_balance: float,
    minimum_price: float = 0.35,
    maximum_price: float = 0.80,
) -> RobustExecutableCost:
    """Walk asks and deduct every named cost exactly once."""

    normalized_side = TokenSide(side)
    for name, value, positive in (
        ("requested_shares", requested_shares, True),
        ("available_balance", available_balance, True),
        ("fee_rate", fee_rate, False),
        ("slippage_stress", slippage_stress, False),
        ("latency_stress", latency_stress, False),
        ("minimum_net_edge", minimum_net_edge, False),
    ):
        if (
            isinstance(value, bool)
            or not isfinite(value)
            or (value <= 0.0 if positive else value < 0.0)
        ):
            raise ValueError(f"{name} is invalid")
    if fee_rate >= 1.0:
        raise ValueError("fee_rate must be < 1")
    if not 0.0 < minimum_price < maximum_price < 1.0:
        raise ValueError("price band must satisfy 0 < minimum < maximum < 1")
    robust_fair = interval.robust_fair(normalized_side)
    point_fair = interval.point_fair(normalized_side)
    filled = notional = fees = 0.0
    limit_price: float | None = None
    outside_band = False
    remaining = requested_shares
    for level in book.asks:
        if not minimum_price <= level.price <= maximum_price:
            outside_band = True
            break
        quantity = min(remaining, level.size)
        fee = float(
            calculate_commission(
                quantity=Decimal(str(quantity)),
                price=Decimal(str(level.price)),
                fee_rate=Decimal(str(fee_rate)),
                liquidity_side=LiquiditySide.TAKER,
            )
        )
        filled += quantity
        notional += quantity * level.price
        fees += fee
        remaining -= quantity
        limit_price = level.price
        if remaining <= 1e-12:
            break
    if outside_band:
        reason = RobustTakerRejection.OUTSIDE_PRICE_BAND
    elif remaining > 1e-12:
        reason = RobustTakerRejection.INSUFFICIENT_DEPTH
    elif filled + 1e-12 < book.minimum_order_size:
        reason = RobustTakerRejection.BELOW_MINIMUM_SIZE
    else:
        executable_vwap = notional / filled
        fee_per_share = fees / filled
        gross_edge = robust_fair - executable_vwap
        robust_net_edge = gross_edge - fee_per_share - slippage_stress - latency_stress
        if notional + fees > available_balance + 1e-12:
            reason = RobustTakerRejection.INSUFFICIENT_BALANCE
        elif robust_net_edge + 1e-12 < minimum_net_edge:
            reason = RobustTakerRejection.EDGE_BELOW_THRESHOLD
        else:
            reason = RobustTakerRejection.SELECTED
        return RobustExecutableCost(
            side=normalized_side,
            requested_shares=requested_shares,
            executable_shares=filled,
            point_fair=point_fair,
            robust_fair=robust_fair,
            executable_vwap=executable_vwap,
            fee_per_share=fee_per_share,
            slippage_stress=slippage_stress,
            latency_stress=latency_stress,
            gross_edge=gross_edge,
            robust_net_edge=robust_net_edge,
            limit_price=limit_price,
            filled_notional=notional,
            taker_fees=fees,
            reason=reason,
        )
    return RobustExecutableCost(
        side=normalized_side,
        requested_shares=requested_shares,
        executable_shares=filled,
        point_fair=point_fair,
        robust_fair=robust_fair,
        executable_vwap=None,
        fee_per_share=None,
        slippage_stress=slippage_stress,
        latency_stress=latency_stress,
        gross_edge=None,
        robust_net_edge=None,
        limit_price=limit_price,
        filled_notional=notional,
        taker_fees=fees,
        reason=reason,
    )


class OneSignalTakerPolicy:
    """Fresh 5-second evaluations until one opportunity consumes the market."""

    def __init__(self) -> None:
        self._consumed_markets: set[str] = set()
        self._last_evaluation_ts: dict[str, int] = {}
        self._execution_results: dict[str, float] = {}

    def evaluate(
        self,
        *,
        session: CausalPairSession,
        pair: ValidatedCausalPair,
        interval: ProbabilityInterval,
        requested_shares: float,
        fee_rate_by_side: Mapping[TokenSide, float],
        slippage_stress: float,
        latency_stress: float,
        minimum_net_edge: float,
        available_balance: float,
        minimum_price: float = 0.35,
        maximum_price: float = 0.80,
    ) -> RobustTakerDecision:
        if pair.session != session:
            raise ValueError("validated pair does not belong to the supplied session")
        if session.market_slug in self._consumed_markets:
            return RobustTakerDecision(None, RobustTakerRejection.ALREADY_CONSUMED, ())
        elapsed_ns = pair.decision_ts_ns - session.t0_ns
        cadence_ns = 5_000_000_000
        cadence_index = round(elapsed_ns / cadence_ns)
        if (
            cadence_index < 1
            or cadence_index > 36
            or abs(elapsed_ns - cadence_index * cadence_ns) > 1_000_000_000
        ):
            return RobustTakerDecision(None, RobustTakerRejection.INVALID_CADENCE, ())
        previous = self._last_evaluation_ts.get(session.market_slug)
        if previous is not None and pair.decision_ts_ns <= previous:
            raise ValueError("one-signal evaluations must be strictly chronological")
        self._last_evaluation_ts[session.market_slug] = pair.decision_ts_ns
        if set(fee_rate_by_side) != {TokenSide.UP, TokenSide.DOWN}:
            raise ValueError("fee rates must cover exactly Up and Down")
        evaluations = tuple(
            evaluate_robust_executable_cost(
                side=side,
                interval=interval,
                book=pair.up.book if side is TokenSide.UP else pair.down.book,
                requested_shares=requested_shares,
                fee_rate=fee_rate_by_side[side],
                slippage_stress=slippage_stress,
                latency_stress=latency_stress,
                minimum_net_edge=minimum_net_edge,
                available_balance=available_balance,
                minimum_price=minimum_price,
                maximum_price=maximum_price,
            )
            for side in (TokenSide.UP, TokenSide.DOWN)
        )
        eligible = tuple(
            item for item in evaluations if item.reason is RobustTakerRejection.SELECTED
        )
        if not eligible:
            best = max(
                evaluations,
                key=lambda item: (
                    float("-inf") if item.robust_net_edge is None else item.robust_net_edge,
                    1 if item.side is TokenSide.UP else 0,
                ),
            )
            return RobustTakerDecision(None, best.reason, evaluations)
        selected = max(
            eligible,
            key=lambda item: (
                item.robust_net_edge if item.robust_net_edge is not None else float("-inf"),
                1 if item.side is TokenSide.UP else 0,
            ),
        )
        self._consumed_markets.add(session.market_slug)
        token_id = session.up_token_id if selected.side is TokenSide.UP else session.down_token_id
        return RobustTakerDecision(
            RobustTakerPlan(
                market_slug=session.market_slug,
                token_id=token_id,
                side=selected.side,
                decision_ts_ns=pair.decision_ts_ns,
                evaluation=selected,
            ),
            RobustTakerRejection.SELECTED,
            evaluations,
        )

    def record_execution_result(self, market_slug: str, *, filled_shares: float) -> None:
        if market_slug not in self._consumed_markets:
            raise ValueError("execution result requires a consumed opportunity")
        if isinstance(filled_shares, bool) or not isfinite(filled_shares) or filled_shares < 0.0:
            raise ValueError("filled_shares must be finite and >= 0")
        if market_slug in self._execution_results:
            raise ValueError("execution result is immutable")
        self._execution_results[market_slug] = filled_shares


__all__ = [
    "OneSignalTakerPolicy",
    "RobustExecutableCost",
    "RobustTakerDecision",
    "RobustTakerPlan",
    "RobustTakerRejection",
    "evaluate_robust_executable_cost",
]
