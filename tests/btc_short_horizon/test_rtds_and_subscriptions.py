from __future__ import annotations

from datetime import UTC, datetime

import pytest

from btc_short_horizon.data.rtds import normalize_chainlink_btc_usd
from btc_short_horizon.data.subscriptions import (
    binance_combined_stream_subscription,
    binance_futures_market_stream_subscription,
    binance_futures_public_stream_subscription,
    okx_public_subscription,
    polymarket_market_subscription,
    polymarket_rtds_chainlink_btc_subscription,
)


def test_rtds_chainlink_reference_uses_inner_source_timestamp() -> None:
    record = normalize_chainlink_btc_usd(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "timestamp": 1_776_038_400_100,
            "payload": {"symbol": "btc/usd", "timestamp": 1_776_038_400_000, "value": 100000.0},
        },
        collector_receive_ts=datetime(2026, 4, 13, 0, 0, 1, tzinfo=UTC),
    )

    assert record.timing.source_ts.timestamp() == pytest.approx(1_776_038_400.0)
    assert record.reference.price == pytest.approx(100000.0)


def test_public_subscription_builders_follow_current_channel_heartbeats() -> None:
    market = polymarket_market_subscription(("up", "down"))
    rtds = polymarket_rtds_chainlink_btc_subscription()
    binance = binance_combined_stream_subscription(
        (
            "btcusdt@trade",
            "btcusdt@kline_1s",
            "btcusdt@depth@100ms",
            "btcusdt@bookTicker",
        )
    )
    futures_market = binance_futures_market_stream_subscription(("btcusdt@aggTrade",))
    futures_public = binance_futures_public_stream_subscription(
        ("btcusdt@depth@100ms", "btcusdt@bookTicker")
    )
    okx = okx_public_subscription(
        (
            {"channel": "trades", "instId": "BTC-USDT"},
            {"channel": "books", "instId": "BTC-USDT-SWAP"},
        )
    )

    assert market.heartbeat_payload == "PING"
    assert market.heartbeat_interval_seconds == 10.0
    assert market.subscribe_payload is not None
    assert market.subscribe_payload["initial_dump"] is True
    assert rtds.heartbeat_interval_seconds == 5.0
    assert rtds.business_payload_timeout_seconds == 15.0
    assert (
        "streams=btcusdt@trade/btcusdt@kline_1s/btcusdt@depth@100ms/btcusdt@bookTicker"
        in binance.endpoint
    )
    assert futures_market.endpoint.endswith("/market/stream?streams=btcusdt@aggTrade")
    assert futures_public.endpoint.endswith(
        "/public/stream?streams=btcusdt@depth@100ms/btcusdt@bookTicker"
    )
    assert futures_market.subscribe_payload is None
    assert futures_public.subscribe_payload is None
    assert binance.business_payload_timeout_seconds == 10.0
    assert futures_market.business_payload_timeout_seconds is None
    assert futures_public.business_payload_timeout_seconds is None
    assert market.business_payload_timeout_seconds is None
    assert okx.business_payload_timeout_seconds is None
    assert okx.subscribe_payload == {
        "op": "subscribe",
        "args": [
            {"channel": "trades", "instId": "BTC-USDT"},
            {"channel": "books", "instId": "BTC-USDT-SWAP"},
        ],
    }
    assert okx.heartbeat_payload == "ping"
