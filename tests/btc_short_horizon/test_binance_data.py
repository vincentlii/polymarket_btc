from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btc_short_horizon.data.binance import (
    BinanceDiffDepthBook,
    BinanceDepthSynchronizer,
    DepthUpdateStatus,
    normalize_binance_book_ticker,
    normalize_binance_depth_snapshot,
    normalize_binance_depth_update,
    normalize_binance_kline,
    normalize_binance_trade,
)


RECEIVE = datetime(2026, 4, 13, 0, 0, 1, tzinfo=UTC)


def test_binance_trade_uses_real_receive_time_and_correct_aggressor_mapping() -> None:
    record = normalize_binance_trade(
        {"s": "BTCUSDT", "T": 1_776_038_400_000, "p": "100000", "q": "0.25", "m": True, "t": 7},
        collector_receive_ts=RECEIVE,
    )

    assert record.timing.collector_receive_ts == RECEIVE
    assert record.timing.available_ts == RECEIVE
    assert record.trade.aggressor_side == "sell"
    assert record.trade.source == "binance_spot"


def test_event_time_only_history_gets_conservative_available_delay() -> None:
    record = normalize_binance_trade(
        {"s": "BTCUSDT", "T": 1_776_038_400_000, "p": "100000", "q": "0.25", "m": False, "t": 8},
        collector_receive_ts=None,
        availability_delay=timedelta(seconds=2),
    )

    assert record.timing.is_event_time_only
    assert record.timing.available_ts - record.timing.source_ts == timedelta(seconds=2)
    assert record.trade.aggressor_side == "buy"


def test_final_one_second_kline_preserves_live_availability_and_training_fields() -> None:
    timing = normalize_binance_kline(
        {
            "e": "kline",
            "E": 1_776_038_401_010,
            "s": "BTCUSDT",
            "k": {
                "t": 1_776_038_400_000,
                "T": 1_776_038_400_999,
                "s": "BTCUSDT",
                "i": "1s",
                "o": "100000",
                "c": "100001",
                "h": "100002",
                "l": "99999",
                "v": "2",
                "q": "200001",
                "V": "1.25",
                "Q": "125001",
                "n": 10,
                "x": True,
            },
        },
        collector_receive_ts=RECEIVE + timedelta(milliseconds=20),
    )

    assert timing.instrument == "BTCUSDT"
    assert timing.available_ts == RECEIVE + timedelta(milliseconds=20)
    assert timing.sequence_or_hash == "kline:1776038400000"
    assert timing.schema_version == "binance-kline-1s-v1"


def test_diff_depth_requires_snapshot_detects_gap_and_rebuilds_top_of_book() -> None:
    book = BinanceDiffDepthBook(instrument="BTCUSDT")
    assert (
        book.apply_delta(
            {"U": 1, "u": 1, "b": [], "a": [], "E": 1}, collector_receive_ts=RECEIVE
        ).status
        is DepthUpdateStatus.AWAITING_SNAPSHOT
    )

    snapshot = book.apply_snapshot(
        {
            "lastUpdateId": 100,
            "bids": [["100", "2"]],
            "asks": [["101", "3"]],
            "E": 1_776_038_400_000,
        },
        collector_receive_ts=RECEIVE,
    )
    assert snapshot.book_top is not None
    assert snapshot.book_top.bid == pytest.approx(100.0)

    applied = book.apply_delta(
        {"U": 101, "u": 101, "b": [["100.5", "4"]], "a": [], "E": 1_776_038_400_100},
        collector_receive_ts=RECEIVE,
    )
    assert applied.status is DepthUpdateStatus.APPLIED
    assert applied.book_top is not None
    assert applied.book_top.bid == pytest.approx(100.5)

    gap = book.apply_delta(
        {"U": 103, "u": 103, "b": [], "a": [], "E": 1_776_038_400_200},
        collector_receive_ts=RECEIVE,
    )
    assert gap.status is DepthUpdateStatus.GAP
    assert (
        book.apply_delta(
            {"U": 104, "u": 104, "b": [], "a": [], "E": 1_776_038_400_300},
            collector_receive_ts=RECEIVE,
        ).status
        is DepthUpdateStatus.AWAITING_SNAPSHOT
    )


def test_depth_synchronizer_buffers_before_snapshot_and_resets_after_gap() -> None:
    synchronizer = BinanceDepthSynchronizer(instrument="BTCUSDT")
    buffered = synchronizer.observe_delta(
        {"U": 101, "u": 101, "b": [["100.5", "4"]], "a": [], "E": 1_776_038_400_100},
        collector_receive_ts=RECEIVE,
    )

    applied = synchronizer.apply_snapshot(
        {
            "lastUpdateId": 100,
            "bids": [["100", "2"]],
            "asks": [["101", "3"]],
            "E": 1_776_038_400_000,
        },
        collector_receive_ts=RECEIVE,
    )
    assert buffered.status is DepthUpdateStatus.AWAITING_SNAPSHOT
    assert synchronizer.synchronized
    assert applied.status is DepthUpdateStatus.APPLIED
    assert applied.book_top is not None
    assert applied.book_top.bid == pytest.approx(100.5)

    gap = synchronizer.observe_delta(
        {"U": 103, "u": 103, "b": [], "a": [], "E": 1_776_038_400_200},
        collector_receive_ts=RECEIVE,
    )

    assert gap.status is DepthUpdateStatus.GAP
    assert not synchronizer.synchronized
    assert synchronizer.needs_snapshot


def test_depth_synchronizer_rejects_first_buffered_update_after_snapshot_gap() -> None:
    synchronizer = BinanceDepthSynchronizer(instrument="BTCUSDT")
    synchronizer.observe_delta(
        {"U": 102, "u": 102, "b": [], "a": [], "E": 1_776_038_400_100},
        collector_receive_ts=RECEIVE,
    )

    result = synchronizer.apply_snapshot(
        {
            "lastUpdateId": 100,
            "bids": [["100", "2"]],
            "asks": [["101", "3"]],
            "E": 1_776_038_400_000,
        },
        collector_receive_ts=RECEIVE,
    )

    assert result.status is DepthUpdateStatus.AWAITING_SNAPSHOT
    assert result.reason == "snapshot_too_old"
    assert not synchronizer.synchronized


def test_depth_and_book_ticker_timing_preserve_their_available_evidence() -> None:
    depth = normalize_binance_depth_update(
        {"s": "BTCUSDT", "E": 1_776_038_400_000, "U": 10, "u": 11, "b": [], "a": []},
        collector_receive_ts=RECEIVE,
    )
    snapshot = normalize_binance_depth_snapshot(
        {"lastUpdateId": 11, "bids": [["100", "1"]], "asks": [["101", "1"]]},
        instrument="BTCUSDT",
        collector_receive_ts=RECEIVE,
    )
    ticker = normalize_binance_book_ticker(
        {"s": "BTCUSDT", "u": 11, "b": "100", "B": "1", "a": "101", "A": "1"},
        collector_receive_ts=RECEIVE,
    )

    assert depth.source_ts.timestamp() == pytest.approx(1_776_038_400.0)
    assert depth.sequence_or_hash == "11"
    assert snapshot.source_ts == RECEIVE
    assert snapshot.schema_version == "binance-depth-snapshot-receive-v1"
    assert ticker.source_ts == RECEIVE
    assert ticker.schema_version == "binance-book-ticker-receive-v1"
