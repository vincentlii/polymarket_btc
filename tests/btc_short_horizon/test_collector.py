from __future__ import annotations

import asyncio
from datetime import datetime
import json

import pytest

from btc_short_horizon.data.collector import (
    BusinessPayloadInactivityError,
    JsonWebSocketCollector,
    WebSocketSubscription,
)


class _FakeSocket:
    def __init__(self, frames: list[str]) -> None:
        self._frames = frames

    async def recv(self) -> str:
        return self._frames.pop(0)

    async def send(self, _payload: str) -> None:
        return None


class _ActiveSocket:
    def __init__(self, *, stop_event: asyncio.Event | None = None) -> None:
        self.received = 0
        self.sent: list[str] = []
        self.stop_event = stop_event

    async def recv(self) -> str:
        await asyncio.sleep(0)
        self.received += 1
        return '{"event_type":"book"}'

    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        if self.stop_event is not None and len(self.sent) == 2:
            self.stop_event.set()


class _BlockingSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def recv(self) -> str:
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


class _DelayedSocket:
    def __init__(self, *, delay_seconds: float, frame: str) -> None:
        self.delay_seconds = delay_seconds
        self.frame = frame

    async def recv(self) -> str:
        await asyncio.sleep(self.delay_seconds)
        return self.frame

    async def send(self, _payload: str) -> None:
        return None


class _FailingSocket:
    async def recv(self) -> str:
        await asyncio.sleep(0)
        raise RuntimeError("connection failed")

    async def send(self, _payload: str) -> None:
        return None


class _SocketContext:
    def __init__(self, socket: object) -> None:
        self.socket = socket

    async def __aenter__(self) -> object:
        return self.socket

    async def __aexit__(self, *_args: object) -> None:
        return None


class _OnePayloadThenFailSocket:
    def __init__(self) -> None:
        self.calls = 0

    async def recv(self) -> str:
        self.calls += 1
        if self.calls == 1:
            return '{"event_type":"book"}'
        raise RuntimeError("connection failed after activity")

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


def test_websocket_collector_sends_heartbeat_while_messages_remain_active() -> None:
    async def collect() -> tuple[list[str], int]:
        stop_event = asyncio.Event()
        socket = _ActiveSocket(stop_event=stop_event)
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                heartbeat_payload="PING",
                heartbeat_interval_seconds=0.005,
            )
        )

        await asyncio.wait_for(
            collector._collect_connection(
                socket=socket,
                stop_event=stop_event,
                on_payload=lambda _payload, _received: None,
            ),
            timeout=0.2,
        )
        return socket.sent, socket.received

    sent, received = asyncio.run(collect())

    assert sent == ["PING", "PING"]
    assert received > 2


def test_websocket_collector_without_heartbeat_stops_without_sending() -> None:
    async def collect() -> list[str]:
        stop_event = asyncio.Event()
        socket = _BlockingSocket()
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                heartbeat_payload=None,
                heartbeat_interval_seconds=60.0,
            )
        )

        task = asyncio.create_task(
            collector._collect_connection(
                socket=socket,
                stop_event=stop_event,
                on_payload=lambda _payload, _received: None,
            )
        )
        await asyncio.sleep(0)
        stop_event.set()
        await asyncio.wait_for(task, timeout=0.05)
        return socket.sent

    assert asyncio.run(collect()) == []


def test_websocket_collector_raises_when_business_payloads_are_silent() -> None:
    async def collect() -> None:
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                business_payload_timeout_seconds=0.01,
            )
        )

        with pytest.raises(BusinessPayloadInactivityError) as caught:
            await asyncio.wait_for(
                collector._collect_connection(
                    socket=_BlockingSocket(),
                    stop_event=asyncio.Event(),
                    on_payload=lambda _payload, _received: True,
                ),
                timeout=0.1,
            )

        assert caught.value.timeout_seconds == pytest.approx(0.01)
        assert caught.value.endpoint == "wss://example.test/market"

    asyncio.run(collect())


