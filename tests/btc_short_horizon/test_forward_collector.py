from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import textwrap
from threading import Event, Thread

import httpx
import pyarrow.parquet as pq
import pytest

from btc_short_horizon.data.collector import BusinessPayloadInactivityError
from btc_short_horizon.data.forward import (
    AdmittedEventBuffer,
    DEFAULT_BINANCE_FUTURES_MARKET_STREAMS,
    DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS,
    DEFAULT_BINANCE_STREAMS,
    DEFAULT_OKX_SUBSCRIPTIONS,
    BtcForwardCollector,
    CollectorBufferCapacityError,
    PolymarketSubscriptionWindow,
)
from btc_short_horizon.data.session_inventory import (
    SESSION_STATUS_COMPLETE,
    SESSION_STATUS_FAILED,
    SESSION_STATUS_OPEN,
    SessionInventoryRepository,
)
from btc_short_horizon.research.opening_evidence import (
    load_forward_polymarket_book_events,
    load_forward_raw_events,
)


SOURCE_TIME = datetime(2026, 4, 13, tzinfo=UTC)


def test_default_forward_collection_is_lightweight_for_bounded_vps_storage() -> None:
    assert DEFAULT_BINANCE_STREAMS == ("btcusdt@kline_1s",)
    assert DEFAULT_BINANCE_FUTURES_MARKET_STREAMS == ()
    assert DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS == ()
    assert DEFAULT_OKX_SUBSCRIPTIONS == ()


def test_collector_persists_point_and_twap_as_distinct_required_streams(tmp_path) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=2,
    )
    observed_at = datetime(2026, 8, 7, tzinfo=UTC)

    assert (
        collector.handle_chainlink_rtds(
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "timestamp": int(observed_at.timestamp() * 1_000),
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": int(observed_at.timestamp() * 1_000),
                    "value": 100_000.0,
                },
            },
            collector_receive_ts=observed_at,
        ).accepted_events
        == 1
    )
    assert (
        collector.handle_chainlink_twap_60s_rtds(
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "timestamp": int(observed_at.timestamp() * 1_000) + 123,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": int(observed_at.timestamp() * 1_000),
                    "value": 100_000.0,
                    "full_accuracy_value": "100000000000000000000000",
                    "window_s": 60,
                },
            },
            collector_receive_ts=observed_at,
        ).accepted_events
        == 1
    )
    collector.flush()

    sources = {
        row["source"]
        for path in tmp_path.rglob("part-*.parquet")
        for row in pq.read_table(path).to_pylist()
    }
    assert sources == {
        "polymarket_rtds_chainlink",
        "polymarket_rtds_chainlink_twap_60s",
    }
    health = collector.feed_health(now=observed_at, stale_after_seconds=1.0)
    assert {item["key"] for item in health.feeds if item["key"].startswith("polymarket_rtds")} == {
        "polymarket_rtds_chainlink:btc/usd:price",
        "polymarket_rtds_chainlink_twap_60s:btc/usd:twap_60s",
    }


@pytest.mark.asyncio
async def test_business_payload_inactivity_records_one_gap_before_recovered_data(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=2,
        ingest_version="watchdog-test-v1",
    )

    async def fake_socket_loop(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_connected
        if "ws-live-data" not in websocket_collector.subscription.endpoint:
            await stop_event.wait()
            return
        assert on_error is not None
        await on_error(
            BusinessPayloadInactivityError(
                endpoint=websocket_collector.subscription.endpoint,
                timeout_seconds=15.0,
            )
        )
        recovered_at = datetime.now(UTC)
        topic = websocket_collector.subscription.subscribe_payload["subscriptions"][0]["topic"]
        message = (
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "timestamp": int(recovered_at.timestamp() * 1_000),
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": int(recovered_at.timestamp() * 1_000),
                    "value": 100_000.0,
                },
            }
            if topic == "crypto_prices_chainlink"
            else {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "timestamp": int(recovered_at.timestamp() * 1_000),
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": int(recovered_at.timestamp() * 1_000),
                    "value": 100_000.0,
                    "full_accuracy_value": "100000000000000000000000",
                    "window_s": 60,
                },
            }
        )
        accepted = await on_payload(message, recovered_at)
        assert accepted
        stop_event.set()

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fake_socket_loop,
    )
    await asyncio.wait_for(
        collector.collect_forever(stop_event=asyncio.Event(), binance_streams=()),
        timeout=5.0,
    )

    rows_by_source: dict[str, list[dict[str, object]]] = {}
    for path in tmp_path.rglob("part-*.parquet"):
        for row in pq.read_table(path).to_pylist():
            if row["source"] in {
                "polymarket_rtds_chainlink",
                "polymarket_rtds_chainlink_twap_60s",
            }:
                rows_by_source.setdefault(row["source"], []).append(row)
    for rows in rows_by_source.values():
        rows.sort(key=lambda row: row["admission_sequence"])

    assert set(rows_by_source) == {
        "polymarket_rtds_chainlink",
        "polymarket_rtds_chainlink_twap_60s",
    }
    assert [row["event_type"] for row in rows_by_source["polymarket_rtds_chainlink"]] == [
        "continuity_gap",
        "crypto_prices_chainlink",
    ]
    assert [row["event_type"] for row in rows_by_source["polymarket_rtds_chainlink_twap_60s"]] == [
        "continuity_gap",
        "crypto_prices_twap_sixty",
    ]
    for rows in rows_by_source.values():
        assert json.loads(rows[0]["payload_json"])["reason"] == "BusinessPayloadInactivityError"
        assert rows[0]["epoch_id"] == rows[1]["epoch_id"] == 1
    assert collector.quality_stats[("polymarket_rtds_chainlink", "btc/usd", "price")].epochs == 2
    assert (
        collector.quality_stats[
            ("polymarket_rtds_chainlink_twap_60s", "btc/usd", "twap_60s")
        ].epochs
        == 2
    )


@pytest.mark.asyncio
async def test_storage_session_rotates_without_stopping_public_feed_tasks(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token", "down-token"),
        polymarket_token_groups=(("up-token", "down-token"),),
        polymarket_subscription_windows=(
            PolymarketSubscriptionWindow(
                token_ids=("up-token", "down-token"),
                start=now + timedelta(hours=1),
                end=now + timedelta(hours=1, minutes=3),
            ),
        ),
    )
    feed_started = asyncio.Event()
    feed_stops = 0

    async def keep_connection_open(  # type: ignore[no-untyped-def]
        _websocket_collector,
        *,
        stop_event: asyncio.Event,
        **_kwargs,
    ) -> None:
        nonlocal feed_stops
        feed_started.set()
        await stop_event.wait()
        feed_stops += 1

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        keep_connection_open,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.collect_forever(
            stop_event=stop_event,
            binance_streams=(),
            binance_futures_market_streams=(),
            binance_futures_public_streams=(),
            okx_subscriptions=(),
        )
    )
    await asyncio.wait_for(feed_started.wait(), timeout=1.0)
    old_session_id = collector.collector_session_id

    await collector.rotate_storage_session(epoch_id_offset=int(now.timestamp()) + 900)

    assert feed_stops == 0
    assert collector.collector_session_id != old_session_id
    assert (
        SessionInventoryRepository(tmp_path).read_session(old_session_id).status
        == SESSION_STATUS_COMPLETE
    )
    assert collector._session_inventory.snapshot().status == SESSION_STATUS_OPEN

    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert feed_stops == 2
    assert collector._session_inventory.snapshot().status == SESSION_STATUS_COMPLETE


@pytest.mark.asyncio
async def test_dynamic_clob_windows_retire_without_ending_shared_feed_collection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    initial = PolymarketSubscriptionWindow(
        token_ids=("up-token", "down-token"),
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=1, minutes=3),
    )
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=initial.token_ids,
        polymarket_token_groups=(initial.token_ids,),
        polymarket_subscription_windows=(initial,),
    )
    observed: list[tuple[str, ...]] = []
    first_complete = asyncio.Event()
    second_complete = asyncio.Event()

    async def keep_shared_connection_open(  # type: ignore[no-untyped-def]
        _websocket_collector,
        *,
        stop_event: asyncio.Event,
        **_kwargs,
    ) -> None:
        await stop_event.wait()

    async def complete_clob_window(*, token_group, **_kwargs):  # type: ignore[no-untyped-def]
        observed.append(token_group)
        (first_complete if len(observed) == 1 else second_complete).set()

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        keep_shared_connection_open,
    )
    monkeypatch.setattr(
        "btc_short_horizon.data.forward._collect_polymarket_during_window",
        complete_clob_window,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.collect_forever(
            stop_event=stop_event,
            binance_streams=(),
            binance_futures_market_streams=(),
            binance_futures_public_streams=(),
            okx_subscriptions=(),
        )
    )
    await asyncio.wait_for(first_complete.wait(), timeout=1.0)
    for _ in range(100):
        if initial.token_ids not in collector.polymarket_token_groups:
            break
        await asyncio.sleep(0)

    assert not task.done()
    assert initial.token_ids not in collector.polymarket_token_groups
    assert await collector.wait_polymarket_subscription_window(
        initial,
        stop_event=stop_event,
        timeout_seconds=1.0,
    )

    following = PolymarketSubscriptionWindow(
        token_ids=("next-up-token", "next-down-token"),
        start=now + timedelta(hours=2),
        end=now + timedelta(hours=2, minutes=3),
    )
    assert collector.register_polymarket_subscription_window(following) == following
    assert json.loads(
        collector._session_inventory.snapshot().attributes["polymarket_token_ids"]
    ) == list(following.token_ids)
    await asyncio.wait_for(second_complete.wait(), timeout=1.0)
    for _ in range(100):
        if following.token_ids not in collector.polymarket_token_groups:
            break
        await asyncio.sleep(0)

    assert not task.done()
    assert observed == [initial.token_ids, following.token_ids]

    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)


