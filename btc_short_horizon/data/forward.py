"""Forward public-feed collector that preserves causal timing and data epochs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
from math import isfinite
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, RLock, Thread
from time import monotonic_ns
from typing import Any
from uuid import uuid4

import httpx

from btc_short_horizon.data.binance import (
    BinanceDepthSynchronizer,
    DepthUpdateStatus,
    normalize_binance_book_ticker,
    normalize_binance_depth_snapshot,
    normalize_binance_depth_update,
    normalize_binance_kline,
    normalize_binance_trade,
)
from btc_short_horizon.data.collector import (
    JsonWebSocketCollector,
    PartitionedRawEventWriter,
    RawCollectorEvent,
    normalize_collector_session_id,
)
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.coverage_index import SessionCoverageIndex
from btc_short_horizon.data.disk_pressure import supervise_optional_feed
from btc_short_horizon.data.okx import (
    OkxBookSynchronizer,
    normalize_okx_book_update,
    normalize_okx_trade,
)
from btc_short_horizon.data.polymarket import (
    PolymarketL2Normalizer,
    PolymarketL2Result,
    PolymarketL2Status,
)
from btc_short_horizon.data.quality import DataQualityStats, EventQualityValidator
from btc_short_horizon.data.rtds import (
    normalize_chainlink_btc_usd,
    normalize_chainlink_btc_usd_twap_60s,
)
from btc_short_horizon.data.session_inventory import (
    SESSION_INVENTORY_MANIFEST_ATTRIBUTE,
    SESSION_INVENTORY_SCHEMA_VERSION,
    CollectorSessionInventory,
    SessionInventoryRepository,
)
from btc_short_horizon.data.storage import DataPartitionManifest
from btc_short_horizon.data.subscriptions import (
    binance_combined_stream_subscription,
    binance_futures_market_stream_subscription,
    binance_futures_public_stream_subscription,
    okx_public_subscription,
    polymarket_market_subscription,
    polymarket_rtds_chainlink_btc_subscription,
    polymarket_rtds_chainlink_btc_twap_60s_subscription,
)

DEFAULT_BINANCE_STREAMS = ("btcusdt@kline_1s",)
DEFAULT_BINANCE_FUTURES_MARKET_STREAMS: tuple[str, ...] = ()
DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS: tuple[str, ...] = ()
DEFAULT_OKX_SUBSCRIPTIONS: tuple[dict[str, str], ...] = ()
_BINANCE_DEPTH_SNAPSHOT_URL = "https://data-api.binance.vision/api/v3/depth"
_BINANCE_FUTURES_DEPTH_SNAPSHOT_URL = "https://fapi.binance.com/fapi/v1/depth"
_OKX_PUBLIC_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"

_MARKET_STREAM = "market"
_PRICE_STREAM = "price"
_TWAP_60S_STREAM = "twap_60s"
_TRADE_STREAM = "trade"
_KLINE_STREAM = "kline_1s"
_DEPTH_STREAM = "depth"
_PARTIAL_BOOK_STREAM = "partial_book"
_BOOK_TICKER_STREAM = "book_ticker"
_BOOK_STREAM = "book"
_INSTRUMENT_METADATA_STREAM = "instrument_metadata"
_CONTINUITY_GAP_EVENT_TYPE = "continuity_gap"
_CONTINUITY_GAP_SCHEMA_VERSION = "btc-continuity-gap-v1"
_MAX_GAP_REASON_LENGTH = 256
_DEFAULT_MAX_PENDING_EVENTS = 100_000
_DEFAULT_MAX_PENDING_BYTES = 64 * 1024 * 1024

type _QualityStreamKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class PolymarketSubscriptionWindow:
    """Wall-clock interval during which one isolated CLOB token group is connected."""

    token_ids: tuple[str, ...]
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        token_ids = tuple(token_id.strip() for token_id in self.token_ids if token_id.strip())
        if not token_ids or len(set(token_ids)) != len(token_ids):
            raise ValueError("subscription window token_ids must be non-empty and unique")
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("subscription window start must be timezone-aware")
        if self.end.tzinfo is None or self.end.utcoffset() is None:
            raise ValueError("subscription window end must be timezone-aware")
        start = self.start.astimezone(UTC)
        end = self.end.astimezone(UTC)
        if end <= start:
            raise ValueError("subscription window end must be after start")
        object.__setattr__(self, "token_ids", token_ids)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class CollectorIngressResult:
    accepted_events: int = 0
    rejected_events: int = 0
    stale_events: int = 0
    reasons: tuple[str, ...] = ()
    resubscribe_required: bool = False

    def merged(self, other: CollectorIngressResult) -> CollectorIngressResult:
        return CollectorIngressResult(
            accepted_events=self.accepted_events + other.accepted_events,
            rejected_events=self.rejected_events + other.rejected_events,
            stale_events=self.stale_events + other.stale_events,
            reasons=(*self.reasons, *other.reasons),
            resubscribe_required=(self.resubscribe_required or other.resubscribe_required),
        )


@dataclass(frozen=True, slots=True)
class RequiredFeedHealth:
    healthy: bool
    reason: str
    feeds: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class CollectorBufferStats:
    pending_events: int
    pending_bytes: int
    inflight_events: int
    inflight_bytes: int
    total_events: int
    total_bytes: int
    high_water_events: int
    high_water_bytes: int
    flush_count: int
    flush_failures: int
    last_flush_duration_ms: float | None
    max_queue_delay_ms: float


class CollectorBufferCapacityError(RuntimeError):
    """Raised before admission when the bounded raw-event buffer is full."""


class AdmittedEventBuffer:
    """Bounded, non-blocking copy of collector-admitted events for research consumers.

    Overflow is sticky and never backpressures or interrupts the durable raw collector.
    A consumer must fail closed when ``overflowed`` becomes true.
    """

    def __init__(self, *, max_events: int = 10_000) -> None:
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        self._queue: Queue[RawCollectorEvent] = Queue(maxsize=max_events)
        self._overflowed = False
        self._dropped_events = 0

    @property
    def pending_events(self) -> int:
        return self._queue.qsize()

    @property
    def overflowed(self) -> bool:
        return self._overflowed

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

    def publish(self, event: RawCollectorEvent) -> None:
        if not isinstance(event, RawCollectorEvent):
            raise TypeError("event must be a RawCollectorEvent")
        try:
            self._queue.put_nowait(event)
        except Full:
            self._overflowed = True
            self._dropped_events += 1

    def get_nowait(self) -> RawCollectorEvent:
        try:
            return self._queue.get_nowait()
        except Empty:
            raise


class _FeedResubscribeRequired(RuntimeError):
    """Internal signal that the current subscription can no longer recover state."""


class BtcForwardCollector:
    """Collect BTC-only CLOB, Chainlink RTDS, and Binance trade evidence safely."""

    def __init__(
        self,
        *,
        raw_data_root: Path,
        polymarket_token_ids: Sequence[str],
        polymarket_token_groups: Sequence[Sequence[str]] | None = None,
        polymarket_subscription_windows: Sequence[PolymarketSubscriptionWindow] | None = None,
        flush_size: int = 10_000,
        flush_interval_seconds: float = 60.0,
        shutdown_flush_timeout_seconds: float = 30.0,
        ingest_version: str = "btc-short-horizon-v1",
        epoch_id_offset: int = 0,
        collector_session_id: str | None = None,
        max_pending_events: int = _DEFAULT_MAX_PENDING_EVENTS,
        max_pending_bytes: int = _DEFAULT_MAX_PENDING_BYTES,
        binance_depth_snapshot_url: str = _BINANCE_DEPTH_SNAPSHOT_URL,
        binance_futures_depth_snapshot_url: str = _BINANCE_FUTURES_DEPTH_SNAPSHOT_URL,
        binance_spot_depth_snapshot_limit: int = 1_000,
        binance_futures_depth_snapshot_limit: int = 1_000,
        binance_depth_snapshot_timeout_seconds: float = 10.0,
        binance_depth_snapshot_retry_initial_seconds: float = 0.5,
        binance_depth_snapshot_retry_max_seconds: float = 30.0,
        polymarket_source_timestamp_regression_tolerance_seconds: float = 1.0,
        okx_instruments_url: str = _OKX_PUBLIC_INSTRUMENTS_URL,
        okx_swap_contract_value: float | None = None,
        rule_contract_sha256: str | None = None,
    ) -> None:
        token_ids = tuple(token_id.strip() for token_id in polymarket_token_ids if token_id.strip())
        if not token_ids:
            raise ValueError("polymarket_token_ids must not be empty")
        if len(set(token_ids)) != len(token_ids):
            raise ValueError("polymarket_token_ids must be unique")
        token_groups = _normalize_polymarket_token_groups(
            token_ids=token_ids,
            token_groups=polymarket_token_groups,
        )
        subscription_windows = _normalize_polymarket_subscription_windows(
            token_groups=token_groups,
            subscription_windows=polymarket_subscription_windows,
        )
        if isinstance(flush_size, bool) or not isinstance(flush_size, int) or flush_size < 1:
            raise ValueError("flush_size must be >= 1")
        if not isfinite(flush_interval_seconds) or flush_interval_seconds <= 0.0:
            raise ValueError("flush_interval_seconds must be finite and > 0")
        if not isfinite(shutdown_flush_timeout_seconds) or shutdown_flush_timeout_seconds <= 0.0:
            raise ValueError("shutdown_flush_timeout_seconds must be finite and > 0")
        if not ingest_version or not ingest_version.strip():
            raise ValueError("ingest_version is required")
        if (
            isinstance(epoch_id_offset, bool)
            or not isinstance(epoch_id_offset, int)
            or epoch_id_offset < 0
        ):
            raise ValueError("epoch_id_offset must be a non-negative integer")
        if (
            isinstance(max_pending_events, bool)
            or not isinstance(max_pending_events, int)
            or max_pending_events < flush_size
        ):
            raise ValueError("max_pending_events must be >= flush_size")
        if (
            isinstance(max_pending_bytes, bool)
            or not isinstance(max_pending_bytes, int)
            or max_pending_bytes < 1
        ):
            raise ValueError("max_pending_bytes must be >= 1")
        if not binance_depth_snapshot_url.startswith("https://"):
            raise ValueError("binance_depth_snapshot_url must use https")
        if not binance_futures_depth_snapshot_url.startswith("https://"):
            raise ValueError("binance_futures_depth_snapshot_url must use https")
        if (
            isinstance(binance_spot_depth_snapshot_limit, bool)
            or not isinstance(binance_spot_depth_snapshot_limit, int)
            or not 1 <= binance_spot_depth_snapshot_limit <= 5_000
        ):
            raise ValueError("binance_spot_depth_snapshot_limit must be in [1, 5000]")
        if (
            isinstance(binance_futures_depth_snapshot_limit, bool)
            or not isinstance(binance_futures_depth_snapshot_limit, int)
            or not 1 <= binance_futures_depth_snapshot_limit <= 1_000
        ):
            raise ValueError("binance_futures_depth_snapshot_limit must be in [1, 1000]")
        if (
            not isfinite(binance_depth_snapshot_timeout_seconds)
            or binance_depth_snapshot_timeout_seconds <= 0.0
        ):
            raise ValueError("binance_depth_snapshot_timeout_seconds must be finite and > 0")
        if (
            not isfinite(binance_depth_snapshot_retry_initial_seconds)
            or binance_depth_snapshot_retry_initial_seconds <= 0.0
        ):
            raise ValueError("binance_depth_snapshot_retry_initial_seconds must be finite and > 0")
        if (
            not isfinite(binance_depth_snapshot_retry_max_seconds)
            or binance_depth_snapshot_retry_max_seconds
            < binance_depth_snapshot_retry_initial_seconds
        ):
            raise ValueError(
                "binance_depth_snapshot_retry_max_seconds must be finite and "
                ">= binance_depth_snapshot_retry_initial_seconds"
            )
        if (
            not isfinite(polymarket_source_timestamp_regression_tolerance_seconds)
            or polymarket_source_timestamp_regression_tolerance_seconds < 0.0
        ):
            raise ValueError(
                "polymarket_source_timestamp_regression_tolerance_seconds must be finite and >= 0"
            )
        if not okx_instruments_url.startswith("https://"):
            raise ValueError("okx_instruments_url must use https")
        if okx_swap_contract_value is not None and (
            not isfinite(okx_swap_contract_value) or okx_swap_contract_value <= 0.0
        ):
            raise ValueError("okx_swap_contract_value must be finite and > 0 when provided")
        self.token_ids = token_ids
        self.polymarket_token_groups = token_groups
        self.polymarket_subscription_windows = subscription_windows
        self.flush_size = flush_size
        self.flush_interval_seconds = flush_interval_seconds
        self.shutdown_flush_timeout_seconds = shutdown_flush_timeout_seconds
        self.ingest_version = ingest_version.strip()
        self.epoch_id_offset = epoch_id_offset
        session_id = uuid4().hex if collector_session_id is None else collector_session_id
        self.collector_session_id = normalize_collector_session_id(session_id)
        self.max_pending_events = max_pending_events
        self.max_pending_bytes = max_pending_bytes
        self.binance_depth_snapshot_url = binance_depth_snapshot_url
        self.binance_futures_depth_snapshot_url = binance_futures_depth_snapshot_url
        self.binance_spot_depth_snapshot_limit = binance_spot_depth_snapshot_limit
        self.binance_futures_depth_snapshot_limit = binance_futures_depth_snapshot_limit
        self.binance_depth_snapshot_timeout_seconds = binance_depth_snapshot_timeout_seconds
        self.binance_depth_snapshot_retry_initial_seconds = (
            binance_depth_snapshot_retry_initial_seconds
        )
        self.binance_depth_snapshot_retry_max_seconds = binance_depth_snapshot_retry_max_seconds
        self.polymarket_source_timestamp_regression_tolerance_seconds = (
            polymarket_source_timestamp_regression_tolerance_seconds
        )
        self.okx_instruments_url = okx_instruments_url
        self.okx_swap_contract_value = okx_swap_contract_value
        self.raw_data_root = raw_data_root.resolve()
        self._scheduled_polymarket_mode = bool(subscription_windows)
        self._scheduled_polymarket_windows = {item.token_ids: item for item in subscription_windows}
        self._polymarket_window_completions = {
            item.token_ids: asyncio.Event() for item in subscription_windows
        }
        self._polymarket_window_queue: asyncio.Queue[PolymarketSubscriptionWindow] = asyncio.Queue()
        for item in subscription_windows:
            self._polymarket_window_queue.put_nowait(item)
        self._ingress_lock: asyncio.Lock | None = None
        self.rule_contract_sha256 = rule_contract_sha256
        self._session_inventory, self._writer = self._start_storage_session()
        source_regression_tolerance = timedelta(
            seconds=polymarket_source_timestamp_regression_tolerance_seconds
        )
        self._polymarket_normalizers = {
            token_id: PolymarketL2Normalizer(
                token_id=token_id,
                source_timestamp_regression_tolerance=source_regression_tolerance,
            )
            for token_id in token_ids
        }
        self._binance_depth: dict[tuple[str, str], BinanceDepthSynchronizer] = {}
        self._okx_books: dict[tuple[str, str], OkxBookSynchronizer] = {}
        self._validators: dict[_QualityStreamKey, EventQualityValidator] = {}
        self._pending: list[RawCollectorEvent] = []
        self._pending_bytes = 0
        self._inflight_events = 0
        self._inflight_bytes = 0
        self._oldest_pending_monotonic_ns: int | None = None
        self._high_water_events = 0
        self._high_water_bytes = 0
        self._flush_count = 0
        self._flush_failures = 0
        self._last_flush_duration_ms: float | None = None
        self._max_queue_delay_ms = 0.0
        self._pending_quality_counts: dict[_RawPartitionKey, tuple[int, int]] = {}
        self._unresolved_gaps: dict[_QualityStreamKey, int] = {}
        self._required_feed_keys: set[_QualityStreamKey] = {
            ("polymarket_clob", token_id, _MARKET_STREAM) for token_id in token_ids
        }
        self._required_feed_keys.add(("polymarket_rtds_chainlink", "btc/usd", _PRICE_STREAM))
        self._required_feed_keys.add(
            ("polymarket_rtds_chainlink_twap_60s", "btc/usd", _TWAP_60S_STREAM)
        )
        self._last_feed_event_at: dict[_QualityStreamKey, datetime] = {}
        self._optional_feeds_enabled: asyncio.Event | None = None
        self._next_admission_sequence = 0
        self._lock = RLock()
        self._flush_lock = Lock()
        self._admitted_event_buffer: AdmittedEventBuffer | None = None

    def _storage_session_attributes(self) -> dict[str, str]:
        return {
            "epoch_id_offset": str(self.epoch_id_offset),
            "polymarket_token_ids": json.dumps(self.token_ids, separators=(",", ":")),
            "polymarket_token_groups": json.dumps(
                self.polymarket_token_groups,
                separators=(",", ":"),
            ),
            "polymarket_subscription_windows": json.dumps(
                [
                    {
                        "token_ids": item.token_ids,
                        "start": item.start.isoformat(),
                        "end": item.end.isoformat(),
                    }
                    for item in self.polymarket_subscription_windows
                ],
                separators=(",", ":"),
            ),
            **(
                {}
                if self.rule_contract_sha256 is None
                else {"rule_contract_sha256": self.rule_contract_sha256}
            ),
        }

    def _start_storage_session(
        self,
    ) -> tuple[CollectorSessionInventory, PartitionedRawEventWriter]:
        inventory = SessionInventoryRepository(self.raw_data_root).start_session(
            session_id=self.collector_session_id,
            ingest_version=self.ingest_version,
            attributes=self._storage_session_attributes(),
        )
        writer = PartitionedRawEventWriter(
            self.raw_data_root,
            manifest_attributes={
                "polymarket_source_timestamp_regression_tolerance_seconds": format(
                    self.polymarket_source_timestamp_regression_tolerance_seconds,
                    ".17g",
                ),
                SESSION_INVENTORY_MANIFEST_ATTRIBUTE: SESSION_INVENTORY_SCHEMA_VERSION,
                **(
                    {}
                    if self.rule_contract_sha256 is None
                    else {"rule_contract_sha256": self.rule_contract_sha256}
                ),
            },
            inventory=inventory,
        )
        return inventory, writer

    def register_polymarket_subscription_window(
        self,
        window: PolymarketSubscriptionWindow,
    ) -> bool:
        """Schedule one new bounded CLOB pair without reconnecting shared BTC feeds."""

        if not isinstance(window, PolymarketSubscriptionWindow):
            raise TypeError("window must be a PolymarketSubscriptionWindow")
        if not self._scheduled_polymarket_mode:
            raise RuntimeError("dynamic windows require a scheduled Polymarket collector")
        with self._lock:
            existing = self._scheduled_polymarket_windows.get(window.token_ids)
            if existing is not None:
                if existing != window:
                    raise ValueError("Polymarket token group already has a different window")
                return False
            overlap = set(window.token_ids).intersection(self._polymarket_normalizers)
            if overlap:
                raise ValueError("Polymarket subscription windows must not reuse token IDs")
            self.token_ids = (*self.token_ids, *window.token_ids)
            self.polymarket_token_groups = (*self.polymarket_token_groups, window.token_ids)
            self.polymarket_subscription_windows = (
                *self.polymarket_subscription_windows,
                window,
            )
            self._scheduled_polymarket_windows[window.token_ids] = window
            self._polymarket_window_completions[window.token_ids] = asyncio.Event()
            self._polymarket_normalizers.update(
                {
                    token_id: PolymarketL2Normalizer(
                        token_id=token_id,
                        source_timestamp_regression_tolerance=timedelta(
                            seconds=self.polymarket_source_timestamp_regression_tolerance_seconds
                        ),
                    )
                    for token_id in window.token_ids
                }
            )
            self._required_feed_keys.update(
                ("polymarket_clob", token_id, _MARKET_STREAM) for token_id in window.token_ids
            )
            self._polymarket_window_queue.put_nowait(window)
        self._session_inventory.update_attributes(self._storage_session_attributes())
        return True

    async def wait_polymarket_subscription_window(
        self,
        window: PolymarketSubscriptionWindow,
        *,
        stop_event: asyncio.Event,
        timeout_seconds: float,
    ) -> bool:
        """Wait until one CLOB socket is closed, or return false on service stop."""

        if not isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be finite and > 0")
        with self._lock:
            completion = self._polymarket_window_completions.get(window.token_ids)
        if completion is None:
            raise ValueError("Polymarket subscription window is not registered")
        completion_task = asyncio.create_task(completion.wait())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {completion_task, stop_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                return False
            if completion_task not in done:
                raise RuntimeError("Polymarket subscription did not close before session rotation")
            await completion_task
            with self._lock:
                if self._polymarket_window_completions.get(window.token_ids) is completion:
                    self._polymarket_window_completions.pop(window.token_ids, None)
            return True
        finally:
            for task in (completion_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(completion_task, stop_task, return_exceptions=True)

    async def rotate_storage_session(self, *, epoch_id_offset: int) -> str:
        """Rotate durable inventory at a market boundary without closing public sockets."""

        if (
            isinstance(epoch_id_offset, bool)
            or not isinstance(epoch_id_offset, int)
            or epoch_id_offset < 0
        ):
            raise ValueError("epoch_id_offset must be a non-negative integer")
        ingress_lock = self._ingress_lock
        if ingress_lock is None:
            raise RuntimeError("collector must be running before its storage session can rotate")
        async with ingress_lock:
            await self._flush_async()
            closed_session_id = self.collector_session_id
            self._session_inventory.complete()
            repository = SessionInventoryRepository(self.raw_data_root)
            coverage = SessionCoverageIndex(self.raw_data_root)
            coverage.append(
                repository.read_session(closed_session_id),
                inventory_path=repository.inventory_path(closed_session_id),
            )
            self.epoch_id_offset = epoch_id_offset
            self.collector_session_id = normalize_collector_session_id(uuid4().hex)
            self._next_admission_sequence = 0
            self._session_inventory, self._writer = self._start_storage_session()
            return closed_session_id

    def _retire_polymarket_subscription_window(
        self,
        window: PolymarketSubscriptionWindow,
    ) -> None:
        with self._lock:
            if self._scheduled_polymarket_windows.get(window.token_ids) != window:
                return
            self._scheduled_polymarket_windows.pop(window.token_ids, None)
            retired = set(window.token_ids)
            self.token_ids = tuple(token for token in self.token_ids if token not in retired)
            self.polymarket_token_groups = tuple(
                group for group in self.polymarket_token_groups if group != window.token_ids
            )
            self.polymarket_subscription_windows = tuple(
                item for item in self.polymarket_subscription_windows if item != window
            )
            for token_id in window.token_ids:
                self._polymarket_normalizers.pop(token_id, None)
                key = ("polymarket_clob", token_id, _MARKET_STREAM)
                self._required_feed_keys.discard(key)
                self._validators.pop(key, None)
                self._unresolved_gaps.pop(key, None)
                self._last_feed_event_at.pop(key, None)
            completion = self._polymarket_window_completions.get(window.token_ids)
            if completion is not None:
                completion.set()

    def subscribe_admitted_events(self, buffer: AdmittedEventBuffer) -> None:
        """Attach one non-blocking research subscriber before collection starts."""

        if not isinstance(buffer, AdmittedEventBuffer):
            raise TypeError("buffer must be an AdmittedEventBuffer")
        with self._lock:
            if self._admitted_event_buffer is not None:
                raise RuntimeError("an admitted-event subscriber is already attached")
            if self._next_admission_sequence != 0:
                raise RuntimeError("subscribe before the collector admits its first event")
            self._admitted_event_buffer = buffer

    @property
    def pending_event_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return self._pending_bytes

    @property
    def flush_required(self) -> bool:
        stats = self.buffer_stats
        return stats.pending_events >= self.flush_size or stats.pending_bytes >= max(
            1, self.max_pending_bytes // 2
        )

    @property
    def buffer_at_capacity(self) -> bool:
        stats = self.buffer_stats
        return (
            stats.total_events >= self.max_pending_events
            or stats.total_bytes >= self.max_pending_bytes
        )

    @property
    def buffer_stats(self) -> CollectorBufferStats:
        with self._lock:
            return CollectorBufferStats(
                pending_events=len(self._pending),
                pending_bytes=self._pending_bytes,
                inflight_events=self._inflight_events,
                inflight_bytes=self._inflight_bytes,
                total_events=len(self._pending) + self._inflight_events,
                total_bytes=self._pending_bytes + self._inflight_bytes,
                high_water_events=self._high_water_events,
                high_water_bytes=self._high_water_bytes,
                flush_count=self._flush_count,
                flush_failures=self._flush_failures,
                last_flush_duration_ms=self._last_flush_duration_ms,
                max_queue_delay_ms=self._max_queue_delay_ms,
            )

    @property
    def quality_stats(self) -> dict[_QualityStreamKey, DataQualityStats]:
        return {key: validator.stats for key, validator in self._validators.items()}

    def configure_required_feeds(
        self,
        *,
        binance_streams: Sequence[str] = DEFAULT_BINANCE_STREAMS,
        binance_futures_market_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_MARKET_STREAMS,
        binance_futures_public_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS,
        okx_subscriptions: Sequence[Mapping[str, str]] = DEFAULT_OKX_SUBSCRIPTIONS,
    ) -> None:
        """Register enabled feeds so silence is visible before their first accepted event."""

        keys = {
            *_binance_feed_keys("binance_spot", binance_streams),
            *_binance_feed_keys("binance_perp", binance_futures_market_streams),
            *_binance_feed_keys("binance_perp", binance_futures_public_streams),
        }
        for subscription in okx_subscriptions:
            instrument = subscription.get("instId")
            stream_id = _okx_stream_id(subscription.get("channel"))
            source = _okx_source_for_instrument(str(instrument))
            if source is not None and isinstance(instrument, str) and stream_id is not None:
                keys.add((source, instrument, stream_id))
        with self._lock:
            self._required_feed_keys.update(keys)

    def configure_required_polymarket_tokens(self, token_ids: Sequence[str]) -> None:
        """Set required CLOB tokens without crashing during a handoff race."""
        required = {token_id.strip() for token_id in token_ids if token_id.strip()}
        with self._lock:
            self._required_feed_keys = {
                key for key in self._required_feed_keys if key[0] != "polymarket_clob"
            }
            self._required_feed_keys.update(
                ("polymarket_clob", token_id, _MARKET_STREAM) for token_id in required
            )

    async def record_optional_feed_boundary(self, source: str, *, reason: str) -> None:
        """Persist a continuity boundary and discard stale L2 state before reconnect."""
        if source not in {"binance_perp", "okx_spot", "okx_swap"}:
            raise ValueError("optional feed boundary source is unsupported")
        ingress_lock = self._ingress_lock
        if ingress_lock is None:
            return
        async with ingress_lock:
            now = datetime.now(UTC)
            with self._lock:
                keys = tuple(key for key in self._required_feed_keys if key[0] == source)
                for key in keys:
                    _, instrument, stream_id = key
                    validator = self._validators.get(key) or EventQualityValidator()
                    event = self._new_gap_event(
                        source=source,
                        instrument=instrument,
                        stream_id=stream_id,
                        reason=reason,
                        observed_at=now,
                        previous_available_ts=self._last_feed_event_at.get(key),
                    )
                    self._ensure_capacity_locked((event,))
                    self._commit_gap_event_locked(
                        event=event,
                        validator=validator,
                        stream_id=stream_id,
                        reason=reason,
                    )
                if source == "binance_perp":
                    self._binance_depth = {
                        key: value for key, value in self._binance_depth.items() if key[0] != source
                    }
                else:
                    self._okx_books = {
                        key: value for key, value in self._okx_books.items() if key[0] != source
                    }

    def feed_health(self, *, now: datetime, stale_after_seconds: float) -> RequiredFeedHealth:
        if not isfinite(stale_after_seconds) or stale_after_seconds <= 0.0:
            raise ValueError("stale_after_seconds must be finite and > 0")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_time = now.astimezone(UTC)
        with self._lock:
            required = tuple(sorted(self._required_feed_keys))
            last_events = dict(self._last_feed_event_at)
            unresolved_gaps = dict(self._unresolved_gaps)
        feeds: list[dict[str, object]] = []
        reasons: list[str] = []
        for key in required:
            source, instrument, stream_id = key
            if (
                self._optional_feeds_enabled is not None
                and not self._optional_feeds_enabled.is_set()
                and source in {"binance_perp", "okx_spot", "okx_swap"}
            ):
                feeds.append(
                    {
                        "key": f"{source}:{instrument}:{stream_id}",
                        "source": source,
                        "instrument": instrument,
                        "stream_id": stream_id,
                        "state": "degraded",
                        "reason": "disk_pressure_optional_feed_disabled",
                    }
                )
                continue
            last_event = last_events.get(key)
            age_seconds = (
                None if last_event is None else (current_time - last_event).total_seconds()
            )
            validator = self._validators.get(key)
            gap_count = 0 if validator is None else validator.stats.gap_events
            if source == "polymarket_clob" and instrument not in self._polymarket_normalizers:
                state, reason = "gap", "required_feed_token_not_registered"
            elif last_event is None:
                state, reason = "silent", "required_feed_silent"
            elif unresolved_gaps.get(key, 0):
                state, reason = "gap", "required_feed_gap"
            elif (
                source in {"binance_spot", "binance_perp"}
                and stream_id == _DEPTH_STREAM
                and (
                    (source, instrument) not in self._binance_depth
                    or not self._binance_depth[(source, instrument)].synchronized
                )
            ):
                state, reason = "gap", "required_feed_snapshot_missing"
            elif (
                source in {"okx_spot", "okx_swap"}
                and stream_id == _BOOK_STREAM
                and (
                    (source, instrument) not in self._okx_books
                    or not self._okx_books[(source, instrument)].synchronized
                )
            ):
                state, reason = "gap", "required_feed_snapshot_missing"
            elif source == "polymarket_clob" and not self._polymarket_normalizers[instrument].has_snapshot:
                state, reason = "gap", "required_feed_snapshot_missing"
            elif age_seconds is not None and age_seconds < -5.0:
                state, reason = "future", "required_feed_timestamp_in_future"
            elif age_seconds is not None and age_seconds > stale_after_seconds:
                state, reason = "stale", "required_feed_stale"
            else:
                state, reason = "ok", "ok"
            if reason != "ok":
                reasons.append(reason)
            feeds.append(
                {
                    "key": f"{source}:{instrument}:{stream_id}",
                    "source": source,
                    "instrument": instrument,
                    "stream_id": stream_id,
                    "state": state,
                    "last_event_at": None if last_event is None else last_event.isoformat(),
                    "age_seconds": age_seconds,
                    "gap_count": gap_count,
                }
            )
        return RequiredFeedHealth(
            healthy=not reasons,
            reason=reasons[0] if reasons else "ok",
            feeds=tuple(feeds),
        )

    def handle_polymarket(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime
    ) -> CollectorIngressResult:
        """Normalize each configured token without persisting ignored channel messages."""

        return self._handle_polymarket_tokens(
            payload,
            collector_receive_ts=collector_receive_ts,
            token_ids=self.token_ids,
        )

    def _handle_polymarket_tokens(
        self,
        payload: Mapping[str, object],
        *,
        collector_receive_ts: datetime,
        token_ids: tuple[str, ...],
    ) -> CollectorIngressResult:
        """Apply one payload only to tokens sharing its physical CLOB connection."""

        with self._lock:
            candidates = {
                token_id: self._polymarket_normalizers[token_id].fork() for token_id in token_ids
            }
            outcome = CollectorIngressResult()
            processed: list[tuple[str, PolymarketL2Result]] = []
            for token_id, normalizer in candidates.items():
                try:
                    result = normalizer.apply(
                        payload,
                        collector_receive_ts=collector_receive_ts,
                    )
                except ValueError as exc:
                    outcome = outcome.merged(_rejected(str(exc)))
                    continue
                if result.status is not PolymarketL2Status.IGNORED and result.timing is not None:
                    processed.append((token_id, result))
            if outcome.rejected_events:
                planned_gaps = tuple(
                    (
                        token_id,
                        self._validator(
                            source="polymarket_clob",
                            instrument=token_id,
                            stream_id=_MARKET_STREAM,
                        ),
                        self._new_gap_event(
                            source="polymarket_clob",
                            instrument=token_id,
                            stream_id=_MARKET_STREAM,
                            reason="invalid_market_payload",
                            observed_at=collector_receive_ts,
                            previous_available_ts=self._validator(
                                source="polymarket_clob",
                                instrument=token_id,
                                stream_id=_MARKET_STREAM,
                            ).last_available_ts,
                        ),
                    )
                    for token_id in token_ids
                )
                self._ensure_capacity_locked(tuple(event for _, _, event in planned_gaps))
                for token_id in token_ids:
                    self._polymarket_normalizers[token_id].reset()
                for _token_id, validator, gap_event in planned_gaps:
                    self._commit_gap_event_locked(
                        event=gap_event,
                        validator=validator,
                        stream_id=_MARKET_STREAM,
                        reason="invalid_market_payload",
                    )
                return outcome.merged(CollectorIngressResult(resubscribe_required=True))

            reservation: list[RawCollectorEvent] = []
            planned_start_gaps: dict[str, tuple[EventQualityValidator, RawCollectorEvent, str]] = {}
            for token_id, result in processed:
                assert result.timing is not None
                timing = replace(result.timing, ingest_version=self.ingest_version)
                source_event = _pending_event_candidate(
                    timing=timing,
                    event_type=result.event_type,
                    payload=payload,
                    collector_session_id=self.collector_session_id,
                )
                validator = self._validator(
                    source=timing.source,
                    instrument=timing.instrument,
                    stream_id=_MARKET_STREAM,
                )
                if result.starts_new_epoch:
                    reason = result.reason or result.status.value
                    gap_event = self._new_gap_event(
                        source=timing.source,
                        instrument=token_id,
                        stream_id=_MARKET_STREAM,
                        reason=reason,
                        observed_at=collector_receive_ts,
                        previous_available_ts=validator.last_available_ts,
                    )
                    planned_start_gaps[token_id] = (validator, gap_event, reason)
                    reservation.extend((gap_event, source_event))
                    continue
                decision = validator.prepare(timing).decision
                if decision.accepted:
                    reservation.append(source_event)
                elif decision.reason == "out_of_order":
                    reservation.append(
                        self._new_gap_event(
                            source=timing.source,
                            instrument=token_id,
                            stream_id=_MARKET_STREAM,
                            reason="available_time_regression",
                            observed_at=collector_receive_ts,
                            previous_available_ts=validator.last_available_ts,
                        )
                    )
            self._ensure_capacity_locked(tuple(reservation))
            self._polymarket_normalizers.update(candidates)
            for token_id, result in processed:
                assert result.timing is not None
                planned_gap = planned_start_gaps.get(token_id)
                if planned_gap is not None:
                    validator, gap_event, reason = planned_gap
                    self._commit_gap_event_locked(
                        event=gap_event,
                        validator=validator,
                        stream_id=_MARKET_STREAM,
                        reason=reason,
                    )
                ingested = self._ingest(
                    timing=result.timing,
                    event_type=result.event_type,
                    payload=payload,
                    stream_id=_MARKET_STREAM,
                )
                outcome = outcome.merged(ingested)
                if result.reason is not None:
                    outcome = outcome.merged(
                        CollectorIngressResult(
                            reasons=(result.reason,),
                            resubscribe_required=result.requires_resubscribe,
                        )
                    )
                elif result.requires_resubscribe:
                    outcome = outcome.merged(
                        CollectorIngressResult(
                            reasons=(result.status.value,),
                            resubscribe_required=True,
                        )
                    )
            return outcome

    def handle_chainlink_rtds(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime
    ) -> CollectorIngressResult:
        try:
            record = normalize_chainlink_btc_usd(payload, collector_receive_ts=collector_receive_ts)
        except ValueError as exc:
            return _rejected(str(exc))
        return self._ingest(
            timing=record.timing,
            event_type="crypto_prices_chainlink",
            payload=payload,
            stream_id=_PRICE_STREAM,
        )

    def handle_chainlink_twap_60s_rtds(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime
    ) -> CollectorIngressResult:
        try:
            record = normalize_chainlink_btc_usd_twap_60s(
                payload,
                collector_receive_ts=collector_receive_ts,
            )
        except ValueError as exc:
            return _rejected(str(exc))
        return self._ingest(
            timing=record.timing,
            event_type="crypto_prices_twap_sixty",
            payload=payload,
            stream_id=_TWAP_60S_STREAM,
        )

    def invalidate_polymarket_token(self, token_id: str) -> None:
        try:
            normalizer = self._polymarket_normalizers[token_id]
        except KeyError as exc:
            raise KeyError(f"unknown Polymarket token ID: {token_id}") from exc
        normalizer.reset()

    def handle_binance(
        self,
        payload: Mapping[str, object],
        *,
        collector_receive_ts: datetime,
        source: str = "binance_spot",
    ) -> CollectorIngressResult:
        """Persist public Binance trades, diff-depth, and receive-only Book Ticker evidence."""

        data = payload.get("data")
        message = data if isinstance(data, Mapping) else payload
        event_type = str(message.get("e") or "").casefold()
        stream = str(payload.get("stream") or "").casefold()
        if event_type in {"trade", "aggtrade"}:
            return self._handle_binance_trade(
                message,
                payload=payload,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        if event_type == "kline":
            return self._handle_binance_kline(
                message,
                payload=payload,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        if _is_binance_partial_depth_stream(stream):
            instrument = stream.partition("@")[0].upper()
            return self._handle_binance_partial_depth_snapshot(
                message,
                payload=payload,
                instrument=instrument,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        if event_type == "depthupdate" or "@depth" in stream:
            return self._handle_binance_depth_update(
                message,
                payload=payload,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        if stream.endswith("@bookticker"):
            return self._handle_binance_book_ticker(
                message,
                payload=payload,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        return _rejected("unsupported_binance_event")

    def _handle_binance_partial_depth_snapshot(
        self,
        message: Mapping[str, object],
        *,
        payload: Mapping[str, object],
        instrument: str,
        collector_receive_ts: datetime,
        source: str,
    ) -> CollectorIngressResult:
        """Persist Binance top-N depth as an independent receive-timed snapshot."""

        snapshot = dict(message)
        if source == "binance_perp" and "lastUpdateId" not in snapshot:
            snapshot["lastUpdateId"] = snapshot.get("u")
        try:
            timing = normalize_binance_depth_snapshot(
                snapshot,
                instrument=instrument,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        except ValueError as exc:
            return _rejected(str(exc))
        return self._ingest(
            timing=timing,
            event_type="partial_depth_snapshot",
            payload=payload,
            stream_id=_PARTIAL_BOOK_STREAM,
        )

    def handle_binance_depth_snapshot(
        self,
        *,
        instrument: str,
        payload: Mapping[str, object],
        collector_receive_ts: datetime,
        source: str = "binance_spot",
    ) -> CollectorIngressResult:
        """Persist one REST snapshot and advance the buffered diff-depth synchronizer."""

        try:
            timing = normalize_binance_depth_snapshot(
                payload,
                instrument=instrument,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
            synchronizer = self._depth_synchronizer(
                source=source,
                instrument=instrument,
            )
        except ValueError as exc:
            if instrument and source in {"binance_spot", "binance_perp"}:
                self._invalidate_binance_depth_after_error(
                    source=source,
                    instrument=instrument,
                    reason="invalid_depth_snapshot",
                    observed_at=collector_receive_ts,
                )
            return _rejected(str(exc))
        with self._lock:
            source_event = _pending_event_candidate(
                timing=replace(timing, ingest_version=self.ingest_version),
                event_type="depth_snapshot",
                payload=payload,
                collector_session_id=self.collector_session_id,
            )
            gap_capacity = self._gap_capacity_event_locked(
                source=timing.source,
                instrument=timing.instrument,
                stream_id=_DEPTH_STREAM,
                observed_at=collector_receive_ts,
            )
            if not self._quality_would_accept(timing=timing, stream_id=_DEPTH_STREAM):
                return self._ingest(
                    timing=timing,
                    event_type="depth_snapshot",
                    payload=payload,
                    stream_id=_DEPTH_STREAM,
                )
            try:
                self._ensure_capacity_locked((source_event, gap_capacity))
            except CollectorBufferCapacityError as capacity_error:
                self._ensure_capacity_locked((source_event,))
                candidate = deepcopy(synchronizer)
                try:
                    candidate_result = candidate.apply_snapshot(
                        payload,
                        collector_receive_ts=collector_receive_ts,
                    )
                except ValueError:
                    raise capacity_error
                if candidate_result.status is DepthUpdateStatus.GAP:
                    raise capacity_error
                self._binance_depth[(source, instrument)] = candidate
                outcome = self._ingest(
                    timing=timing,
                    event_type="depth_snapshot",
                    payload=payload,
                    stream_id=_DEPTH_STREAM,
                )
                return _with_depth_status(
                    outcome,
                    candidate_result.status,
                    candidate_result.reason,
                    resubscribe_on_unavailable=False,
                )
            try:
                result = synchronizer.apply_snapshot(
                    payload,
                    collector_receive_ts=collector_receive_ts,
                )
            except ValueError as exc:
                self._invalidate_binance_depth_after_error(
                    source=source,
                    instrument=instrument,
                    reason="invalid_depth_snapshot",
                    observed_at=collector_receive_ts,
                )
                return self._ingest(
                    timing=timing,
                    event_type="depth_snapshot",
                    payload=payload,
                    stream_id=_DEPTH_STREAM,
                ).merged(_rejected(str(exc)))
            if result.status is DepthUpdateStatus.GAP:
                self.mark_gap(
                    source=timing.source,
                    instrument=timing.instrument,
                    stream_id=_DEPTH_STREAM,
                    reason=result.reason or "depth_snapshot_gap",
                    observed_at=collector_receive_ts,
                )
            outcome = self._ingest(
                timing=timing,
                event_type="depth_snapshot",
                payload=payload,
                stream_id=_DEPTH_STREAM,
            )
            return _with_depth_status(
                outcome,
                result.status,
                result.reason,
                resubscribe_on_unavailable=False,
            )

    async def refresh_binance_depth_snapshot(
        self,
        *,
        instrument: str,
        client: httpx.AsyncClient | None = None,
        source: str = "binance_spot",
        snapshot_handler: (
            Callable[
                [Mapping[str, object], datetime],
                Awaitable[CollectorIngressResult],
            ]
            | None
        ) = None,
    ) -> CollectorIngressResult:
        """Fetch a public REST snapshot and preserve its local receive timestamp explicitly."""

        if not instrument:
            raise ValueError("instrument is required")
        owns_client = client is None
        active_client = client or httpx.AsyncClient(
            timeout=self.binance_depth_snapshot_timeout_seconds,
        )
        try:
            response = await active_client.get(
                self._binance_depth_snapshot_url(source),
                params={
                    "symbol": instrument,
                    "limit": self._binance_depth_snapshot_limit(source),
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("Binance depth snapshot must be a JSON object")
            received_at = datetime.now(UTC)
            if snapshot_handler is not None:
                return await snapshot_handler(payload, received_at)
            return self.handle_binance_depth_snapshot(
                instrument=instrument,
                payload=payload,
                collector_receive_ts=received_at,
                source=source,
            )
        finally:
            if owns_client:
                await active_client.aclose()

    def binance_depth_needs_snapshot(
        self, instrument: str, *, source: str = "binance_spot"
    ) -> bool:
        return self._depth_synchronizer(source=source, instrument=instrument).needs_snapshot

    def binance_depth_requires_snapshot(
        self, instrument: str, *, source: str = "binance_spot"
    ) -> bool:
        return self._depth_synchronizer(
            source=source,
            instrument=instrument,
        ).requires_snapshot

    def binance_depth_is_synchronized(
        self, instrument: str, *, source: str = "binance_spot"
    ) -> bool:
        return self._depth_synchronizer(
            source=source,
            instrument=instrument,
        ).synchronized

    def invalidate_binance_depth(self, instrument: str, *, source: str = "binance_spot") -> None:
        self._depth_synchronizer(source=source, instrument=instrument).invalidate()

    def _invalidate_binance_depth_after_error(
        self,
        *,
        source: str,
        instrument: str,
        reason: str,
        observed_at: datetime,
    ) -> None:
        with self._lock:
            validator = self._validator(
                source=source,
                instrument=instrument,
                stream_id=_DEPTH_STREAM,
            )
            gap_event = self._new_gap_event(
                source=source,
                instrument=instrument,
                stream_id=_DEPTH_STREAM,
                reason=reason,
                observed_at=observed_at,
                previous_available_ts=validator.last_available_ts,
            )
            self._ensure_capacity_locked((gap_event,))
            self.invalidate_binance_depth(instrument, source=source)
            self._commit_gap_event_locked(
                event=gap_event,
                validator=validator,
                stream_id=_DEPTH_STREAM,
                reason=reason,
            )

    def _handle_binance_trade(
        self,
        message: Mapping[str, object],
        *,
        payload: Mapping[str, object],
        collector_receive_ts: datetime,
        source: str,
    ) -> CollectorIngressResult:
        try:
            record = normalize_binance_trade(
                message,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        except ValueError as exc:
            return _rejected(str(exc))
        return self._ingest(
            timing=record.timing,
            event_type=str(message.get("e")).casefold(),
            payload=payload,
            stream_id=_TRADE_STREAM,
        )

    def _handle_binance_kline(
        self,
        message: Mapping[str, object],
        *,
        payload: Mapping[str, object],
        collector_receive_ts: datetime,
        source: str,
    ) -> CollectorIngressResult:
        kline = message.get("k")
        if isinstance(kline, Mapping) and kline.get("x") is False:
            return CollectorIngressResult()
        try:
            timing = normalize_binance_kline(
                message,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        except ValueError as exc:
            return _rejected(str(exc))
        return self._ingest(
            timing=timing,
            event_type="kline_1s",
            payload=payload,
            stream_id=_KLINE_STREAM,
        )

    def _handle_binance_depth_update(
        self,
        message: Mapping[str, object],
        *,
        payload: Mapping[str, object],
        collector_receive_ts: datetime,
        source: str,
    ) -> CollectorIngressResult:
        try:
            timing = normalize_binance_depth_update(
                message,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
            synchronizer = self._depth_synchronizer(
                source=source,
                instrument=timing.instrument,
            )
        except ValueError as exc:
            instrument = message.get("s")
            if isinstance(instrument, str) and instrument:
                self._invalidate_binance_depth_after_error(
                    source=source,
                    instrument=instrument,
                    reason="invalid_depth_update",
                    observed_at=collector_receive_ts,
                )
            return _rejected(str(exc)).merged(CollectorIngressResult(resubscribe_required=True))
        with self._lock:
            source_event = _pending_event_candidate(
                timing=replace(timing, ingest_version=self.ingest_version),
                event_type="depth_update",
                payload=payload,
                collector_session_id=self.collector_session_id,
            )
            gap_capacity = self._gap_capacity_event_locked(
                source=timing.source,
                instrument=timing.instrument,
                stream_id=_DEPTH_STREAM,
                observed_at=collector_receive_ts,
            )
            if not self._quality_would_accept(timing=timing, stream_id=_DEPTH_STREAM):
                return self._ingest(
                    timing=timing,
                    event_type="depth_update",
                    payload=payload,
                    stream_id=_DEPTH_STREAM,
                )
            try:
                self._ensure_capacity_locked((source_event, gap_capacity))
            except CollectorBufferCapacityError as capacity_error:
                self._ensure_capacity_locked((source_event,))
                candidate = deepcopy(synchronizer)
                try:
                    candidate_result = candidate.observe_delta(
                        message,
                        collector_receive_ts=collector_receive_ts,
                    )
                except ValueError:
                    raise capacity_error
                if candidate_result.status is DepthUpdateStatus.GAP:
                    raise capacity_error
                self._binance_depth[(source, timing.instrument)] = candidate
                outcome = self._ingest(
                    timing=timing,
                    event_type="depth_update",
                    payload=payload,
                    stream_id=_DEPTH_STREAM,
                )
                return _with_depth_status(
                    outcome,
                    candidate_result.status,
                    candidate_result.reason,
                    resubscribe_on_unavailable=False,
                )
            try:
                result = synchronizer.observe_delta(
                    message,
                    collector_receive_ts=collector_receive_ts,
                )
            except ValueError as exc:
                self._invalidate_binance_depth_after_error(
                    source=source,
                    instrument=timing.instrument,
                    reason="invalid_depth_update",
                    observed_at=collector_receive_ts,
                )
                return self._ingest(
                    timing=timing,
                    event_type="depth_update",
                    payload=payload,
                    stream_id=_DEPTH_STREAM,
                ).merged(
                    _rejected(str(exc)).merged(CollectorIngressResult(resubscribe_required=True))
                )
            if result.status is DepthUpdateStatus.GAP:
                self.mark_gap(
                    source=timing.source,
                    instrument=timing.instrument,
                    stream_id=_DEPTH_STREAM,
                    reason=result.reason or "depth_update_gap",
                    observed_at=collector_receive_ts,
                )
            outcome = self._ingest(
                timing=timing,
                event_type="depth_update",
                payload=payload,
                stream_id=_DEPTH_STREAM,
            )
            return _with_depth_status(
                outcome,
                result.status,
                result.reason,
                resubscribe_on_unavailable=False,
            )

    def _handle_binance_book_ticker(
        self,
        message: Mapping[str, object],
        *,
        payload: Mapping[str, object],
        collector_receive_ts: datetime,
        source: str,
    ) -> CollectorIngressResult:
        try:
            timing = normalize_binance_book_ticker(
                message,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        except ValueError as exc:
            return _rejected(str(exc))
        return self._ingest(
            timing=timing,
            event_type="book_ticker",
            payload=payload,
            stream_id=_BOOK_TICKER_STREAM,
        )

    def handle_okx(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime
    ) -> CollectorIngressResult:
        """Persist only validated OKX BTC public `trades` and `books` messages."""

        control_event = payload.get("event")
        if control_event is not None:
            if isinstance(control_event, str) and control_event.casefold() in {
                "subscribe",
                "unsubscribe",
            }:
                return CollectorIngressResult()
            event_name = (
                control_event.casefold()
                if isinstance(control_event, str) and control_event
                else "invalid"
            )
            return _rejected(f"okx_control_{event_name}").merged(
                CollectorIngressResult(resubscribe_required=True)
            )
        argument = payload.get("arg")
        data = payload.get("data")
        if (
            not isinstance(argument, Mapping)
            or not isinstance(data, Sequence)
            or isinstance(data, (str, bytes))
        ):
            return _rejected("unsupported_okx_event").merged(
                CollectorIngressResult(resubscribe_required=True)
            )
        channel = argument.get("channel")
        instrument = argument.get("instId")
        if not isinstance(channel, str) or not isinstance(instrument, str):
            return _rejected("unsupported_okx_event").merged(
                CollectorIngressResult(resubscribe_required=True)
            )
        source = _okx_source_for_instrument(instrument)
        if source is None:
            return _rejected("unsupported_okx_instrument").merged(
                CollectorIngressResult(resubscribe_required=True)
            )
        if channel == "trades":
            return self._handle_okx_trades(
                data=data,
                payload=payload,
                instrument=instrument,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        if channel in {"books", "books5"}:
            snapshot_only = channel == "books5"
            action = "snapshot" if snapshot_only else payload.get("action")
            if not isinstance(action, str):
                self._invalidate_okx_book_after_error(
                    source=source,
                    instrument=instrument,
                    reason="okx_book_action_missing",
                    observed_at=collector_receive_ts,
                )
                return _rejected("okx_book_action_missing").merged(
                    CollectorIngressResult(resubscribe_required=True)
                )
            return self._handle_okx_books(
                data=data,
                payload=payload,
                action=action,
                instrument=instrument,
                collector_receive_ts=collector_receive_ts,
                source=source,
                snapshot_only=snapshot_only,
            )
        return _rejected("unsupported_okx_channel")

    async def refresh_okx_swap_contract_value(
        self, *, client: httpx.AsyncClient | None = None
    ) -> float:
        """Read the live contract value instead of hard-coding a swap-size conversion."""

        owns_client = client is None
        active_client = client or httpx.AsyncClient(
            timeout=self.binance_depth_snapshot_timeout_seconds,
        )
        try:
            response = await active_client.get(
                self.okx_instruments_url,
                params={"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("OKX instruments response must be an object")
            data = payload.get("data")
            if not isinstance(data, Sequence) or isinstance(data, (str, bytes)) or len(data) != 1:
                raise ValueError("OKX BTC-USDT-SWAP instrument must resolve to exactly one record")
            record = data[0]
            if not isinstance(record, Mapping):
                raise ValueError("OKX instrument record must be an object")
            if record.get("instId") != "BTC-USDT-SWAP" or record.get("ctValCcy") != "BTC":
                raise ValueError("OKX swap contract must be BTC-denominated BTC-USDT-SWAP")
            contract_value = _positive_number(record.get("ctVal"), "OKX ctVal")
            self.okx_swap_contract_value = contract_value
            received = datetime.now(UTC)
            self._ingest(
                timing=TimedMarketEvent(
                    source_ts=received,
                    collector_receive_ts=received,
                    available_ts=received,
                    sequence_or_hash=f"BTC-USDT-SWAP:ctVal={contract_value}",
                    source="okx_instrument",
                    instrument="BTC-USDT-SWAP",
                    schema_version="okx-instrument-v1",
                    ingest_version="btc-short-horizon-v1",
                ),
                event_type="instrument_metadata",
                payload=payload,
                stream_id=_INSTRUMENT_METADATA_STREAM,
            )
            return contract_value
        finally:
            if owns_client:
                await active_client.aclose()

    def _handle_okx_trades(
        self,
        *,
        data: Sequence[object],
        payload: Mapping[str, object],
        instrument: str,
        collector_receive_ts: datetime,
        source: str,
    ) -> CollectorIngressResult:
        multiplier = 1.0 if source == "okx_spot" else self.okx_swap_contract_value
        if multiplier is None:
            return _rejected("okx_swap_contract_value_unavailable")
        outcome = CollectorIngressResult()
        normalized: list[tuple[TimedMarketEvent, Mapping[str, object]]] = []
        for item in data:
            if not isinstance(item, Mapping):
                outcome = outcome.merged(_rejected("OKX trade data must contain objects"))
                continue
            try:
                record = normalize_okx_trade(
                    item,
                    collector_receive_ts=collector_receive_ts,
                    source=source,
                    quantity_multiplier=multiplier,
                )
            except ValueError as exc:
                outcome = outcome.merged(_rejected(str(exc)))
                continue
            if record.timing.instrument != instrument:
                outcome = outcome.merged(_rejected("OKX item instId does not match arg.instId"))
                continue
            normalized.append((record.timing, _okx_item_payload(payload, item)))
        if outcome.rejected_events:
            return outcome
        with self._lock:
            self._ensure_capacity_locked(
                tuple(
                    _pending_event_candidate(
                        timing=replace(timing, ingest_version=self.ingest_version),
                        event_type="trade",
                        payload=item_payload,
                        collector_session_id=self.collector_session_id,
                    )
                    for timing, item_payload in normalized
                )
            )
            for timing, item_payload in normalized:
                outcome = outcome.merged(
                    self._ingest(
                        timing=timing,
                        event_type="trade",
                        payload=item_payload,
                        stream_id=_TRADE_STREAM,
                    )
                )
            return outcome

    def _handle_okx_books(
        self,
        *,
        data: Sequence[object],
        payload: Mapping[str, object],
        action: str,
        instrument: str,
        collector_receive_ts: datetime,
        source: str,
        snapshot_only: bool = False,
    ) -> CollectorIngressResult:
        outcome = CollectorIngressResult()
        normalized: list[tuple[TimedMarketEvent, Mapping[str, object], Mapping[str, object]]] = []
        for item in data:
            if not isinstance(item, Mapping):
                outcome = outcome.merged(_rejected("OKX book data must contain objects"))
                continue
            normalized_item = dict(item)
            if snapshot_only:
                normalized_item["prevSeqId"] = -1
            try:
                timing = normalize_okx_book_update(
                    normalized_item,
                    action=action,
                    instrument=instrument,
                    collector_receive_ts=collector_receive_ts,
                    source=source,
                )
            except ValueError as exc:
                outcome = outcome.merged(_rejected(str(exc)))
                continue
            normalized.append(
                (
                    timing,
                    _okx_item_payload(payload, item),
                    normalized_item,
                )
            )
        if outcome.rejected_events:
            self._invalidate_okx_book_after_error(
                source=source,
                instrument=instrument,
                reason="invalid_okx_book_payload",
                observed_at=collector_receive_ts,
            )
            return outcome.merged(CollectorIngressResult(resubscribe_required=True))
        with self._lock:
            source_events = tuple(
                _pending_event_candidate(
                    timing=replace(timing, ingest_version=self.ingest_version),
                    event_type=f"books_{action}",
                    payload=item_payload,
                    collector_session_id=self.collector_session_id,
                )
                for timing, item_payload, _item in normalized
            )
            gap_capacity_events = tuple(
                self._gap_capacity_event_locked(
                    source=source,
                    instrument=instrument,
                    stream_id=_BOOK_STREAM,
                    observed_at=collector_receive_ts,
                )
                for _ in normalized
            )
            self._ensure_capacity_locked((*source_events, *gap_capacity_events))
            synchronizer = self._okx_book_synchronizer(
                source=source,
                instrument=instrument,
            )
            for timing, item_payload, item in normalized:
                if not self._quality_would_accept(timing=timing, stream_id=_BOOK_STREAM):
                    outcome = outcome.merged(
                        self._ingest(
                            timing=timing,
                            event_type=f"books_{action}",
                            payload=item_payload,
                            stream_id=_BOOK_STREAM,
                        )
                    )
                    continue
                try:
                    result = synchronizer.apply(
                        action=action,
                        payload=item,
                        collector_receive_ts=collector_receive_ts,
                    )
                except ValueError as exc:
                    self._invalidate_okx_book_after_error(
                        source=source,
                        instrument=instrument,
                        reason="invalid_okx_book_payload",
                        observed_at=collector_receive_ts,
                    )
                    outcome = outcome.merged(
                        self._ingest(
                            timing=timing,
                            event_type=f"books_{action}",
                            payload=item_payload,
                            stream_id=_BOOK_STREAM,
                        )
                    )
                    return outcome.merged(
                        _rejected(str(exc)).merged(
                            CollectorIngressResult(resubscribe_required=True)
                        )
                    )
                if result.status is DepthUpdateStatus.GAP:
                    self.mark_gap(
                        source=source,
                        instrument=instrument,
                        stream_id=_BOOK_STREAM,
                        reason=result.reason or "okx_book_gap",
                        observed_at=collector_receive_ts,
                    )
                outcome = outcome.merged(
                    _with_depth_status(
                        self._ingest(
                            timing=timing,
                            event_type=f"books_{action}",
                            payload=item_payload,
                            stream_id=_BOOK_STREAM,
                        ),
                        result.status,
                        result.reason,
                    )
                )
            return outcome

    def _invalidate_okx_book_after_error(
        self,
        *,
        source: str,
        instrument: str,
        reason: str,
        observed_at: datetime,
    ) -> None:
        with self._lock:
            validator = self._validator(
                source=source,
                instrument=instrument,
                stream_id=_BOOK_STREAM,
            )
            gap_event = self._new_gap_event(
                source=source,
                instrument=instrument,
                stream_id=_BOOK_STREAM,
                reason=reason,
                observed_at=observed_at,
                previous_available_ts=validator.last_available_ts,
            )
            self._ensure_capacity_locked((gap_event,))
            self._okx_book_synchronizer(
                source=source,
                instrument=instrument,
            ).reset()
            self._commit_gap_event_locked(
                event=gap_event,
                validator=validator,
                stream_id=_BOOK_STREAM,
                reason=reason,
            )

    def mark_gap(
        self,
        *,
        source: str,
        instrument: str,
        stream_id: str,
        reason: str,
        observed_at: datetime | None = None,
    ) -> None:
        """Persist an explicit causal boundary before any post-gap source event."""

        self.mark_gaps(
            ((source, instrument, stream_id, reason, observed_at),),
        )

    def mark_gaps(
        self,
        gaps: Sequence[tuple[str, str, str, str, datetime | None]],
    ) -> None:
        """Persist a multi-stream continuity boundary as one capacity transaction."""

        if not gaps:
            raise ValueError("gaps must not be empty")
        with self._lock:
            planned: list[tuple[EventQualityValidator, RawCollectorEvent, str, str]] = []
            for source, instrument, stream_id, reason, observed_at in gaps:
                source, instrument, stream_id, reason = _normalize_gap_identity(
                    source=source,
                    instrument=instrument,
                    stream_id=stream_id,
                    reason=reason,
                )
                validator = self._validator(
                    source=source,
                    instrument=instrument,
                    stream_id=stream_id,
                )
                gap_event = self._new_gap_event(
                    source=source,
                    instrument=instrument,
                    stream_id=stream_id,
                    reason=reason,
                    observed_at=observed_at,
                    previous_available_ts=validator.last_available_ts,
                )
                planned.append((validator, gap_event, stream_id, reason))
            self._ensure_capacity_locked(tuple(event for _, event, _, _ in planned))
            for validator, gap_event, stream_id, reason in planned:
                self._commit_gap_event_locked(
                    event=gap_event,
                    validator=validator,
                    stream_id=stream_id,
                    reason=reason,
                )

    def _gap_capacity_event_locked(
        self,
        *,
        source: str,
        instrument: str,
        stream_id: str,
        observed_at: datetime | None,
    ) -> RawCollectorEvent:
        validator = self._validator(
            source=source,
            instrument=instrument,
            stream_id=stream_id,
        )
        return self._new_gap_event(
            source=source,
            instrument=instrument,
            stream_id=stream_id,
            reason="x" * _MAX_GAP_REASON_LENGTH,
            observed_at=observed_at,
            previous_available_ts=validator.last_available_ts,
        )

    def flush(self) -> tuple[DataPartitionManifest, ...]:
        """Atomically persist the currently buffered events without dropping concurrent ingress."""

        with self._flush_lock:
            with self._lock:
                events = tuple(self._pending)
                pending_bytes = self._pending_bytes
                oldest_pending_monotonic_ns = self._oldest_pending_monotonic_ns
                event_keys = {
                    _partition_key(
                        event.timing,
                        collector_session_id=event.collector_session_id,
                    )
                    for event in events
                }
                quality_counts = {
                    key: self._pending_quality_counts.pop(key)
                    for key in event_keys
                    if key in self._pending_quality_counts
                }
                self._pending.clear()
                self._pending_bytes = 0
                self._inflight_events = len(events)
                self._inflight_bytes = pending_bytes
                self._oldest_pending_monotonic_ns = None
            if not events:
                return ()
            flush_started_ns = monotonic_ns()
            try:
                manifests = self._writer.write(events, quality_counts=quality_counts)
            except Exception:
                with self._lock:
                    self._pending[0:0] = events
                    self._pending_bytes += pending_bytes
                    self._inflight_events = 0
                    self._inflight_bytes = 0
                    self._high_water_events = max(
                        self._high_water_events,
                        len(self._pending),
                    )
                    self._high_water_bytes = max(
                        self._high_water_bytes,
                        self._pending_bytes,
                    )
                    if self._oldest_pending_monotonic_ns is None or (
                        oldest_pending_monotonic_ns is not None
                        and oldest_pending_monotonic_ns < self._oldest_pending_monotonic_ns
                    ):
                        self._oldest_pending_monotonic_ns = oldest_pending_monotonic_ns
                    self._flush_failures += 1
                    for key, counts in quality_counts.items():
                        _add_quality_counts(
                            self._pending_quality_counts,
                            key,
                            duplicate_count=counts[0],
                            gap_count=counts[1],
                        )
                raise
            flush_finished_ns = monotonic_ns()
            queue_delay_ms = (
                0.0
                if oldest_pending_monotonic_ns is None
                else (flush_started_ns - oldest_pending_monotonic_ns) / 1_000_000
            )
            with self._lock:
                self._inflight_events = 0
                self._inflight_bytes = 0
                self._flush_count += 1
                self._last_flush_duration_ms = (flush_finished_ns - flush_started_ns) / 1_000_000
                self._max_queue_delay_ms = max(self._max_queue_delay_ms, queue_delay_ms)
            return manifests

    async def collect_forever(
        self,
        *,
        stop_event: asyncio.Event,
        binance_streams: Sequence[str] = DEFAULT_BINANCE_STREAMS,
        binance_futures_market_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_MARKET_STREAMS,
        binance_futures_public_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS,
        okx_subscriptions: Sequence[Mapping[str, str]] = DEFAULT_OKX_SUBSCRIPTIONS,
        optional_feeds_enabled: asyncio.Event | None = None,
    ) -> None:
        """Run BTC public subscriptions until stopped, flushing outside socket callbacks."""

        if stop_event.is_set():
            try:
                loop = asyncio.get_running_loop()
                flush_task = asyncio.create_task(
                    self._flush_async(),
                    name="btc-raw-final-flush",
                )
                await self._await_flush_task(
                    flush_task,
                    deadline=loop.time() + self.shutdown_flush_timeout_seconds,
                )
                self._session_inventory.complete()
            except BaseException as error:
                self._record_session_failure(error)
                raise
            return
        self.configure_required_feeds(
            binance_streams=binance_streams,
            binance_futures_market_streams=binance_futures_market_streams,
            binance_futures_public_streams=binance_futures_public_streams,
            okx_subscriptions=okx_subscriptions,
        )
        spot_stream_names = tuple(binance_streams)
        futures_market_stream_names = tuple(binance_futures_market_streams)
        futures_public_stream_names = tuple(binance_futures_public_streams)
        spot_instruments = _binance_instruments(spot_stream_names)
        futures_market_instruments = _binance_instruments(futures_market_stream_names)
        futures_public_instruments = _binance_instruments(futures_public_stream_names)
        spot_depth_instruments = _binance_depth_instruments(spot_stream_names)
        futures_depth_instruments = _binance_depth_instruments(futures_public_stream_names)
        spot_stream_ids = _binance_stream_ids(spot_stream_names)
        futures_market_stream_ids = _binance_stream_ids(futures_market_stream_names)
        futures_public_stream_ids = _binance_stream_ids(futures_public_stream_names)
        normalized_okx_subscriptions = tuple(dict(item) for item in okx_subscriptions)
        optional_enabled = optional_feeds_enabled or asyncio.Event()
        if optional_feeds_enabled is None:
            optional_enabled.set()
        self._optional_feeds_enabled = optional_enabled
        resync_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        resync_failures: asyncio.Queue[Exception] = asyncio.Queue()
        okx_metadata_task: asyncio.Task[None] | None = None
        flush_requested = asyncio.Event()
        capacity_available = asyncio.Event()
        capacity_available.set()
        ingress_lock = asyncio.Lock()
        if self._ingress_lock is not None:
            raise RuntimeError("collector is already running")
        self._ingress_lock = ingress_lock

        async def admit_with_backpressure(operation: Callable[[], Any]) -> Any:
            while True:
                await self._coordinate_buffer(
                    flush_requested=flush_requested,
                    capacity_available=capacity_available,
                )
                try:
                    result = operation()
                except CollectorBufferCapacityError:
                    if self.buffer_stats.total_events == 0:
                        raise
                    capacity_available.clear()
                    flush_requested.set()
                    await capacity_available.wait()
                    continue
                await self._coordinate_buffer(
                    flush_requested=flush_requested,
                    capacity_available=capacity_available,
                )
                return result

        async with httpx.AsyncClient(
            timeout=self.binance_depth_snapshot_timeout_seconds
        ) as snapshot_client:

            async def refresh_depth_safely(source: str, instrument: str) -> None:
                attempt = 0
                fetch_gap_recorded = False

                async def admit_snapshot(
                    payload: Mapping[str, object],
                    received_at: datetime,
                ) -> CollectorIngressResult:
                    async with ingress_lock:
                        return await admit_with_backpressure(
                            lambda: self.handle_binance_depth_snapshot(
                                instrument=instrument,
                                payload=payload,
                                collector_receive_ts=received_at,
                                source=source,
                            )
                        )

                while not stop_event.is_set():
                    try:
                        result = await self.refresh_binance_depth_snapshot(
                            instrument=instrument,
                            client=snapshot_client,
                            source=source,
                            snapshot_handler=admit_snapshot,
                        )
                    except (httpx.HTTPError, ValueError) as exc:
                        if not fetch_gap_recorded:
                            gap_reason = f"depth_snapshot_{type(exc).__name__}"
                            async with ingress_lock:
                                await admit_with_backpressure(
                                    lambda: self.mark_gap(
                                        source=source,
                                        instrument=instrument,
                                        stream_id=_DEPTH_STREAM,
                                        reason=gap_reason,
                                    )
                                )
                            fetch_gap_recorded = True
                    else:
                        if result.rejected_events and not fetch_gap_recorded:
                            async with ingress_lock:
                                await admit_with_backpressure(
                                    lambda: self.mark_gap(
                                        source=source,
                                        instrument=instrument,
                                        stream_id=_DEPTH_STREAM,
                                        reason="depth_snapshot_rejected",
                                    )
                                )
                            fetch_gap_recorded = True
                        if not self.binance_depth_requires_snapshot(
                            instrument,
                            source=source,
                        ):
                            return
                    delay = min(
                        self.binance_depth_snapshot_retry_max_seconds,
                        self.binance_depth_snapshot_retry_initial_seconds * (2 ** min(attempt, 63)),
                    )
                    attempt += 1
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    except TimeoutError:
                        continue
                    return

            def schedule_depth_refresh(source: str, instrument: str) -> None:
                key = (source, instrument)
                current = resync_tasks.get(key)
                if current is None or current.done():
                    task = asyncio.create_task(refresh_depth_safely(source, instrument))
                    resync_tasks[key] = task

                    def observe_result(completed: asyncio.Task[None]) -> None:
                        if resync_tasks.get(key) is completed:
                            resync_tasks.pop(key, None)
                        if completed.cancelled():
                            return
                        error = completed.exception()
                        if error is not None:
                            resync_failures.put_nowait(error)

                    task.add_done_callback(observe_result)

            def on_binance_connected(
                source: str,
                instruments: tuple[str, ...],
            ):
                def schedule_initial_snapshots() -> None:
                    for instrument in instruments:
                        schedule_depth_refresh(source, instrument)

                return schedule_initial_snapshots

            async def supervise_depth_refreshes() -> None:
                failure_task = asyncio.create_task(resync_failures.get())
                stop_task = asyncio.create_task(stop_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {failure_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if failure_task in done:
                        raise failure_task.result()
                finally:
                    for task in (failure_task, stop_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(failure_task, stop_task, return_exceptions=True)

            def on_polymarket(token_group: tuple[str, ...]):
                async def receive(payload: Mapping[str, object], received: datetime) -> bool:
                    async with ingress_lock:
                        result = await admit_with_backpressure(
                            lambda: self._handle_polymarket_tokens(
                                payload,
                                collector_receive_ts=received,
                                token_ids=token_group,
                            )
                        )
                    if result.resubscribe_required:
                        raise _FeedResubscribeRequired(
                            "Polymarket book requires a fresh subscription"
                        )
                    return result.accepted_events > 0

                return receive

            async def on_rtds(payload: Mapping[str, object], received: datetime) -> bool:
                async with ingress_lock:
                    result = await admit_with_backpressure(
                        lambda: self.handle_chainlink_rtds(
                            payload,
                            collector_receive_ts=received,
                        )
                    )
                return result.accepted_events > 0

            async def on_twap_rtds(payload: Mapping[str, object], received: datetime) -> bool:
                async with ingress_lock:
                    result = await admit_with_backpressure(
                        lambda: self.handle_chainlink_twap_60s_rtds(
                            payload,
                            collector_receive_ts=received,
                        )
                    )
                return result.accepted_events > 0

            def on_binance(source: str):
                async def receive(payload: Mapping[str, object], received: datetime) -> bool:
                    async with ingress_lock:
                        result = await admit_with_backpressure(
                            lambda: self.handle_binance(
                                payload,
                                collector_receive_ts=received,
                                source=source,
                            )
                        )
                        message = payload.get("data")
                        event = message if isinstance(message, Mapping) else payload
                        instrument = event.get("s")
                        if (
                            str(event.get("e") or "").casefold() == "depthupdate"
                            and isinstance(instrument, str)
                            and instrument
                            and self.binance_depth_requires_snapshot(
                                instrument,
                                source=source,
                            )
                        ):
                            schedule_depth_refresh(source, instrument)
                    if result.resubscribe_required:
                        raise _FeedResubscribeRequired(
                            "Binance depth payload requires a fresh connection"
                        )
                    return result.accepted_events > 0

                return receive

            async def on_okx(payload: Mapping[str, object], received: datetime) -> bool:
                if okx_metadata_task is not None:
                    await asyncio.shield(okx_metadata_task)
                async with ingress_lock:
                    result = await admit_with_backpressure(
                        lambda: self.handle_okx(
                            payload,
                            collector_receive_ts=received,
                        )
                    )
                if result.resubscribe_required:
                    raise _FeedResubscribeRequired("OKX book requires a fresh subscription")
                return result.accepted_events > 0

            def on_polymarket_error(token_group: tuple[str, ...]):
                async def report(exc: Exception) -> None:
                    if isinstance(exc, _FeedResubscribeRequired):
                        return
                    async with ingress_lock:

                        def record_error() -> None:
                            self.mark_gaps(
                                tuple(
                                    (
                                        "polymarket_clob",
                                        token_id,
                                        _MARKET_STREAM,
                                        type(exc).__name__,
                                        None,
                                    )
                                    for token_id in token_group
                                )
                            )
                            for token_id in token_group:
                                self.invalidate_polymarket_token(token_id)

                        await admit_with_backpressure(record_error)

                return report

            async def on_rtds_error(exc: Exception) -> None:
                async with ingress_lock:
                    await admit_with_backpressure(
                        lambda: self.mark_gap(
                            source="polymarket_rtds_chainlink",
                            instrument="btc/usd",
                            stream_id=_PRICE_STREAM,
                            reason=type(exc).__name__,
                        )
                    )

            async def on_twap_rtds_error(exc: Exception) -> None:
                async with ingress_lock:
                    await admit_with_backpressure(
                        lambda: self.mark_gap(
                            source="polymarket_rtds_chainlink_twap_60s",
                            instrument="btc/usd",
                            stream_id=_TWAP_60S_STREAM,
                            reason=type(exc).__name__,
                        )
                    )

            def on_binance_error(
                source: str,
                instruments: tuple[str, ...],
                depth_instruments: tuple[str, ...],
                stream_ids: tuple[str, ...],
            ):
                async def report(exc: Exception) -> None:
                    async with ingress_lock:

                        def record_error() -> None:
                            gaps = tuple(
                                (
                                    source,
                                    instrument,
                                    stream_id,
                                    type(exc).__name__,
                                    None,
                                )
                                for instrument in instruments
                                for stream_id in stream_ids
                            )
                            if gaps:
                                self.mark_gaps(gaps)
                            for instrument in depth_instruments:
                                self.invalidate_binance_depth(instrument, source=source)

                        await admit_with_backpressure(record_error)

                return report

            async def on_okx_error(exc: Exception) -> None:
                async with ingress_lock:
                    gaps: list[tuple[str, str, str, str, datetime | None]] = []
                    book_streams: list[tuple[str, str]] = []
                    for subscription in normalized_okx_subscriptions:
                        instrument = subscription.get("instId")
                        if not isinstance(instrument, str):
                            continue
                        source = _okx_source_for_instrument(instrument)
                        if source is None:
                            continue
                        stream_id = _okx_stream_id(subscription.get("channel"))
                        if stream_id is None:
                            continue
                        if stream_id == _BOOK_STREAM:
                            book_streams.append((source, instrument))
                        gaps.append((source, instrument, stream_id, type(exc).__name__, None))

                    def record_error() -> None:
                        if gaps:
                            self.mark_gaps(tuple(gaps))
                        for source, instrument in book_streams:
                            self._okx_book_synchronizer(
                                source=source,
                                instrument=instrument,
                            ).reset()

                    await admit_with_backpressure(record_error)

            async def initialize_okx_metadata() -> None:
                try:
                    await self.refresh_okx_swap_contract_value(client=snapshot_client)
                except (httpx.HTTPError, ValueError) as exc:
                    error_name = type(exc).__name__
                    async with ingress_lock:
                        await admit_with_backpressure(
                            lambda: self.mark_gap(
                                source="okx_swap",
                                instrument="BTC-USDT-SWAP",
                                stream_id=_TRADE_STREAM,
                                reason=f"contract_metadata_{error_name}",
                            )
                        )

            async def supervise_polymarket_windows() -> None:
                active: dict[asyncio.Task[None], PolymarketSubscriptionWindow] = {}
                receive_task: asyncio.Task[PolymarketSubscriptionWindow] | None = None
                stop_task = asyncio.create_task(stop_event.wait())
                try:
                    while not stop_event.is_set():
                        if receive_task is None:
                            receive_task = asyncio.create_task(
                                self._polymarket_window_queue.get(),
                                name="polymarket-window-registration",
                            )
                        done, _ = await asyncio.wait(
                            {stop_task, receive_task, *active},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_task in done:
                            return
                        if receive_task in done:
                            window = receive_task.result()
                            receive_task = None
                            worker = asyncio.create_task(
                                _collect_polymarket_during_window(
                                    token_group=window.token_ids,
                                    window=window,
                                    stop_event=stop_event,
                                    on_payload=on_polymarket(window.token_ids),
                                    on_error=on_polymarket_error(window.token_ids),
                                ),
                                name=f"polymarket-window-{'-'.join(window.token_ids)}",
                            )
                            active[worker] = window
                        for worker in tuple(active):
                            if worker not in done:
                                continue
                            window = active.pop(worker)
                            await worker
                            self._retire_polymarket_subscription_window(window)
                finally:
                    tasks = [stop_task, *active]
                    if receive_task is not None:
                        tasks.append(receive_task)
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

            if self.okx_swap_contract_value is None and any(
                item.get("instId") == "BTC-USDT-SWAP" for item in normalized_okx_subscriptions
            ):
                okx_metadata_task = asyncio.create_task(
                    initialize_okx_metadata(),
                    name="btc-okx-contract-metadata",
                )

            collectors = [
                JsonWebSocketCollector(
                    polymarket_rtds_chainlink_btc_subscription()
                ).collect_forever(
                    stop_event=stop_event,
                    on_payload=on_rtds,
                    on_error=on_rtds_error,
                ),
                JsonWebSocketCollector(
                    polymarket_rtds_chainlink_btc_twap_60s_subscription()
                ).collect_forever(
                    stop_event=stop_event,
                    on_payload=on_twap_rtds,
                    on_error=on_twap_rtds_error,
                ),
                supervise_depth_refreshes(),
            ]
            if self._scheduled_polymarket_mode:
                collectors.append(supervise_polymarket_windows())
            else:
                collectors.extend(
                    JsonWebSocketCollector(
                        polymarket_market_subscription(token_group)
                    ).collect_forever(
                        stop_event=stop_event,
                        on_payload=on_polymarket(token_group),
                        on_error=on_polymarket_error(token_group),
                    )
                    for token_group in self.polymarket_token_groups
                )
            if spot_stream_names:
                spot_collector = JsonWebSocketCollector(
                    binance_combined_stream_subscription(spot_stream_names)
                )
                spot_error = on_binance_error(
                    "binance_spot",
                    spot_instruments,
                    spot_depth_instruments,
                    spot_stream_ids,
                )
                if spot_depth_instruments:
                    collectors.append(
                        spot_collector.collect_forever(
                            stop_event=stop_event,
                            on_payload=on_binance("binance_spot"),
                            on_error=spot_error,
                            on_connected=on_binance_connected(
                                "binance_spot",
                                spot_depth_instruments,
                            ),
                        )
                    )
                else:
                    collectors.append(
                        spot_collector.collect_forever(
                            stop_event=stop_event,
                            on_payload=on_binance("binance_spot"),
                            on_error=spot_error,
                        )
                    )
            if futures_market_stream_names:

                async def run_optional_futures(local_stop: asyncio.Event) -> None:
                    await JsonWebSocketCollector(
                        binance_futures_market_stream_subscription(futures_market_stream_names)
                    ).collect_forever(
                        stop_event=local_stop,
                        on_payload=on_binance("binance_perp"),
                        on_error=on_binance_error(
                            "binance_perp",
                            futures_market_instruments,
                            (),
                            futures_market_stream_ids,
                        ),
                    )

                collectors.append(
                    supervise_optional_feed(
                        stop_event=stop_event,
                        enabled_event=optional_enabled,
                        run_once=run_optional_futures,
                    )
                )
            if futures_public_stream_names:
                futures_public_error = on_binance_error(
                    "binance_perp",
                    futures_public_instruments,
                    futures_depth_instruments,
                    futures_public_stream_ids,
                )

                async def run_optional_futures_public(local_stop: asyncio.Event) -> None:
                    collector = JsonWebSocketCollector(
                        binance_futures_public_stream_subscription(futures_public_stream_names)
                    )
                    if futures_depth_instruments:
                        await collector.collect_forever(
                            stop_event=local_stop,
                            on_payload=on_binance("binance_perp"),
                            on_error=futures_public_error,
                            on_connected=on_binance_connected(
                                "binance_perp",
                                futures_depth_instruments,
                            ),
                        )
                    else:
                        await collector.collect_forever(
                            stop_event=local_stop,
                            on_payload=on_binance("binance_perp"),
                            on_error=futures_public_error,
                        )

                collectors.append(
                    supervise_optional_feed(
                        stop_event=stop_event,
                        enabled_event=optional_enabled,
                        run_once=run_optional_futures_public,
                        on_disabled=lambda: self.record_optional_feed_boundary(
                            "binance_perp", reason="disk_pressure_optional_feed_disabled"
                        ),
                        on_recovered=lambda: self.record_optional_feed_boundary(
                            "binance_perp", reason="disk_pressure_optional_feed_recovered"
                        ),
                    )
                )
            if normalized_okx_subscriptions:

                async def run_optional_okx(local_stop: asyncio.Event) -> None:
                    await JsonWebSocketCollector(
                        okx_public_subscription(tuple(normalized_okx_subscriptions))
                    ).collect_forever(
                        stop_event=local_stop, on_payload=on_okx, on_error=on_okx_error
                    )

                collectors.append(
                    supervise_optional_feed(
                        stop_event=stop_event,
                        enabled_event=optional_enabled,
                        run_once=run_optional_okx,
                        on_disabled=lambda: _record_okx_boundaries(
                            self, "disk_pressure_optional_feed_disabled"
                        ),
                        on_recovered=lambda: _record_okx_boundaries(
                            self, "disk_pressure_optional_feed_recovered"
                        ),
                    )
                )
            feed_tasks = tuple(
                asyncio.create_task(
                    coroutine,
                    name=f"btc-public-feed-{index}",
                )
                for index, coroutine in enumerate(collectors)
            )
            flush_worker = asyncio.create_task(
                self._run_flush_worker(
                    stop_event=stop_event,
                    flush_requested=flush_requested,
                    capacity_available=capacity_available,
                ),
                name="btc-raw-flush-worker",
            )
            primary_exception: BaseException | None = None
            try:
                try:
                    done, _ = await asyncio.wait(
                        {*feed_tasks, flush_worker},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        await task
                    if not stop_event.is_set():
                        if flush_worker in done:
                            raise RuntimeError("raw flush worker exited unexpectedly")
                        raise RuntimeError("public feed collector exited unexpectedly")
                except BaseException as exc:
                    primary_exception = exc
                    raise
            finally:
                self._ingress_lock = None
                shutdown_deadline = (
                    asyncio.get_running_loop().time() + self.shutdown_flush_timeout_seconds
                )
                stop_event.set()
                producer_errors: tuple[BaseException, ...] = ()
                try:
                    producer_errors = await self._quiesce_tasks(
                        (
                            *feed_tasks,
                            *resync_tasks.values(),
                            *((okx_metadata_task,) if okx_metadata_task is not None else ()),
                        ),
                        deadline=shutdown_deadline,
                    )
                    flush_requested.set()
                    await self._await_flush_task(
                        flush_worker,
                        deadline=shutdown_deadline,
                    )
                    if self.buffer_stats.total_events:
                        final_flush = asyncio.create_task(
                            self._flush_async(),
                            name="btc-raw-final-flush",
                        )
                        await self._await_flush_task(
                            final_flush,
                            deadline=shutdown_deadline,
                        )
                    remaining = self.buffer_stats
                    if remaining.total_events or remaining.total_bytes:
                        raise RuntimeError(
                            "raw flush completed without draining the collector buffer"
                        )
                    additional_producer_errors = tuple(
                        error for error in producer_errors if error is not primary_exception
                    )
                    if additional_producer_errors:
                        _raise_first_with_notes(
                            additional_producer_errors,
                            note_prefix="additional producer cleanup failure",
                        )
                    if primary_exception is None:
                        self._session_inventory.complete()
                    else:
                        self._record_session_failure(primary_exception)
                except BaseException as cleanup_error:
                    self._record_session_failure(cleanup_error)
                    additional_producer_errors = tuple(
                        error
                        for error in producer_errors
                        if error is not primary_exception and error is not cleanup_error
                    )
                    for error in additional_producer_errors:
                        cleanup_error.add_note(f"producer cleanup also failed: {error!r}")
                    if not flush_worker.done():
                        flush_worker.cancel()
                        flush_worker.add_done_callback(_consume_background_task_exception)
                    if primary_exception is None:
                        raise
                    for error in additional_producer_errors:
                        primary_exception.add_note(f"producer cleanup also failed: {error!r}")
                    primary_exception.add_note(f"collector shutdown also failed: {cleanup_error!r}")

    def _record_session_failure(self, error: BaseException) -> None:
        try:
            self._session_inventory.fail(f"{type(error).__name__}: {error}")
        except BaseException as inventory_error:
            error.add_note(f"session inventory failure could not be recorded: {inventory_error!r}")

    async def _await_flush_task(
        self,
        task: asyncio.Task[Any],
        *,
        deadline: float,
    ) -> Any:
        if task.done():
            return await task
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds <= 0.0:
            _cancel_background_task(task)
            raise RuntimeError("raw flush did not finish before shutdown_flush_timeout_seconds")
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=remaining_seconds,
            )
        except asyncio.CancelledError:
            _cancel_background_task(task)
            raise
        except TimeoutError as exc:
            if task.done():
                return await task
            _cancel_background_task(task)
            raise RuntimeError(
                "raw flush did not finish before shutdown_flush_timeout_seconds"
            ) from exc

    async def _flush_async(self) -> tuple[DataPartitionManifest, ...]:
        """Run one blocking write on a daemon thread so shutdown cannot hang Python."""

        loop = asyncio.get_running_loop()
        completion: asyncio.Future[tuple[DataPartitionManifest, ...]] = loop.create_future()

        def publish_result(result: tuple[DataPartitionManifest, ...]) -> None:
            if not completion.done():
                completion.set_result(result)

        def publish_error(error: BaseException) -> None:
            if not completion.done():
                completion.set_exception(error)

        def run_flush() -> None:
            try:
                result = self.flush()
            except BaseException as exc:
                try:
                    loop.call_soon_threadsafe(publish_error, exc)
                except RuntimeError:
                    pass
            else:
                try:
                    loop.call_soon_threadsafe(publish_result, result)
                except RuntimeError:
                    pass

        Thread(
            target=run_flush,
            name=f"btc-raw-flush-{self.collector_session_id[:8]}",
            daemon=True,
        ).start()
        return await completion

    async def _quiesce_tasks(
        self,
        tasks: Sequence[asyncio.Task[Any]],
        *,
        deadline: float,
    ) -> tuple[BaseException, ...]:
        """Cancel producers and wait only within the collector-wide shutdown budget."""

        all_tasks = tuple(dict.fromkeys(tasks))
        for task in all_tasks:
            if not task.done():
                task.cancel()
        if not all_tasks:
            return ()
        remaining_seconds = deadline - asyncio.get_running_loop().time()
        if remaining_seconds > 0.0:
            done, pending = await asyncio.wait(
                all_tasks,
                timeout=remaining_seconds,
            )
        else:
            done = {task for task in all_tasks if task.done()}
            pending = set(all_tasks) - done
        errors: list[BaseException] = []
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                errors.append(error)
        if pending:
            for task in pending:
                task.cancel()
                task.add_done_callback(_consume_background_task_exception)
            task_names = ", ".join(sorted(task.get_name() for task in pending))
            raise RuntimeError(
                f"collector tasks did not stop before shutdown_flush_timeout_seconds: {task_names}"
            )
        return tuple(errors)

    async def _coordinate_buffer(
        self,
        *,
        flush_requested: asyncio.Event,
        capacity_available: asyncio.Event,
    ) -> None:
        if self.flush_required:
            flush_requested.set()
        if not self.buffer_at_capacity:
            return
        capacity_available.clear()
        flush_requested.set()
        await capacity_available.wait()

    async def _run_flush_worker(
        self,
        *,
        stop_event: asyncio.Event,
        flush_requested: asyncio.Event,
        capacity_available: asyncio.Event,
    ) -> None:
        stop_task = asyncio.create_task(stop_event.wait())
        flush_signal_task: asyncio.Task[bool] | None = None
        try:
            while True:
                flush_signal_task = asyncio.create_task(flush_requested.wait())
                done, _ = await asyncio.wait(
                    {flush_signal_task, stop_task},
                    timeout=self.flush_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if flush_signal_task in done:
                    flush_requested.clear()
                else:
                    flush_signal_task.cancel()
                    await asyncio.gather(flush_signal_task, return_exceptions=True)
                flush_signal_task = None
                if self.pending_event_count:
                    await self._flush_async()
                if not self.buffer_at_capacity:
                    capacity_available.set()
                if stop_task in done and not self.pending_event_count:
                    return
        finally:
            waiters = tuple(task for task in (flush_signal_task, stop_task) if task is not None)
            for task in waiters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def _ingest(
        self,
        *,
        timing: TimedMarketEvent,
        event_type: str,
        payload: Mapping[str, object],
        stream_id: str,
    ) -> CollectorIngressResult:
        if not stream_id:
            raise ValueError("stream_id is required")
        timing = replace(timing, ingest_version=self.ingest_version)
        validator_key = (timing.source, timing.instrument, stream_id)
        with self._lock:
            validator = self._validator(
                source=timing.source,
                instrument=timing.instrument,
                stream_id=stream_id,
            )
            prepared = validator.prepare(timing)
            decision = prepared.decision
            if not decision.accepted:
                gap_event: RawCollectorEvent | None = None
                if decision.reason == "out_of_order":
                    gap_event = self._new_gap_event(
                        source=timing.source,
                        instrument=timing.instrument,
                        stream_id=stream_id,
                        reason="available_time_regression",
                        observed_at=timing.collector_receive_ts or timing.available_ts,
                        previous_available_ts=validator.last_available_ts,
                    )
                    self._ensure_capacity_locked((gap_event,))
                validator.commit(prepared)
                if decision.reason == "duplicate":
                    _add_quality_counts(
                        self._pending_quality_counts,
                        _partition_key(
                            timing,
                            collector_session_id=self.collector_session_id,
                        ),
                        duplicate_count=1,
                    )
                elif decision.reason == "out_of_order":
                    self._invalidate_state_after_quality_gap(timing, stream_id=stream_id)
                    assert gap_event is not None
                    self._append_gap_event_locked(
                        event=gap_event,
                        validator=validator,
                        stream_id=stream_id,
                    )
                return _rejected(decision.reason).merged(
                    CollectorIngressResult(
                        resubscribe_required=_quality_gap_requires_resubscribe(
                            source=timing.source,
                            stream_id=stream_id,
                        )
                    )
                )
            event = RawCollectorEvent(
                timing=timing,
                event_type=event_type,
                payload=payload,
                collector_session_id=self.collector_session_id,
                epoch_id=_session_epoch_id(
                    epoch_id_offset=self.epoch_id_offset,
                    local_epoch_id=decision.epoch_id,
                ),
            )
            self._ensure_capacity_locked((event,))
            validator.commit(prepared)
            self._validators[validator_key] = validator
            source_key = (timing.source, timing.instrument, stream_id)
            self._unresolved_gaps.pop(source_key, None)
            self._append_pending_event_locked(event)
            self._last_feed_event_at[source_key] = (
                timing.collector_receive_ts or timing.available_ts
            ).astimezone(UTC)
        return CollectorIngressResult(accepted_events=1, stale_events=int(decision.stale))

    def _quality_would_accept(
        self,
        *,
        timing: TimedMarketEvent,
        stream_id: str,
    ) -> bool:
        timing = replace(timing, ingest_version=self.ingest_version)
        with self._lock:
            return (
                self._validator(
                    source=timing.source,
                    instrument=timing.instrument,
                    stream_id=stream_id,
                )
                .prepare(timing)
                .decision.accepted
            )

    def _ensure_capacity_locked(self, events: Sequence[RawCollectorEvent]) -> None:
        added_events = len(events)
        added_bytes = sum(event.estimated_size_bytes for event in events)
        projected_events = len(self._pending) + self._inflight_events + added_events
        projected_bytes = self._pending_bytes + self._inflight_bytes + added_bytes
        if projected_events > self.max_pending_events:
            raise CollectorBufferCapacityError("raw event buffer would exceed max_pending_events")
        if projected_bytes > self.max_pending_bytes:
            raise CollectorBufferCapacityError("raw event buffer would exceed max_pending_bytes")

    def _validator(self, *, source: str, instrument: str, stream_id: str) -> EventQualityValidator:
        key = (source, instrument, stream_id)
        validator = self._validators.get(key)
        if validator is None:
            validator = EventQualityValidator()
            self._validators[key] = validator
        return validator

    def _new_gap_event(
        self,
        *,
        source: str,
        instrument: str,
        stream_id: str,
        reason: str,
        observed_at: datetime | None,
        previous_available_ts: datetime | None,
    ) -> RawCollectorEvent:
        source, instrument, stream_id, reason = _normalize_gap_identity(
            source=source,
            instrument=instrument,
            stream_id=stream_id,
            reason=reason,
        )
        boundary_at = _gap_boundary_time(
            observed_at=observed_at,
            previous_available_ts=previous_available_ts,
        )
        return RawCollectorEvent(
            timing=TimedMarketEvent(
                source_ts=boundary_at,
                collector_receive_ts=boundary_at,
                available_ts=boundary_at,
                sequence_or_hash=f"gap:{stream_id}:{uuid4().hex}",
                source=source,
                instrument=instrument,
                schema_version=_CONTINUITY_GAP_SCHEMA_VERSION,
                ingest_version=self.ingest_version,
            ),
            event_type=_CONTINUITY_GAP_EVENT_TYPE,
            payload={
                "event_type": _CONTINUITY_GAP_EVENT_TYPE,
                "stream_id": stream_id,
                "reason": reason,
            },
            collector_session_id=self.collector_session_id,
            epoch_id=0,
        )

    def _append_gap_event_locked(
        self,
        *,
        event: RawCollectorEvent,
        validator: EventQualityValidator,
        stream_id: str,
    ) -> None:
        prepared = validator.prepare(event.timing)
        if not prepared.decision.accepted:
            raise RuntimeError("continuity gap boundary was not causally admissible")
        event = RawCollectorEvent(
            timing=event.timing,
            event_type=event.event_type,
            payload=dict(event.payload),
            collector_session_id=event.collector_session_id,
            epoch_id=_session_epoch_id(
                epoch_id_offset=self.epoch_id_offset,
                local_epoch_id=prepared.decision.epoch_id,
            ),
        )
        validator.commit(prepared)
        key = (event.timing.source, event.timing.instrument, stream_id)
        self._validators[key] = validator
        self._unresolved_gaps[key] = self._unresolved_gaps.get(key, 0) + 1
        _add_quality_counts(
            self._pending_quality_counts,
            _partition_key(
                event.timing,
                collector_session_id=self.collector_session_id,
            ),
            gap_count=1,
        )
        self._append_pending_event_locked(event)

    def _commit_gap_event_locked(
        self,
        *,
        event: RawCollectorEvent,
        validator: EventQualityValidator,
        stream_id: str,
        reason: str,
    ) -> None:
        validator.mark_gap(reason=reason)
        self._append_gap_event_locked(
            event=event,
            validator=validator,
            stream_id=stream_id,
        )

    def _append_pending_event_locked(self, event: RawCollectorEvent) -> None:
        event = event.with_admission_sequence(self._next_admission_sequence)
        self._next_admission_sequence += 1
        if not self._pending:
            self._oldest_pending_monotonic_ns = monotonic_ns()
        self._pending.append(event)
        self._pending_bytes += event.estimated_size_bytes
        if self._admitted_event_buffer is not None:
            self._admitted_event_buffer.publish(event)
        self._high_water_events = max(
            self._high_water_events,
            len(self._pending) + self._inflight_events,
        )
        self._high_water_bytes = max(
            self._high_water_bytes,
            self._pending_bytes + self._inflight_bytes,
        )

    def _binance_depth_snapshot_url(self, source: str) -> str:
        if source == "binance_spot":
            return self.binance_depth_snapshot_url
        if source == "binance_perp":
            return self.binance_futures_depth_snapshot_url
        raise ValueError("Binance source must be 'binance_spot' or 'binance_perp'")

    def _binance_depth_snapshot_limit(self, source: str) -> int:
        if source == "binance_spot":
            return self.binance_spot_depth_snapshot_limit
        if source == "binance_perp":
            return self.binance_futures_depth_snapshot_limit
        raise ValueError("Binance source must be 'binance_spot' or 'binance_perp'")

    def _depth_synchronizer(self, *, source: str, instrument: str) -> BinanceDepthSynchronizer:
        self._binance_depth_snapshot_url(source)
        key = (source, instrument)
        synchronizer = self._binance_depth.get(key)
        if synchronizer is None:
            synchronizer = BinanceDepthSynchronizer(instrument=instrument, source=source)
            self._binance_depth[key] = synchronizer
        return synchronizer

    def _okx_book_synchronizer(self, *, source: str, instrument: str) -> OkxBookSynchronizer:
        key = (source, instrument)
        synchronizer = self._okx_books.get(key)
        if synchronizer is None:
            synchronizer = OkxBookSynchronizer(instrument=instrument, source=source)
            self._okx_books[key] = synchronizer
        return synchronizer

    def _invalidate_state_after_quality_gap(
        self, timing: TimedMarketEvent, *, stream_id: str
    ) -> None:
        """Do not let a rejected time regression reuse a pre-epoch L2 book."""

        if timing.source == "polymarket_clob" and timing.instrument in self._polymarket_normalizers:
            self.invalidate_polymarket_token(timing.instrument)
        elif timing.source in {"binance_spot", "binance_perp"} and stream_id == _DEPTH_STREAM:
            self.invalidate_binance_depth(timing.instrument, source=timing.source)
        elif timing.source in {"okx_spot", "okx_swap"} and stream_id == _BOOK_STREAM:
            self._okx_book_synchronizer(source=timing.source, instrument=timing.instrument).reset()


def _session_epoch_id(
    *,
    epoch_id_offset: int,
    local_epoch_id: int,
) -> int:
    epoch_id = epoch_id_offset + local_epoch_id
    if epoch_id > (1 << 63) - 1:
        raise OverflowError("collector epoch exceeds signed 64-bit storage")
    return epoch_id


def _normalize_gap_identity(
    *,
    source: str,
    instrument: str,
    stream_id: str,
    reason: str,
) -> tuple[str, str, str, str]:
    normalized_source = source.strip().casefold()
    normalized_instrument = instrument.strip()
    normalized_stream_id = stream_id.strip()
    if not normalized_source or not normalized_instrument or not normalized_stream_id:
        raise ValueError("source, instrument, and stream_id are required")
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("gap reason is required")
    if len(normalized_reason) > _MAX_GAP_REASON_LENGTH:
        raise ValueError(f"gap reason must not exceed {_MAX_GAP_REASON_LENGTH} characters")
    return (
        normalized_source,
        normalized_instrument,
        normalized_stream_id,
        normalized_reason,
    )


def _consume_background_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _cancel_background_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
        task.add_done_callback(_consume_background_task_exception)


def _gap_boundary_time(
    *,
    observed_at: datetime | None,
    previous_available_ts: datetime | None,
) -> datetime:
    boundary = datetime.now(UTC) if observed_at is None else observed_at
    if boundary.tzinfo is None or boundary.utcoffset() is None:
        raise ValueError("gap observed_at must be timezone-aware")
    boundary = boundary.astimezone(UTC)
    if previous_available_ts is not None:
        boundary = max(
            boundary,
            previous_available_ts.astimezone(UTC) + timedelta(microseconds=1),
        )
    return boundary


def _quality_gap_requires_resubscribe(*, source: str, stream_id: str) -> bool:
    return source == "polymarket_clob" or (
        source in {"okx_spot", "okx_swap"} and stream_id == _BOOK_STREAM
    )


def _raise_first_with_notes(
    errors: Sequence[BaseException],
    *,
    note_prefix: str,
) -> None:
    if not errors:
        raise ValueError("errors must not be empty")
    first, *additional = errors
    for error in additional:
        first.add_note(f"{note_prefix}: {error!r}")
    raise first


def _pending_event_candidate(
    *,
    timing: TimedMarketEvent,
    event_type: str,
    payload: Mapping[str, object],
    collector_session_id: str,
) -> RawCollectorEvent:
    return RawCollectorEvent(
        timing=timing,
        event_type=event_type,
        payload=payload,
        collector_session_id=collector_session_id,
        epoch_id=0,
    )


def _normalize_polymarket_token_groups(
    *,
    token_ids: tuple[str, ...],
    token_groups: Sequence[Sequence[str]] | None,
) -> tuple[tuple[str, ...], ...]:
    if token_groups is None:
        return (token_ids,)
    groups = tuple(
        tuple(token_id.strip() for token_id in group if token_id.strip()) for group in token_groups
    )
    if not groups or any(not group for group in groups):
        raise ValueError("polymarket_token_groups must contain non-empty groups")
    flattened = tuple(token_id for group in groups for token_id in group)
    if len(set(flattened)) != len(flattened):
        raise ValueError("polymarket_token_groups must not repeat token IDs")
    if set(flattened) != set(token_ids):
        raise ValueError("polymarket_token_groups must be an exact partition of token IDs")
    return groups


def _normalize_polymarket_subscription_windows(
    *,
    token_groups: tuple[tuple[str, ...], ...],
    subscription_windows: Sequence[PolymarketSubscriptionWindow] | None,
) -> tuple[PolymarketSubscriptionWindow, ...]:
    if subscription_windows is None:
        return ()
    normalized = tuple(subscription_windows)
    if tuple(item.token_ids for item in normalized) != token_groups:
        raise ValueError("subscription windows must exactly match Polymarket token groups")
    return normalized


async def _wait_until_or_stop(*, deadline: datetime, stop_event: asyncio.Event) -> bool:
    """Return true when stopped, or false once the UTC deadline is reached."""

    seconds = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
    if seconds == 0.0:
        return stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def _collect_polymarket_during_window(
    *,
    token_group: tuple[str, ...],
    window: PolymarketSubscriptionWindow,
    stop_event: asyncio.Event,
    on_payload: Callable[[Mapping[str, object], datetime], Awaitable[bool]],
    on_error: Callable[[Exception], Awaitable[None]],
) -> None:
    """Connect for one official CLOB snapshot window while the parent feeds stay alive."""

    if datetime.now(UTC) >= window.end:
        return
    if await _wait_until_or_stop(deadline=window.start, stop_event=stop_event):
        return
    if datetime.now(UTC) >= window.end:
        return

    subscription_stop = asyncio.Event()
    worker = asyncio.create_task(
        JsonWebSocketCollector(polymarket_market_subscription(token_group)).collect_forever(
            stop_event=subscription_stop,
            on_payload=on_payload,
            on_error=on_error,
        ),
        name=f"polymarket-window-{'-'.join(token_group)}",
    )
    deadline = asyncio.create_task(
        _wait_until_or_stop(deadline=window.end, stop_event=stop_event),
        name=f"polymarket-window-deadline-{'-'.join(token_group)}",
    )
    try:
        done, _ = await asyncio.wait({worker, deadline}, return_when=asyncio.FIRST_COMPLETED)
        if worker in done and not subscription_stop.is_set() and not stop_event.is_set():
            await worker
            raise RuntimeError("scheduled Polymarket collector exited unexpectedly")
        subscription_stop.set()
        await worker
    finally:
        subscription_stop.set()
        if not deadline.done():
            deadline.cancel()
        await asyncio.gather(worker, deadline, return_exceptions=True)


def _rejected(reason: str) -> CollectorIngressResult:
    return CollectorIngressResult(rejected_events=1, reasons=(reason,))


def _with_depth_status(
    outcome: CollectorIngressResult,
    status: DepthUpdateStatus,
    reason: str | None,
    *,
    resubscribe_on_unavailable: bool = True,
) -> CollectorIngressResult:
    if status is DepthUpdateStatus.APPLIED:
        return outcome
    return outcome.merged(
        CollectorIngressResult(
            reasons=(reason or status.value,),
            resubscribe_required=(
                resubscribe_on_unavailable
                and status
                in {
                    DepthUpdateStatus.GAP,
                    DepthUpdateStatus.AWAITING_SNAPSHOT,
                }
            ),
        )
    )


def _okx_source_for_instrument(instrument: str) -> str | None:
    if instrument == "BTC-USDT":
        return "okx_spot"
    if instrument == "BTC-USDT-SWAP":
        return "okx_swap"
    return None


def _okx_item_payload(
    payload: Mapping[str, object], item: Mapping[str, object]
) -> Mapping[str, object]:
    argument = payload.get("arg")
    return {
        "arg": dict(argument) if isinstance(argument, Mapping) else {},
        "action": payload.get("action"),
        "data": [dict(item)],
    }


def _positive_number(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be finite and > 0")
    return result


def _binance_instruments(streams: Sequence[str]) -> tuple[str, ...]:
    instruments: list[str] = []
    for stream in streams:
        symbol, separator, _ = stream.partition("@")
        if not separator or not symbol.isalnum():
            continue
        instrument = symbol.upper()
        if instrument not in instruments:
            instruments.append(instrument)
    return tuple(instruments)


def _binance_depth_instruments(streams: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        instrument
        for instrument in _binance_instruments(streams)
        if any(
            stream.partition("@")[0].upper() == instrument
            and stream.casefold().partition("@")[2].startswith("depth")
            and not _is_binance_partial_depth_suffix(stream.casefold().partition("@")[2])
            for stream in streams
        )
    )


def _binance_stream_ids(streams: Sequence[str]) -> tuple[str, ...]:
    stream_ids: list[str] = []
    for stream in streams:
        suffix = stream.casefold().partition("@")[2]
        if suffix in {"trade", "aggtrade"}:
            stream_id = _TRADE_STREAM
        elif suffix == "kline_1s":
            stream_id = _KLINE_STREAM
        elif _is_binance_partial_depth_suffix(suffix):
            stream_id = _PARTIAL_BOOK_STREAM
        elif suffix.startswith("depth"):
            stream_id = _DEPTH_STREAM
        elif suffix == "bookticker":
            stream_id = _BOOK_TICKER_STREAM
        else:
            continue
        if stream_id not in stream_ids:
            stream_ids.append(stream_id)
    return tuple(stream_ids)


def _is_binance_partial_depth_stream(stream: str) -> bool:
    _symbol, separator, suffix = stream.casefold().partition("@")
    return bool(separator) and _is_binance_partial_depth_suffix(suffix)


def _is_binance_partial_depth_suffix(suffix: str) -> bool:
    return suffix in {
        "depth5",
        "depth5@100ms",
        "depth10",
        "depth10@100ms",
        "depth20",
        "depth20@100ms",
    }


def _binance_feed_keys(source: str, streams: Sequence[str]) -> set[_QualityStreamKey]:
    keys: set[_QualityStreamKey] = set()
    for stream in streams:
        symbol, separator, _suffix = stream.casefold().partition("@")
        if not separator or not symbol.isalnum():
            continue
        stream_ids = _binance_stream_ids((stream,))
        if stream_ids:
            keys.add((source, symbol.upper(), stream_ids[0]))
    return keys


def _okx_stream_id(channel: object) -> str | None:
    if channel == "trades":
        return _TRADE_STREAM
    if channel in {"books", "books5"}:
        return _BOOK_STREAM
    return None


type _RawPartitionKey = tuple[str, str, str, str, str, str, str]


def _partition_key(
    timing: TimedMarketEvent,
    *,
    collector_session_id: str,
) -> _RawPartitionKey:
    timestamp = timing.available_ts.astimezone(UTC)
    return (
        timing.source,
        timing.instrument,
        timestamp.strftime("%Y-%m-%d"),
        timestamp.strftime("%H"),
        timing.schema_version,
        timing.ingest_version,
        collector_session_id,
    )


def _add_quality_counts(
    counts_by_partition: dict[_RawPartitionKey, tuple[int, int]],
    key: _RawPartitionKey,
    *,
    duplicate_count: int = 0,
    gap_count: int = 0,
) -> None:
    previous_duplicates, previous_gaps = counts_by_partition.get(key, (0, 0))
    counts_by_partition[key] = (
        previous_duplicates + duplicate_count,
        previous_gaps + gap_count,
    )


async def _record_okx_boundaries(collector: BtcForwardCollector, reason: str) -> None:
    await collector.record_optional_feed_boundary("okx_spot", reason=reason)
    await collector.record_optional_feed_boundary("okx_swap", reason=reason)


__all__ = [
    "BtcForwardCollector",
    "CollectorBufferCapacityError",
    "CollectorBufferStats",
    "CollectorIngressResult",
    "RequiredFeedHealth",
]
