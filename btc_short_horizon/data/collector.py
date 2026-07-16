"""Public WebSocket collection primitives and append-only raw Parquet output."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import inspect
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import websockets

from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.storage import DataPartitionManifest, ImmutableParquetStore

type RawPartitionKey = tuple[str, str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class RawCollectorEvent:
    timing: TimedMarketEvent
    event_type: str
    payload: Mapping[str, object]
    epoch_id: int

    def __post_init__(self) -> None:
        if not self.event_type:
            raise ValueError("event_type is required")
        if self.epoch_id < 0:
            raise ValueError("epoch_id must be non-negative")

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
            "epoch_id": self.epoch_id,
            "payload_json": json.dumps(
                self.payload, sort_keys=True, separators=(",", ":"), default=str
            ),
        }


class PartitionedRawEventWriter:
    """Flushes raw events to immutable source/instrument/date/hour Parquet parts."""

    def __init__(self, root: Path) -> None:
        self.store = ImmutableParquetStore(root)

    def write(
        self,
        events: Sequence[RawCollectorEvent],
        *,
        quality_counts: Mapping[RawPartitionKey, tuple[int, int]] | None = None,
    ) -> tuple[DataPartitionManifest, ...]:
        if not events:
            return ()
        grouped: dict[RawPartitionKey, list[RawCollectorEvent]] = defaultdict(list)
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
                )
            ].append(event)
        manifests: list[DataPartitionManifest] = []
        for (source, instrument, date, hour, schema_version, ingest_version), partition in sorted(
            grouped.items()
        ):
            duplicate_count, gap_count = partition_quality_counts.get(
                (source, instrument, date, hour, schema_version, ingest_version),
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
                    attributes={"epoch_ids": ",".join(str(event.epoch_id) for event in partition)},
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

    def __post_init__(self) -> None:
        if not self.endpoint.startswith("wss://"):
            raise ValueError("endpoint must use wss")
        if self.heartbeat_interval_seconds <= 0.0 or self.reconnect_delay_seconds <= 0.0:
            raise ValueError("heartbeat and reconnect intervals must be > 0")


class JsonWebSocketCollector:
    """Reconnectable public JSON WebSocket loop with non-blocking callbacks."""

    def __init__(self, subscription: WebSocketSubscription) -> None:
        self.subscription = subscription

    async def collect_forever(
        self,
        *,
        stop_event: asyncio.Event,
        on_payload: Callable[[Mapping[str, object], datetime], object | Awaitable[object]],
        on_error: Callable[[Exception], object | Awaitable[object]] | None = None,
    ) -> None:
        while not stop_event.is_set():
            try:
                async with websockets.connect(self.subscription.endpoint) as socket:
                    if self.subscription.subscribe_payload is not None:
                        await socket.send(json.dumps(self.subscription.subscribe_payload))
                    await self._collect_connection(
                        socket=socket,
                        stop_event=stop_event,
                        on_payload=on_payload,
                    )
            except Exception as exc:  # pragma: no cover - network behavior is integration-tested
                if on_error is not None:
                    await _maybe_await(on_error(exc))
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self.subscription.reconnect_delay_seconds
                    )
                except TimeoutError:
                    continue

    async def _collect_connection(
        self,
        *,
        socket: Any,
        stop_event: asyncio.Event,
        on_payload: Callable[[Mapping[str, object], datetime], object | Awaitable[object]],
    ) -> None:
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(
                    socket.recv(), timeout=self.subscription.heartbeat_interval_seconds
                )
            except TimeoutError:
                if self.subscription.heartbeat_payload is not None:
                    heartbeat = self.subscription.heartbeat_payload
                    await socket.send(
                        heartbeat if isinstance(heartbeat, str) else json.dumps(heartbeat)
                    )
                continue
            for payload in _message_payloads(raw):
                await _maybe_await(on_payload(payload, datetime.now(UTC)))


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value


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
