from __future__ import annotations

from datetime import UTC, datetime

import pytest

from btc_short_horizon.live.gateway import PaperOrderGateway
from btc_short_horizon.live.paper_execution import (
    PaperExecutionConfig,
    PaperExecutionSimulator,
    PaperMarketRules,
)
from btc_short_horizon.strategy import (
    MakerOrderLayer,
    OrderPlan,
    SideBook,
    TokenSide,
    VisibleBookLevel,
)


NOW_NS = int(datetime(2026, 7, 27, tzinfo=UTC).timestamp() * 1_000_000_000)
CONDITION_ID = "0x" + "ab" * 32
UP_TOKEN = "1"


def _book(
    *,
    bid: float = 0.4,
    ask: float = 0.42,
    bid_size: float = 10.0,
    asks: tuple[VisibleBookLevel, ...] | None = None,
) -> SideBook:
    return SideBook(
        token_id=UP_TOKEN,
        bids=(VisibleBookLevel(price=bid, size=bid_size),),
        asks=asks or (VisibleBookLevel(price=ask, size=12.0),),
        tick_size=0.01,
        minimum_order_size=1.0,
    )


def _plan() -> OrderPlan:
    return OrderPlan(
        market_slug="btc-updown-15m-1785110400",
        token_id=UP_TOKEN,
        side=TokenSide.UP,
        p_boundary=0.55,
        p_fair=0.60,
        p_market=0.41,
        safety_buffer=0.01,
        minimum_edge=0.10,
        created_ts_ns=NOW_NS,
        expires_ts_ns=NOW_NS + 15_000_000_000,
        layers=(MakerOrderLayer(price=0.4, size=2.0, visible_size=10.0),),
    )


def _rules(*, taker_fee_rate: float = 0.0) -> PaperMarketRules:
    return PaperMarketRules(
        condition_id=CONDITION_ID,
        token_id=UP_TOKEN,
        tick_size="0.01",
        minimum_order_size=1.0,
        neg_risk=False,
        maker_fee_rate_bps=0,
        observed_at_ns=NOW_NS - 1,
        taker_fee_rate=taker_fee_rate,
    )


def _simulator() -> PaperExecutionSimulator:
    return PaperExecutionSimulator(
        gateway=PaperOrderGateway(),
        config=PaperExecutionConfig(
            insert_latency_ms=50.0,
            cancel_latency_ms=100.0,
            taker_latency_ms=50.0,
            trade_volume_multiplier=0.5,
        ),
    )


def test_paper_fill_requires_insert_latency_sell_trade_and_queue_consumption() -> None:
    simulator = _simulator()
    placement = simulator.submit(plan=_plan(), rules=_rules(), book=_book(), now_ts_ns=NOW_NS)

    simulator.on_trade(
        token_id=UP_TOKEN,
        aggressor_side="sell",
        price=0.4,
        size=100.0,
        available_ts_ns=NOW_NS + 49_000_000,
    )
    simulator.advance(now_ts_ns=NOW_NS + 50_000_000, books={UP_TOKEN: _book()})
    simulator.on_trade(
        token_id=UP_TOKEN,
        aggressor_side="buy",
        price=0.4,
        size=100.0,
        available_ts_ns=NOW_NS + 51_000_000,
    )
    simulator.on_trade(
        token_id=UP_TOKEN,
        aggressor_side="sell",
        price=0.4,
        size=20.0,
        available_ts_ns=NOW_NS + 52_000_000,
    )

    assert placement.filled_size == 0.0
    assert placement.layers[0].queue_ahead == pytest.approx(0.0)

    simulator.on_trade(
        token_id=UP_TOKEN,
        aggressor_side="sell",
        price=0.4,
        size=2.0,
        available_ts_ns=NOW_NS + 53_000_000,
    )
    assert placement.filled_size == pytest.approx(1.0)
    assert placement.status == "cancel_pending"


def test_paper_cancel_latency_preserves_race_fill_then_cancels_remainder() -> None:
    simulator = _simulator()
    placement = simulator.submit(plan=_plan(), rules=_rules(), book=_book(), now_ts_ns=NOW_NS)
    simulator.advance(now_ts_ns=NOW_NS + 50_000_000, books={UP_TOKEN: _book()})
    simulator.on_trade(
        token_id=UP_TOKEN,
        aggressor_side="sell",
        price=0.4,
        size=22.0,
        available_ts_ns=NOW_NS + 60_000_000,
    )
    simulator.on_trade(
        token_id=UP_TOKEN,
        aggressor_side="sell",
        price=0.4,
        size=2.0,
        available_ts_ns=NOW_NS + 100_000_000,
    )
    simulator.advance(now_ts_ns=NOW_NS + 160_000_000, books={UP_TOKEN: _book()})

    assert placement.filled_size == pytest.approx(2.0)
    assert placement.status == "filled"
    assert placement.cancel_race_filled_size == pytest.approx(1.0)


