from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pyarrow.parquet as pq
import pytest

from btc_short_horizon.data.forward import BtcForwardCollector


SOURCE_TIME = datetime(2026, 4, 13, tzinfo=UTC)


def test_forward_collector_persists_unique_clob_and_binance_events_by_epoch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=100,
    )
    received = SOURCE_TIME + timedelta(milliseconds=10)
    book_payload = {
        "event_type": "book",
        "asset_id": "up-token",
        "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
        "bids": [{"price": "0.48", "size": "11"}],
        "asks": [{"price": "0.52", "size": "9"}],
        "hash": "book-1",
    }

    first = collector.handle_polymarket(book_payload, collector_receive_ts=received)
    duplicate = collector.handle_polymarket(book_payload, collector_receive_ts=received)
    binance = collector.handle_binance(
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "T": int(SOURCE_TIME.timestamp() * 1_000),
                "p": "100000",
                "q": "0.01",
                "m": False,
                "t": 123,
            },
        },
        collector_receive_ts=received,
    )
    manifests = collector.flush()

    assert first.accepted_events == 1
    assert duplicate.accepted_events == 0
    assert duplicate.rejected_events == 1
    assert binance.accepted_events == 1
    assert len(manifests) == 2
    assert {manifest.source for manifest in manifests} == {"polymarket_clob", "binance_spot"}
    polymarket_manifest = next(
        manifest for manifest in manifests if manifest.source == "polymarket_clob"
    )
    assert polymarket_manifest.duplicate_count == 1
    assert collector.quality_stats[("polymarket_clob", "up-token")].duplicate_events == 1


@pytest.mark.asyncio
async def test_forward_collector_flushes_pending_events_on_interval(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=100,
        flush_interval_seconds=0.01,
    )
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 125,
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=10),
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(collector._flush_periodically(stop_event=stop_event))

    await asyncio.sleep(0.03)
    stop_event.set()
    await task

    assert collector.pending_event_count == 0
    assert list(tmp_path.rglob("manifest-*.json"))


def test_forward_collector_assigns_gap_to_first_post_gap_partition(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.mark_gap(source="binance_spot", instrument="BTCUSDT", reason="disconnect")

    result = collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 124,
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=10),
    )
    manifest = collector.flush()[0]

    assert result.accepted_events == 1
    assert manifest.gap_count == 1
    assert collector.quality_stats[("binance_spot", "BTCUSDT")].gap_events == 1


def test_forward_collector_persists_binance_depth_and_receive_only_book_ticker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    received = SOURCE_TIME + timedelta(milliseconds=10)

    snapshot = collector.handle_binance_depth_snapshot(
        instrument="BTCUSDT",
        payload={
            "lastUpdateId": 100,
            "bids": [["100000", "2"]],
            "asks": [["100001", "3"]],
        },
        collector_receive_ts=received,
    )
    depth = collector.handle_binance(
        {
            "stream": "btcusdt@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "E": int(SOURCE_TIME.timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 101,
                "u": 101,
                "b": [["100000.5", "4"]],
                "a": [],
            },
        },
        collector_receive_ts=received,
    )
    ticker = collector.handle_binance(
        {
            "stream": "btcusdt@bookTicker",
            "data": {"s": "BTCUSDT", "u": 101, "b": "100000", "B": "2", "a": "100001", "A": "3"},
        },
        collector_receive_ts=received,
    )
    manifests = collector.flush()
    event_types = {
        row["event_type"]
        for manifest in manifests
        for row in pq.read_table(tmp_path / manifest.data_path).to_pylist()
    }

    assert snapshot.accepted_events == 1
    assert depth.accepted_events == 1
    assert ticker.accepted_events == 1
    assert event_types == {"depth_snapshot", "depth_update", "book_ticker"}
    assert {manifest.schema_version for manifest in manifests} == {
        "binance-depth-snapshot-receive-v1",
        "binance-depth-update-v1",
        "binance-book-ticker-receive-v1",
    }


@pytest.mark.asyncio
async def test_forward_collector_fetches_public_binance_depth_snapshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params == httpx.QueryParams({"symbol": "BTCUSDT", "limit": "1000"})
        return httpx.Response(
            200,
            json={
                "lastUpdateId": 100,
                "bids": [["100000", "2"]],
                "asks": [["100001", "3"]],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await collector.refresh_binance_depth_snapshot(
            instrument="BTCUSDT",
            client=client,
        )

    assert result.accepted_events == 1
    assert not collector.binance_depth_needs_snapshot("BTCUSDT")


@pytest.mark.asyncio
async def test_forward_collector_does_not_connect_after_stop_requested(
    tmp_path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )

    async def unexpected_snapshot(*, instrument: str, client: httpx.AsyncClient) -> None:
        raise AssertionError(f"unexpected snapshot request for {instrument}")

    monkeypatch.setattr(collector, "refresh_binance_depth_snapshot", unexpected_snapshot)
    stop_event = asyncio.Event()
    stop_event.set()

    await collector.collect_forever(stop_event=stop_event)


def test_forward_collector_invalidates_clob_book_state_by_token(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    received = SOURCE_TIME + timedelta(milliseconds=10)
    collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "book-1",
        },
        collector_receive_ts=received,
    )

    collector.invalidate_polymarket_token("up-token")
    after_gap = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000) + 100,
            "price_changes": [
                {"asset_id": "up-token", "price": "0.49", "size": "5", "side": "BUY"}
            ],
        },
        collector_receive_ts=received,
    )

    assert after_gap.accepted_events == 0


