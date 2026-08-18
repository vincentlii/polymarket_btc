from __future__ import annotations

import pytest

from btc_short_horizon.strategy import (
    ConsecutiveSignalConfirmation,
    LayerStructure,
    MakerStrategyConfig,
    MarketExecution,
    OutcomeBooks,
    SideBook,
    StrategyPhase,
    TokenSide,
    VisibleBookLevel,
    evaluate_cancellation,
    plan_opening_mispricing_orders,
)


def _books() -> OutcomeBooks:
    return OutcomeBooks(
        up=SideBook(
            token_id="up-token",
            bids=tuple(VisibleBookLevel(price=price, size=100.0) for price in (0.60, 0.59, 0.58)),
            asks=(VisibleBookLevel(price=0.61, size=100.0),),
            tick_size=0.01,
            minimum_order_size=1.0,
        ),
        down=SideBook(
            token_id="down-token",
            bids=tuple(VisibleBookLevel(price=price, size=100.0) for price in (0.34, 0.33, 0.32)),
            asks=(VisibleBookLevel(price=0.35, size=100.0),),
            tick_size=0.01,
            minimum_order_size=1.0,
        ),
    )


def _config(**overrides: object) -> MakerStrategyConfig:
    values: dict[str, object] = {
        "structure": LayerStructure.THREE_LEVEL,
        "max_shares": 10.0,
        "safety_buffer": 0.02,
        "minimum_edge": 0.01,
        "entry_start_seconds": 3.0,
        "entry_end_seconds": 180.0,
        "confirmation_signals": 2,
        "signal_cadence_seconds": 5.0,
        "max_work_seconds": 60.0,
        "stale_after_seconds": 1.0,
        "cancel_probability_drop": 0.03,
        "max_visible_depth_fraction": 1.0,
        "price_level_tick_offsets": (0, 1, 2),
    }
    values.update(overrides)
    return MakerStrategyConfig(**values)


def _up_plan():
    decision = plan_opening_mispricing_orders(
        market_slug="btc-updown-15m-1776038400",
        p_boundary_up=0.65,
        p_up=0.72,
        books=_books(),
        decision_ts_ns=5_000_000_000,
        elapsed_seconds=5.0,
        config=_config(),
    )
    assert decision.plan is not None
    return decision.plan


def test_plan_selects_up_from_fair_probability_and_pre_registered_passive_levels() -> None:
    plan = _up_plan()

    assert plan.side is TokenSide.UP
    assert plan.token_id == "up-token"
    assert plan.p_fair == pytest.approx(0.72)
    assert plan.p_market == pytest.approx(0.63)
    assert [layer.price for layer in plan.layers] == [0.60, 0.59, 0.58]
    assert [layer.size for layer in plan.layers] == [5.0, 3.0, 2.0]
    assert all(plan.net_edge(layer.price) >= plan.minimum_edge for layer in plan.layers)


def test_plan_selects_down_and_does_not_invent_a_deeper_price_to_create_edge() -> None:
    down = plan_opening_mispricing_orders(
        market_slug="btc-updown-15m-1776038400",
        p_boundary_up=0.55,
        p_up=0.40,
        books=_books(),
        decision_ts_ns=5_000_000_000,
        elapsed_seconds=5.0,
        config=_config(
            structure=LayerStructure.SINGLE,
            price_level_tick_offsets=(0,),
        ),
    )

    assert down.plan is not None
    assert down.plan.side is TokenSide.DOWN
    assert down.plan.p_fair == pytest.approx(0.60)
    assert down.plan.p_market == pytest.approx(0.37)
    assert down.plan.layers[0].price == pytest.approx(0.34)

    rejected = plan_opening_mispricing_orders(
        market_slug="btc-updown-15m-1776038400",
        p_boundary_up=0.60,
        p_up=0.63,
        books=_books(),
        decision_ts_ns=5_000_000_000,
        elapsed_seconds=5.0,
        config=_config(
            structure=LayerStructure.SINGLE,
            safety_buffer=0.03,
            price_level_tick_offsets=(0,),
        ),
    )

    assert not rejected.accepted
    assert rejected.reason == "no_passive_price_with_required_edge"


def test_plan_respects_opening_entry_window() -> None:
    decision = plan_opening_mispricing_orders(
        market_slug="btc-updown-15m-1776038400",
        p_boundary_up=0.65,
        p_up=0.72,
        books=_books(),
        decision_ts_ns=2_000_000_000,
        elapsed_seconds=2.0,
        config=_config(),
    )

    assert not decision.accepted
    assert decision.reason == "outside_entry_window"


def test_plan_caps_each_layer_by_visible_depth_and_rejects_below_venue_minimum() -> None:
    capped = plan_opening_mispricing_orders(
        market_slug="btc-updown-15m-1776038400",
        p_boundary_up=0.65,
        p_up=0.72,
        books=_books(),
        decision_ts_ns=5_000_000_000,
        elapsed_seconds=5.0,
        config=_config(
            structure=LayerStructure.SINGLE,
            max_shares=10.0,
            max_visible_depth_fraction=0.05,
            price_level_tick_offsets=(0,),
        ),
    )

    assert capped.plan is not None
    assert capped.plan.layers[0].size == pytest.approx(5.0)
    assert capped.plan.layers[0].visible_depth_fraction == pytest.approx(0.05)

    too_small_book = OutcomeBooks(
        up=SideBook(
            token_id="up-token",
            bids=(VisibleBookLevel(price=0.60, size=50.0),),
            asks=(VisibleBookLevel(price=0.61, size=50.0),),
            tick_size=0.01,
            minimum_order_size=5.0,
        ),
        down=_books().down,
    )
    rejected = plan_opening_mispricing_orders(
        market_slug="btc-updown-15m-1776038400",
        p_boundary_up=0.65,
        p_up=0.72,
        books=too_small_book,
        decision_ts_ns=5_000_000_000,
        elapsed_seconds=5.0,
        config=_config(
            structure=LayerStructure.SINGLE,
            max_shares=10.0,
            max_visible_depth_fraction=0.05,
            price_level_tick_offsets=(0,),
        ),
    )

    assert not rejected.accepted