def test_websocket_collector_control_frames_do_not_mask_business_inactivity() -> None:
    async def collect() -> None:
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                business_payload_timeout_seconds=0.01,
            )
        )

        with pytest.raises(BusinessPayloadInactivityError):
            await asyncio.wait_for(
                collector._collect_connection(
                    socket=_DelayedSocket(delay_seconds=0.002, frame="PONG"),
                    stop_event=asyncio.Event(),
                    on_payload=lambda _payload, _received: True,
                ),
                timeout=0.1,
            )

    asyncio.run(collect())


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_websocket_subscription_rejects_invalid_business_payload_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="business_payload_timeout_seconds"):
        WebSocketSubscription(
            endpoint="wss://example.test/market",
            business_payload_timeout_seconds=timeout,
        )


def test_websocket_collector_business_payloads_keep_connection_active() -> None:
    async def collect() -> int:
        stop_event = asyncio.Event()
        socket = _ActiveSocket()
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                business_payload_timeout_seconds=0.01,
            )
        )
        accepted = 0

        def on_payload(_payload: dict[str, object], _received: datetime) -> bool:
            nonlocal accepted
            accepted += 1
            if accepted == 5:
                stop_event.set()
            return True

        await asyncio.wait_for(
            collector._collect_connection(
                socket=socket,
                stop_event=stop_event,
                on_payload=on_payload,
            ),
            timeout=0.1,
        )
        return accepted

    assert asyncio.run(collect()) == 5


def test_websocket_collector_allows_payload_within_low_frequency_threshold() -> None:
    async def collect() -> list[dict[str, object]]:
        stop_event = asyncio.Event()
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                business_payload_timeout_seconds=0.05,
            )
        )
        received: list[dict[str, object]] = []

        def on_payload(payload: dict[str, object], _received: datetime) -> bool:
            received.append(payload)
            stop_event.set()
            return True

        await asyncio.wait_for(
            collector._collect_connection(
                socket=_DelayedSocket(delay_seconds=0.02, frame='{"event_type":"book"}'),
                stop_event=stop_event,
                on_payload=on_payload,
            ),
            timeout=0.1,
        )
        return received

    assert asyncio.run(collect()) == [{"event_type": "book"}]


def test_websocket_collector_stop_wins_before_inactivity_timeout() -> None:
    async def collect() -> None:
        stop_event = asyncio.Event()
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                business_payload_timeout_seconds=0.05,
            )
        )
        task = asyncio.create_task(
            collector._collect_connection(
                socket=_BlockingSocket(),
                stop_event=stop_event,
                on_payload=lambda _payload, _received: True,
            )
        )
        await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=0.1)

    asyncio.run(collect())


def test_websocket_collector_reconnects_after_business_payload_inactivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect() -> tuple[list[Exception], int]:
        stop_event = asyncio.Event()
        sockets = iter(
            (
                _BlockingSocket(),
                _DelayedSocket(delay_seconds=0.0, frame='{"event_type":"book"}'),
            )
        )
        connections = 0

        def connect(_endpoint: str) -> _SocketContext:
            nonlocal connections
            connections += 1
            return _SocketContext(next(sockets))

        monkeypatch.setattr("btc_short_horizon.data.collector.websockets.connect", connect)
        errors: list[Exception] = []
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                business_payload_timeout_seconds=0.01,
                reconnect_delay_seconds=0.001,
                reconnect_max_delay_seconds=0.001,
                reconnect_jitter_ratio=0.0,
            )
        )

        def on_payload(_payload: dict[str, object], _received: datetime) -> bool:
            stop_event.set()
            return True

        await asyncio.wait_for(
            collector.collect_forever(
                stop_event=stop_event,
                on_payload=on_payload,
                on_error=errors.append,
            ),
            timeout=0.1,
        )
        return errors, connections

    errors, connections = asyncio.run(collect())

    assert len(errors) == 1
    assert isinstance(errors[0], BusinessPayloadInactivityError)
    assert connections == 2


