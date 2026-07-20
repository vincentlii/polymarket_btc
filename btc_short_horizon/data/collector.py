"""Public WebSocket collection primitives and append-only raw Parquet output."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import inspect
import json
from math import isfinite
from pathlib import Path
import random
import re
from types import MappingProxyType
from typing import Any

import pyarrow as pa
import websockets

from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.storage import DataPartitionManifest, ImmutableParquetStore

type RawPartitionKey = tuple[str, str, str, str, str, str, str]
_COLLECTOR_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True, slots=True)
class RawCollectorEvent:
    timing: TimedMarketEvent
    event_type: str
    payload: Mapping[str, object]
    collector_session_id: str
    epoch_id: int
    admission_sequence: int = 0
    _payload_json: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.event_type or not self.event_type.strip():
            raise ValueError("event_type is required")
        collector_session_id = normalize_collector_session_id(self.collector_session_id)
        if (
            isinstance(self.epoch_id, bool)
            or not isinstance(self.epoch_id, int)
            or self.epoch_id < 0
        ):
            raise ValueError("epoch_id must be non-negative")
        if (
            isinstance(self.admission_sequence, bool)
            or not isinstance(self.admission_sequence, int)
            or self.admission_sequence < 0
        ):
            raise ValueError("admission_sequence must be non-negative")
        try:
            payload_json = json.dumps(
                self.payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be a finite JSON object") from exc
        normalized_payload = json.loads(payload_json)
        if not isinstance(normalized_payload, dict):
            raise ValueError("payload must serialize to a JSON object")
        object.__setattr__(self, "event_type", self.event_type.strip())
        object.__setattr__(self, "collector_session_id", collector_session_id)
        object.__setattr__(self, "payload", _freeze_json_value(normalized_payload))
        object.__setattr__(self, "_payload_json", payload_json)

    @property
    def estimated_size_bytes(self) -> int:
        """Bound the in-memory queue using the immutable serialized payload."""

        identity_bytes = sum(
            len(value.encode("utf-8"))
            for value in (
                self.timing.sequence_or_hash,
                self.timing.source,
                self.timing.instrument,
                self.timing.schema_version,
                self.timing.ingest_version,
                self.event_type,
                self.collector_session_id,
            )
        )
        return len(self._payload_json.encode("utf-8")) + identity_bytes + 128

    def as_row(self) -> dict[str, object]:
        return {
            "source_ts_ns": int(self.timing.source_ts.timestamp() * 1_000_000_000),
            "collector_receive_ts_ns": (
                None
                if self.timing.collector_receive_ts is None
                else int(self.timing.collector_receive_ts.timestamp() * 1_000_000_000)
            ),
            "available_ts_ns": int(self.timing.available_ts.timestamp() * 1_000_000_000),
            "sequence_or_hash": self.timing.sequence_or_hash,
            "source": self.timing.source,
            "instrument": self.timing.instrument,
            "schema_version": self.timing.schema_version,
            "ingest_version": self.timing.ingest_version,
            "event_type": self.event_type,
            "collector_session_id": self.collector_session_id,
            "epoch_id": self.epoch_id,
            "admission_sequence": self.admission_sequence,
            "payload_json": self._payload_json,
        }

    def with_admission_sequence(self, value: int) -> RawCollectorEvent:
        """Return the immutable event with its collector-session arrival order."""

        return RawCollectorEvent(
            timing=self.timing,
            event_type=self.event_type,
            payload=json.loads(self._payload_json),
            collector_session_id=self.collector_session_id,
            epoch_id=self.epoch_id,
            admission_sequence=value,
        )


class PartitionedRawEventWriter:
    """Flushes raw events to immutable source/instrument/date/hour Parquet parts."""

    def __init__(
        self,
        root: Path,
        *,
        manifest_attributes: Mapping[str, str] | None = None,
    ) -> None:
        self.store = ImmutableParquetStore(root)
        self.manifest_attributes = dict(manifest_attributes or {})
        if any(
            not isinstance(key, str) or not key or not isinstance(value, str) or not value
            for key, value in self.manifest_attributes.items()
        ):
            raise ValueError("manifest_attributes must contain non-empty string pairs")
        reserved = {"collector_session_id", "epoch_ids"}
        if reserved.intersection(self.manifest_attributes):
            raise ValueError("manifest_attributes contains a reserved key")

    def write(
        self,
        events: Sequence[RawCollectorEvent],
        *,
        quality_counts: Mapping[RawPartitionKey, tuple[int, int]] | None = None,
    ) -> tuple[DataPartitionManifest, ...]:
        if not events:
            return ()
        grouped: dict[tuple[str, ...], list[RawCollectorEvent]] = defaultdict(list)
        partition_quality_counts = quality_counts or {}
        for event in events:
            timestamp = event.timing.available_ts.astimezone(UTC)
            grouped[
                (
                    event.timing.source,
                    event.timing.instrument,
                    timestamp.strftime("%Y-%m-%d"),
                    timestamp.strftime("%H"),
                    event.timing.schema_version,
                    event.timing.ingest_version,
                    event.collector_session_id,
                )
            ].append(event)
        manifests: list[DataPartitionManifest] = []
        for (
            source,
            instrument,
            date,
            hour,
            schema_version,
            ingest_version,
            collector_session_id,
        ), partition in sorted(grouped.items()):
            duplicate_count, gap_count = partition_quality_counts.get(
                (
                    source,
                    instrument,
                    date,
                    hour,
                    schema_version,
                    ingest_version,
                    collector_session_id,
                ),
                (0, 0),
            )
            table = pa.Table.from_pylist([event.as_row() for event in partition])
            manifests.append(
                self.store.write(
                    table=table,
                    source=source,
                    instrument=instrument,
                    partition_date=date,
                    partition_hour=hour,
                    schema_version=schema_version,
                    ingest_version=ingest_version,
                    duplicate_count=duplicate_count,
                    gap_count=gap_count,
                    attributes={
                        **self.manifest_attributes,
                        "collector_session_id": collector_session_id,
                        "epoch_ids": ",".join(str(event.epoch_id) for event in partition),
                    },
                )
            )
        return tuple(manifests)


@dataclass(frozen=True, slots=True)
class WebSocketSubscription:
    endpoint: str
    subscribe_payload: Mapping[str, object] | None = None
    heartbeat_payload: str | Mapping[str, object] | None = None
    heartbeat_interval_seconds: float = 5.0
    reconnect_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
    reconnect_jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if not self.endpoint.startswith("wss://"):
            raise ValueError("endpoint must use wss")
        if (
            not isfinite(self.heartbeat_interval_seconds)
            or not isfinite(self.reconnect_delay_seconds)
            or not isfinite(self.reconnect_max_delay_seconds)
            or self.heartbeat_interval_seconds <= 0.0
            or self.reconnect_delay_seconds <= 0.0
            or self.reconnect_max_delay_seconds < self.reconnect_delay_seconds
        ):
            raise ValueError("heartbeat and reconnect intervals must be finite and > 0")
        if (
            not isfinite(self.reconnect_jitter_ratio)
            or not 0.0 <= self.reconnect_jitter_ratio < 1.0
        ):
            raise ValueError("reconnect_jitter_ratio must be in [0, 1)")


class JsonWebSocketCollector:
    """Reconnectable public JSON WebSocket loop with non-blocking callbacks."""

    def __init__(
        self,
        subscription: WebSocketSubscription,
        *,
        jitter_source: Callable[[], float] = random.random,
    ) -> None:
        self.subscription = subscription
        self._jitter_source = jitter_source

    async def collect_forever(
        self,
        *,
        stop_event: asyncio.Event,
        on_payload: Callable[[Mapping[str, object], datetime], object | Awaitable[object]],
        on_error: Callable[[Exception], object | Awaitable[object]] | None = None,
        on_connected: Callable[[], object | Awaitable[object]] | None = None,
    ) -> None:
        reconnect_attempt = 0
        while not stop_event.is_set():

            def reset_attempt() -> None:
                nonlocal reconnect_attempt
                reconnect_attempt = 0

            connection_task = asyncio.create_task(
                self._connect_once(
                    stop_event=stop_event,
                    on_payload=on_payload,
                    on_activity=reset_attempt,
                    on_connected=on_connected,
                )
            )
            stop_task = asyncio.create_task(stop_event.wait())

            try:
                done, _ = await asyncio.wait(
                    {connection_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task in done:
                    connection_task.cancel()
                    await asyncio.gather(connection_task, return_exceptions=True)
                    return
                await connection_task
            except Exception as exc:  # pragma: no cover - network behavior is integration-tested
                if on_error is not None:
                    await _maybe_await(on_error(exc))
                delay = self._reconnect_delay(reconnect_attempt)
                reconnect_attempt += 1
                if await self._wait_for_stop(stop_event, delay):
                    return
            finally:
                if not connection_task.done():
                    connection_task.cancel()
                if not stop_task.done():
                    stop_task.cancel()
                await asyncio.gather(connection_task, stop_task, return_exceptions=True)

    async def _connect_once(
        self,
        *,
        stop_event: asyncio.Event,
        on_payload: Callable[[Mapping[str, object], datetime], object | Awaitable[object]],
        on_activity: Callable[[], None],
        on_connected: Callable[[], object | Awaitable[object]] | None,
    ) -> None:
        async with websockets.connect(self.subscription.endpoint) as socket:
            if self.subscription.subscribe_payload is not None:
                await socket.send(json.dumps(self.subscription.subscribe_payload))
            if on_connected is not None:
                await _maybe_await(on_connected())

            async def on_connection_payload(
                payload: Mapping[str, object],
                received_at: datetime,
            ) -> None:
                accepted = await _maybe_await(on_payload(payload, received_at))
                if accepted is not False:
                    on_activity()

            await self._collect_connection(
                socket=socket,
                stop_event=stop_event,
                on_payload=on_connection_payload,
            )

    async def _collect_connection(
        self,
        *,
        socket: Any,
        stop_event: asyncio.Event,
        on_payload: Callable[[Mapping[str, object], datetime], object | Awaitable[object]],
    ) -> None:
        stop_task = asyncio.create_task(stop_event.wait())
        heartbeat_task = (
            asyncio.create_task(self._send_heartbeats(socket=socket, stop_event=stop_event))
            if self.subscription.heartbeat_payload is not None
            else None
        )
        recv_task: asyncio.Task[object] | None = None
        try:
            while True:
                recv_task = asyncio.create_task(socket.recv())
                wait_tasks = {recv_task, stop_task}
                if heartbeat_task is not None:
                    wait_tasks.add(heartbeat_task)
                done, _ = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
                if stop_task in done:
                    return
                if heartbeat_task is not None and heartbeat_task in done:
                    heartbeat_task.result()
                    return
                raw = recv_task.result()
                recv_task = None
                for payload in _message_payloads(raw):
                    await _maybe_await(on_payload(payload, datetime.now(UTC)))
        finally:
            tasks = tuple(
                task for task in (recv_task, heartbeat_task, stop_task) if task is not None
            )
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_heartbeats(self, *, socket: Any, stop_event: asyncio.Event) -> None:
        heartbeat = self.subscription.heartbeat_payload
        if heartbeat is None:
            return
        payload = heartbeat if isinstance(heartbeat, str) else json.dumps(heartbeat)
        while not await self._wait_for_stop(
            stop_event,
            self.subscription.heartbeat_interval_seconds,
        ):
            await socket.send(payload)

    async def _wait_for_stop(self, stop_event: asyncio.Event, delay: float) -> bool:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    def _reconnect_delay(self, attempt: int) -> float:
        base_delay = self.subscription.reconnect_delay_seconds
        max_delay = self.subscription.reconnect_max_delay_seconds
        exponent = min(max(attempt, 0), 63)
        exponential_delay = min(max_delay, base_delay * (2**exponent))
        jitter_sample = self._jitter_source()
        if not 0.0 <= jitter_sample <= 1.0:
            raise ValueError("jitter_source must return a value in [0, 1]")
        jitter_ratio = self.subscription.reconnect_jitter_ratio
        jittered_delay = exponential_delay * (1.0 + jitter_ratio * ((2.0 * jitter_sample) - 1.0))
        return min(max_delay, jittered_delay)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _message_payloads(raw: str | bytes) -> tuple[Mapping[str, object], ...]:
    """Expand JSON batch envelopes and ignore documented text heartbeat acknowledgements."""

    if isinstance(raw, str) and raw.strip().casefold() in {"", "pong"}:
        return ()
    payload = json.loads(raw)
    if isinstance(payload, Mapping):
        return (payload,)
    if isinstance(payload, list):
        if not all(isinstance(item, Mapping) for item in payload):
            raise ValueError("JSON batch message must contain only objects")
        return tuple(payload)
    if isinstance(payload, str) and payload.strip().casefold() in {"", "pong"}:
        return ()
    raise ValueError("WebSocket JSON message must be an object or an object array")


def normalize_collector_session_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("collector_session_id has an invalid format")
    normalized = value.strip()
    if not _COLLECTOR_SESSION_ID.fullmatch(normalized):
        raise ValueError("collector_session_id has an invalid format")
    return normalized


def _freeze_json_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


__all__ = [
    "JsonWebSocketCollector",
    "PartitionedRawEventWriter",
    "RawCollectorEvent",
    "RawPartitionKey",
    "WebSocketSubscription",
    "normalize_collector_session_id",
]