def test_admitted_event_buffer_receives_only_committed_events_without_blocking_storage(
    tmp_path,
) -> None:
    buffer = AdmittedEventBuffer(max_events=1)
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token", "down-token"),
    )
    collector.subscribe_admitted_events(buffer)
    payload = {
        "event_type": "book",
        "asset_id": "up-token",
        "timestamp": str(int(SOURCE_TIME.timestamp() * 1_000)),
        "hash": "book-1",
        "bids": [{"price": "0.48", "size": "11"}],
        "asks": [{"price": "0.52", "size": "12"}],
    }

    accepted = collector.handle_polymarket(payload, collector_receive_ts=SOURCE_TIME)
    duplicate = collector.handle_polymarket(payload, collector_receive_ts=SOURCE_TIME)

    assert accepted.accepted_events == 1
    assert duplicate.accepted_events == 0
    assert buffer.pending_events == 1
    assert buffer.get_nowait().event_type == "book"
    assert not buffer.overflowed


def test_admitted_event_buffer_overflow_fails_the_subscriber_only(tmp_path) -> None:
    buffer = AdmittedEventBuffer(max_events=1)
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token", "down-token"),
    )
    collector.subscribe_admitted_events(buffer)
    for index, token_id in enumerate(("up-token", "down-token")):
        collector.handle_polymarket(
            {
                "event_type": "book",
                "asset_id": token_id,
                "timestamp": str(int(SOURCE_TIME.timestamp() * 1_000) + index),
                "hash": f"book-{index}",
                "bids": [{"price": "0.48", "size": "11"}],
                "asks": [{"price": "0.52", "size": "12"}],
            },
            collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=index),
        )

    assert buffer.overflowed
    assert buffer.dropped_events == 1
    assert collector.pending_event_count == 2


def test_forward_collector_requires_disjoint_clob_connection_groups(tmp_path) -> None:
    with pytest.raises(ValueError, match="exact partition"):
        BtcForwardCollector(
            raw_data_root=tmp_path,
            polymarket_token_ids=("current-up", "current-down", "next-up", "next-down"),
            polymarket_token_groups=(
                ("current-up", "current-down"),
                ("next-up",),
            ),
        )


def test_forward_collector_requires_subscription_windows_to_match_token_groups(tmp_path) -> None:
    start = SOURCE_TIME
    end = start + timedelta(minutes=1)

    with pytest.raises(ValueError, match="exactly match"):
        BtcForwardCollector(
            raw_data_root=tmp_path,
            polymarket_token_ids=("up-token", "down-token"),
            polymarket_token_groups=(("up-token", "down-token"),),
            polymarket_subscription_windows=(
                PolymarketSubscriptionWindow(
                    token_ids=("up-token",),
                    start=start,
                    end=end,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_forward_collector_limits_clob_connection_without_stopping_btc_feeds(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    clob_started = asyncio.Event()
    clob_stopped = asyncio.Event()
    rtds_started = asyncio.Event()
    rtds_stopped = asyncio.Event()

    async def fake_socket_loop(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_payload, on_error, on_connected
        endpoint = websocket_collector.subscription.endpoint
        if "ws-subscriptions-clob" in endpoint:
            clob_started.set()
            await stop_event.wait()
            clob_stopped.set()
            return
        if "ws-live-data" in endpoint:
            rtds_started.set()
            await stop_event.wait()
            rtds_stopped.set()
            return
        await stop_event.wait()

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fake_socket_loop,
    )
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token", "down-token"),
        polymarket_token_groups=(("up-token", "down-token"),),
        polymarket_subscription_windows=(
            PolymarketSubscriptionWindow(
                token_ids=("up-token", "down-token"),
                start=now + timedelta(seconds=2),
                end=now + timedelta(seconds=3),
            ),
        ),
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(collector.collect_forever(stop_event=stop_event))

    await asyncio.sleep(0)
    assert not task.done(), repr(task.exception()) if task.done() else ""
    await asyncio.wait_for(rtds_started.wait(), timeout=1.0)
    await asyncio.sleep(0.03)
    assert not clob_started.is_set()
    await asyncio.wait_for(clob_started.wait(), timeout=3.0)
    await asyncio.wait_for(clob_stopped.wait(), timeout=3.0)
    assert not task.done()
    assert not rtds_stopped.is_set()

    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert rtds_stopped.is_set()

    with pytest.raises(ValueError, match="must not repeat"):
        BtcForwardCollector(
            raw_data_root=tmp_path,
            polymarket_token_ids=("current-up", "current-down", "next-up", "next-down"),
            polymarket_token_groups=(
                ("current-up", "current-down"),
                ("next-up", "next-down", "current-up"),
            ),
        )


@pytest.mark.asyncio
async def test_okx_metadata_initialization_does_not_block_other_public_feeds(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rtds_started = asyncio.Event()
    metadata_started = asyncio.Event()
    metadata_release = asyncio.Event()

    async def fake_socket_loop(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_payload, on_error, on_connected
        if "ws-live-data" in websocket_collector.subscription.endpoint:
            rtds_started.set()
        await stop_event.wait()

    async def delayed_metadata(self, *, client):  # type: ignore[no-untyped-def]
        del self, client
        metadata_started.set()
        await metadata_release.wait()
        return 1.0

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fake_socket_loop,
    )
    monkeypatch.setattr(
        BtcForwardCollector,
        "refresh_okx_swap_contract_value",
        delayed_metadata,
    )
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token", "down-token"),
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.collect_forever(
            stop_event=stop_event,
            binance_streams=(),
            okx_subscriptions=({"channel": "trades", "instId": "BTC-USDT-SWAP"},),
        )
    )

    await asyncio.wait_for(rtds_started.wait(), timeout=3.0)
    await asyncio.wait_for(metadata_started.wait(), timeout=3.0)
    assert not metadata_release.is_set()
    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_forward_collector_isolates_clob_failure_to_its_market_connection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups = (
        ("current-up", "current-down"),
        ("next-up", "next-down"),
    )
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=tuple(token for group in groups for token in group),
        polymarket_token_groups=groups,
    )
    stop_event = asyncio.Event()
    next_ready = asyncio.Event()
    current_failed = asyncio.Event()

    async def fake_socket_loop(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_connected
        subscription = websocket_collector.subscription
        if "ws-subscriptions-clob" not in subscription.endpoint:
            await stop_event.wait()
            return
        token_ids = tuple(subscription.subscribe_payload["assets_ids"])
        for token_id in token_ids:
            assert await on_payload(
                {
                    "event_type": "book",
                    "asset_id": token_id,
                    "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
                    "bids": [{"price": "0.48", "size": "11"}],
                    "asks": [{"price": "0.52", "size": "9"}],
                    "hash": f"book-{token_id}",
                },
                SOURCE_TIME,
            )
        if token_ids == groups[1]:
            next_ready.set()
            await current_failed.wait()
            stop_event.set()
            return
        await next_ready.wait()
        try:
            await on_payload(
                {
                    "event_type": "price_change",
                    "timestamp": int(
                        (SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000
                    ),
                    "price_changes": [
                        {
                            "asset_id": "current-up",
                            "price": "0.49",
                            "size": "5",
                            "side": "HOLD",
                        }
                    ],
                },
                SOURCE_TIME + timedelta(milliseconds=110),
            )
        except Exception as exc:
            assert on_error is not None
            await on_error(exc)
        current_failed.set()
        await stop_event.wait()

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fake_socket_loop,
    )
    await asyncio.wait_for(
        collector.collect_forever(
            stop_event=stop_event,
            binance_streams=(),
        ),
        timeout=5.0,
    )

    stats = collector.quality_stats
    assert stats[("polymarket_clob", "current-up", "market")].gap_events >= 1
    assert stats[("polymarket_clob", "current-down", "market")].gap_events >= 1
    assert stats[("polymarket_clob", "next-up", "market")].gap_events == 0
    assert stats[("polymarket_clob", "next-down", "market")].gap_events == 0


def test_forward_collector_enforces_source_specific_depth_snapshot_limits(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(
        ValueError,
        match="binance_futures_depth_snapshot_limit",
    ):
        BtcForwardCollector(
            raw_data_root=tmp_path,
            polymarket_token_ids=("up-token",),
            binance_futures_depth_snapshot_limit=1_001,
        )

    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        binance_spot_depth_snapshot_limit=5_000,
        binance_futures_depth_snapshot_limit=1_000,
    )

    assert collector._binance_depth_snapshot_limit("binance_spot") == 5_000
    assert collector._binance_depth_snapshot_limit("binance_perp") == 1_000


def test_forward_collector_feed_health_fails_closed_for_silent_and_stale_required_feeds(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.configure_required_feeds(
        binance_streams=("btcusdt@kline_1s",),
        binance_futures_market_streams=(),
        binance_futures_public_streams=(),
    )

    silent = collector.feed_health(now=SOURCE_TIME, stale_after_seconds=30.0)

    assert not silent.healthy
    assert silent.reason == "required_feed_silent"
    assert {item["key"] for item in silent.feeds} == {
        "binance_spot:BTCUSDT:kline_1s",
        "polymarket_clob:up-token:market",
        "polymarket_rtds_chainlink:btc/usd:price",
        "polymarket_rtds_chainlink_twap_60s:btc/usd:twap_60s",
    }

    collector.handle_binance(
        {
            "stream": "btcusdt@kline_1s",
            "data": {
                "e": "kline",
                "E": int((SOURCE_TIME + timedelta(seconds=1)).timestamp() * 1_000),
                "s": "BTCUSDT",
                "k": {
                    "t": int(SOURCE_TIME.timestamp() * 1_000),
                    "T": int(SOURCE_TIME.timestamp() * 1_000) + 999,
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
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=1),
    )

    stale = collector.feed_health(now=SOURCE_TIME + timedelta(seconds=32), stale_after_seconds=30.0)

    binance = next(item for item in stale.feeds if item["source"] == "binance_spot")
    assert binance["age_seconds"] == 31.0
    assert binance["state"] == "stale"


def test_forward_collector_feed_health_reports_gap_count(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(raw_data_root=tmp_path, polymarket_token_ids=("up-token",))
    collector.mark_gap(
        source="polymarket_clob",
        instrument="up-token",
        stream_id="market",
        reason="disconnect",
    )

    feed = next(
        item
        for item in collector.feed_health(now=SOURCE_TIME, stale_after_seconds=30.0).feeds
        if item["source"] == "polymarket_clob"
    )

    assert feed["gap_count"] == 1


def test_forward_collector_rotates_required_clob_tokens_without_old_market_staleness(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("old-up", "old-down", "next-up", "next-down"),
    )

    collector.configure_required_polymarket_tokens(("next-up", "next-down"))
    clob_feeds = {
        item["instrument"]
        for item in collector.feed_health(now=SOURCE_TIME, stale_after_seconds=30.0).feeds
        if item["source"] == "polymarket_clob"
    }

    assert clob_feeds == {"next-up", "next-down"}

    collector.configure_required_polymarket_tokens(())
    assert all(
        item["source"] != "polymarket_clob"
        for item in collector.feed_health(now=SOURCE_TIME, stale_after_seconds=30.0).feeds
    )


def test_forward_collector_fails_closed_when_handoff_token_is_not_registered(tmp_path) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("current-up", "current-down"),
    )
    collector.configure_required_polymarket_tokens(("next-up", "next-down"))

    health = collector.feed_health(now=SOURCE_TIME, stale_after_seconds=30.0)

    assert not health.healthy
    assert health.reason == "required_feed_token_not_registered"
    assert {item["state"] for item in health.feeds if item["source"] == "polymarket_clob"} == {
        "gap"
    }


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
    assert collector.quality_stats[("polymarket_clob", "up-token", "market")].duplicate_events == 1


def test_forward_collector_keeps_clob_source_timestamp_jitter_in_one_epoch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(raw_data_root=tmp_path, polymarket_token_ids=("up-token",))
    snapshot = collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "book-jitter",
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=10),
    )
    changed = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int((SOURCE_TIME + timedelta(milliseconds=50)).timestamp() * 1_000),
            "price_changes": [
                {"asset_id": "up-token", "price": "0.49", "size": "12", "side": "BUY"}
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=20),
    )

    stats = collector.quality_stats[("polymarket_clob", "up-token", "market")]
    assert snapshot.accepted_events == 1
    assert changed.accepted_events == 1
    assert stats.gap_events == 0
    assert stats.epochs == 1


def test_forward_collector_writes_the_configured_ingest_version(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        ingest_version="test-v2",
        polymarket_source_timestamp_regression_tolerance_seconds=0.25,
    )
    result = collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "book-versioned",
        },
        collector_receive_ts=SOURCE_TIME,
    )
    manifest = collector.flush()[0]

    row = pq.read_table(tmp_path / manifest.data_path).to_pylist()[0]
    assert result.accepted_events == 1
    assert row["ingest_version"] == "test-v2"
    assert manifest.attributes["polymarket_source_timestamp_regression_tolerance_seconds"] == "0.25"


def test_forward_collector_namespaces_epochs_across_process_restarts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rows: list[dict[str, object]] = []
    for session_id in ("session-a", "session-b"):
        collector = BtcForwardCollector(
            raw_data_root=tmp_path,
            polymarket_token_ids=("up-token",),
            epoch_id_offset=1_776_038_400,
            collector_session_id=session_id,
        )
        collector.handle_polymarket(
            {
                "event_type": "book",
                "asset_id": "up-token",
                "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
                "bids": [{"price": "0.48", "size": "11"}],
                "asks": [{"price": "0.52", "size": "9"}],
                "hash": f"book-{session_id}",
            },
            collector_receive_ts=SOURCE_TIME,
        )
        manifest = collector.flush()[0]
        assert manifest.attributes["collector_session_id"] == session_id
        rows.extend(pq.read_table(tmp_path / manifest.data_path).to_pylist())

    assert {row["collector_session_id"] for row in rows} == {"session-a", "session-b"}
    assert len({(row["collector_session_id"], row["epoch_id"]) for row in rows}) == 2
    assert len({row["epoch_id"] for row in rows}) == 1


def test_forward_collector_rejects_non_ascii_session_id_at_construction(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="collector_session_id"):
        BtcForwardCollector(
            raw_data_root=tmp_path,
            polymarket_token_ids=("up-token",),
            collector_session_id="会话",
        )


def test_forward_collector_rejects_cross_token_batch_atomically(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token", "down-token"),
    )
    for token_id, bid, ask in (
        ("up-token", "0.48", "0.52"),
        ("down-token", "0.47", "0.53"),
    ):
        collector.handle_polymarket(
            {
                "event_type": "book",
                "asset_id": token_id,
                "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
                "bids": [{"price": bid, "size": "11"}],
                "asks": [{"price": ask, "size": "9"}],
                "hash": f"snapshot-{token_id}",
            },
            collector_receive_ts=SOURCE_TIME,
        )
    collector.flush()

    result = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000),
            "price_changes": [
                {"asset_id": "up-token", "price": "0.49", "size": "12", "side": "BUY"},
                {"asset_id": "down-token", "price": "0.46", "size": "8", "side": "HOLD"},
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=100),
    )

    assert result.accepted_events == 0
    assert result.rejected_events == 1
    assert collector.pending_event_count == 2
    assert 0.49 not in collector._polymarket_normalizers["up-token"]._bids