def test_websocket_collector_reports_connection_after_subscription_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect() -> tuple[list[str], list[dict[str, object]]]:
        stop_event = asyncio.Event()
        socket = _BlockingSocket()
        observed_payloads: list[dict[str, object]] = []
        monkeypatch.setattr(
            "btc_short_horizon.data.collector.websockets.connect",
            lambda _endpoint: _SocketContext(socket),
        )
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                subscribe_payload={"method": "SUBSCRIBE"},
            )
        )

        def on_connected() -> None:
            observed_payloads.extend(json.loads(payload) for payload in socket.sent)
            stop_event.set()

        await collector.collect_forever(
            stop_event=stop_event,
            on_payload=lambda _payload, _received: None,
            on_connected=on_connected,
        )
        return socket.sent, observed_payloads

    sent, observed = asyncio.run(collect())

    assert [json.loads(payload) for payload in sent] == [{"method": "SUBSCRIBE"}]
    assert observed == [{"method": "SUBSCRIBE"}]


def test_websocket_collector_connection_error_cleans_up_internal_tasks() -> None:
    async def collect() -> None:
        collector = JsonWebSocketCollector(
            WebSocketSubscription(
                endpoint="wss://example.test/market",
                heartbeat_payload="PING",
                heartbeat_interval_seconds=60.0,
            )
        )
        tasks_before = set(asyncio.all_tasks())

        with pytest.raises(RuntimeError, match="connection failed"):
            await collector._collect_connection(
                socket=_FailingSocket(),
                stop_event=asyncio.Event(),
                on_payload=lambda _payload, _received: None,
            )

        await asyncio.sleep(0)
        assert set(asyncio.all_tasks()) == tasks_before

    asyncio.run(collect())


def test_websocket_collector_connection_errors_use_bounded_deterministic_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect() -> list[float]:
        class _RecordingCollector(JsonWebSocketCollector):
            def __init__(self) -> None:
                super().__init__(
                    WebSocketSubscription(
                        endpoint="wss://example.test/market",
                        reconnect_delay_seconds=1.0,
                        reconnect_max_delay_seconds=4.0,
                        reconnect_jitter_ratio=0.2,
                    ),
                    jitter_source=lambda: 0.5,
                )
                self.delays: list[float] = []

            async def _wait_for_stop(self, stop_event: asyncio.Event, delay: float) -> bool:
                self.delays.append(delay)
                if len(self.delays) == 4:
                    stop_event.set()
                    return True
                return False

        def fail_connect(_endpoint: str) -> None:
            raise OSError("connect failed")

        monkeypatch.setattr(
            "btc_short_horizon.data.collector.websockets.connect",
            fail_connect,
        )
        collector = _RecordingCollector()
        await collector.collect_forever(
            stop_event=asyncio.Event(),
            on_payload=lambda _payload, _received: None,
        )
        return collector.delays

    assert asyncio.run(collect()) == [1.0, 2.0, 4.0, 4.0]


def test_websocket_collector_does_not_reset_backoff_for_empty_handshakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect() -> list[float]:
        class _RecordingCollector(JsonWebSocketCollector):
            def __init__(self) -> None:
                super().__init__(
                    WebSocketSubscription(
                        endpoint="wss://example.test/market",
                        reconnect_delay_seconds=1.0,
                        reconnect_max_delay_seconds=4.0,
                        reconnect_jitter_ratio=0.0,
                    )
                )
                self.delays: list[float] = []

            async def _wait_for_stop(self, stop_event: asyncio.Event, delay: float) -> bool:
                self.delays.append(delay)
                if len(self.delays) == 4:
                    stop_event.set()
                    return True
                return False

        monkeypatch.setattr(
            "btc_short_horizon.data.collector.websockets.connect",
            lambda _endpoint: _SocketContext(_FailingSocket()),
        )
        collector = _RecordingCollector()
        await collector.collect_forever(
            stop_event=asyncio.Event(),
            on_payload=lambda _payload, _received: None,
        )
        return collector.delays

    assert asyncio.run(collect()) == [1.0, 2.0, 4.0, 4.0]


