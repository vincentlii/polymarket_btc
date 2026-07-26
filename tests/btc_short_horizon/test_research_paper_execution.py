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


def _book(*, bid: float = 0.4, ask: float = 0.42, bid_size: float = 10.0) -> SideBook:
    return SideBook(
        token_id=UP_TOKEN,
        bids=(VisibleBookLevel(price=bid, size=bid_size),),
        asks=(VisibleBookLevel(price=ask, size=12.0),),
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


def _rules() -> PaperMarketRules:
    return PaperMarketRules(
        condition_id=CONDITION_ID,
        token_id=UP_TOKEN,
        tick_size="0.01",
        minimum_order_size=1.0,
        neg_risk=False,
        maker_fee_rate_bps=0,
        observed_at_ns=NOW_NS - 1,
    )


def _simulator() -> PaperExecutionSimulator:
    return PaperExecutionSimulator(
        gateway=PaperOrderGateway(),
        config=PaperExecutionConfig(
            insert_latency_ms=50.0,
            cancel_latency_ms=100.0,
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
