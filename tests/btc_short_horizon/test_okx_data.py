from __future__ import annotations

from datetime import UTC, datetime

import pytest

from btc_short_horizon.data.binance import DepthUpdateStatus
from btc_short_horizon.data.okx import (
    OkxBookSynchronizer,
    normalize_okx_book_update,
    normalize_okx_trade,
)


RECEIVE = datetime(2026, 4, 13, 0, 0, 1, tzinfo=UTC)


def _snapshot() -> dict[str, object]:
    return {
        "ts": "1776038400000",
        "seqId": "100",
        "prevSeqId": "-1",
        "bids": [["100", "2", "0", "1"]],
        "asks": [["101", "3", "0", "1"]],
    }


def test_okx_trade_keeps_venue_time_and_explicit_contract_multiplier() -> None:
    spot = normalize_okx_trade(
        {
            "instId": "BTC-USDT",
            "px": "100000",
            "sz": "0.25",
            "side": "buy",
            "ts": "1776038400000",
            "tradeId": "7",
        },
        collector_receive_ts=RECEIVE,
        source="okx_spot",
    )
    swap = normalize_okx_trade(
        {
            "instId": "BTC-USDT-SWAP",
            "px": "100000",
            "sz": "25",
            "side": "sell",
            "ts": "1776038400000",
            "tradeId": "8",
        },
        collector_receive_ts=RECEIVE,
        source="okx_swap",
        quantity_multiplier=0.01,
    )

    assert spot.timing.collector_receive_ts == RECEIVE
    assert spot.trade.aggressor_side == "buy"
    assert spot.trade.quantity == pytest.approx(0.25)
    assert swap.trade.aggressor_side == "sell"
    assert swap.trade.quantity == pytest.approx(0.25)
    assert swap.trade.source == "okx_swap"


def test_okx_book_synchronizer_requires_snapshot_and_sequence_continuity() -> None:
    book = OkxBookSynchronizer(instrument="BTC-USDT", source="okx_spot")
    awaiting = book.apply(
        action="update",
        payload={**_snapshot(), "seqId": "101", "prevSeqId": "100"},
        collector_receive_ts=RECEIVE,
    )
    snapshot = book.apply(action="snapshot", payload=_snapshot(), collector_receive_ts=RECEIVE)
    applied = book.apply(
        action="update",
        payload={
            "ts": "1776038400100",
            "seqId": "101",
            "prevSeqId": "100",
            "bids": [["100.5", "4", "0", "1"]],
            "asks": [],
        },
        collector_receive_ts=RECEIVE,
    )
    gap = book.apply(
        action="update",
        payload={
            "ts": "1776038400200",
            "seqId": "103",
            "prevSeqId": "102",
            "bids": [],
            "asks": [],
        },
        collector_receive_ts=RECEIVE,
    )

    assert awaiting.status is DepthUpdateStatus.AWAITING_SNAPSHOT
    assert snapshot.book_top is not None
    assert snapshot.book_top.bid == pytest.approx(100.0)
    assert applied.status is DepthUpdateStatus.APPLIED
    assert applied.book_top is not None
    assert applied.book_top.bid == pytest.approx(100.5)
    assert gap.status is DepthUpdateStatus.GAP


def test_okx_book_timing_is_venue_timestamped() -> None:
    timing = normalize_okx_book_update(
        _snapshot(),
        action="snapshot",
        instrument="BTC-USDT",
        collector_receive_ts=RECEIVE,
        source="okx_spot",
    )

    assert timing.source_ts.timestamp() == pytest.approx(1_776_038_400.0)
    assert timing.sequence_or_hash == "snapshot:100"
    assert timing.source == "okx_spot"
