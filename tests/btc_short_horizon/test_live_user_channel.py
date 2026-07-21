from __future__ import annotations

import asyncio
import json

import pytest

from btc_short_horizon.live.authentication import LiveCredentials
from btc_short_horizon.live.user_channel import (
    AuthenticatedUserChannel,
    UserChannelConfig,
)


CONDITION_A = "0x" + ("a" * 64)
CONDITION_B = "0x" + ("b" * 64)


def _credentials() -> LiveCredentials:
    return LiveCredentials.from_environment(
        {
            "POLY_PRIVATE_KEY": "0x" + ("1" * 64),
            "POLY_API_KEY": "api-key-value",
            "POLY_API_SECRET": "api-secret-value",
            "POLY_PASSPHRASE": "api-passphrase-value",
            "POLY_FUNDER": "0x" + ("2" * 40),
            "POLY_SIGNATURE_TYPE": "2",
        }
    )


class _ScriptedSocket:
    def __init__(self, frames: list[str], *, respond_to_ping: bool = True) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)
        self.respond_to_ping = respond_to_ping
        self.sent: list[str] = []

    async def recv(self) -> str:
        return await self.frames.get()

    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        if payload == "PING" and self.respond_to_ping:
            self.frames.put_nowait("PONG")


def test_user_channel_repr_and_health_never_expose_credentials() -> None:
    channel = AuthenticatedUserChannel(
        _credentials(),
        initial_markets=(CONDITION_A,),
    )

    rendered = repr(channel)
    assert "api-key-value" not in rendered
    assert "api-secret-value" not in rendered
    assert "api-passphrase-value" not in rendered
    assert channel.health.ready is False
    assert "api" not in channel.health.__dataclass_fields__


def test_subscription_payload_uses_condition_ids_and_exact_auth_shape() -> None:
    channel = AuthenticatedUserChannel(
        _credentials(),
        initial_markets=(CONDITION_B, CONDITION_A),
    )

    assert channel._subscription_payload() == {
        "auth": {
            "apiKey": "api-key-value",
            "secret": "api-secret-value",
            "passphrase": "api-passphrase-value",
        },
        "markets": [CONDITION_A, CONDITION_B],
        "type": "user",
    }

    all_markets = AuthenticatedUserChannel(_credentials())
    assert all_markets._subscription_payload() == {
        "auth": {
            "apiKey": "api-key-value",
            "secret": "api-secret-value",
            "passphrase": "api-passphrase-value",
        },
        "type": "user",
    }


@pytest.mark.parametrize(
    "market",
    ["asset-token-id", "0x1234", "", True],
)
def test_user_channel_rejects_non_condition_market_ids(market: object) -> None:
    with pytest.raises(ValueError, match="condition ID"):
        AuthenticatedUserChannel(_credentials(), initial_markets=(market,))  # type: ignore[arg-type]


def test_user_channel_rejects_credential_exfiltration_endpoint() -> None:
    with pytest.raises(ValueError, match="official user channel"):
        AuthenticatedUserChannel(
            _credentials(),
            config=UserChannelConfig(endpoint="wss://attacker.example/ws/user"),
        )


def test_connection_requires_pong_and_explicit_reconciliation_before_ready() -> None:
    async def exercise() -> tuple[list[dict[str, object]], list[bool], list[str]]:
        channel = AuthenticatedUserChannel(
            _credentials(),
            initial_markets=(CONDITION_A,),
            config=UserChannelConfig(
                ping_interval_seconds=0.03,
                pong_timeout_seconds=0.01,
            ),
        )
        socket = _ScriptedSocket([json.dumps({"event_type": "order", "id": "venue-order"})])
        stop_event = asyncio.Event()
        events: list[dict[str, object]] = []
        ready_states: list[bool] = []

        async def on_event(event: dict[str, object], _received_at_ns: int) -> None:
            events.append(event)
            if channel.health.pong_healthy:
                channel.acknowledge_reconciliation(channel.health.generation)
                ready_states.append(channel.health.ready)
                stop_event.set()

        async def on_health() -> None:
            ready_states.append(channel.health.ready)
            if channel.health.pong_healthy and events:
                channel.acknowledge_reconciliation(channel.health.generation)
                ready_states.append(channel.health.ready)
                stop_event.set()

        await asyncio.wait_for(
            channel._run_connection(
                socket=socket,
                stop_event=stop_event,
                on_event=on_event,
                on_health=on_health,
            ),
            timeout=0.2,
        )
        return events, ready_states, socket.sent

    events, ready_states, sent = asyncio.run(exercise())

    assert events == [{"event_type": "order", "id": "venue-order"}]
    assert any(payload == "PING" for payload in sent)
    assert ready_states[0] is False
    assert ready_states[-1] is True