def test_plan_improves_one_tick_only_when_spread_and_edge_allow_it() -> None:
    wide_up = SideBook(
        token_id="up-token",
        bids=(VisibleBookLevel(price=0.60, size=100.0),),
        asks=(VisibleBookLevel(price=0.63, size=100.0),),
        tick_size=0.01,
        minimum_order_size=1.0,
    )
    books = OutcomeBooks(up=wide_up, down=_books().down)

    improved = plan_opening_mispricing_orders(
        market_slug="btc-updown-15m-1776038400",
        p_boundary_up=0.65,
        p_up=0.72,
        books=books,
        decision_ts_ns=5_000_000_000,
        elapsed_seconds=5.0,
        config=_config(
            structure=LayerStructure.SINGLE,
            price_level_tick_offsets=(0,),
            improve_inside_spread=True,
        ),
    )

    assert improved.plan is not None
    assert improved.plan.layers[0].price == pytest.approx(0.61)
    assert improved.plan.layers[0].queue_ahead == pytest.approx(0.0)
    assert improved.plan.layers[0].visible_size == pytest.approx(100.0)

    edge_limited = plan_opening_mispricing_orders(
        market_slug="btc-updown-15m-1776038400",
        p_boundary_up=0.65,
        p_up=0.72,
        books=books,
        decision_ts_ns=5_000_000_000,
        elapsed_seconds=5.0,
        config=_config(
            structure=LayerStructure.SINGLE,
            safety_buffer=0.01,
            minimum_edge=0.11,
            price_level_tick_offsets=(0,),
            improve_inside_spread=True,
        ),
    )

    assert edge_limited.plan is not None
    assert edge_limited.plan.layers[0].price == pytest.approx(0.60)
    assert edge_limited.plan.layers[0].queue_ahead == pytest.approx(100.0)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    (
        ({"has_data_gap": True}, "data_gap"),
        ({"data_age_seconds": 1.1}, "book_stale_connection_unobserved"),
        ({"tick_unchanged": False}, "tick_changed"),
        ({"selected_probability": 0.68}, "probability_drop"),
        (
            {
                "selected_probability": 0.60,
                "config": _config(cancel_probability_drop=0.20),
            },
            "edge_exhausted",
        ),
    ),
)
def test_cancellation_is_conservative_and_auditable(kwargs: dict[str, object], reason: str) -> None:
    values: dict[str, object] = {
        "plan": _up_plan(),
        "now_ts_ns": 5_100_000_000,
        "selected_probability": 0.72,
        "data_age_seconds": 0.1,
        "has_data_gap": False,
        "structure_valid": True,
        "tick_unchanged": True,
        "fee_unchanged": True,
        "latency_healthy": True,
        "config": _config(),
    }
    values.update(kwargs)

    assessment = evaluate_cancellation(**values)

    assert assessment.should_cancel
    assert assessment.reason == reason


def test_cancel_fill_race_preserves_fill_and_lifecycle_never_replaces_it() -> None:
    execution = MarketExecution(market_slug=_up_plan().market_slug)
    execution = execution.begin_monitoring().submit_plan(_up_plan())
    cancel_requested = execution.request_cancel()
    after_race_fill = cancel_requested.record_fill(3.0)
    canceled = after_race_fill.acknowledge_cancel()

    assert after_race_fill.phase is StrategyPhase.CANCEL_REQUESTED
    assert canceled.phase is StrategyPhase.PARTIALLY_FILLED
    assert canceled.filled_size == pytest.approx(3.0)
    resolved = canceled.record_resolution()
    assert resolved.redeem().phase is StrategyPhase.REDEEMED


def test_lifecycle_rejects_replacement_cycle_and_impossible_fill() -> None:
    execution = MarketExecution(market_slug=_up_plan().market_slug)
    execution = execution.begin_monitoring().submit_plan(_up_plan())

    with pytest.raises(ValueError, match="one placement cycle"):
        execution.submit_plan(_up_plan())
    with pytest.raises(ValueError, match="exceeds remaining"):
        execution.record_fill(100.0)


def test_signal_confirmation_requires_same_side_at_the_frozen_cadence() -> None:
    confirmation = ConsecutiveSignalConfirmation(
        required_signals=2,
        cadence_seconds=5.0,
        tolerance_seconds=0.25,
    )

    assert not confirmation.observe(TokenSide.UP, signal_ts_ns=5_000_000_000)
    assert not confirmation.observe(TokenSide.UP, signal_ts_ns=9_000_000_000)
    assert confirmation.observe(TokenSide.UP, signal_ts_ns=14_000_000_000)
    assert confirmation.count == 2
    assert not confirmation.observe(TokenSide.DOWN, signal_ts_ns=19_000_000_000)
