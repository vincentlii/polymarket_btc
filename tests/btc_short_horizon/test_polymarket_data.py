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


def test_polymarket_small_source_regressions_cannot_ratchet_down_watermark() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)
    source_start = datetime(2026, 4, 13, tzinfo=UTC)
    normalizer.apply(
        {
            "event_type": "book",
            "asset_id": TOKEN,
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.52", "size": "20"}],
            "timestamp": int((source_start + timedelta(seconds=2)).timestamp() * 1_000),
        },
        collector_receive_ts=source_start + timedelta(seconds=3),
    )
    bounded = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": int((source_start + timedelta(seconds=1.4)).timestamp() * 1_000),
            "price_changes": [
                {
                    "asset_id": TOKEN,
                    "price": "0.51",
                    "size": "5",
                    "side": "BUY",
                }
            ],
        },
        collector_receive_ts=source_start + timedelta(seconds=4),
    )
    cumulative = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": int((source_start + timedelta(seconds=0.8)).timestamp() * 1_000),
            "price_changes": [
                {
                    "asset_id": TOKEN,
                    "price": "0.49",
                    "size": "5",
                    "side": "BUY",
                }
            ],
        },
        collector_receive_ts=source_start + timedelta(seconds=5),
    )

    assert bounded.status is PolymarketL2Status.APPLIED
    assert cumulative.status is PolymarketL2Status.INVALID
    assert cumulative.reason == "source_timestamp_regression"
    assert not normalizer.has_snapshot


def test_polymarket_material_source_regression_fails_closed_with_monotonic_receive_time() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)
    source_start = datetime(2026, 4, 13, tzinfo=UTC)
    normalizer.apply(
        {
            "event_type": "book",
            "asset_id": TOKEN,
            "bids": [{"price": "0.50", "size": "10"}],
            "asks": [{"price": "0.52", "size": "20"}],
            "timestamp": int((source_start + timedelta(seconds=2)).timestamp() * 1_000),
        },
        collector_receive_ts=source_start + timedelta(seconds=3),
    )

    regressed = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": int(source_start.timestamp() * 1_000),
            "price_changes": [{"asset_id": TOKEN, "price": "0.51", "size": "5", "side": "BUY"}],
        },
        collector_receive_ts=source_start + timedelta(seconds=4),
    )

    assert regressed.status is PolymarketL2Status.INVALID
    assert regressed.reason == "source_timestamp_regression"
    assert regressed.starts_new_epoch
    assert regressed.requires_resubscribe
    assert not normalizer.has_snapshot


def test_polymarket_delta_before_snapshot_requires_resnapshot_and_crossed_book_is_invalid() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)
    waiting = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400000",
            "price_changes": [{"asset_id": TOKEN, "price": "0.50", "size": "1", "side": "BUY"}],
        },
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


def test_polymarket_other_token_delta_does_not_require_a_snapshot() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)

    ignored = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400000",
            "price_changes": [
                {"asset_id": "other-token", "price": "0.50", "size": "1", "side": "BUY"}
            ],
        },
        collector_receive_ts=RECEIVE,
    )

    assert ignored.status is PolymarketL2Status.IGNORED
    assert ignored.reason == "other_token"
    assert not ignored.requires_resubscribe


def test_polymarket_one_sided_snapshot_is_valid_terminal_book_state() -> None:
    normalizer = PolymarketL2Normalizer(token_id=TOKEN)

    terminal = normalizer.apply(
        {
            "event_type": "book",
            "asset_id": TOKEN,
            "bids": [{"price": "0.99", "size": "10"}],
            "asks": [],
            "timestamp": "1776038400000",
        },
        collector_receive_ts=RECEIVE,
    )

    assert terminal.status is PolymarketL2Status.APPLIED
    assert terminal.book_top is None
    assert normalizer.has_snapshot
    assert not terminal.requires_resubscribe


def test_polymarket_price_change_may_leave_a_valid_one_sided_book() -> None:
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

    terminal = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400100",
            "price_changes": [{"asset_id": TOKEN, "price": "0.52", "size": "0", "side": "SELL"}],
        },
        collector_receive_ts=RECEIVE,
    )

    assert terminal.status is PolymarketL2Status.APPLIED
    assert terminal.book_top is None
    assert normalizer.has_snapshot
    assert not terminal.requires_resubscribe