def test_paper_post_only_rejects_when_book_crosses_before_insert_arrives() -> None:
    simulator = _simulator()
    placement = simulator.submit(plan=_plan(), rules=_rules(), book=_book(), now_ts_ns=NOW_NS)

    simulator.advance(
        now_ts_ns=NOW_NS + 50_000_000,
        books={UP_TOKEN: _book(bid=0.39, ask=0.4)},
    )

    assert placement.status == "rejected"
    assert placement.rejection_reason == "post_only_would_cross"


def test_paper_market_rules_reject_stale_or_mismatched_order_inputs() -> None:
    simulator = _simulator()
    stale = PaperMarketRules(
        condition_id=CONDITION_ID,
        token_id=UP_TOKEN,
        tick_size="0.001",
        minimum_order_size=1.0,
        neg_risk=False,
        maker_fee_rate_bps=0,
        observed_at_ns=NOW_NS - 1,
    )

    with pytest.raises(ValueError, match="tick size"):
        simulator.submit(plan=_plan(), rules=stale, book=_book(), now_ts_ns=NOW_NS)


def test_fak_waits_for_cancel_ack_and_charges_taker_fee() -> None:
    simulator = _simulator()
    rules = PaperMarketRules(
        condition_id=CONDITION_ID,
        token_id=UP_TOKEN,
        tick_size="0.01",
        minimum_order_size=1.0,
        neg_risk=False,
        maker_fee_rate_bps=0,
        observed_at_ns=NOW_NS - 1,
        taker_fee_rate=0.07,
    )
    placement = simulator.submit(plan=_plan(), rules=rules, book=_book(), now_ts_ns=NOW_NS)
    simulator.advance(now_ts_ns=NOW_NS + 50_000_000, books={UP_TOKEN: _book()})
    simulator.request_cancel(placement, now_ts_ns=NOW_NS + 60_000_000, reason="fak_upgrade")

    assert not simulator.request_fak(
        placement,
        now_ts_ns=NOW_NS + 60_000_000,
        selected_probability=0.60,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.03,
    )

    simulator.advance(now_ts_ns=NOW_NS + 160_000_000, books={UP_TOKEN: _book()})
    assert simulator.request_fak(
        placement,
        now_ts_ns=NOW_NS + 160_000_000,
        selected_probability=0.60,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.03,
    )
    simulator.advance(now_ts_ns=NOW_NS + 210_000_000, books={UP_TOKEN: _book()})

    assert placement.status == "filled"
    assert placement.execution_route == "maker_then_fak"
    assert placement.taker_filled_size == pytest.approx(2.0)
    assert placement.taker_fees == pytest.approx(0.0341)
    assert placement.terminal_reason == "fak_filled"


def test_direct_fak_waits_for_taker_latency_without_creating_a_maker_order() -> None:
    simulator = _simulator()
    placement = simulator.submit_direct_fak(
        plan=_plan(),
        rules=_rules(),
        book=_book(ask=0.55),
        now_ts_ns=NOW_NS,
        selected_probability=0.60,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.03,
    )

    assert placement.status == "fak_pending"
    assert placement.execution_route == "direct_fak"
    assert placement.layers == ()
    assert placement.cancel_requested_ts_ns is None
    assert placement.cancel_ack_ts_ns is None

    simulator.advance(now_ts_ns=NOW_NS + 49_000_000, books={UP_TOKEN: _book(ask=0.42)})
    assert placement.filled_size == 0.0

    simulator.advance(now_ts_ns=NOW_NS + 50_000_000, books={UP_TOKEN: _book(ask=0.42)})
    assert placement.status == "filled"
    assert placement.taker_filled_size == pytest.approx(2.0)
    assert placement.taker_filled_notional == pytest.approx(0.84)
    assert placement.taker_fees == 0.0
    assert placement.terminal_reason == "fak_filled"


def test_fak_preview_walks_full_ask_depth_with_fee_and_buffers() -> None:
    simulator = _simulator()
    quote = simulator.preview_fak(
        token_id=UP_TOKEN,
        requested_size=2.0,
        rules=_rules(taker_fee_rate=0.07),
        book=_book(
            asks=(
                VisibleBookLevel(price=0.42, size=1.0),
                VisibleBookLevel(price=0.43, size=1.5),
            )
        ),
        now_ts_ns=NOW_NS,
        selected_probability=0.60,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.03,
    )

    assert quote.requested_size == pytest.approx(2.0)
    assert quote.filled_size == pytest.approx(2.0)
    assert quote.filled_notional == pytest.approx(0.85)
    assert quote.taker_fees == pytest.approx(0.03421)
    assert quote.average_price == pytest.approx(0.425)
    assert quote.all_in_average_price == pytest.approx(0.442105)
    assert quote.limit_price == pytest.approx(0.43)
    assert quote.net_edge_per_share == pytest.approx(0.122895)
    assert quote.fully_filled
    assert simulator.placements == ()


