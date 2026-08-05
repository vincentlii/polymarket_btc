from __future__ import annotations

import pytest

from btc_short_horizon.strategy import (
    EdgeStableSignalConfirmation,
    OutcomeBooks,
    SideBook,
    TakerRejectionReason,
    TokenSide,
    VisibleBookLevel,
    plan_independent_taker_order,
)


def _book(token: str, bids, asks) -> SideBook:  # type: ignore[no-untyped-def]
    return SideBook(
        token_id=token,
        bids=tuple(VisibleBookLevel(price=p, size=s) for p, s in bids),
        asks=tuple(VisibleBookLevel(price=p, size=s) for p, s in asks),
        tick_size=0.01,
        minimum_order_size=1.0,
    )


def test_independent_taker_selects_best_net_edge_across_full_ask_depth() -> None:
    books = OutcomeBooks(
        up=_book("up", [(0.39, 20)], [(0.40, 2), (0.42, 5)]),
        down=_book("down", [(0.53, 20)], [(0.54, 5), (0.56, 5)]),
    )

    decision = plan_independent_taker_order(
        market_slug="btc-updown-15m-1",
        p_boundary_up=0.50,
        p_up=0.64,
        books=books,
        fee_rate_by_side={TokenSide.UP: 0.02, TokenSide.DOWN: 0.02},
        max_shares=5.0,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.02,
        available_balance=100.0,
        decision_ts_ns=10,
    )

    assert decision.plan is not None
    assert decision.plan.side is TokenSide.UP
    assert decision.plan.executable_vwap == pytest.approx((0.40 * 2 + 0.42 * 3) / 5)
    assert decision.plan.total_size == pytest.approx(5.0)
    assert decision.plan.taker_fees > 0.0
    assert decision.plan.net_edge_per_share > 0.03


def test_independent_taker_reports_typed_edge_rejection() -> None:
    books = OutcomeBooks(
        up=_book("up", [(0.48, 20)], [(0.50, 5)]),
        down=_book("down", [(0.48, 20)], [(0.50, 5)]),
    )

    decision = plan_independent_taker_order(
        market_slug="btc-updown-15m-1",
        p_boundary_up=0.50,
        p_up=0.51,
        books=books,
        fee_rate_by_side={TokenSide.UP: 0.0, TokenSide.DOWN: 0.0},
        max_shares=5.0,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.02,
        available_balance=100.0,
        decision_ts_ns=10,
    )

    assert decision.plan is None
    assert decision.reason is TakerRejectionReason.EDGE_BELOW_THRESHOLD
    assert len(decision.evaluations) == 2


def test_independent_taker_keeps_profitable_prefix_instead_of_diluting_to_max_size() -> None:
    books = OutcomeBooks(
        up=_book("up", [(0.49, 20)], [(0.50, 2), (0.70, 10)]),
        down=_book("down", [(0.37, 20)], [(0.39, 10)]),
    )
    decision = plan_independent_taker_order(
        market_slug="btc-updown-15m-1",
        p_boundary_up=0.50,
        p_up=0.60,
        books=books,
        fee_rate_by_side={TokenSide.UP: 0.0, TokenSide.DOWN: 0.0},
        max_shares=5.0,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.02,
        available_balance=100.0,
        decision_ts_ns=10,
    )

    assert decision.plan is not None
    assert decision.plan.side is TokenSide.UP
    assert decision.plan.total_size == pytest.approx(2.0)
    assert decision.plan.limit_price == pytest.approx(0.50)


def test_independent_taker_rejects_tail_prices_when_stage_band_is_core_only() -> None:
    books = OutcomeBooks(
        up=_book("up", [(0.10, 20)], [(0.12, 5)]),
        down=_book("down", [(0.19, 20)], [(0.20, 5)]),
    )
    decision = plan_independent_taker_order(
        market_slug="btc-updown-15m-1",
        p_boundary_up=0.50,
        p_up=0.70,
        books=books,
        fee_rate_by_side={TokenSide.UP: 0.0, TokenSide.DOWN: 0.0},
        max_shares=2.0,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.02,
        available_balance=100.0,
        decision_ts_ns=10,
        minimum_price=0.20,
        maximum_price=0.80,
    )

    assert decision.plan is not None
    assert decision.plan.side is TokenSide.DOWN


def test_edge_stable_confirmation_resets_on_decay_without_changing_old_confirmation() -> None:
    confirmation = EdgeStableSignalConfirmation(
        required_signals=3,
        cadence_seconds=5.0,
        tolerance_seconds=0.25,
        maximum_edge_decay=0.01,
    )

    assert not confirmation.observe(TokenSide.UP, net_edge=0.08, signal_ts_ns=5_000_000_000)
    assert not confirmation.observe(TokenSide.UP, net_edge=0.075, signal_ts_ns=10_000_000_000)
    assert not confirmation.observe(TokenSide.UP, net_edge=0.06, signal_ts_ns=15_000_000_000)
    assert confirmation.count == 1
    assert confirmation.last_reset_reason == "edge_decay_reset"
    assert not confirmation.observe(TokenSide.UP, net_edge=0.059, signal_ts_ns=20_000_000_000)
    assert confirmation.observe(TokenSide.UP, net_edge=0.058, signal_ts_ns=25_000_000_000)


def test_edge_stable_confirmation_resets_on_cadence_break() -> None:
    confirmation = EdgeStableSignalConfirmation(
        required_signals=2,
        cadence_seconds=5.0,
        tolerance_seconds=0.25,
        maximum_edge_decay=0.01,
    )
    confirmation.observe(TokenSide.DOWN, net_edge=0.05, signal_ts_ns=5_000_000_000)

    assert not confirmation.observe(
        TokenSide.DOWN,
        net_edge=0.05,
        signal_ts_ns=11_000_000_000,
    )
    assert confirmation.count == 1
    assert confirmation.last_reset_reason == "cadence_reset"