def test_polymarket_malformed_snapshot_discards_prior_book_and_tick() -> None:
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

    with pytest.raises(ValueError, match="price must be numeric"):
        normalizer.apply(
            {
                "event_type": "book",
                "asset_id": TOKEN,
                "bids": [
                    {"price": "0.49", "size": "5"},
                    {"price": "invalid", "size": "1"},
                ],
                "asks": [{"price": "0.53", "size": "4"}],
                "timestamp": "1776038400200",
            },
            collector_receive_ts=RECEIVE,
        )

    changed = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400300",
            "price_changes": [{"asset_id": TOKEN, "price": "0.51", "size": "2", "side": "BUY"}],
        },
        collector_receive_ts=RECEIVE,
    )

    assert changed.status is PolymarketL2Status.AWAITING_SNAPSHOT
    assert changed.book_top is None
    assert normalizer.tick_size is None


def test_polymarket_malformed_price_change_batch_discards_prior_book() -> None:
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

    with pytest.raises(ValueError, match="side must be BUY or SELL"):
        normalizer.apply(
            {
                "event_type": "price_change",
                "timestamp": "1776038400100",
                "price_changes": [
                    {"asset_id": TOKEN, "price": "0.51", "size": "5", "side": "BUY"},
                    {"asset_id": TOKEN, "price": "0.53", "size": "2", "side": "HOLD"},
                ],
            },
            collector_receive_ts=RECEIVE,
        )

    changed = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400200",
            "price_changes": [
                {"asset_id": TOKEN, "price": "0.54", "size": "3", "side": "SELL"},
                {"asset_id": TOKEN, "price": "0.52", "size": "0", "side": "SELL"},
            ],
        },
        collector_receive_ts=RECEIVE,
    )

    assert changed.status is PolymarketL2Status.AWAITING_SNAPSHOT
    assert changed.book_top is None


def test_polymarket_fork_uses_independent_copy_on_write_book_state() -> None:
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
    candidate = normalizer.fork()
    candidate_change = candidate.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400100",
            "price_changes": [
                {
                    "asset_id": TOKEN,
                    "price": "0.51",
                    "size": "5",
                    "side": "BUY",
                }
            ],
        },
        collector_receive_ts=RECEIVE,
    )
    original_change = normalizer.apply(
        {
            "event_type": "price_change",
            "timestamp": "1776038400100",
            "price_changes": [
                {
                    "asset_id": TOKEN,
                    "price": "0.505",
                    "size": "5",
                    "side": "BUY",
                }
            ],
        },
        collector_receive_ts=RECEIVE,
    )

    assert candidate_change.book_top is not None
    assert candidate_change.book_top.bid == pytest.approx(0.51)
    assert original_change.book_top is not None
    assert original_change.book_top.bid == pytest.approx(0.505)

    untouched_candidate = normalizer.fork()
    untouched_candidate.reset()
    assert normalizer.has_snapshot


@pytest.mark.parametrize("invalid_event", ["book", "price_change"])
def test_polymarket_complete_invalid_book_discards_prior_state_until_snapshot(
    invalid_event: str,
) -> None:
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
    payload = (
        {
            "event_type": "book",
            "asset_id": TOKEN,
            "bids": [{"price": "0.53", "size": "1"}],
            "asks": [{"price": "0.52", "size": "1"}],
            "timestamp": "1776038400200",
        }
        if invalid_event == "book"
        else {
            "event_type": "price_change",
            "price_changes": [{"asset_id": TOKEN, "price": "0.53", "size": "1", "side": "BUY"}],
            "timestamp": "1776038400200",
        }
    )

    invalid = normalizer.apply(payload, collector_receive_ts=RECEIVE)
    waiting = normalizer.apply(
        {
            "event_type": "price_change",
            "price_changes": [{"asset_id": TOKEN, "price": "0.49", "size": "1", "side": "BUY"}],
            "timestamp": "1776038400300",
        },
        collector_receive_ts=RECEIVE,
    )

    assert invalid.status is PolymarketL2Status.INVALID
    assert waiting.status is PolymarketL2Status.AWAITING_SNAPSHOT
    assert normalizer.tick_size is None


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