def test_forward_collector_reserves_gap_and_source_event_atomically(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=1,
        max_pending_events=2,
        polymarket_source_timestamp_regression_tolerance_seconds=0.25,
    )
    original = {
        "event_type": "book",
        "asset_id": "up-token",
        "timestamp": int((SOURCE_TIME + timedelta(seconds=1)).timestamp() * 1_000),
        "bids": [{"price": "0.48", "size": "11"}],
        "asks": [{"price": "0.52", "size": "9"}],
        "hash": "original-book",
    }
    regressed = {
        "event_type": "book",
        "asset_id": "up-token",
        "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
        "bids": [{"price": "0.49", "size": "12"}],
        "asks": [{"price": "0.53", "size": "8"}],
        "hash": "regressed-book",
    }
    collector.handle_polymarket(
        original,
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=1),
    )

    with pytest.raises(CollectorBufferCapacityError, match="max_pending_events"):
        collector.handle_polymarket(
            regressed,
            collector_receive_ts=SOURCE_TIME + timedelta(seconds=2),
        )

    assert collector.pending_event_count == 1
    normalizer = collector._polymarket_normalizers["up-token"]
    assert normalizer._bids == {0.48: 11.0}
    assert normalizer._asks == {0.52: 9.0}

    collector.flush()
    accepted = collector.handle_polymarket(
        regressed,
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=2),
    )

    assert accepted.accepted_events == 1
    assert collector.pending_event_count == 2
    assert collector._polymarket_normalizers["up-token"]._bids == {0.49: 12.0}


@pytest.mark.parametrize("flush_between_events", (False, True))
def test_forward_replay_preserves_same_timestamp_admission_order(
    tmp_path,
    flush_between_events: bool,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        ingest_version="btc-short-horizon-v8",
    )
    collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "z-book",
        },
        collector_receive_ts=SOURCE_TIME,
    )
    if flush_between_events:
        collector.flush()
    collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
            "price_changes": [
                {
                    "asset_id": "up-token",
                    "price": "0.49",
                    "size": "12",
                    "side": "BUY",
                    "hash": "a-delta",
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )
    collector.flush()

    raw = load_forward_raw_events(
        raw_data_root=tmp_path,
        source="polymarket_clob",
        instrument="up-token",
        start_time=SOURCE_TIME,
        end_time=SOURCE_TIME,
        ingest_version="btc-short-horizon-v8",
    )
    replay = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id="up-token",
        start_time=SOURCE_TIME,
        end_time=SOURCE_TIME,
        ingest_version="btc-short-horizon-v8",
        expected_source_timestamp_regression_tolerance_seconds=1.0,
    )

    assert [event.admission_sequence for event in raw.events] == [0, 1]
    assert replay.awaiting_snapshot_count == 0
    assert replay.events[-1].book is not None
    assert replay.events[-1].book.bid == 0.49


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
    flush_requested = asyncio.Event()
    capacity_available = asyncio.Event()
    capacity_available.set()
    task = asyncio.create_task(
        collector._run_flush_worker(
            stop_event=stop_event,
            flush_requested=flush_requested,
            capacity_available=capacity_available,
        )
    )

    await asyncio.sleep(0.03)
    stop_event.set()
    await task

    assert collector.pending_event_count == 0
    assert list(tmp_path.rglob("manifest-*.json"))
    # The worker owns flushing only; the enclosing collector lifecycle closes the session.
    assert collector._session_inventory.snapshot().status == SESSION_STATUS_OPEN


@pytest.mark.asyncio
async def test_forward_collector_applies_backpressure_until_single_writer_flushes(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=1,
        max_pending_events=1,
        max_pending_bytes=1_024,
    )
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 126,
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=10),
    )
    stop_event = asyncio.Event()
    flush_requested = asyncio.Event()
    capacity_available = asyncio.Event()
    capacity_available.set()
    worker = asyncio.create_task(
        collector._run_flush_worker(
            stop_event=stop_event,
            flush_requested=flush_requested,
            capacity_available=capacity_available,
        )
    )

    await asyncio.wait_for(
        collector._coordinate_buffer(
            flush_requested=flush_requested,
            capacity_available=capacity_available,
        ),
        timeout=1.0,
    )
    stop_event.set()
    await worker

    stats = collector.buffer_stats
    assert stats.pending_events == 0
    assert stats.high_water_events == 1
    assert stats.flush_count == 1
    assert stats.flush_failures == 0
    assert stats.last_flush_duration_ms is not None
    assert list(tmp_path.rglob("manifest-*.json"))