def test_forward_collector_persists_market_resolution_for_each_subscribed_token(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token", "down-token"),
    )
    result = collector.handle_polymarket(
        {
            "event_type": "market_resolved",
            "assets_ids": ["up-token", "down-token"],
            "winning_asset_id": "up-token",
            "winning_outcome": "Up",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=10),
    )
    manifests = collector.flush()

    rows = [
        row
        for manifest in manifests
        for row in pq.read_table(tmp_path / manifest.data_path).to_pylist()
    ]
    assert result.accepted_events == 2
    assert {row["instrument"] for row in rows} == {"up-token", "down-token"}
    assert {row["event_type"] for row in rows} == {"market_resolved"}


def test_forward_collector_invalidates_l2_state_after_quality_time_regression(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    future_source_ms = int((SOURCE_TIME + timedelta(seconds=2)).timestamp() * 1_000)
    earlier_source_ms = int((SOURCE_TIME + timedelta(seconds=1)).timestamp() * 1_000)
    collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": future_source_ms,
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "book-1",
        },
        collector_receive_ts=SOURCE_TIME,
    )
    rejected = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": earlier_source_ms,
            "price_changes": [
                {"asset_id": "up-token", "price": "0.49", "size": "5", "side": "BUY"}
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )
    after_gap = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": future_source_ms + 1_000,
            "price_changes": [
                {"asset_id": "up-token", "price": "0.49", "size": "5", "side": "BUY"}
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )

    assert rejected.reasons == ("out_of_order",)
    assert after_gap.accepted_events == 0


def test_forward_collector_invalidates_binance_depth_after_quality_time_regression(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.handle_binance_depth_snapshot(
        instrument="BTCUSDT",
        payload={
            "lastUpdateId": 100,
            "bids": [["100000", "2"]],
            "asks": [["100001", "3"]],
        },
        collector_receive_ts=SOURCE_TIME,
    )
    collector.handle_binance(
        {
            "stream": "btcusdt@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "E": int((SOURCE_TIME + timedelta(seconds=2)).timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 101,
                "u": 101,
                "b": [["100000.5", "4"]],
                "a": [],
            },
        },
        collector_receive_ts=SOURCE_TIME,
    )
    rejected = collector.handle_binance(
        {
            "stream": "btcusdt@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "E": int((SOURCE_TIME + timedelta(seconds=1)).timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 102,
                "u": 102,
                "b": [],
                "a": [],
            },
        },
        collector_receive_ts=SOURCE_TIME,
    )

    assert rejected.reasons == ("out_of_order",)
    assert collector.binance_depth_needs_snapshot("BTCUSDT")


def test_forward_collector_keeps_binance_spot_and_perpetual_depth_sequences_separate(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(raw_data_root=tmp_path, polymarket_token_ids=("up-token",))
    snapshot = {"lastUpdateId": 100, "bids": [["100000", "2"]], "asks": [["100001", "3"]]}
    collector.handle_binance_depth_snapshot(
        instrument="BTCUSDT",
        payload=snapshot,
        collector_receive_ts=SOURCE_TIME,
        source="binance_spot",
    )
    collector.handle_binance_depth_snapshot(
        instrument="BTCUSDT",
        payload=snapshot,
        collector_receive_ts=SOURCE_TIME,
        source="binance_perp",
    )
    result = collector.handle_binance(
        {
            "stream": "btcusdt@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "E": int(SOURCE_TIME.timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 101,
                "u": 101,
                "b": [["100000.5", "4"]],
                "a": [],
            },
        },
        collector_receive_ts=SOURCE_TIME,
        source="binance_perp",
    )

    assert result.accepted_events == 1
    assert not collector.binance_depth_needs_snapshot("BTCUSDT", source="binance_spot")
    assert not collector.binance_depth_needs_snapshot("BTCUSDT", source="binance_perp")


@pytest.mark.asyncio
async def test_forward_collector_persists_okx_contract_metadata_and_normalizes_swap_trade(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(raw_data_root=tmp_path, polymarket_token_ids=("up-token",))
    contract_value = await collector.refresh_okx_swap_contract_value(client=_OkxInstrumentClient())
    trade = collector.handle_okx(
        {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "px": "100000",
                    "sz": "25",
                    "side": "buy",
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000)),
                    "tradeId": "trade-1",
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )
    manifests = collector.flush()
    rows = [
        row
        for manifest in manifests
        for row in pq.read_table(tmp_path / manifest.data_path).to_pylist()
    ]

    assert contract_value == pytest.approx(0.01)
    assert trade.accepted_events == 1
    assert {row["event_type"] for row in rows} == {"instrument_metadata", "trade"}
    assert {row["source"] for row in rows} == {"okx_instrument", "okx_swap"}


class _OkxInstrumentResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ctValCcy": "BTC",
                    "ctVal": "0.01",
                }
            ]
        }


class _OkxInstrumentClient:
    async def get(self, _url: str, *, params: dict[str, str]) -> _OkxInstrumentResponse:
        assert params == {"instType": "SWAP", "instId": "BTC-USDT-SWAP"}
        return _OkxInstrumentResponse()
