"""Credential-redacted authenticated User WebSocket with gap gating."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import inspect
import json
from math import isfinite
import re
from time import time_ns
from typing import Any
from urllib.parse import urlsplit

import websockets

from btc_short_horizon.live.authentication import LiveCredentials


_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_OFFICIAL_USER_CHANNEL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


@dataclass(frozen=True, slots=True)
class UserChannelConfig:
    endpoint: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    ping_interval_seconds: float = 10.0
    pong_timeout_seconds: float = 5.0
    reconnect_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
    open_timeout_seconds: float = 10.0
    close_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint) if isinstance(self.endpoint, str) else None
        if (
            parsed is None
            or parsed.scheme != "wss"
            or parsed.hostname != "ws-subscriptions-clob.polymarket.com"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/ws/user"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("user channel endpoint must be the official user channel")
        object.__setattr__(self, "endpoint", _OFFICIAL_USER_CHANNEL)
        for name in (
            "ping_interval_seconds",
            "pong_timeout_seconds",
            "reconnect_delay_seconds",
            "reconnect_max_delay_seconds",
            "open_timeout_seconds",
            "close_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name} must be a finite number > 0")
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be a finite number > 0")
        if self.pong_timeout_seconds >= self.ping_interval_seconds:
            raise ValueError("pong timeout must be shorter than the ping interval")
        if self.reconnect_max_delay_seconds < self.reconnect_delay_seconds:
            raise ValueError("maximum reconnect delay must cover the initial delay")


@dataclass(frozen=True, slots=True)
class UserChannelHealth:
    connected: bool = False
    pong_healthy: bool = False
    gap_detected: bool = True
    generation: int = 0
    last_event_receive_ts_ns: int | None = None
    last_error_type: str | None = None

    @property
    def ready(self) -> bool:
        return self.connected and self.pong_healthy and not self.gap_detected


type UserEventCallback = Callable[
    [Mapping[str, object], int],
    object | Awaitable[object],
]
type HealthCallback = Callable[[], object | Awaitable[object]]


class AuthenticatedUserChannel:
    """Keep User events live while requiring REST reconciliation after every gap."""

    def __init__(
        self,
        credentials: LiveCredentials,
        *,
        initial_markets: Sequence[str] = (),
        config: UserChannelConfig = UserChannelConfig(),
    ) -> None:
        if not isinstance(credentials, LiveCredentials):
            raise TypeError("credentials must be LiveCredentials")
        self._credentials = credentials
        self.config = config
        self._desired_markets = _condition_ids(initial_markets)
        self._connection_markets: frozenset[str] = frozenset()
        self._market_updates: asyncio.Queue[frozenset[str]] = asyncio.Queue(maxsize=1)
        self._health = UserChannelHealth()
        self._pong_counter = 0

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(market_count={len(self._desired_markets)}, "
            f"health={self._health!r})"
        )

    @property
    def health(self) -> UserChannelHealth:
        return self._health

    async def replace_markets(self, markets: Sequence[str]) -> None:
        desired = _condition_ids(markets)
        if desired == self._desired_markets:
            return
        self._desired_markets = desired
        self._health = replace(self._health, gap_detected=True)
        if self._market_updates.full():
            self._market_updates.get_nowait()
        self._market_updates.put_nowait(desired)

    def acknowledge_reconciliation(self, generation: int) -> None:
        if isinstance(generation, bool) or generation != self._health.generation:
            raise ValueError("cannot reconcile a stale connection generation")
        if not self._health.connected or not self._health.pong_healthy:
            raise ValueError("user channel must be connected with a current PONG")
        if self._desired_markets != self._connection_markets or not self._market_updates.empty():
            raise ValueError("user channel subscription update is still pending")
        self._health = replace(self._health, gap_detected=False)

    async def collect_forever(
        self,
        *,
        stop_event: asyncio.Event,
        on_event: UserEventCallback,
        on_health: HealthCallback | None = None,
    ) -> None:
        reconnect_attempt = 0
        while not stop_event.is_set():
            try:
                await self._connect_once(
                    stop_event=stop_event,
                    on_event=on_event,
                    on_health=on_health,
                )
                reconnect_attempt = 0
            except Exception as exc:  # pragma: no cover - exercised through connect boundaries
                self._mark_disconnected(error_type=type(exc).__name__)
                await _notify(on_health)
                delay = min(
                    self.config.reconnect_max_delay_seconds,
                    self.config.reconnect_delay_seconds * (2 ** min(reconnect_attempt, 16)),
                )
                reconnect_attempt += 1
                if await _wait_for_stop(stop_event, delay):
                    return

    async def _connect_once(
        self,
        *,
        stop_event: asyncio.Event,
        on_event: UserEventCallback,
        on_health: HealthCallback | None,
    ) -> None:
        async with websockets.connect(
            self.config.endpoint,
            ping_interval=None,
            open_timeout=self.config.open_timeout_seconds,
            close_timeout=self.config.close_timeout_seconds,
        ) as socket:
            while not self._market_updates.empty():
                self._desired_markets = self._market_updates.get_nowait()
            await socket.send(json.dumps(self._subscription_payload(), separators=(",", ":")))
            self._connection_markets = self._desired_markets
            self._mark_connected()
            await _notify(on_health)
            try:
                await self._run_connection(
                    socket=socket,
                    stop_event=stop_event,
                    on_event=on_event,
                    on_health=on_health,
                )
            finally:
                self._mark_disconnected()
                await _notify(on_health)

    async def _run_connection(
        self,
        *,
        socket: Any,
        stop_event: asyncio.Event,
        on_event: UserEventCallback,
        on_health: HealthCallback | None = None,
    ) -> None:
        if not self._health.connected:
            self._mark_connected()
            self._connection_markets = self._desired_markets
            await _notify(on_health)
        stop_task = asyncio.create_task(stop_event.wait())
        ping_task = asyncio.create_task(self._ping_loop(socket, stop_event=stop_event))
        recv_task: asyncio.Task[object] | None = None
        update_task: asyncio.Task[frozenset[str]] | None = None
        try:
            while True:
                recv_task = asyncio.create_task(socket.recv())
                update_task = asyncio.create_task(self._market_updates.get())
                done, _ = await asyncio.wait(
                    {recv_task, update_task, stop_task, ping_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    return
                if ping_task in done:
                    ping_task.result()
                    return
                if update_task in done:
                    self._desired_markets = update_task.result()
                    update_task = None
                    if await self._send_pending_market_update(socket):
                        await _notify(on_health)
                if recv_task in done:
                    raw = recv_task.result()
                    recv_task = None
                    await self._handle_frame(
                        raw,
                        on_event=on_event,
                        on_health=on_health,
                    )
                for pending in (recv_task, update_task):
                    if pending is not None and not pending.done():
                        pending.cancel()
                await asyncio.gather(
                    *(task for task in (recv_task, update_task) if task is not None),
                    return_exceptions=True,
                )
                recv_task = None
                update_task = None
        finally:
            tasks = tuple(
                task for task in (recv_task, update_task, ping_task, stop_task) if task is not None
            )
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_frame(
        self,
        raw: object,
        *,
        on_event: UserEventCallback,
        on_health: HealthCallback | None,
    ) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        if not isinstance(raw, str):
            raise ValueError("user channel frame must be text")
        if raw.strip().casefold() == "pong":
            self._mark_pong()
            await _notify(on_health)
            return
        payload = json.loads(raw)
        if not isinstance(payload, Mapping):
            raise ValueError("user channel JSON frame must be an object")
        event_type = payload.get("event_type")
        if not isinstance(event_type, str) or event_type.casefold() not in {"order", "trade"}:
            raise ValueError("user channel event must be an order or trade")
        received_at_ns = time_ns()
        self._health = replace(self._health, last_event_receive_ts_ns=received_at_ns)
        await _maybe_await(on_event(dict(payload), received_at_ns))

    async def _ping_loop(self, socket: Any, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            previous_pong = self._pong_counter
            await socket.send("PING")
            if await _wait_for_stop(stop_event, self.config.pong_timeout_seconds):
                return
            if self._pong_counter == previous_pong:
                raise TimeoutError("user channel did not return PONG before timeout")
            remaining = self.config.ping_interval_seconds - self.config.pong_timeout_seconds
            if await _wait_for_stop(stop_event, remaining):
                return

    async def _send_pending_market_update(self, socket: Any) -> bool:
        removed = sorted(self._connection_markets - self._desired_markets)
        added = sorted(self._desired_markets - self._connection_markets)
        if removed:
            await socket.send(
                json.dumps(
                    {"operation": "unsubscribe", "markets": removed},
                    separators=(",", ":"),
                )
            )
        if added:
            await socket.send(
                json.dumps(
                    {"operation": "subscribe", "markets": added},
                    separators=(",", ":"),
                )
            )
        self._connection_markets = self._desired_markets
        changed = bool(removed or added)
        if changed:
            self._health = replace(
                self._health,
                gap_detected=True,
                generation=self._health.generation + 1,
            )
        return changed

    def _subscription_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "auth": self._credentials.user_channel_auth(),
            "type": "user",
        }
        if self._desired_markets:
            payload["markets"] = sorted(self._desired_markets)
        return payload

    def _mark_connected(self) -> None:
        self._health = UserChannelHealth(
            connected=True,
            pong_healthy=False,
            gap_detected=True,
            generation=self._health.generation + 1,
            last_event_receive_ts_ns=self._health.last_event_receive_ts_ns,
        )

    def _mark_pong(self) -> None:
        if not self._health.connected:
            raise ValueError("received PONG while user channel is disconnected")
        self._pong_counter += 1
        self._health = replace(self._health, pong_healthy=True, last_error_type=None)

    def _mark_disconnected(self, *, error_type: str | None = None) -> None:
        self._health = replace(
            self._health,
            connected=False,
            pong_healthy=False,
            gap_detected=True,
            last_error_type=error_type,
        )


def _condition_ids(values: Sequence[str]) -> frozenset[str]:
    if isinstance(values, str):
        raise ValueError("user channel markets must contain condition IDs")
    result = frozenset(values)
    if any(not isinstance(value, str) or not _CONDITION_ID.fullmatch(value) for value in result):
        raise ValueError("user channel markets must contain 0x-prefixed condition IDs")
    return result


async def _wait_for_stop(stop_event: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


async def _notify(callback: HealthCallback | None) -> None:
    if callback is not None:
        await _maybe_await(callback())


__all__ = [
    "AuthenticatedUserChannel",
    "UserChannelConfig",
    "UserChannelHealth",
]