@pytest.mark.asyncio
async def test_forward_collector_flush_worker_cancellation_cleans_signal_waiter(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    stop_event = asyncio.Event()
    flush_requested = asyncio.Event()
    capacity_available = asyncio.Event()
    capacity_available.set()
    tasks_before = asyncio.all_tasks()
    worker = asyncio.create_task(
        collector._run_flush_worker(
            stop_event=stop_event,
            flush_requested=flush_requested,
            capacity_available=capacity_available,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    await asyncio.sleep(0)

    leaked = {task for task in asyncio.all_tasks() if task not in tasks_before and not task.done()}
    assert not leaked


@pytest.mark.asyncio
async def test_forward_collector_serializes_competing_feed_admission_at_capacity(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=1,
        max_pending_events=1,
        max_pending_bytes=4_096,
    )
    writer_started = Event()
    release_writer = Event()
    original_write = collector._writer.write
    ready_feeds: set[str] = set()
    launch = asyncio.Event()
    completed_feeds: list[str] = []

    def block_first_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        if not writer_started.is_set():
            writer_started.set()
            assert release_writer.wait(timeout=2.0)
        return original_write(*args, **kwargs)

    async def fake_socket_loop(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_error, on_connected
        endpoint = websocket_collector.subscription.endpoint
        if "ws-subscriptions-clob" in endpoint:
            feed = "polymarket"
            payload = {
                "event_type": "book",
                "asset_id": "up-token",
                "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
                "bids": [{"price": "0.48", "size": "11"}],
                "asks": [{"price": "0.52", "size": "9"}],
                "hash": "capacity-book",
            }
        elif "binance" in endpoint:
            feed = "binance"
            payload = {
                "stream": "btcusdt@trade",
                "data": {
                    "e": "trade",
                    "s": "BTCUSDT",
                    "T": int(SOURCE_TIME.timestamp() * 1_000),
                    "p": "100000",
                    "q": "0.01",
                    "m": False,
                    "t": 81001,
                },
            }
        else:
            await stop_event.wait()
            return
        ready_feeds.add(feed)
        if ready_feeds == {"polymarket", "binance"}:
            launch.set()
        await launch.wait()
        accepted = await on_payload(payload, SOURCE_TIME)
        assert accepted
        completed_feeds.append(feed)
        if len(completed_feeds) == 2:
            stop_event.set()
        await stop_event.wait()

    monkeypatch.setattr(collector._writer, "write", block_first_write)
    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fake_socket_loop,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.collect_forever(
            stop_event=stop_event,
            binance_streams=("btcusdt@trade",),
        )
    )
    await asyncio.to_thread(writer_started.wait, 5.0)
    assert writer_started.is_set()
    await asyncio.sleep(0.02)
    assert not completed_feeds

    release_writer.set()
    await asyncio.wait_for(task, timeout=5.0)

    rows = sum(pq.read_table(path).num_rows for path in tmp_path.rglob("part-*.parquet"))
    assert rows == 2
    assert set(completed_feeds) == {"polymarket", "binance"}


@pytest.mark.asyncio
async def test_forward_collector_flushes_before_retrying_an_atomic_gap_batch(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=2,
        max_pending_events=2,
        polymarket_source_timestamp_regression_tolerance_seconds=0.25,
        ingest_version="btc-short-horizon-v8",
    )
    collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int((SOURCE_TIME + timedelta(seconds=1)).timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "preloaded-book",
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=1),
    )

    async def fake_socket_loop(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_error, on_connected
        if "ws-subscriptions-clob" not in websocket_collector.subscription.endpoint:
            await stop_event.wait()
            return
        accepted = await on_payload(
            {
                "event_type": "book",
                "asset_id": "up-token",
                "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
                "bids": [{"price": "0.49", "size": "12"}],
                "asks": [{"price": "0.53", "size": "8"}],
                "hash": "regressed-after-flush",
            },
            SOURCE_TIME + timedelta(seconds=2),
        )
        assert accepted
        stop_event.set()

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fake_socket_loop,
    )
    await asyncio.wait_for(
        collector.collect_forever(
            stop_event=asyncio.Event(),
            binance_streams=(),
        ),
        timeout=5.0,
    )

    rows = sorted(
        (
            row
            for path in tmp_path.rglob("part-*.parquet")
            for row in pq.read_table(path).to_pylist()
        ),
        key=lambda row: row["admission_sequence"],
    )
    assert [row["event_type"] for row in rows] == [
        "book",
        "continuity_gap",
        "book",
    ]
    assert [row["admission_sequence"] for row in rows] == [0, 1, 2]


def test_forward_collector_requeues_events_and_records_disk_flush_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 127,
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=10),
    )

    def disk_full(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(collector._writer, "write", disk_full)

    with pytest.raises(OSError, match="disk full"):
        collector.flush()

    stats = collector.buffer_stats
    assert stats.pending_events == 1
    assert stats.pending_bytes > 0
    assert stats.flush_count == 0
    assert stats.flush_failures == 1


def test_forward_collector_does_not_bind_concurrent_quality_to_inflight_manifest(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    first_payload = {
        "e": "trade",
        "s": "BTCUSDT",
        "T": int(SOURCE_TIME.timestamp() * 1_000),
        "p": "100000",
        "q": "0.01",
        "m": False,
        "t": 601,
    }
    collector.handle_binance(first_payload, collector_receive_ts=SOURCE_TIME)
    entered = Event()
    release = Event()
    original_write = collector._writer.write
    first_manifests: list[object] = []

    def blocked_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(timeout=2.0)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(collector._writer, "write", blocked_write)
    thread = Thread(
        target=lambda: first_manifests.extend(collector.flush()),
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=2.0)

    duplicate = collector.handle_binance(first_payload, collector_receive_ts=SOURCE_TIME)
    assert duplicate.reasons == ("duplicate",)
    release.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert len(first_manifests) == 1
    assert first_manifests[0].duplicate_count == 0  # type: ignore[union-attr]

    second = collector.handle_binance(
        {
            **first_payload,
            "T": int((SOURCE_TIME + timedelta(milliseconds=1)).timestamp() * 1_000),
            "t": 602,
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=1),
    )
    second_manifest = collector.flush()[0]

    assert second.accepted_events == 1
    assert second_manifest.duplicate_count == 1


def test_forward_collector_hard_capacity_includes_inflight_flush(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=1,
        max_pending_events=1,
        max_pending_bytes=1_000_000,
    )
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 501,
        },
        collector_receive_ts=SOURCE_TIME,
    )
    entered = Event()
    release = Event()
    original_write = collector._writer.write
    result: list[object] = []

    def blocked_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        entered.set()
        assert release.wait(timeout=2.0)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(collector._writer, "write", blocked_write)
    thread = Thread(target=lambda: result.extend(collector.flush()), daemon=True)
    thread.start()
    assert entered.wait(timeout=2.0)

    stats = collector.buffer_stats
    assert stats.pending_events == 0
    assert stats.inflight_events == 1
    assert stats.total_events == 1
    with pytest.raises(CollectorBufferCapacityError, match="max_pending_events"):
        collector.handle_binance(
            {
                "e": "trade",
                "s": "BTCUSDT",
                "T": int((SOURCE_TIME + timedelta(milliseconds=1)).timestamp() * 1_000),
                "p": "100001",
                "q": "0.01",
                "m": False,
                "t": 502,
            },
            collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=1),
        )

    release.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert result
    retry = collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int((SOURCE_TIME + timedelta(milliseconds=1)).timestamp() * 1_000),
            "p": "100001",
            "q": "0.01",
            "m": False,
            "t": 502,
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=1),
    )
    assert retry.accepted_events == 1


def test_forward_collector_rejects_single_event_larger_than_byte_capacity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        max_pending_bytes=1,
    )

    with pytest.raises(CollectorBufferCapacityError, match="max_pending_bytes"):
        collector.handle_binance(
            {
                "e": "trade",
                "s": "BTCUSDT",
                "T": int(SOURCE_TIME.timestamp() * 1_000),
                "p": "100000",
                "q": "0.01",
                "m": False,
                "t": 503,
            },
            collector_receive_ts=SOURCE_TIME,
        )

    assert collector.buffer_stats.total_events == 0


def test_forward_collector_capacity_rejection_does_not_advance_binance_depth(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_size=1,
        max_pending_events=1,
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
    collector.flush()
    synchronizer = collector._depth_synchronizer(
        source="binance_spot",
        instrument="BTCUSDT",
    )
    assert synchronizer._book.last_update_id == 100
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 701,
        },
        collector_receive_ts=SOURCE_TIME,
    )

    with pytest.raises(CollectorBufferCapacityError, match="max_pending_events"):
        collector.handle_binance(
            {
                "stream": "btcusdt@depth@100ms",
                "data": {
                    "e": "depthUpdate",
                    "E": int((SOURCE_TIME + timedelta(milliseconds=1)).timestamp() * 1_000),
                    "s": "BTCUSDT",
                    "U": 101,
                    "u": 101,
                    "b": [["100000.5", "4"]],
                    "a": [],
                },
            },
            collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=1),
        )

    current = collector._depth_synchronizer(
        source="binance_spot",
        instrument="BTCUSDT",
    )
    assert current is synchronizer
    assert current._book.last_update_id == 100
    assert current.needs_snapshot
    assert not current.requires_snapshot