def test_direct_fak_partially_fills_once_and_never_retries_the_remainder() -> None:
    simulator = _simulator()
    placement = simulator.submit_direct_fak(
        plan=_plan(),
        rules=_rules(taker_fee_rate=0.07),
        book=_book(),
        now_ts_ns=NOW_NS,
        selected_probability=0.60,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.03,
    )
    partial_book = _book(asks=(VisibleBookLevel(price=0.42, size=1.0),))

    simulator.advance(now_ts_ns=NOW_NS + 50_000_000, books={UP_TOKEN: partial_book})

    assert placement.status == "partially_filled"
    assert placement.taker_filled_size == pytest.approx(1.0)
    assert placement.taker_filled_notional == pytest.approx(0.42)
    assert placement.taker_fees == pytest.approx(0.01705)
    assert placement.terminal_reason == "fak_partial_depth"

    simulator.advance(now_ts_ns=NOW_NS + 100_000_000, books={UP_TOKEN: _book()})
    assert placement.taker_filled_size == pytest.approx(1.0)
    assert placement.taker_fees == pytest.approx(0.01705)


def test_direct_fak_uses_latency_time_book_and_cancels_when_edge_is_insufficient() -> None:
    simulator = _simulator()
    placement = simulator.submit_direct_fak(
        plan=_plan(),
        rules=_rules(taker_fee_rate=0.07),
        book=_book(ask=0.42),
        now_ts_ns=NOW_NS,
        selected_probability=0.60,
        minimum_net_edge=0.03,
        slippage_buffer=0.005,
        model_uncertainty_buffer=0.03,
    )

    simulator.advance(now_ts_ns=NOW_NS + 50_000_000, books={UP_TOKEN: _book(ask=0.55)})

    assert placement.status == "canceled"
    assert placement.filled_size == 0.0
    assert placement.fak_limit_price is None
    assert placement.fak_net_edge_per_share is None
    assert placement.terminal_reason == "fak_net_edge_insufficient"


def test_direct_fak_rejects_a_duplicate_placement() -> None:
    simulator = _simulator()
    arguments = {
        "plan": _plan(),
        "rules": _rules(),
        "book": _book(),
        "now_ts_ns": NOW_NS,
        "selected_probability": 0.60,
        "minimum_net_edge": 0.03,
        "slippage_buffer": 0.005,
        "model_uncertainty_buffer": 0.03,
    }
    simulator.submit_direct_fak(**arguments)

    with pytest.raises(ValueError, match="already exists"):
        simulator.submit_direct_fak(**arguments)


def test_direct_fak_validates_market_time_and_execution_policy() -> None:
    simulator = _simulator()
    mismatched_rules = PaperMarketRules(
        condition_id=CONDITION_ID,
        token_id="2",
        tick_size="0.01",
        minimum_order_size=1.0,
        neg_risk=False,
        maker_fee_rate_bps=0,
        observed_at_ns=NOW_NS - 1,
    )
    future_rules = PaperMarketRules(
        condition_id=CONDITION_ID,
        token_id=UP_TOKEN,
        tick_size="0.01",
        minimum_order_size=1.0,
        neg_risk=False,
        maker_fee_rate_bps=0,
        observed_at_ns=NOW_NS + 1,
    )
    valid = {
        "plan": _plan(),
        "rules": _rules(),
        "book": _book(),
        "now_ts_ns": NOW_NS,
        "selected_probability": 0.60,
        "minimum_net_edge": 0.03,
        "slippage_buffer": 0.005,
        "model_uncertainty_buffer": 0.03,
    }

    with pytest.raises(ValueError, match="IDs must match"):
        simulator.submit_direct_fak(**(valid | {"rules": mismatched_rules}))
    with pytest.raises(ValueError, match="observed in the future"):
        simulator.submit_direct_fak(**(valid | {"rules": future_rules}))
    with pytest.raises(ValueError, match="at or after plan creation"):
        simulator.submit_direct_fak(**(valid | {"now_ts_ns": NOW_NS - 1}))
    with pytest.raises(ValueError, match="before plan expiry"):
        simulator.submit_direct_fak(**(valid | {"now_ts_ns": NOW_NS + 16_000_000_000}))
    with pytest.raises(ValueError, match="selected_probability"):
        simulator.submit_direct_fak(**(valid | {"selected_probability": 1.0}))
    with pytest.raises(ValueError, match="selected_probability"):
        simulator.submit_direct_fak(**(valid | {"selected_probability": "0.60"}))
    with pytest.raises(ValueError, match="minimum_net_edge"):
        simulator.submit_direct_fak(**(valid | {"minimum_net_edge": -0.01}))
    with pytest.raises(ValueError, match="sum to less than 1"):
        simulator.submit_direct_fak(
            **(
                valid
                | {
                    "minimum_net_edge": 0.40,
                    "slippage_buffer": 0.30,
                    "model_uncertainty_buffer": 0.30,
                }
            )
        )

    with pytest.raises(ValueError, match="minimum order size"):
        simulator.preview_fak(
            token_id=UP_TOKEN,
            requested_size=0.5,
            rules=_rules(),
            book=_book(),
            now_ts_ns=NOW_NS,
            selected_probability=0.60,
            minimum_net_edge=0.03,
            slippage_buffer=0.005,
            model_uncertainty_buffer=0.03,
        )
    with pytest.raises(TypeError, match="plan"):
        simulator.submit_direct_fak(**(valid | {"plan": object()}))
