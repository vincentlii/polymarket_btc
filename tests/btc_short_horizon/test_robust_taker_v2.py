from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.model.enums import LiquiditySide
from prediction_market_extensions.adapters.polymarket.parsing import calculate_commission

from btc_short_horizon.models.market_relative import ProbabilityInterval
from btc_short_horizon.strategy.causal_pair import (
    CausalPairSession,
    PairBookEvidence,
)
from btc_short_horizon.strategy.robust_taker import (
    OneSignalTakerPolicy,
    RobustTakerRejection,
    evaluate_robust_executable_cost,
)
from btc_short_horizon.strategy.types import SideBook, TokenSide, VisibleBookLevel


def _book(token: str, bid: float, asks: tuple[tuple[float, float], ...]) -> SideBook:
    return SideBook(
        token_id=token,
        bids=(VisibleBookLevel(bid, 20.0),),
        asks=tuple(VisibleBookLevel(price, size) for price, size in asks),
        tick_size=0.01,
        minimum_order_size=1.0,
    )


def _pair(
    *,
    decision_seconds: int = 5,
    up_ask: float = 0.40,
    down_ask: float = 0.60,
    up_asks: tuple[tuple[float, float], ...] | None = None,
    down_asks: tuple[tuple[float, float], ...] | None = None,
):
    session = CausalPairSession(
        market_slug="btc-updown-15m-1",
        condition_id="condition",
        rule_epoch="chainlink-btc-usd-point-v1",
        rule_hash="a" * 64,
        up_token_id="up",
        down_token_id="down",
        t0_ns=0,
        t1_ns=900_000_000_000,
    )
    up_book = _book("up", up_ask - 0.01, up_asks or ((up_ask, 10.0),))
    down_book = _book("down", down_ask - 0.01, down_asks or ((down_ask, 10.0),))
    decision_ns = decision_seconds * 1_000_000_000
    return session, session.validate(
        up=PairBookEvidence(
            TokenSide.UP,
            "up",
            up_book,
            decision_ns - 200_000_000,
            decision_ns - 100_000_000,
            1,
            0,
            "session-a",
            True,
            True,
        ),
        down=PairBookEvidence(
            TokenSide.DOWN,
            "down",
            down_book,
            decision_ns - 200_000_000,
            decision_ns - 100_000_000,
            1,
            0,
            "session-a",
            True,
            True,
        ),
        decision_ts_ns=decision_ns,
        maximum_age_ns=200_000_000,
        maximum_midpoint_sum_deviation=0.30,
    )


def test_executable_cost_reconciles_once_with_book_walk_fee_and_stresses() -> None:
    book = _book("up", 0.39, ((0.40, 2.0), (0.42, 3.0)))
    interval = ProbabilityInterval(0.55, 0.60, 0.65)

    result = evaluate_robust_executable_cost(
        side=TokenSide.UP,
        interval=interval,
        book=book,
        requested_shares=5.0,
        fee_rate=0.02,
        slippage_stress=0.005,
        latency_stress=0.006,
        minimum_net_edge=0.01,
        minimum_price=0.35,
        maximum_price=0.80,
        available_balance=100.0,
    )

    expected_fee = sum(
        float(
            calculate_commission(
                quantity=Decimal(str(quantity)),
                price=Decimal(str(price)),
                fee_rate=Decimal("0.02"),
                liquidity_side=LiquiditySide.TAKER,
            )
        )
        for price, quantity in ((0.40, 2.0), (0.42, 3.0))
    )
    assert result.reason is RobustTakerRejection.SELECTED
    assert result.executable_vwap == pytest.approx((0.40 * 2 + 0.42 * 3) / 5)
    assert result.fee_per_share == pytest.approx(expected_fee / 5)
    assert result.robust_net_edge == pytest.approx(
        result.robust_fair
        - result.executable_vwap
        - result.fee_per_share
        - result.slippage_stress
        - result.latency_stress
    )


