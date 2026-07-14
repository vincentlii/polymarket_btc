from __future__ import annotations

import asyncio
from datetime import datetime

from btc_short_horizon.data.collector import JsonWebSocketCollector, WebSocketSubscription


class _FakeSocket:
    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    async def recv(self) -> str:
        return self._frames.pop(0)

    async def send(self, _payload: str) -> None:
        return None


def test_websocket_collector_expands_initial_batch_and_ignores_control_frames() -> None:
    async def collect() -> list[dict[str, object]]:
        collector = JsonWebSocketCollector(
            WebSocketSubscription(endpoint="wss://example.test/market")
        )
        socket = _FakeSocket(
            [
                "",
                "PONG",
                '[{"event_type":"book","asset_id":"up"},{"event_type":"book","asset_id":"down"}]',
                '{"event_type":"price_change","asset_id":"up"}',
            ]
        )
        stop_event = asyncio.Event()
        received: list[dict[str, object]] = []

        async def on_payload(payload: dict[str, object], _received: datetime) -> None:
            received.append(payload)
            if len(received) == 3:
                stop_event.set()

        await collector._collect_connection(
            socket=socket,
            stop_event=stop_event,
            on_payload=on_payload,
        )
        return received

    received = asyncio.run(collect())

    assert [payload["event_type"] for payload in received] == [
        "book",
        "book",
        "price_change",
    ]