def test_forward_collector_persists_gap_in_its_own_partition(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.mark_gap(
        source="binance_spot",
        instrument="BTCUSDT",
        stream_id="trade",
        reason="disconnect",
        observed_at=SOURCE_TIME,
    )

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
    manifests = collector.flush()

    assert result.accepted_events == 1
    assert sum(manifest.gap_count for manifest in manifests) == 1
    assert any(manifest.schema_version == "btc-continuity-gap-v1" for manifest in manifests)
    assert collector.quality_stats[("binance_spot", "BTCUSDT", "trade")].gap_events == 1


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
                "U": 99,
                "u": 101,
                "pu": 98,
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


def test_forward_collector_persists_only_final_one_second_binance_klines(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(raw_data_root=tmp_path, polymarket_token_ids=("up-token",))
    payload = {
        "stream": "btcusdt@kline_1s",
        "data": {
            "e": "kline",
            "E": int((SOURCE_TIME + timedelta(seconds=1)).timestamp() * 1_000),
            "s": "BTCUSDT",
            "k": {
                "t": int(SOURCE_TIME.timestamp() * 1_000),
                "T": int(SOURCE_TIME.timestamp() * 1_000) + 999,
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
    }

    accepted = collector.handle_binance(
        payload,
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=1, milliseconds=10),
    )
    payload["data"]["k"]["x"] = False  # type: ignore[index]
    ignored = collector.handle_binance(
        payload,
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=1, milliseconds=20),
    )
    manifest = collector.flush()[0]
    row = pq.read_table(tmp_path / manifest.data_path).to_pylist()[0]

    assert accepted.accepted_events == 1
    assert ignored == type(ignored)()
    assert row["event_type"] == "kline_1s"
    assert row["schema_version"] == "binance-kline-1s-v1"


def test_forward_collector_does_not_compare_ordering_across_binance_streams(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    later_trade = collector.handle_binance(
        {
            "stream": "btcusdt@trade",
            "data": {
                "e": "trade",
                "s": "BTCUSDT",
                "T": int((SOURCE_TIME + timedelta(seconds=2)).timestamp() * 1_000),
                "p": "100000",
                "q": "0.01",
                "m": False,
                "t": 9001,
            },
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=2),
    )
    earlier_book_ticker = collector.handle_binance(
        {
            "stream": "btcusdt@bookTicker",
            "data": {
                "s": "BTCUSDT",
                "u": 9002,
                "b": "99999",
                "B": "2",
                "a": "100001",
                "A": "3",
            },
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=1),
    )

    assert later_trade.accepted_events == 1
    assert earlier_book_ticker.accepted_events == 1
    assert earlier_book_ticker.rejected_events == 0


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
    assert collector.binance_depth_needs_snapshot("BTCUSDT")
    assert not collector.binance_depth_requires_snapshot("BTCUSDT")


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
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 504,
        },
        collector_receive_ts=SOURCE_TIME,
    )
    stop_event = asyncio.Event()
    stop_event.set()

    await collector.collect_forever(stop_event=stop_event)

    assert collector.pending_event_count == 0
    assert list(tmp_path.rglob("manifest-*.json"))
    assert collector._session_inventory.snapshot().status == SESSION_STATUS_COMPLETE


def test_dynamic_clob_window_keeps_first_registered_capture_horizon(tmp_path) -> None:
    now = datetime.now(UTC)
    core = PolymarketSubscriptionWindow(
        token_ids=("up-token", "down-token"),
        start=now - timedelta(seconds=90),
        end=now + timedelta(seconds=180),
    )
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=core.token_ids,
        polymarket_token_groups=(core.token_ids,),
        polymarket_subscription_windows=(core,),
    )
    extended = PolymarketSubscriptionWindow(
        token_ids=core.token_ids,
        start=core.start,
        end=now + timedelta(seconds=900),
    )

    assert collector.register_polymarket_subscription_window(extended) == core


@pytest.mark.asyncio
async def test_forward_collector_direct_cancellation_drains_pending_events(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_interval_seconds=60.0,
    )
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 505,
        },
        collector_receive_ts=SOURCE_TIME,
    )
    started = asyncio.Event()

    async def wait_for_stop(  # type: ignore[no-untyped-def]
        _websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
    ) -> None:
        del on_payload, on_error
        started.set()
        await stop_event.wait()

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        wait_for_stop,
    )
    task = asyncio.create_task(collector.collect_forever(stop_event=asyncio.Event()))
    await asyncio.wait_for(started.wait(), timeout=5.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert collector.pending_event_count == 0
    assert list(tmp_path.rglob("manifest-*.json"))


@pytest.mark.asyncio
async def test_forward_collector_shutdown_flush_has_a_bounded_deadline(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_interval_seconds=60.0,
        shutdown_flush_timeout_seconds=0.05,
    )
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 506,
        },
        collector_receive_ts=SOURCE_TIME,
    )
    started = asyncio.Event()
    write_started = Event()
    release_write = Event()
    original_write = collector._writer.write

    async def wait_for_stop(  # type: ignore[no-untyped-def]
        _websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
    ) -> None:
        del on_payload, on_error
        started.set()
        await stop_event.wait()

    def blocked_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        write_started.set()
        assert release_write.wait(timeout=2.0)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        wait_for_stop,
    )
    monkeypatch.setattr(collector._writer, "write", blocked_write)
    stop_event = asyncio.Event()
    task = asyncio.create_task(collector.collect_forever(stop_event=stop_event))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    stop_event.set()

    try:
        with pytest.raises(RuntimeError, match="raw flush did not finish"):
            await asyncio.wait_for(task, timeout=1.0)
        assert write_started.is_set()
    finally:
        release_write.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    await asyncio.sleep(0.1)
    assert not list(tmp_path.rglob("manifest-*.json"))
    session = SessionInventoryRepository(tmp_path).read_all()[0]
    assert session.status == "failed"
    assert session.completed_at == session.updated_at


