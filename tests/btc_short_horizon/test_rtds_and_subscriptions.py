from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from btc_short_horizon.data.rtds import (
    normalize_chainlink_btc_usd,
    normalize_chainlink_btc_usd_twap_60s,
)
from btc_short_horizon.data.subscriptions import (
    binance_combined_stream_subscription,
    binance_futures_market_stream_subscription,
    binance_futures_public_stream_subscription,
    okx_public_subscription,
    polymarket_market_subscription,
    polymarket_rtds_chainlink_btc_subscription,
    polymarket_rtds_chainlink_btc_twap_60s_subscription,
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


def test_rtds_twap_60s_preserves_exact_e18_and_publication_time() -> None:
    record = normalize_chainlink_btc_usd_twap_60s(
        {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "timestamp": 1_776_038_400_123,
            "payload": {
                "symbol": "btc/usd",
                "timestamp": 1_776_038_400_000,
                "value": 100000.25,
                "full_accuracy_value": "100000250000000000000000",
                "window_s": 60,
            },
        },
        collector_receive_ts=datetime(2026, 4, 13, 0, 0, 1, tzinfo=UTC),
    )

    assert record.timing.source_ts.timestamp() == pytest.approx(1_776_038_400.0)
    assert record.publication_ts.timestamp() == pytest.approx(1_776_038_400.123)
    assert record.full_accuracy_value == "100000250000000000000000"
    assert record.value == Decimal("100000.25")
    assert record.window_seconds == 60


def test_rtds_twap_60s_rejects_display_value_that_disagrees_with_signed_e18() -> None:
    with pytest.raises(ValueError, match="display value.*full_accuracy_value"):
        normalize_chainlink_btc_usd_twap_60s(
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "timestamp": 1_776_038_400_123,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp": 1_776_038_400_000,
                    "value": "100001.25",
                    "full_accuracy_value": "100000250000000000000000",
                    "window_s": 60,
                },
            },
            collector_receive_ts=datetime(2026, 4, 13, 0, 0, 1, tzinfo=UTC),
        )


def test_public_subscription_builders_follow_current_channel_heartbeats() -> None:
    market = polymarket_market_subscription(("up", "down"))
    rtds = polymarket_rtds_chainlink_btc_subscription()
    twap = polymarket_rtds_chainlink_btc_twap_60s_subscription()
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
    assert twap.heartbeat_payload == "PING"
    assert twap.heartbeat_interval_seconds == 5.0
    assert twap.subscribe_payload == {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            }
        ],
    }
    assert (
        "streams=btcusdt@trade/btcusdt@kline_1s/btcusdt@depth@100ms/btcusdt@bookTicker"
        in binance.endpoint
    )
    assert futures_market.endpoint.endswith("/market/stream?streams=btcusdt@aggTrade")
    assert futures_public.endpoint.endswith(
        "/market/stream?streams=btcusdt@depth@100ms/btcusdt@bookTicker"
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
