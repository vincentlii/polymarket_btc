"""Official public WebSocket subscription payload builders."""

from __future__ import annotations

import json
from urllib.parse import quote

from btc_short_horizon.data.collector import WebSocketSubscription

POLYMARKET_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLYMARKET_RTDS_WS = "wss://ws-live-data.polymarket.com"
BINANCE_SPOT_STREAM_WS = "wss://stream.binance.com:9443/stream"
BINANCE_FUTURES_MARKET_STREAM_WS = "wss://fstream.binance.com/market/stream"
BINANCE_FUTURES_PUBLIC_STREAM_WS = "wss://fstream.binance.com/public/stream"
OKX_PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"


def polymarket_market_subscription(token_ids: tuple[str, ...]) -> WebSocketSubscription:
    if not token_ids or any(not token_id for token_id in token_ids):
        raise ValueError("at least one non-empty token ID is required")
    return WebSocketSubscription(
        endpoint=POLYMARKET_MARKET_WS,
        subscribe_payload={
            "type": "market",
            "assets_ids": list(token_ids),
            "custom_feature_enabled": True,
            "initial_dump": True,
        },
        heartbeat_payload="PING",
        heartbeat_interval_seconds=10.0,
    )


def polymarket_rtds_chainlink_btc_subscription() -> WebSocketSubscription:
    return WebSocketSubscription(
        endpoint=POLYMARKET_RTDS_WS,
        subscribe_payload={
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": json.dumps({"symbol": "btc/usd"}, separators=(",", ":")),
                }
            ],
        },
        heartbeat_payload="PING",
        heartbeat_interval_seconds=5.0,
    )


def binance_combined_stream_subscription(streams: tuple[str, ...]) -> WebSocketSubscription:
    return _binance_combined_stream_subscription(
        endpoint=BINANCE_SPOT_STREAM_WS,
        streams=streams,
    )


def binance_futures_market_stream_subscription(
    streams: tuple[str, ...],
) -> WebSocketSubscription:
    return _binance_combined_stream_subscription(
        endpoint=BINANCE_FUTURES_MARKET_STREAM_WS,
        streams=streams,
    )


def binance_futures_public_stream_subscription(
    streams: tuple[str, ...],
) -> WebSocketSubscription:
    return _binance_combined_stream_subscription(
        endpoint=BINANCE_FUTURES_PUBLIC_STREAM_WS,
        streams=streams,
    )


def _binance_combined_stream_subscription(
    *, endpoint: str, streams: tuple[str, ...]
) -> WebSocketSubscription:
    if not streams or any(
        not isinstance(stream, str) or not stream or stream.strip() != stream for stream in streams
    ):
        raise ValueError(
            "Binance stream names must be non-empty strings without surrounding whitespace"
        )
    stream_path = "/".join(streams)
    return WebSocketSubscription(
        endpoint=f"{endpoint}?streams={quote(stream_path, safe='/@')}",
    )


def okx_public_subscription(
    arguments: tuple[dict[str, str], ...],
) -> WebSocketSubscription:
    if not arguments:
        raise ValueError("OKX subscription arguments must not be empty")
    normalized: list[dict[str, str]] = []
    for argument in arguments:
        channel = argument.get("channel")
        instrument = argument.get("instId")
        if (
            not isinstance(channel, str)
            or not channel
            or not isinstance(instrument, str)
            or not instrument
        ):
            raise ValueError("each OKX subscription requires non-empty channel and instId")
        normalized.append({"channel": channel, "instId": instrument})
    return WebSocketSubscription(
        endpoint=OKX_PUBLIC_WS,
        subscribe_payload={"op": "subscribe", "args": normalized},
        heartbeat_payload="ping",
        heartbeat_interval_seconds=20.0,
    )
