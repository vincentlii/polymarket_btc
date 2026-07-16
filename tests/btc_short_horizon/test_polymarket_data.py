from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btc_short_horizon.data.polymarket import PolymarketL2Normalizer, PolymarketL2Status


RECEIVE = datetime(2026, 4, 13, 0, 0, 1, tzinfo=UTC)
TOKEN = "token-1"


def test_polymarket_book_price_change_trade_and_tick_events_are_causal() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)
    snapshot = normalizer.apply(
        {
            "event_type": "book",
            "asset_id": TOKEN,
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.52", "size": "20"}],
            "timestamp": "1776038400000",
            "hash": "snapshot",
        },
        collector_receive_ts=RECEIVE,
    )
    assert snapshot.status is PolymarketL2Status.APPLIED
    assert snapshot.book_top is not None

    changed = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400100",
            "price_changes": [{"asset_id": TOKEN, "price": "0.51", "size": "5", "side": "BUY"}],
        },
        collector_receive_ts=RECEIVE,
    )
    assert changed.book_top is not None
    assert changed.book_top.bid == pytest.approx(0.51)

    trade = normalizer.apply(
        {
            "event_type": "last_trade_price",
            "asset_id": TOKEN,
            "price": "0.51",
            "size": "2",
            "side": "BUY",
            "timestamp": "1776038400200",
        },
        collector_receive_ts=RECEIVE,
    )
    assert trade.trade is not None
    assert trade.trade.aggressor_side == "buy"

    tick = normalizer.apply(
        {
            "event_type": "tick_size_change",
            "asset_id": TOKEN,
            "new_tick_size": "0.01",
            "timestamp": "1776038400300",
        },
        collector_receive_ts=RECEIVE,
    )
    assert tick.tick_size == pytest.approx(0.01)
    assert not tick.tick_size_changed


def test_polymarket_availability_remains_monotonic_when_source_clock_jitters() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)
    source_start = datetime(2026, 4, 13, tzinfo=UTC)
    snapshot = normalizer.apply(
        {
            "event_type": "book",
            "asset_id": TOKEN,
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.52", "size": "20"}],
            "timestamp": int((source_start + timedelta(milliseconds=100)).timestamp() * 1_000),
        },
        collector_receive_ts=source_start + timedelta(milliseconds=10),
    )
    changed = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": int((source_start + timedelta(milliseconds=50)).timestamp() * 1_000),
            "price_changes": [{"asset_id": TOKEN, "price": "0.51", "size": "5", "side": "BUY"}],
        },
        collector_receive_ts=source_start + timedelta(milliseconds=20),
    )

    assert snapshot.timing is not None
    assert changed.timing is not None
    assert changed.timing.available_ts >= snapshot.timing.available_ts
    assert changed.book_top is not None
    assert changed.book_top.available_ts_ns == int(changed.timing.available_ts.timestamp() * 1e9)


def test_polymarket_delta_before_snapshot_requires_resnapshot_and_crossed_book_is_invalid() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)
    waiting = normalizer.apply(
        {"event_type": "price_change", "timestamp": "1776038400000", "price_changes": []},
        collector_receive_ts=RECEIVE,
    )
    assert waiting.status is PolymarketL2Status.AWAITING_SNAPSHOT

    invalid = normalizer.apply(
        {
            "event_type": "book",
            "asset_id": TOKEN,
            "bids": [{"price": "0.52", "size": "1"}],
            "asks": [{"price": "0.52", "size": "1"}],
            "timestamp": "1776038400000",
        },
        collector_receive_ts=RECEIVE,
    )
    assert invalid.status is PolymarketL2Status.INVALID


def test_polymarket_reset_discards_book_and_tick_state_after_a_connection_gap() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)
    normalizer.apply(
        {
            "event_type": "book",
            "asset_id": TOKEN,
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.52", "size": "20"}],
            "timestamp": "1776038400000",
        },
        collector_receive_ts=RECEIVE,
    )
    normalizer.apply(
        {
            "event_type": "tick_size_change",
            "asset_id": TOKEN,
            "new_tick_size": "0.01",
            "timestamp": "1776038400100",
        },
        collector_receive_ts=RECEIVE,
    )

    normalizer.reset()
    waiting = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400200",
            "price_changes": [{"asset_id": TOKEN, "price": "0.51", "size": "5", "side": "BUY"}],
        },
        collector_receive_ts=RECEIVE,
    )

    assert normalizer.tick_size is None
    assert waiting.status is PolymarketL2Status.AWAITING_SNAPSHOT


def test_polymarket_market_resolved_event_is_retained_for_its_token_ids() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)
    resolved = normalizer.apply(
        {
            "event_type": "market_resolved",
            "assets_ids": [TOKEN, "token-2"],
            "winning_asset_id": TOKEN,
            "winning_outcome": "Up",
            "timestamp": "1776038400200",
        },
        collector_receive_ts=RECEIVE,
    )
    unrelated = PolymarketL2Normalizer(token_id="token-3").apply(
        {
            "event_type": "market_resolved",
            "assets_ids": [TOKEN, "token-2"],
            "winning_asset_id": TOKEN,
            "winning_outcome": "Up",
            "timestamp": "1776038400200",
        },
        collector_receive_ts=RECEIVE,
    )

    assert resolved.status is PolymarketL2Status.APPLIED
    assert resolved.event_type == "market_resolved"
    assert unrelated.status is PolymarketL2Status.IGNORED