def test_reconnect_generation_invalidates_previous_reconciliation() -> None:
    channel = AuthenticatedUserChannel(_credentials(), initial_markets=(CONDITION_A,))
    channel._mark_connected()
    channel._connection_markets = frozenset({CONDITION_A})
    channel._mark_pong()
    first_generation = channel.health.generation
    channel.acknowledge_reconciliation(first_generation)
    assert channel.health.ready

    channel._mark_disconnected(error_type="ConnectionClosed")
    channel._mark_connected()
    channel._connection_markets = frozenset({CONDITION_A})
    channel._mark_pong()

    assert channel.health.generation == first_generation + 1
    assert not channel.health.ready
    with pytest.raises(ValueError, match="stale connection generation"):
        channel.acknowledge_reconciliation(first_generation)


def test_market_replacement_sends_minimal_dynamic_updates() -> None:
    async def exercise() -> list[dict[str, object]]:
        channel = AuthenticatedUserChannel(_credentials(), initial_markets=(CONDITION_A,))
        channel._mark_connected()
        socket = _ScriptedSocket([], respond_to_ping=False)
        channel._connection_markets = frozenset({CONDITION_A})

        await channel.replace_markets((CONDITION_B,))
        await channel._send_pending_market_update(socket)
        return [json.loads(payload) for payload in socket.sent]

    assert asyncio.run(exercise()) == [
        {"operation": "unsubscribe", "markets": [CONDITION_A]},
        {"operation": "subscribe", "markets": [CONDITION_B]},
    ]


def test_market_replacement_reopens_the_gap_gate_until_fresh_reconciliation() -> None:
    async def exercise() -> tuple[int, int, bool]:
        channel = AuthenticatedUserChannel(_credentials(), initial_markets=(CONDITION_A,))
        channel._mark_connected()
        channel._connection_markets = frozenset({CONDITION_A})
        channel._mark_pong()
        channel.acknowledge_reconciliation(channel.health.generation)
        previous_generation = channel.health.generation
        socket = _ScriptedSocket([], respond_to_ping=False)

        await channel.replace_markets((CONDITION_B,))
        assert not channel.health.ready
        with pytest.raises(ValueError, match="subscription update"):
            channel.acknowledge_reconciliation(previous_generation)
        channel._market_updates.get_nowait()
        assert await channel._send_pending_market_update(socket)
        changed_generation = channel.health.generation
        assert changed_generation == previous_generation + 1
        assert not channel.health.ready
        channel.acknowledge_reconciliation(changed_generation)
        return previous_generation, changed_generation, channel.health.ready

    previous, changed, ready = asyncio.run(exercise())
    assert changed == previous + 1
    assert ready


def test_market_replacement_queue_coalesces_to_the_latest_desired_set() -> None:
    async def exercise() -> tuple[int, frozenset[str]]:
        channel = AuthenticatedUserChannel(_credentials(), initial_markets=(CONDITION_A,))
        await channel.replace_markets((CONDITION_A, CONDITION_B))
        await channel.replace_markets((CONDITION_B,))
        return channel._market_updates.qsize(), channel._market_updates.get_nowait()

    queue_size, queued = asyncio.run(exercise())
    assert queue_size == 1
    assert queued == {CONDITION_B}


def test_missing_pong_fails_the_connection_and_keeps_channel_unready() -> None:
    async def exercise() -> bool:
        channel = AuthenticatedUserChannel(
            _credentials(),
            initial_markets=(CONDITION_A,),
            config=UserChannelConfig(
                ping_interval_seconds=0.02,
                pong_timeout_seconds=0.005,
            ),
        )
        socket = _ScriptedSocket([], respond_to_ping=False)
        with pytest.raises(TimeoutError, match="PONG"):
            await asyncio.wait_for(
                channel._run_connection(
                    socket=socket,
                    stop_event=asyncio.Event(),
                    on_event=lambda _event, _received_at_ns: None,
                ),
                timeout=0.1,
            )
        return channel.health.ready

    assert asyncio.run(exercise()) is False


def test_user_channel_rejects_non_order_trade_payloads() -> None:
    async def exercise() -> None:
        channel = AuthenticatedUserChannel(_credentials(), initial_markets=(CONDITION_A,))
        socket = _ScriptedSocket(['{"event_type":"book"}'])
        with pytest.raises(ValueError, match="order or trade"):
            await asyncio.wait_for(
                channel._run_connection(
                    socket=socket,
                    stop_event=asyncio.Event(),
                    on_event=lambda _event, _received_at_ns: None,
                ),
                timeout=0.1,
            )

    asyncio.run(exercise())