def test_executable_cost_selects_profitable_partial_fak_and_rejects_tail_price() -> None:
    interval = ProbabilityInterval(0.49, 0.52, 0.56)
    insufficient = evaluate_robust_executable_cost(
        side="up",
        interval=interval,
        book=_book("up", 0.34, ((0.35, 1.0),)),
        requested_shares=2.0,
        fee_rate=0.0,
        slippage_stress=0.0,
        latency_stress=0.0,
        minimum_net_edge=0.01,
        available_balance=100.0,
    )
    tail = evaluate_robust_executable_cost(
        side="up",
        interval=interval,
        book=_book("up", 0.33, ((0.34, 2.0),)),
        requested_shares=2.0,
        fee_rate=0.0,
        slippage_stress=0.0,
        latency_stress=0.0,
        minimum_net_edge=0.01,
        available_balance=100.0,
    )

    assert insufficient.reason is RobustTakerRejection.SELECTED
    assert insufficient.requested_shares == 2.0
    assert insufficient.executable_shares == 1.0
    assert insufficient.executable_vwap == pytest.approx(0.35)
    assert tail.reason is RobustTakerRejection.OUTSIDE_PRICE_BAND


def test_executable_cost_chooses_size_with_highest_total_robust_ev() -> None:
    result = evaluate_robust_executable_cost(
        side="up",
        interval=ProbabilityInterval(0.50, 0.60, 0.65),
        book=_book("up", 0.39, ((0.40, 1.0), (0.58, 4.0))),
        requested_shares=5.0,
        fee_rate=0.0,
        slippage_stress=0.0,
        latency_stress=0.0,
        minimum_net_edge=0.01,
        available_balance=100.0,
    )

    assert result.reason is RobustTakerRejection.SELECTED
    assert result.executable_shares == 1.0
    assert result.robust_net_edge == pytest.approx(0.10)


def test_probability_below_half_can_trade_when_robust_edge_is_supported() -> None:
    result = evaluate_robust_executable_cost(
        side="up",
        interval=ProbabilityInterval(0.49, 0.49, 0.51),
        book=_book("up", 0.34, ((0.35, 2.0),)),
        requested_shares=2.0,
        fee_rate=0.0,
        slippage_stress=0.01,
        latency_stress=0.01,
        minimum_net_edge=0.03,
        available_balance=100.0,
    )

    assert result.reason is RobustTakerRejection.SELECTED


def test_one_signal_policy_submits_first_qualifying_cadence_only_even_after_zero_fill() -> None:
    session, pair = _pair()
    policy = OneSignalTakerPolicy()
    rejected = policy.evaluate(
        session=session,
        pair=pair,
        interval=ProbabilityInterval(0.48, 0.50, 0.52),
        requested_shares=2.0,
        fee_rate_by_side={TokenSide.UP: 0.0, TokenSide.DOWN: 0.0},
        slippage_stress=0.0,
        latency_stress=0.0,
        minimum_net_edge=0.10,
        available_balance=100.0,
    )
    assert rejected.plan is None

    session, pair = _pair(decision_seconds=10)
    qualified = policy.evaluate(
        session=session,
        pair=pair,
        interval=ProbabilityInterval(0.60, 0.62, 0.65),
        requested_shares=2.0,
        fee_rate_by_side={TokenSide.UP: 0.0, TokenSide.DOWN: 0.0},
        slippage_stress=0.0,
        latency_stress=0.0,
        minimum_net_edge=0.03,
        available_balance=100.0,
    )
    assert qualified.plan is not None
    policy.record_execution_result(session.market_slug, filled_shares=0.0)

    session, pair = _pair(decision_seconds=15)
    later = policy.evaluate(
        session=session,
        pair=pair,
        interval=ProbabilityInterval(0.70, 0.72, 0.75),
        requested_shares=2.0,
        fee_rate_by_side={TokenSide.UP: 0.0, TokenSide.DOWN: 0.0},
        slippage_stress=0.0,
        latency_stress=0.0,
        minimum_net_edge=0.03,
        available_balance=100.0,
    )
    assert later.reason is RobustTakerRejection.ALREADY_CONSUMED


def test_one_signal_policy_selects_side_with_highest_total_robust_ev() -> None:
    session, pair = _pair(
        up_ask=0.40,
        down_ask=0.35,
        up_asks=((0.40, 1.0), (0.59, 4.0)),
        down_asks=((0.35, 5.0),),
    )

    decision = OneSignalTakerPolicy().evaluate(
        session=session,
        pair=pair,
        interval=ProbabilityInterval(0.55, 0.55, 0.55),
        requested_shares=5.0,
        fee_rate_by_side={TokenSide.UP: 0.0, TokenSide.DOWN: 0.0},
        slippage_stress=0.0,
        latency_stress=0.0,
        minimum_net_edge=0.01,
        available_balance=100.0,
    )

    assert decision.plan is not None
    assert decision.plan.side is TokenSide.DOWN
    assert decision.plan.evaluation.executable_shares == 5.0