@pytest.mark.asyncio
async def test_forward_collector_flush_deadline_does_not_await_cancellation_resistance(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        shutdown_flush_timeout_seconds=0.02,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def resist_cancellation() -> None:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    flush_task = asyncio.create_task(
        resist_cancellation(),
        name="cancellation-resistant-flush",
    )
    await started.wait()

    with pytest.raises(RuntimeError, match="raw flush did not finish"):
        await asyncio.wait_for(
            collector._await_flush_task(
                flush_task,
                deadline=asyncio.get_running_loop().time() + 0.02,
            ),
            timeout=0.2,
        )

    assert not flush_task.done()
    release.set()
    await flush_task


def test_forward_collector_stuck_writer_cannot_block_process_exit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    script = textwrap.dedent(
        f"""
        import asyncio
        from datetime import UTC, datetime
        from pathlib import Path
        from threading import Event

        from btc_short_horizon.data.forward import BtcForwardCollector

        collector = BtcForwardCollector(
            raw_data_root=Path({str(tmp_path)!r}),
            polymarket_token_ids=("up-token",),
            shutdown_flush_timeout_seconds=0.02,
        )
        now = datetime(2026, 4, 13, tzinfo=UTC)
        collector.handle_binance(
            {{
                "e": "trade",
                "s": "BTCUSDT",
                "T": int(now.timestamp() * 1_000),
                "p": "100000",
                "q": "0.01",
                "m": False,
                "t": 90001,
            }},
            collector_receive_ts=now,
        )
        collector._writer.write = lambda *_args, **_kwargs: Event().wait()
        stop = asyncio.Event()
        stop.set()
        try:
            asyncio.run(collector.collect_forever(stop_event=stop))
        except RuntimeError as exc:
            assert "raw flush did not finish" in str(exc)
            print("bounded-exit")
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "bounded-exit" in completed.stdout


@pytest.mark.asyncio
async def test_forward_collector_shutdown_deadline_includes_producer_quiescence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        shutdown_flush_timeout_seconds=0.05,
    )
    started = asyncio.Event()
    release = asyncio.Event()
    producer_tasks: list[asyncio.Task[object]] = []

    async def resist_cancellation(  # type: ignore[no-untyped-def]
        _websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
    ) -> None:
        del stop_event, on_payload, on_error
        current = asyncio.current_task()
        assert current is not None
        producer_tasks.append(current)
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        resist_cancellation,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(collector.collect_forever(stop_event=stop_event))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    stop_event.set()

    try:
        with pytest.raises(RuntimeError, match="collector tasks did not stop"):
            await asyncio.wait_for(task, timeout=0.5)
    finally:
        release.set()
        await asyncio.gather(*producer_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_forward_collector_propagates_producer_cleanup_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    binance_started = asyncio.Event()
    wait_forever = asyncio.Event()

    async def fail_during_cleanup(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
    ) -> None:
        del on_payload, on_error
        if "binance" not in websocket_collector.subscription.endpoint.casefold():
            await stop_event.wait()
            return
        binance_started.set()
        try:
            await wait_forever.wait()
        finally:
            raise RuntimeError("producer cleanup failed")

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fail_during_cleanup,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(collector.collect_forever(stop_event=stop_event))
    await asyncio.wait_for(binance_started.wait(), timeout=5.0)
    stop_event.set()

    with pytest.raises(RuntimeError, match="producer cleanup failed"):
        await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_forward_collector_preserves_producer_error_when_final_flush_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 509,
        },
        collector_receive_ts=SOURCE_TIME,
    )
    producer_started = asyncio.Event()
    wait_forever = asyncio.Event()

    async def fail_during_cleanup(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_payload, on_error, on_connected
        if "binance" in websocket_collector.subscription.endpoint:
            producer_started.set()
            try:
                await wait_forever.wait()
            finally:
                raise RuntimeError("producer cleanup failed")
        await stop_event.wait()

    def fail_write(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("storage failed")

    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fail_during_cleanup,
    )
    monkeypatch.setattr(collector._writer, "write", fail_write)
    stop_event = asyncio.Event()
    task = asyncio.create_task(collector.collect_forever(stop_event=stop_event))
    await asyncio.wait_for(producer_started.wait(), timeout=5.0)
    stop_event.set()

    with pytest.raises(OSError, match="storage failed") as captured:
        await asyncio.wait_for(task, timeout=1.0)

    assert any("producer cleanup" in note for note in getattr(captured.value, "__notes__", ()))


@pytest.mark.asyncio
async def test_forward_collector_preserves_writer_timeout_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        shutdown_flush_timeout_seconds=1.0,
    )
    collector.handle_binance(
        {
            "e": "trade",
            "s": "BTCUSDT",
            "T": int(SOURCE_TIME.timestamp() * 1_000),
            "p": "100000",
            "q": "0.01",
            "m": False,
            "t": 507,
        },
        collector_receive_ts=SOURCE_TIME,
    )

    def fail_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise TimeoutError("storage timeout")

    monkeypatch.setattr(collector._writer, "write", fail_write)
    stop_event = asyncio.Event()
    stop_event.set()

    with pytest.raises(TimeoutError, match="storage timeout"):
        await collector.collect_forever(stop_event=stop_event)

    assert collector.pending_event_count == 1
    assert collector._session_inventory.snapshot().status == SESSION_STATUS_FAILED
    assert collector.buffer_stats.flush_failures == 1


@pytest.mark.asyncio
async def test_forward_collector_flushes_event_admitted_during_producer_shutdown(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        flush_interval_seconds=60.0,
    )
    binance_started = asyncio.Event()
    flush_worker_exited = asyncio.Event()
    wait_forever = asyncio.Event()
    late_results: list[object] = []
    late_errors: list[BaseException] = []

    async def exit_flush_worker(  # type: ignore[no-untyped-def]
        *,
        stop_event: asyncio.Event,
        flush_requested: asyncio.Event,
        capacity_available: asyncio.Event,
    ) -> None:
        del flush_requested, capacity_available
        await stop_event.wait()
        flush_worker_exited.set()

    async def admit_after_cancel(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
    ) -> None:
        del on_error
        if "binance" not in websocket_collector.subscription.endpoint.casefold():
            await stop_event.wait()
            await wait_forever.wait()
            return
        binance_started.set()
        await stop_event.wait()
        try:
            await wait_forever.wait()
        except asyncio.CancelledError:
            assert flush_worker_exited.is_set()
            try:
                result = await on_payload(
                    {
                        "stream": "btcusdt@trade",
                        "data": {
                            "e": "trade",
                            "s": "BTCUSDT",
                            "T": int(SOURCE_TIME.timestamp() * 1_000),
                            "p": "100000",
                            "q": "0.01",
                            "m": False,
                            "t": 508,
                        },
                    },
                    SOURCE_TIME,
                )
            except BaseException as exc:
                late_errors.append(exc)
                raise
            late_results.append(result)
            raise

    monkeypatch.setattr(collector, "_run_flush_worker", exit_flush_worker)
    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        admit_after_cancel,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.collect_forever(
            stop_event=stop_event,
            binance_streams=("btcusdt@trade",),
        )
    )
    await asyncio.wait_for(binance_started.wait(), timeout=5.0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert not late_errors
    assert late_results == [True]
    assert collector.pending_event_count == 0
    parquet_files = list(tmp_path.rglob("part-*.parquet"))
    assert len(parquet_files) == 1
    assert pq.read_table(parquet_files[0]).column("event_type").to_pylist() == ["trade"]


@pytest.mark.asyncio
async def test_forward_collector_propagates_unexpected_depth_resync_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    refresh_calls = 0
    first_refresh_finished = asyncio.Event()

    async def refresh(  # type: ignore[no-untyped-def]
        *,
        instrument: str,
        client: httpx.AsyncClient,
        source: str,
        snapshot_handler=None,
    ):
        nonlocal refresh_calls
        assert instrument == "BTCUSDT"
        assert client is not None
        assert source == "binance_spot"
        refresh_calls += 1
        if refresh_calls > 1:
            raise RuntimeError("unexpected resync failure")
        payload = {
            "lastUpdateId": 100,
            "bids": [["100000", "2"]],
            "asks": [["100001", "3"]],
        }
        result = (
            await snapshot_handler(payload, SOURCE_TIME)
            if snapshot_handler is not None
            else collector.handle_binance_depth_snapshot(
                instrument=instrument,
                payload=payload,
                collector_receive_ts=SOURCE_TIME,
                source=source,
            )
        )
        first_refresh_finished.set()
        return result

    async def fake_socket_loop(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_error
        if "binance" in websocket_collector.subscription.endpoint:
            assert on_connected is not None
            assert refresh_calls == 0
            connected = on_connected()
            if connected is not None:
                await connected
            await first_refresh_finished.wait()
            await on_payload(
                {
                    "stream": "btcusdt@depth@100ms",
                    "data": {
                        "e": "depthUpdate",
                        "E": int(SOURCE_TIME.timestamp() * 1_000),
                        "s": "BTCUSDT",
                        "U": 101,
                        "u": 101,
                        "b": [],
                        "a": [],
                    },
                },
                SOURCE_TIME,
            )
            await on_payload(
                {
                    "stream": "btcusdt@depth@100ms",
                    "data": {
                        "e": "depthUpdate",
                        "E": int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000),
                        "s": "BTCUSDT",
                        "U": 103,
                        "u": 103,
                        "b": [],
                        "a": [],
                    },
                },
                SOURCE_TIME + timedelta(milliseconds=100),
            )
        await stop_event.wait()

    monkeypatch.setattr(collector, "refresh_binance_depth_snapshot", refresh)
    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fake_socket_loop,
    )

    with pytest.raises(RuntimeError, match="unexpected resync failure"):
        await asyncio.wait_for(
            collector.collect_forever(
                stop_event=asyncio.Event(),
                binance_streams=("btcusdt@depth@100ms",),
            ),
            timeout=3.0,
        )

    assert refresh_calls == 2


@pytest.mark.asyncio
async def test_forward_collector_retries_depth_snapshot_with_bounded_backoff(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        binance_depth_snapshot_retry_initial_seconds=0.03,
        binance_depth_snapshot_retry_max_seconds=0.06,
    )
    call_times: list[float] = []
    synchronized_snapshot = asyncio.Event()

    async def refresh(  # type: ignore[no-untyped-def]
        *,
        instrument: str,
        client: httpx.AsyncClient,
        source: str,
        snapshot_handler=None,
    ):
        call_times.append(asyncio.get_running_loop().time())
        if len(call_times) < 3:
            request = httpx.Request("GET", "https://example.test/depth")
            raise httpx.ConnectError("snapshot unavailable", request=request)
        payload = {
            "lastUpdateId": 100,
            "bids": [["100000", "2"]],
            "asks": [["100001", "3"]],
        }
        result = (
            await snapshot_handler(payload, datetime.now(UTC))
            if snapshot_handler is not None
            else collector.handle_binance_depth_snapshot(
                instrument=instrument,
                payload=payload,
                collector_receive_ts=SOURCE_TIME,
                source=source,
            )
        )
        synchronized_snapshot.set()
        return result

    async def fake_socket_loop(  # type: ignore[no-untyped-def]
        websocket_collector,
        *,
        stop_event: asyncio.Event,
        on_payload,
        on_error=None,
        on_connected=None,
    ) -> None:
        del on_payload, on_error
        if "binance" in websocket_collector.subscription.endpoint:
            assert on_connected is not None
            connected = on_connected()
            if connected is not None:
                await connected
            await synchronized_snapshot.wait()
            stop_event.set()
            return
        await stop_event.wait()

    monkeypatch.setattr(collector, "refresh_binance_depth_snapshot", refresh)
    monkeypatch.setattr(
        "btc_short_horizon.data.forward.JsonWebSocketCollector.collect_forever",
        fake_socket_loop,
    )

    await asyncio.wait_for(
        collector.collect_forever(
            stop_event=asyncio.Event(),
            binance_streams=("btcusdt@depth@100ms",),
        ),
        timeout=3.0,
    )

    assert len(call_times) == 3
    assert call_times[1] - call_times[0] >= 0.02
    assert call_times[2] - call_times[1] >= 0.05
    assert collector.binance_depth_needs_snapshot("BTCUSDT")
    assert not collector.binance_depth_requires_snapshot("BTCUSDT")


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

    assert after_gap.accepted_events == 1
    assert after_gap.resubscribe_required


def test_forward_collector_persists_invalid_clob_reset_for_offline_replay(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        ingest_version="test-v7",
    )
    collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "book-before-invalid",
        },
        collector_receive_ts=SOURCE_TIME,
    )
    collector.flush()

    invalid = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000),
            "price_changes": [
                {"asset_id": "up-token", "price": "0.53", "size": "5", "side": "BUY"}
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=110),
    )
    collector.flush()
    loaded = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id="up-token",
        start_time=SOURCE_TIME - timedelta(seconds=1),
        end_time=SOURCE_TIME + timedelta(seconds=1),
        ingest_version="test-v7",
    )

    assert invalid.accepted_events == 1
    assert invalid.resubscribe_required
    assert len(loaded.events) == 2
    assert loaded.events[0].book is not None
    assert loaded.events[1].book is None
    assert loaded.events[1].reset_book
    assert not collector.feed_health(
        now=SOURCE_TIME + timedelta(milliseconds=110),
        stale_after_seconds=30.0,
    ).healthy


def test_forward_collector_malformed_clob_payload_invalidates_all_connection_books(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token", "down-token"),
    )
    for token_id, bid, ask in (
        ("up-token", "0.48", "0.52"),
        ("down-token", "0.47", "0.53"),
    ):
        collector.handle_polymarket(
            {
                "event_type": "book",
                "asset_id": token_id,
                "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
                "bids": [{"price": bid, "size": "11"}],
                "asks": [{"price": ask, "size": "9"}],
                "hash": f"book-{token_id}",
            },
            collector_receive_ts=SOURCE_TIME,
        )

    malformed = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000),
            "price_changes": [
                {
                    "asset_id": "up-token",
                    "price": "0.49",
                    "size": "5",
                    "side": "HOLD",
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=110),
    )

    assert malformed.accepted_events == 0
    assert malformed.rejected_events == 1
    assert malformed.resubscribe_required
    assert not collector._polymarket_normalizers["up-token"].has_snapshot
    assert not collector._polymarket_normalizers["down-token"].has_snapshot
    assert collector.quality_stats[("polymarket_clob", "up-token", "market")].gap_events == 1
    assert collector.quality_stats[("polymarket_clob", "down-token", "market")].gap_events == 1


def test_forward_collector_accepts_documented_empty_book_bbo_sentinels(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "book-before-terminal-empty",
        },
        collector_receive_ts=SOURCE_TIME,
    )

    result = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000),
            "price_changes": [
                {
                    "asset_id": "up-token",
                    "price": "0.48",
                    "size": "0",
                    "side": "BUY",
                    "best_bid": "0",
                    "best_ask": "1",
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=110),
    )

    assert result.accepted_events == 1
    assert result.rejected_events == 0
    assert not result.resubscribe_required
    assert collector._polymarket_normalizers["up-token"].book_levels() == ((), ())
    assert collector.quality_stats[("polymarket_clob", "up-token", "market")].gap_events == 0


def test_forward_collector_persists_terminal_malformed_clob_gap_for_replay(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        ingest_version="test-v8",
    )
    collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "terminal-gap-book",
        },
        collector_receive_ts=SOURCE_TIME,
    )
    collector.flush()
    gap_at = SOURCE_TIME + timedelta(milliseconds=100)

    malformed = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int(gap_at.timestamp() * 1_000),
            "price_changes": [
                {
                    "asset_id": "up-token",
                    "price": "0.49",
                    "size": "5",
                    "side": "HOLD",
                }
            ],
        },
        collector_receive_ts=gap_at,
    )
    collector.flush()
    loaded = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id="up-token",
        start_time=SOURCE_TIME,
        end_time=SOURCE_TIME + timedelta(seconds=1),
        ingest_version="test-v8",
    )

    assert malformed.resubscribe_required
    assert len(loaded.events) == 2
    assert loaded.events[0].book is not None
    assert loaded.events[1].reset_book
    assert loaded.events[1].book is None
    assert loaded.events[1].available_ts_ns == int(gap_at.timestamp() * 1_000_000_000)


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

    assert rejected.accepted_events == 0
    assert rejected.rejected_events == 1
    assert rejected.resubscribe_required
    assert "source_timestamp_regression" in rejected.reasons
    assert rejected.resubscribe_required
    assert after_gap.accepted_events == 1
    assert after_gap.resubscribe_required


def test_forward_collector_keeps_delayed_trade_out_of_book_continuity_lane(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    book = collector.handle_polymarket(
        {
            "event_type": "book",
            "asset_id": "up-token",
            "timestamp": int(SOURCE_TIME.timestamp() * 1_000),
            "bids": [{"price": "0.48", "size": "11"}],
            "asks": [{"price": "0.52", "size": "9"}],
            "hash": "book-1",
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=1),
    )
    changed = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int((SOURCE_TIME + timedelta(seconds=5)).timestamp() * 1_000),
            "price_changes": [
                {"asset_id": "up-token", "price": "0.49", "size": "5", "side": "BUY"}
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=6),
    )
    newer_trade = collector.handle_polymarket(
        {
            "event_type": "last_trade_price",
            "asset_id": "up-token",
            "price": "0.51",
            "size": "1",
            "side": "BUY",
            "timestamp": int((SOURCE_TIME + timedelta(seconds=4)).timestamp() * 1_000),
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=6, milliseconds=500),
    )
    delayed_trade = collector.handle_polymarket(
        {
            "event_type": "last_trade_price",
            "asset_id": "up-token",
            "price": "0.51",
            "size": "2",
            "side": "BUY",
            "timestamp": int((SOURCE_TIME + timedelta(seconds=2)).timestamp() * 1_000),
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=7),
    )
    next_change = collector.handle_polymarket(
        {
            "event_type": "price_change",
            "timestamp": int((SOURCE_TIME + timedelta(seconds=6)).timestamp() * 1_000),
            "price_changes": [
                {"asset_id": "up-token", "price": "0.50", "size": "4", "side": "BUY"}
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(seconds=8),
    )
    collector.flush()
    loaded = load_forward_raw_events(
        raw_data_root=tmp_path,
        source="polymarket_clob",
        instrument="up-token",
        start_time=SOURCE_TIME,
        end_time=SOURCE_TIME + timedelta(seconds=9),
        ingest_version="btc-short-horizon-v1",
    )

    assert all(
        result.accepted_events == 1 and not result.resubscribe_required
        for result in (book, changed, newer_trade, delayed_trade, next_change)
    )
    assert sum(item.gap_events for item in collector.quality_stats.values()) == 0
    assert not [event for event in loaded.events if event.event_type == "continuity_gap"]


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


def test_forward_collector_resubscribes_okx_immediately_on_quality_time_regression(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "snapshot",
            "data": [
                {
                    "ts": str(int((SOURCE_TIME + timedelta(seconds=2)).timestamp() * 1_000)),
                    "seqId": "100",
                    "prevSeqId": "-1",
                    "bids": [["100000", "2", "0", "1"]],
                    "asks": [["100001", "3", "0", "1"]],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )

    rejected = collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "update",
            "data": [
                {
                    "ts": str(int((SOURCE_TIME + timedelta(seconds=1)).timestamp() * 1_000)),
                    "seqId": "101",
                    "prevSeqId": "100",
                    "bids": [],
                    "asks": [],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )

    assert rejected.accepted_events == 0
    assert rejected.rejected_events == 1
    assert rejected.reasons == ("out_of_order",)
    assert rejected.resubscribe_required
    assert not collector._okx_book_synchronizer(
        source="okx_spot",
        instrument="BTC-USDT",
    ).synchronized


def test_forward_collector_malformed_binance_depth_invalidates_and_reconnects(
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
                "E": int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 101,
                "u": 101,
                "b": [["100000.5", "4"]],
                "a": [],
            },
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=110),
    )
    assert collector.binance_depth_is_synchronized("BTCUSDT")

    invalid = collector.handle_binance(
        {
            "stream": "btcusdt@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "E": int((SOURCE_TIME + timedelta(milliseconds=200)).timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 102,
                "u": 102,
                "b": [["not-a-price", "4"]],
                "a": [],
            },
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=210),
    )

    assert invalid.accepted_events == 0
    assert invalid.rejected_events == 1
    assert invalid.resubscribe_required
    assert collector.binance_depth_needs_snapshot("BTCUSDT")
    assert collector.binance_depth_requires_snapshot("BTCUSDT")


def test_forward_collector_persists_terminal_malformed_binance_depth_gap(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        ingest_version="test-v8",
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
                "E": int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 101,
                "u": 101,
                "b": [["100000.5", "4"]],
                "a": [],
            },
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=110),
    )
    collector.flush()
    gap_at = SOURCE_TIME + timedelta(milliseconds=210)

    invalid = collector.handle_binance(
        {
            "stream": "btcusdt@depth@100ms",
            "data": {
                "e": "depthUpdate",
                "E": int((SOURCE_TIME + timedelta(milliseconds=200)).timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 102,
                "u": 102,
                "b": [["not-a-price", "4"]],
                "a": [],
            },
        },
        collector_receive_ts=gap_at,
    )
    collector.flush()
    loaded = load_forward_raw_events(
        raw_data_root=tmp_path,
        source="binance_spot",
        instrument="BTCUSDT",
        start_time=SOURCE_TIME,
        end_time=SOURCE_TIME + timedelta(seconds=1),
        ingest_version="test-v8",
    )
    gaps = [event for event in loaded.events if event.event_type == "continuity_gap"]

    assert invalid.resubscribe_required
    assert len(gaps) == 1
    assert gaps[0].payload["stream_id"] == "depth"
    assert gaps[0].available_ts_ns == int(gap_at.timestamp() * 1_000_000_000)


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
                "U": 99,
                "u": 101,
                "b": [["100000.5", "4"]],
                "a": [],
            },
        },
        collector_receive_ts=SOURCE_TIME,
        source="binance_perp",
    )

    assert result.accepted_events == 1
    assert collector.binance_depth_needs_snapshot("BTCUSDT", source="binance_spot")
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


def test_forward_collector_rejects_okx_trade_batch_atomically(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(raw_data_root=tmp_path, polymarket_token_ids=("up-token",))

    result = collector.handle_okx(
        {
            "arg": {"channel": "trades", "instId": "BTC-USDT"},
            "data": [
                {
                    "instId": "BTC-USDT",
                    "px": "100000",
                    "sz": "0.1",
                    "side": "buy",
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000)),
                    "tradeId": "trade-valid",
                },
                {
                    "instId": "BTC-USDT",
                    "px": "100001",
                    "sz": "0.1",
                    "side": "hold",
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000) + 1),
                    "tradeId": "trade-invalid",
                },
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )

    assert result.accepted_events == 0
    assert result.rejected_events == 1
    assert collector.pending_event_count == 0


def test_forward_collector_rejects_okx_trade_envelope_item_instrument_mismatch(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )

    result = collector.handle_okx(
        {
            "arg": {"channel": "trades", "instId": "BTC-USDT"},
            "data": [
                {
                    "instId": "BTC-USDT-SWAP",
                    "px": "100000",
                    "sz": "1",
                    "side": "buy",
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000)),
                    "tradeId": "wrong-instrument",
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )

    assert result.accepted_events == 0
    assert result.rejected_events == 1
    assert result.reasons == ("OKX item instId does not match arg.instId",)
    assert collector.pending_event_count == 0


def test_forward_collector_requests_okx_resubscribe_after_sequence_gap(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    snapshot = collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "snapshot",
            "data": [
                {
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000)),
                    "seqId": "100",
                    "prevSeqId": "-1",
                    "bids": [["100000", "2", "0", "1"]],
                    "asks": [["100001", "3", "0", "1"]],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )
    gap = collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "update",
            "data": [
                {
                    "ts": str(int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000)),
                    "seqId": "103",
                    "prevSeqId": "102",
                    "bids": [],
                    "asks": [],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=110),
    )

    assert snapshot.accepted_events == 1
    assert not snapshot.resubscribe_required
    assert gap.accepted_events == 1
    assert gap.resubscribe_required
    assert "previous_sequence_mismatch" in gap.reasons
    assert not collector._okx_book_synchronizer(
        source="okx_spot",
        instrument="BTC-USDT",
    ).synchronized


def test_forward_collector_applies_okx_maintenance_reset_when_sequence_is_reused(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )

    def books(
        *,
        action: str,
        sequence: int,
        previous: int,
        offset_ms: int,
    ):  # type: ignore[no-untyped-def]
        return collector.handle_okx(
            {
                "arg": {"channel": "books", "instId": "BTC-USDT"},
                "action": action,
                "data": [
                    {
                        "ts": str(
                            int(
                                (SOURCE_TIME + timedelta(milliseconds=offset_ms)).timestamp()
                                * 1_000
                            )
                        ),
                        "seqId": str(sequence),
                        "prevSeqId": str(previous),
                        "bids": [["100000", "2", "0", "1"]],
                        "asks": [["100001", "3", "0", "1"]],
                    }
                ],
            },
            collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=offset_ms),
        )

    assert books(action="snapshot", sequence=1, previous=-1, offset_ms=0).accepted_events == 1
    assert books(action="update", sequence=3, previous=1, offset_ms=100).accepted_events == 1
    assert books(action="update", sequence=15, previous=3, offset_ms=200).accepted_events == 1
    reset = books(action="update", sequence=3, previous=15, offset_ms=300)
    after_reset = books(action="update", sequence=5, previous=3, offset_ms=400)

    assert reset.accepted_events == 1
    assert reset.rejected_events == 0
    assert after_reset.accepted_events == 1
    assert collector.quality_stats[("okx_spot", "BTC-USDT", "book")].duplicate_events == 0
    assert collector._okx_book_synchronizer(
        source="okx_spot",
        instrument="BTC-USDT",
    ).synchronized


def test_forward_collector_okx_heartbeat_refreshes_feed_activity(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.configure_required_feeds(
        binance_streams=(),
        binance_futures_market_streams=(),
        binance_futures_public_streams=(),
        okx_subscriptions=({"channel": "books", "instId": "BTC-USDT"},),
    )
    collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "snapshot",
            "data": [
                {
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000)),
                    "seqId": "100",
                    "prevSeqId": "-1",
                    "bids": [["100000", "2", "0", "1"]],
                    "asks": [["100001", "3", "0", "1"]],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )
    for offset_seconds in (5, 20):
        heartbeat = collector.handle_okx(
            {
                "arg": {"channel": "books", "instId": "BTC-USDT"},
                "action": "update",
                "data": [
                    {
                        "ts": str(
                            int(
                                (SOURCE_TIME + timedelta(seconds=offset_seconds)).timestamp()
                                * 1_000
                            )
                        ),
                        "seqId": "100",
                        "prevSeqId": "100",
                        "bids": [],
                        "asks": [],
                    }
                ],
            },
            collector_receive_ts=SOURCE_TIME + timedelta(seconds=offset_seconds),
        )
        assert heartbeat.accepted_events == 1
        assert "heartbeat" in heartbeat.reasons

    health = collector.feed_health(
        now=SOURCE_TIME + timedelta(seconds=25),
        stale_after_seconds=10.0,
    )
    okx_feed = next(feed for feed in health.feeds if feed["source"] == "okx_spot")

    assert okx_feed["state"] == "ok"
    assert okx_feed["age_seconds"] == 5.0


def test_forward_collector_invalid_okx_book_payload_invalidates_and_resubscribes(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )
    collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "snapshot",
            "data": [
                {
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000)),
                    "seqId": "100",
                    "prevSeqId": "-1",
                    "bids": [["100000", "2", "0", "1"]],
                    "asks": [["100001", "3", "0", "1"]],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )

    invalid = collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "update",
            "data": [
                {
                    "ts": str(int((SOURCE_TIME + timedelta(milliseconds=100)).timestamp() * 1_000)),
                    "seqId": "not-an-integer",
                    "prevSeqId": "100",
                    "bids": [],
                    "asks": [],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME + timedelta(milliseconds=100),
    )

    assert invalid.accepted_events == 0
    assert invalid.rejected_events == 1
    assert invalid.resubscribe_required
    assert not collector._okx_book_synchronizer(
        source="okx_spot",
        instrument="BTC-USDT",
    ).synchronized


def test_forward_collector_persists_terminal_malformed_okx_book_gap(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
        ingest_version="test-v8",
    )
    collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "snapshot",
            "data": [
                {
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000)),
                    "seqId": "100",
                    "prevSeqId": "-1",
                    "bids": [["100000", "2", "0", "1"]],
                    "asks": [["100001", "3", "0", "1"]],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )
    collector.flush()
    gap_at = SOURCE_TIME + timedelta(milliseconds=100)

    invalid = collector.handle_okx(
        {
            "arg": {"channel": "books", "instId": "BTC-USDT"},
            "action": "update",
            "data": [
                {
                    "ts": str(int(gap_at.timestamp() * 1_000)),
                    "seqId": "not-an-integer",
                    "prevSeqId": "100",
                    "bids": [],
                    "asks": [],
                }
            ],
        },
        collector_receive_ts=gap_at,
    )
    collector.flush()
    loaded = load_forward_raw_events(
        raw_data_root=tmp_path,
        source="okx_spot",
        instrument="BTC-USDT",
        start_time=SOURCE_TIME,
        end_time=SOURCE_TIME + timedelta(seconds=1),
        ingest_version="test-v8",
    )
    gaps = [event for event in loaded.events if event.event_type == "continuity_gap"]

    assert invalid.resubscribe_required
    assert len(gaps) == 1
    assert gaps[0].payload["stream_id"] == "book"
    assert gaps[0].available_ts_ns == int(gap_at.timestamp() * 1_000_000_000)


@pytest.mark.parametrize("control_event", ["error", "notice", "unexpected"])
def test_forward_collector_reconnects_on_non_ack_okx_control_event(
    tmp_path,
    control_event: str,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )

    result = collector.handle_okx(
        {"event": control_event, "code": "60012", "msg": "subscription failed"},
        collector_receive_ts=SOURCE_TIME,
    )

    assert result.accepted_events == 0
    assert result.rejected_events == 1
    assert result.reasons == (f"okx_control_{control_event}",)
    assert result.resubscribe_required


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


def test_forward_collector_accepts_binance_partial_depth_as_snapshot(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )

    result = collector.handle_binance(
        {
            "stream": "btcusdt@depth20@100ms",
            "data": {
                "lastUpdateId": 160,
                "bids": [["100000", "2"]],
                "asks": [["100001", "3"]],
            },
        },
        collector_receive_ts=SOURCE_TIME,
    )

    assert result.accepted_events == 1
    assert result.rejected_events == 0
    assert not result.resubscribe_required
    assert collector.quality_stats[("binance_spot", "BTCUSDT", "partial_book")].accepted_events == 1


def test_forward_collector_accepts_futures_partial_depth_envelope_as_snapshot(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )

    result = collector.handle_binance(
        {
            "stream": "btcusdt@depth20@100ms",
            "data": {
                "e": "depthUpdate",
                "E": int(SOURCE_TIME.timestamp() * 1_000),
                "T": int(SOURCE_TIME.timestamp() * 1_000),
                "s": "BTCUSDT",
                "U": 150,
                "u": 160,
                "pu": 149,
                "b": [["100000", "2"]],
                "a": [["100001", "3"]],
            },
        },
        collector_receive_ts=SOURCE_TIME,
        source="binance_perp",
    )

    assert result.accepted_events == 1
    assert result.rejected_events == 0
    assert not result.resubscribe_required
    assert collector.quality_stats[("binance_perp", "BTCUSDT", "partial_book")].accepted_events == 1


def test_forward_collector_accepts_okx_books5_snapshot_without_incremental_fields(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    collector = BtcForwardCollector(
        raw_data_root=tmp_path,
        polymarket_token_ids=("up-token",),
    )

    result = collector.handle_okx(
        {
            "arg": {"channel": "books5", "instId": "BTC-USDT"},
            "data": [
                {
                    "instId": "BTC-USDT",
                    "ts": str(int(SOURCE_TIME.timestamp() * 1_000)),
                    "seqId": "100",
                    "bids": [["100000", "2", "0", "1"]],
                    "asks": [["100001", "3", "0", "1"]],
                }
            ],
        },
        collector_receive_ts=SOURCE_TIME,
    )

    assert result.accepted_events == 1
    assert result.rejected_events == 0
    assert not result.resubscribe_required
    assert collector.quality_stats[("okx_spot", "BTC-USDT", "book")].accepted_events == 1