def test_websocket_collector_resets_backoff_only_after_processed_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect() -> list[float]:
        sockets = iter((_FailingSocket(), _FailingSocket(), _OnePayloadThenFailSocket()))

        class _RecordingCollector(JsonWebSocketCollector):
            def __init__(self) -> None:
                super().__init__(
                    WebSocketSubscription(
                        endpoint="wss://example.test/market",
                        reconnect_delay_seconds=1.0,
                        reconnect_max_delay_seconds=4.0,
                        reconnect_jitter_ratio=0.0,
                    )
                )
                self.delays: list[float] = []

            async def _wait_for_stop(self, stop_event: asyncio.Event, delay: float) -> bool:
                self.delays.append(delay)
                if len(self.delays) == 3:
                    stop_event.set()
                    return True
                return False

        monkeypatch.setattr(
            "btc_short_horizon.data.collector.websockets.connect",
            lambda _endpoint: _SocketContext(next(sockets)),
        )
        collector = _RecordingCollector()
        await collector.collect_forever(
            stop_event=asyncio.Event(),
            on_payload=lambda _payload, _received: None,
        )
        return collector.delays

    assert asyncio.run(collect()) == [1.0, 2.0, 1.0]


def test_websocket_collector_stop_cancels_a_blocked_connection_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BlockedContext:
        async def __aenter__(self) -> object:
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def collect() -> None:
        monkeypatch.setattr(
            "btc_short_horizon.data.collector.websockets.connect",
            lambda _endpoint: _BlockedContext(),
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            JsonWebSocketCollector(
                WebSocketSubscription(endpoint="wss://example.test/market")
            ).collect_forever(
                stop_event=stop_event,
                on_payload=lambda _payload, _received: None,
            )
        )
        await asyncio.sleep(0)
        stop_event.set()
        await asyncio.wait_for(task, timeout=0.1)

    asyncio.run(collect())


def test_websocket_collector_direct_cancellation_cleans_up_connection_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect() -> None:
        monkeypatch.setattr(
            "btc_short_horizon.data.collector.websockets.connect",
            lambda _endpoint: _SocketContext(_BlockingSocket()),
        )
        tasks_before = set(asyncio.all_tasks())
        task = asyncio.create_task(
            JsonWebSocketCollector(
                WebSocketSubscription(endpoint="wss://example.test/market")
            ).collect_forever(
                stop_event=asyncio.Event(),
                on_payload=lambda _payload, _received: None,
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert set(asyncio.all_tasks()) == tasks_before

    asyncio.run(collect())


def test_websocket_collector_control_only_frames_do_not_reset_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def collect() -> list[float]:
        class _RecordingCollector(JsonWebSocketCollector):
            def __init__(self) -> None:
                super().__init__(
                    WebSocketSubscription(
                        endpoint="wss://example.test/market",
                        reconnect_delay_seconds=1.0,
                        reconnect_max_delay_seconds=4.0,
                        reconnect_jitter_ratio=0.0,
                    )
                )
                self.delays: list[float] = []

            async def _wait_for_stop(self, stop_event: asyncio.Event, delay: float) -> bool:
                self.delays.append(delay)
                if len(self.delays) == 4:
                    stop_event.set()
                    return True
                return False

        monkeypatch.setattr(
            "btc_short_horizon.data.collector.websockets.connect",
            lambda _endpoint: _SocketContext(_OnePayloadThenFailSocket()),
        )
        collector = _RecordingCollector()
        await collector.collect_forever(
            stop_event=asyncio.Event(),
            on_payload=lambda _payload, _received: False,
        )
        return collector.delays

    assert asyncio.run(collect()) == [1.0, 2.0, 4.0, 4.0]
