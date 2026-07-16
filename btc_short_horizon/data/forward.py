"""Forward public-feed collector that preserves causal timing and data epochs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from threading import Lock

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
)
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.okx import (
    OkxBookSynchronizer,
    normalize_okx_book_update,
    normalize_okx_trade,
)
from btc_short_horizon.data.polymarket import PolymarketL2Normalizer, PolymarketL2Status
from btc_short_horizon.data.quality import DataQualityStats, EventQualityValidator
from btc_short_horizon.data.rtds import normalize_chainlink_btc_usd
from btc_short_horizon.data.storage import DataPartitionManifest
from btc_short_horizon.data.subscriptions import (
    binance_combined_stream_subscription,
    binance_futures_market_stream_subscription,
    binance_futures_public_stream_subscription,
    okx_public_subscription,
    polymarket_market_subscription,
    polymarket_rtds_chainlink_btc_subscription,
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
_TRADE_STREAM = "trade"
_KLINE_STREAM = "kline_1s"
_DEPTH_STREAM = "depth"
_BOOK_TICKER_STREAM = "book_ticker"
_BOOK_STREAM = "book"
_INSTRUMENT_METADATA_STREAM = "instrument_metadata"

type _QualityStreamKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class CollectorIngressResult:
    accepted_events: int = 0
    rejected_events: int = 0
    stale_events: int = 0
    reasons: tuple[str, ...] = ()

    def merged(self, other: CollectorIngressResult) -> CollectorIngressResult:
        return CollectorIngressResult(
            accepted_events=self.accepted_events + other.accepted_events,
            rejected_events=self.rejected_events + other.rejected_events,
            stale_events=self.stale_events + other.stale_events,
            reasons=(*self.reasons, *other.reasons),
        )


@dataclass(frozen=True, slots=True)
class RequiredFeedHealth:
    healthy: bool
    reason: str
    feeds: tuple[dict[str, object], ...]


class BtcForwardCollector:
    """Collect BTC-only CLOB, Chainlink RTDS, and Binance trade evidence safely."""

    def __init__(
        self,
        *,
        raw_data_root: Path,
        polymarket_token_ids: Sequence[str],
        flush_size: int = 10_000,
        flush_interval_seconds: float = 60.0,
        ingest_version: str = "btc-short-horizon-v1",
        epoch_id_offset: int = 0,
        binance_depth_snapshot_url: str = _BINANCE_DEPTH_SNAPSHOT_URL,
        binance_futures_depth_snapshot_url: str = _BINANCE_FUTURES_DEPTH_SNAPSHOT_URL,
        binance_depth_snapshot_limit: int = 1_000,
        binance_depth_snapshot_timeout_seconds: float = 10.0,
        okx_instruments_url: str = _OKX_PUBLIC_INSTRUMENTS_URL,
        okx_swap_contract_value: float | None = None,
    ) -> None:
        token_ids = tuple(token_id.strip() for token_id in polymarket_token_ids if token_id.strip())
        if not token_ids:
            raise ValueError("polymarket_token_ids must not be empty")
        if len(set(token_ids)) != len(token_ids):
            raise ValueError("polymarket_token_ids must be unique")
        if flush_size < 1:
            raise ValueError("flush_size must be >= 1")
        if not isfinite(flush_interval_seconds) or flush_interval_seconds <= 0.0:
            raise ValueError("flush_interval_seconds must be finite and > 0")
        if not ingest_version or not ingest_version.strip():
            raise ValueError("ingest_version is required")
        if isinstance(epoch_id_offset, bool) or epoch_id_offset < 0:
            raise ValueError("epoch_id_offset must be a non-negative integer")
        if not binance_depth_snapshot_url.startswith("https://"):
            raise ValueError("binance_depth_snapshot_url must use https")
        if not binance_futures_depth_snapshot_url.startswith("https://"):
            raise ValueError("binance_futures_depth_snapshot_url must use https")
        if not 1 <= binance_depth_snapshot_limit <= 5_000:
            raise ValueError("binance_depth_snapshot_limit must be in [1, 5000]")
        if (
            not isfinite(binance_depth_snapshot_timeout_seconds)
            or binance_depth_snapshot_timeout_seconds <= 0.0
        ):
            raise ValueError("binance_depth_snapshot_timeout_seconds must be finite and > 0")
        if not okx_instruments_url.startswith("https://"):
            raise ValueError("okx_instruments_url must use https")
        if okx_swap_contract_value is not None and (
            not isfinite(okx_swap_contract_value) or okx_swap_contract_value <= 0.0
        ):
            raise ValueError("okx_swap_contract_value must be finite and > 0 when provided")
        self.token_ids = token_ids
        self.flush_size = flush_size
        self.flush_interval_seconds = flush_interval_seconds
        self.ingest_version = ingest_version.strip()
        self.epoch_id_offset = epoch_id_offset
        self.binance_depth_snapshot_url = binance_depth_snapshot_url
        self.binance_futures_depth_snapshot_url = binance_futures_depth_snapshot_url
        self.binance_depth_snapshot_limit = binance_depth_snapshot_limit
        self.binance_depth_snapshot_timeout_seconds = binance_depth_snapshot_timeout_seconds
        self.okx_instruments_url = okx_instruments_url
        self.okx_swap_contract_value = okx_swap_contract_value
        self._writer = PartitionedRawEventWriter(raw_data_root)
        self._polymarket_normalizers = {
            token_id: PolymarketL2Normalizer(token_id=token_id) for token_id in token_ids
        }
        self._binance_depth: dict[tuple[str, str], BinanceDepthSynchronizer] = {}
        self._okx_books: dict[tuple[str, str], OkxBookSynchronizer] = {}
        self._validators: dict[_QualityStreamKey, EventQualityValidator] = {}
        self._pending: list[RawCollectorEvent] = []
        self._pending_quality_counts: dict[_RawPartitionKey, tuple[int, int]] = {}
        self._unattributed_gaps: dict[_QualityStreamKey, int] = {}
        self._required_feed_keys: set[_QualityStreamKey] = {
            ("polymarket_clob", token_id, _MARKET_STREAM) for token_id in token_ids
        }
        self._required_feed_keys.add(("polymarket_rtds_chainlink", "btc/usd", _PRICE_STREAM))
        self._last_feed_event_at: dict[_QualityStreamKey, datetime] = {}
        self._lock = Lock()
        self._flush_lock = Lock()

    @property
    def pending_event_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def flush_required(self) -> bool:
        return self.pending_event_count >= self.flush_size

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
        required = {token_id.strip() for token_id in token_ids if token_id.strip()}
        if not required or not required.issubset(self._polymarket_normalizers):
            raise ValueError("required Polymarket tokens must belong to this collector")
        with self._lock:
            self._required_feed_keys = {
                key for key in self._required_feed_keys if key[0] != "polymarket_clob"
            }
            self._required_feed_keys.update(
                ("polymarket_clob", token_id, _MARKET_STREAM) for token_id in required
            )

    def feed_health(self, *, now: datetime, stale_after_seconds: float) -> RequiredFeedHealth:
        if not isfinite(stale_after_seconds) or stale_after_seconds <= 0.0:
            raise ValueError("stale_after_seconds must be finite and > 0")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_time = now.astimezone(UTC)
        with self._lock:
            required = tuple(sorted(self._required_feed_keys))
            last_events = dict(self._last_feed_event_at)
            unresolved_gaps = dict(self._unattributed_gaps)
        feeds: list[dict[str, object]] = []
        reasons: list[str] = []
        for key in required:
            source, instrument, stream_id = key
            last_event = last_events.get(key)
            age_seconds = (
                None if last_event is None else (current_time - last_event).total_seconds()
            )
            validator = self._validators.get(key)
            gap_count = 0 if validator is None else validator.stats.gap_events
            if last_event is None:
                state, reason = "silent", "required_feed_silent"
            elif unresolved_gaps.get(key, 0):
                state, reason = "gap", "required_feed_gap"
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

        outcome = CollectorIngressResult()
        for normalizer in self._polymarket_normalizers.values():
            try:
                result = normalizer.apply(payload, collector_receive_ts=collector_receive_ts)
            except ValueError as exc:
                outcome = outcome.merged(_rejected(str(exc)))
                continue
            if result.status is PolymarketL2Status.APPLIED and result.timing is not None:
                outcome = outcome.merged(
                    self._ingest(
                        timing=result.timing,
                        event_type=result.event_type,
                        payload=payload,
                        stream_id=_MARKET_STREAM,
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
        if event_type == "depthupdate":
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
            result = self._depth_synchronizer(source=source, instrument=instrument).apply_snapshot(
                payload,
                collector_receive_ts=collector_receive_ts,
            )
        except ValueError as exc:
            return _rejected(str(exc))
        if result.status is DepthUpdateStatus.GAP:
            self.mark_gap(
                source=timing.source,
                instrument=timing.instrument,
                stream_id=_DEPTH_STREAM,
                reason=result.reason or "depth_snapshot_gap",
            )
        outcome = self._ingest(
            timing=timing,
            event_type="depth_snapshot",
            payload=payload,
            stream_id=_DEPTH_STREAM,
        )
        return _with_depth_status(outcome, result.status, result.reason)

    async def refresh_binance_depth_snapshot(
        self,
        *,
        instrument: str,
        client: httpx.AsyncClient | None = None,
        source: str = "binance_spot",
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
                params={"symbol": instrument, "limit": self.binance_depth_snapshot_limit},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("Binance depth snapshot must be a JSON object")
            return self.handle_binance_depth_snapshot(
                instrument=instrument,
                payload=payload,
                collector_receive_ts=datetime.now(UTC),
                source=source,
            )
        finally:
            if owns_client:
                await active_client.aclose()

    def binance_depth_needs_snapshot(
        self, instrument: str, *, source: str = "binance_spot"
    ) -> bool:
        return self._depth_synchronizer(source=source, instrument=instrument).needs_snapshot

    def invalidate_binance_depth(self, instrument: str, *, source: str = "binance_spot") -> None:
        self._depth_synchronizer(source=source, instrument=instrument).invalidate()

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
            result = self._depth_synchronizer(
                source=source, instrument=timing.instrument
            ).observe_delta(
                message,
                collector_receive_ts=collector_receive_ts,
            )
        except ValueError as exc:
            return _rejected(str(exc))
        if result.status is DepthUpdateStatus.GAP:
            self.mark_gap(
                source=timing.source,
                instrument=timing.instrument,
                stream_id=_DEPTH_STREAM,
                reason=result.reason or "depth_update_gap",
            )
        outcome = self._ingest(
            timing=timing,
            event_type="depth_update",
            payload=payload,
            stream_id=_DEPTH_STREAM,
        )
        return _with_depth_status(outcome, result.status, result.reason)

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

        if payload.get("event") is not None:
            return CollectorIngressResult()
        argument = payload.get("arg")
        data = payload.get("data")
        if (
            not isinstance(argument, Mapping)
            or not isinstance(data, Sequence)
            or isinstance(data, (str, bytes))
        ):
            return _rejected("unsupported_okx_event")
        channel = argument.get("channel")
        instrument = argument.get("instId")
        if not isinstance(channel, str) or not isinstance(instrument, str):
            return _rejected("unsupported_okx_event")
        source = _okx_source_for_instrument(instrument)
        if source is None:
            return _rejected("unsupported_okx_instrument")
        if channel == "trades":
            return self._handle_okx_trades(
                data=data,
                payload=payload,
                collector_receive_ts=collector_receive_ts,
                source=source,
            )
        if channel == "books":
            action = payload.get("action")
            if not isinstance(action, str):
                return _rejected("okx_book_action_missing")
            return self._handle_okx_books(
                data=data,
                payload=payload,
                action=action,
                instrument=instrument,
                collector_receive_ts=collector_receive_ts,
                source=source,
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
        collector_receive_ts: datetime,
        source: str,
    ) -> CollectorIngressResult:
        multiplier = 1.0 if source == "okx_spot" else self.okx_swap_contract_value
        if multiplier is None:
            return _rejected("okx_swap_contract_value_unavailable")
        outcome = CollectorIngressResult()
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
            outcome = outcome.merged(
                self._ingest(
                    timing=record.timing,
                    event_type="trade",
                    payload=_okx_item_payload(payload, item),
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
    ) -> CollectorIngressResult:
        outcome = CollectorIngressResult()
        synchronizer = self._okx_book_synchronizer(source=source, instrument=instrument)
        for item in data:
            if not isinstance(item, Mapping):
                outcome = outcome.merged(_rejected("OKX book data must contain objects"))
                continue
            try:
                timing = normalize_okx_book_update(
                    item,
                    action=action,
                    instrument=instrument,
                    collector_receive_ts=collector_receive_ts,
                    source=source,
                )
                result = synchronizer.apply(
                    action=action,
                    payload=item,
                    collector_receive_ts=collector_receive_ts,
                )
            except ValueError as exc:
                outcome = outcome.merged(_rejected(str(exc)))
                continue
            if result.status is DepthUpdateStatus.GAP:
                self.mark_gap(
                    source=source,
                    instrument=instrument,
                    stream_id=_BOOK_STREAM,
                    reason=result.reason or "okx_book_gap",
                )
            outcome = outcome.merged(
                _with_depth_status(
                    self._ingest(
                        timing=timing,
                        event_type=f"books_{action}",
                        payload=_okx_item_payload(payload, item),
                        stream_id=_BOOK_STREAM,
                    ),
                    result.status,
                    result.reason,
                )
            )
        return outcome

    def mark_gap(self, *, source: str, instrument: str, stream_id: str, reason: str) -> None:
        if not source or not instrument or not stream_id:
            raise ValueError("source, instrument, and stream_id are required")
        self._validator(
            source=source,
            instrument=instrument,
            stream_id=stream_id,
        ).mark_gap(reason=reason)
        with self._lock:
            key = (source, instrument, stream_id)
            self._unattributed_gaps[key] = self._unattributed_gaps.get(key, 0) + 1

    def flush(self) -> tuple[DataPartitionManifest, ...]:
        """Atomically persist the currently buffered events without dropping concurrent ingress."""

        with self._flush_lock:
            with self._lock:
                events = tuple(self._pending)
                self._pending.clear()
            if not events:
                return ()
            event_keys = {_partition_key(event.timing) for event in events}
            with self._lock:
                quality_counts = {
                    key: self._pending_quality_counts.pop(key)
                    for key in event_keys
                    if key in self._pending_quality_counts
                }
            try:
                return self._writer.write(events, quality_counts=quality_counts)
            except Exception:
                with self._lock:
                    self._pending[0:0] = events
                    for key, counts in quality_counts.items():
                        _add_quality_counts(
                            self._pending_quality_counts,
                            key,
                            duplicate_count=counts[0],
                            gap_count=counts[1],
                        )
                raise

    async def collect_forever(
        self,
        *,
        stop_event: asyncio.Event,
        binance_streams: Sequence[str] = DEFAULT_BINANCE_STREAMS,
        binance_futures_market_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_MARKET_STREAMS,
        binance_futures_public_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS,
        okx_subscriptions: Sequence[Mapping[str, str]] = DEFAULT_OKX_SUBSCRIPTIONS,
    ) -> None:
        """Run BTC public subscriptions until stopped, flushing outside socket callbacks."""

        if stop_event.is_set():
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
        resync_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        async with httpx.AsyncClient(
            timeout=self.binance_depth_snapshot_timeout_seconds
        ) as snapshot_client:

            async def refresh_depth_safely(source: str, instrument: str) -> None:
                try:
                    await self.refresh_binance_depth_snapshot(
                        instrument=instrument,
                        client=snapshot_client,
                        source=source,
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    self.invalidate_binance_depth(instrument, source=source)
                    self.mark_gap(
                        source=source,
                        instrument=instrument,
                        stream_id=_DEPTH_STREAM,
                        reason=f"depth_snapshot_{type(exc).__name__}",
                    )

            def schedule_depth_refresh(source: str, instrument: str) -> None:
                key = (source, instrument)
                current = resync_tasks.get(key)
                if current is None or current.done():
                    resync_tasks[key] = asyncio.create_task(
                        refresh_depth_safely(source, instrument)
                    )

            async def on_polymarket(payload: Mapping[str, object], received: datetime) -> None:
                self.handle_polymarket(payload, collector_receive_ts=received)
                await self._flush_if_required()

            async def on_rtds(payload: Mapping[str, object], received: datetime) -> None:
                self.handle_chainlink_rtds(payload, collector_receive_ts=received)
                await self._flush_if_required()

            def on_binance(source: str):
                async def receive(payload: Mapping[str, object], received: datetime) -> None:
                    self.handle_binance(
                        payload,
                        collector_receive_ts=received,
                        source=source,
                    )
                    message = payload.get("data")
                    event = message if isinstance(message, Mapping) else payload
                    instrument = event.get("s")
                    if (
                        str(event.get("e") or "").casefold() == "depthupdate"
                        and isinstance(instrument, str)
                        and instrument
                        and self.binance_depth_needs_snapshot(instrument, source=source)
                    ):
                        schedule_depth_refresh(source, instrument)
                    await self._flush_if_required()

                return receive

            async def on_okx(payload: Mapping[str, object], received: datetime) -> None:
                self.handle_okx(payload, collector_receive_ts=received)
                await self._flush_if_required()

            async def on_polymarket_error(exc: Exception) -> None:
                for token_id in self.token_ids:
                    self.invalidate_polymarket_token(token_id)
                    self.mark_gap(
                        source="polymarket_clob",
                        instrument=token_id,
                        stream_id=_MARKET_STREAM,
                        reason=type(exc).__name__,
                    )

            async def on_rtds_error(exc: Exception) -> None:
                self.mark_gap(
                    source="polymarket_rtds_chainlink",
                    instrument="btc/usd",
                    stream_id=_PRICE_STREAM,
                    reason=type(exc).__name__,
                )

            def on_binance_error(
                source: str,
                instruments: tuple[str, ...],
                depth_instruments: tuple[str, ...],
                stream_ids: tuple[str, ...],
            ):
                async def report(exc: Exception) -> None:
                    for instrument in instruments:
                        if instrument in depth_instruments:
                            self.invalidate_binance_depth(instrument, source=source)
                        for stream_id in stream_ids:
                            self.mark_gap(
                                source=source,
                                instrument=instrument,
                                stream_id=stream_id,
                                reason=type(exc).__name__,
                            )

                return report

            async def on_okx_error(exc: Exception) -> None:
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
                        self._okx_book_synchronizer(
                            source=source,
                            instrument=instrument,
                        ).reset()
                    self.mark_gap(
                        source=source,
                        instrument=instrument,
                        stream_id=stream_id,
                        reason=type(exc).__name__,
                    )

            if self.okx_swap_contract_value is None and any(
                item.get("instId") == "BTC-USDT-SWAP" for item in normalized_okx_subscriptions
            ):
                try:
                    await self.refresh_okx_swap_contract_value(client=snapshot_client)
                except (httpx.HTTPError, ValueError) as exc:
                    self.mark_gap(
                        source="okx_swap",
                        instrument="BTC-USDT-SWAP",
                        stream_id=_TRADE_STREAM,
                        reason=f"contract_metadata_{type(exc).__name__}",
                    )

            for source, instruments in (
                ("binance_spot", spot_depth_instruments),
                ("binance_perp", futures_depth_instruments),
            ):
                for instrument in instruments:
                    await refresh_depth_safely(source, instrument)

            collectors: list[object] = [
                JsonWebSocketCollector(
                    polymarket_market_subscription(self.token_ids)
                ).collect_forever(
                    stop_event=stop_event,
                    on_payload=on_polymarket,
                    on_error=on_polymarket_error,
                ),
                JsonWebSocketCollector(
                    polymarket_rtds_chainlink_btc_subscription()
                ).collect_forever(
                    stop_event=stop_event,
                    on_payload=on_rtds,
                    on_error=on_rtds_error,
                ),
            ]
            if spot_stream_names:
                collectors.append(
                    JsonWebSocketCollector(
                        binance_combined_stream_subscription(spot_stream_names)
                    ).collect_forever(
                        stop_event=stop_event,
                        on_payload=on_binance("binance_spot"),
                        on_error=on_binance_error(
                            "binance_spot",
                            spot_instruments,
                            spot_depth_instruments,
                            spot_stream_ids,
                        ),
                    )
                )
            if futures_market_stream_names:
                collectors.append(
                    JsonWebSocketCollector(
                        binance_futures_market_stream_subscription(futures_market_stream_names)
                    ).collect_forever(
                        stop_event=stop_event,
                        on_payload=on_binance("binance_perp"),
                        on_error=on_binance_error(
                            "binance_perp",
                            futures_market_instruments,
                            (),
                            futures_market_stream_ids,
                        ),
                    )
                )
            if futures_public_stream_names:
                collectors.append(
                    JsonWebSocketCollector(
                        binance_futures_public_stream_subscription(futures_public_stream_names)
                    ).collect_forever(
                        stop_event=stop_event,
                        on_payload=on_binance("binance_perp"),
                        on_error=on_binance_error(
                            "binance_perp",
                            futures_public_instruments,
                            futures_depth_instruments,
                            futures_public_stream_ids,
                        ),
                    )
                )
            if normalized_okx_subscriptions:
                collectors.append(
                    JsonWebSocketCollector(
                        okx_public_subscription(tuple(normalized_okx_subscriptions))
                    ).collect_forever(
                        stop_event=stop_event,
                        on_payload=on_okx,
                        on_error=on_okx_error,
                    )
                )
            collectors.append(self._flush_periodically(stop_event=stop_event))
            try:
                await asyncio.gather(*collectors)
            finally:
                await asyncio.gather(*resync_tasks.values(), return_exceptions=True)
                await asyncio.to_thread(self.flush)

    async def _flush_if_required(self) -> None:
        if self.flush_required:
            await asyncio.to_thread(self.flush)

    async def _flush_periodically(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.flush_interval_seconds)
            except TimeoutError:
                if self.pending_event_count:
                    await asyncio.to_thread(self.flush)

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
        decision = self._validator(
            source=timing.source,
            instrument=timing.instrument,
            stream_id=stream_id,
        ).observe(timing)
        if not decision.accepted:
            if decision.reason == "duplicate":
                self._record_partition_quality(timing, duplicate_count=1)
            elif decision.reason == "out_of_order":
                self._invalidate_state_after_quality_gap(timing, stream_id=stream_id)
                self._record_unattributed_gap(
                    source=timing.source,
                    instrument=timing.instrument,
                    stream_id=stream_id,
                )
            return _rejected(decision.reason)
        event = RawCollectorEvent(
            timing=timing,
            event_type=event_type,
            payload=payload,
            epoch_id=self.epoch_id_offset + decision.epoch_id,
        )
        with self._lock:
            source_key = (timing.source, timing.instrument, stream_id)
            gap_count = self._unattributed_gaps.pop(source_key, 0)
            if gap_count:
                _add_quality_counts(
                    self._pending_quality_counts,
                    _partition_key(timing),
                    gap_count=gap_count,
                )
            self._pending.append(event)
            self._last_feed_event_at[source_key] = (
                timing.collector_receive_ts or timing.available_ts
            ).astimezone(UTC)
        return CollectorIngressResult(accepted_events=1, stale_events=int(decision.stale))

    def _record_partition_quality(
        self,
        timing: TimedMarketEvent,
        *,
        duplicate_count: int = 0,
        gap_count: int = 0,
    ) -> None:
        with self._lock:
            _add_quality_counts(
                self._pending_quality_counts,
                _partition_key(timing),
                duplicate_count=duplicate_count,
                gap_count=gap_count,
            )

    def _record_unattributed_gap(self, *, source: str, instrument: str, stream_id: str) -> None:
        with self._lock:
            key = (source, instrument, stream_id)
            self._unattributed_gaps[key] = self._unattributed_gaps.get(key, 0) + 1

    def _validator(self, *, source: str, instrument: str, stream_id: str) -> EventQualityValidator:
        key = (source, instrument, stream_id)
        validator = self._validators.get(key)
        if validator is None:
            validator = EventQualityValidator()
            self._validators[key] = validator
        return validator

    def _binance_depth_snapshot_url(self, source: str) -> str:
        if source == "binance_spot":
            return self.binance_depth_snapshot_url
        if source == "binance_perp":
            return self.binance_futures_depth_snapshot_url
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


def _rejected(reason: str) -> CollectorIngressResult:
    return CollectorIngressResult(rejected_events=1, reasons=(reason,))


def _with_depth_status(
    outcome: CollectorIngressResult,
    status: DepthUpdateStatus,
    reason: str | None,
) -> CollectorIngressResult:
    if status is DepthUpdateStatus.APPLIED:
        return outcome
    return outcome.merged(CollectorIngressResult(reasons=(reason or status.value,)))


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
        elif suffix.startswith("depth"):
            stream_id = _DEPTH_STREAM
        elif suffix == "bookticker":
            stream_id = _BOOK_TICKER_STREAM
        else:
            continue
        if stream_id not in stream_ids:
            stream_ids.append(stream_id)
    return tuple(stream_ids)


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
    if channel == "books":
        return _BOOK_STREAM
    return None


type _RawPartitionKey = tuple[str, str, str, str, str, str]


def _partition_key(timing: TimedMarketEvent) -> _RawPartitionKey:
    timestamp = timing.available_ts.astimezone(UTC)
    return (
        timing.source,
        timing.instrument,
        timestamp.strftime("%Y-%m-%d"),
        timestamp.strftime("%H"),
        timing.schema_version,
        timing.ingest_version,
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


__all__ = ["BtcForwardCollector", "CollectorIngressResult", "RequiredFeedHealth"]
