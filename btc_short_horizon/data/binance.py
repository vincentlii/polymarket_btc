"""Causal Binance trade and diff-depth normalization for BTC research."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Mapping, Sequence

from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.features.events import BtcBookTop, BtcTrade


class DepthUpdateStatus(StrEnum):
    APPLIED = "applied"
    STALE = "stale"
    GAP = "gap"
    AWAITING_SNAPSHOT = "awaiting_snapshot"


@dataclass(frozen=True, slots=True)
class BinanceTradeRecord:
    timing: TimedMarketEvent
    trade: BtcTrade


@dataclass(frozen=True, slots=True)
class DepthApplyResult:
    status: DepthUpdateStatus
    book_top: BtcBookTop | None
    last_update_id: int | None
    reason: str | None = None


def normalize_binance_trade(
    payload: Mapping[str, object],
    *,
    collector_receive_ts: datetime | None,
    source: str = "binance_spot",
    availability_delay: timedelta = timedelta(0),
) -> BinanceTradeRecord:
    """Normalize a Binance trade/aggTrade without inventing receive timestamps."""

    symbol = _required_text(payload, "s")
    source_ts = _timestamp_from_millis(_first_present(payload, "T", "E"), "T/E")
    receive_ts = _normalize_receive_ts(collector_receive_ts)
    available_ts = _available_time(source_ts, receive_ts, availability_delay)
    price = _positive_float(payload, "p")
    quantity = _positive_float(payload, "q")
    buyer_is_maker = _bool(payload.get("m"), "m")
    sequence = _first_present(payload, "t", "a")
    if sequence is None:
        raise ValueError("Binance trade payload requires trade id t or aggregate id a")
    timing = TimedMarketEvent(
        source_ts=source_ts,
        collector_receive_ts=receive_ts,
        available_ts=available_ts,
        sequence_or_hash=str(sequence),
        source=source,
        instrument=symbol,
        schema_version="binance-trade-v1",
        ingest_version="btc-short-horizon-v1",
    )
    return BinanceTradeRecord(
        timing=timing,
        trade=BtcTrade(
            source_ts_ns=_datetime_to_ns(source_ts),
            available_ts_ns=_datetime_to_ns(available_ts),
            price=price,
            quantity=quantity,
            aggressor_side="sell" if buyer_is_maker else "buy",
            source=source,
            instrument=symbol,
        ),
    )


def normalize_binance_kline(
    payload: Mapping[str, object],
    *,
    collector_receive_ts: datetime,
    source: str = "binance_spot",
) -> TimedMarketEvent:
    """Validate one final UTC one-second Kline used by the deployed proxy model."""

    if _required_text(payload, "e").casefold() != "kline":
        raise ValueError("Binance kline payload requires event type 'kline'")
    symbol = _required_text(payload, "s")
    kline_value = payload.get("k")
    if not isinstance(kline_value, Mapping):
        raise ValueError("Binance kline payload field 'k' must be an object")
    kline = kline_value
    if _required_text(kline, "s") != symbol:
        raise ValueError("Binance kline symbols must agree")
    if _required_text(kline, "i") != "1s":
        raise ValueError("only Binance UTC one-second klines are supported")
    if not _bool(kline.get("x"), "x"):
        raise ValueError("Binance kline must be final")
    open_millis = _nonnegative_int(kline, "t")
    close_millis = _nonnegative_int(kline, "T")
    if close_millis != open_millis + 999:
        raise ValueError("Binance one-second kline timestamps are inconsistent")
    open_price = _positive_float(kline, "o")
    close_price = _positive_float(kline, "c")
    high_price = _positive_float(kline, "h")
    low_price = _positive_float(kline, "l")
    if high_price < max(open_price, close_price) or low_price > min(open_price, close_price):
        raise ValueError("Binance kline OHLC prices are inconsistent")
    volume = _nonnegative_float(kline, "v")
    _nonnegative_float(kline, "q")
    taker_buy_volume = _nonnegative_float(kline, "V")
    _nonnegative_float(kline, "Q")
    _nonnegative_int(kline, "n")
    if taker_buy_volume > volume:
        raise ValueError("Binance taker-buy volume cannot exceed total volume")
    source_ts = _timestamp_from_millis(payload.get("E"), "E")
    receive_ts = _normalize_receive_ts(collector_receive_ts)
    assert receive_ts is not None
    return TimedMarketEvent(
        source_ts=source_ts,
        collector_receive_ts=receive_ts,
        available_ts=max(source_ts, receive_ts),
        sequence_or_hash=f"kline:{open_millis}",
        source=source,
        instrument=symbol,
        schema_version="binance-kline-1s-v1",
        ingest_version="btc-short-horizon-v1",
    )


def normalize_binance_depth_update(
    payload: Mapping[str, object],
    *,
    collector_receive_ts: datetime | None,
    source: str = "binance_spot",
    availability_delay: timedelta = timedelta(0),
) -> TimedMarketEvent:
    """Normalize a timestamped diff-depth message before L2 continuity checks."""

    symbol = _required_text(payload, "s")
    _nonnegative_int(payload, "U")
    final_update_id = _nonnegative_int(payload, "u")
    _levels(payload.get("b", ()))
    _levels(payload.get("a", ()))
    source_ts = _timestamp_from_millis(payload.get("E"), "E")
    receive_ts = _normalize_receive_ts(collector_receive_ts)
    return TimedMarketEvent(
        source_ts=source_ts,
        collector_receive_ts=receive_ts,
        available_ts=_available_time(source_ts, receive_ts, availability_delay),
        sequence_or_hash=str(final_update_id),
        source=source,
        instrument=symbol,
        schema_version="binance-depth-update-v1",
        ingest_version="btc-short-horizon-v1",
    )


def normalize_binance_depth_snapshot(
    payload: Mapping[str, object],
    *,
    instrument: str,
    collector_receive_ts: datetime,
    source: str = "binance_spot",
) -> TimedMarketEvent:
    """Normalize a REST depth snapshot whose only trustworthy time is local receive time."""

    if not instrument:
        raise ValueError("instrument is required")
    receive_ts = _normalize_receive_ts(collector_receive_ts)
    assert receive_ts is not None
    update_id = _nonnegative_int(payload, "lastUpdateId")
    _levels(payload.get("bids", payload.get("b", ())))
    _levels(payload.get("asks", payload.get("a", ())))
    receive_ns = _datetime_to_ns(receive_ts)
    return TimedMarketEvent(
        source_ts=receive_ts,
        collector_receive_ts=receive_ts,
        available_ts=receive_ts,
        sequence_or_hash=f"snapshot:{update_id}:{receive_ns}",
        source=source,
        instrument=instrument,
        schema_version="binance-depth-snapshot-receive-v1",
        ingest_version="btc-short-horizon-v1",
    )


def normalize_binance_book_ticker(
    payload: Mapping[str, object],
    *,
    collector_receive_ts: datetime,
    source: str = "binance_spot",
) -> TimedMarketEvent:
    """Normalize receive-only Book Ticker evidence without fabricating a venue timestamp."""

    symbol = _required_text(payload, "s")
    update_id = _nonnegative_int(payload, "u")
    bid = _positive_float(payload, "b")
    _nonnegative_float(payload, "B")
    ask = _positive_float(payload, "a")
    _nonnegative_float(payload, "A")
    if bid >= ask:
        raise ValueError("book ticker bid must be below ask")
    receive_ts = _normalize_receive_ts(collector_receive_ts)
    assert receive_ts is not None
    return TimedMarketEvent(
        source_ts=receive_ts,
        collector_receive_ts=receive_ts,
        available_ts=receive_ts,
        sequence_or_hash=str(update_id),
        source=source,
        instrument=symbol,
        schema_version="binance-book-ticker-receive-v1",
        ingest_version="btc-short-horizon-v1",
    )


class BinanceDiffDepthBook:
    """A snapshot-gated L2 book that makes update-ID gaps explicit."""

    def __init__(
        self,
        *,
        instrument: str,
        source: str = "binance_spot",
        availability_delay: timedelta = timedelta(0),
    ) -> None:
        if not instrument:
            raise ValueError("instrument is required")
        _validate_depth_source(source)
        if availability_delay < timedelta(0):
            raise ValueError("availability_delay must be non-negative")
        self.instrument = instrument
        self.source = source
        self.availability_delay = availability_delay
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._last_update_id: int | None = None
        self._synchronized = False
        self._has_processed_delta = False

    @property
    def last_update_id(self) -> int | None:
        return self._last_update_id

    def apply_snapshot(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime
    ) -> DepthApplyResult:
        """Reset state from a REST snapshot before accepting diff-depth updates."""

        receive_ts = _normalize_receive_ts(collector_receive_ts)
        assert receive_ts is not None
        last_update_id = _nonnegative_int(payload, "lastUpdateId")
        bids = _levels(payload.get("bids", payload.get("b", ())))
        asks = _levels(payload.get("asks", payload.get("a", ())))
        source_ts = _timestamp_from_millis(
            _first_present(payload, "T", "E"), "T/E", default=receive_ts
        )
        candidate_bids = {price: quantity for price, quantity in bids if quantity > 0.0}
        candidate_asks = {price: quantity for price, quantity in asks if quantity > 0.0}
        book_top = self._book_top(
            source_ts=source_ts,
            receive_ts=receive_ts,
            bids=candidate_bids,
            asks=candidate_asks,
        )
        if book_top is None:
            self._bids.clear()
            self._asks.clear()
            self._last_update_id = None
            self._synchronized = False
            self._has_processed_delta = False
            return DepthApplyResult(
                status=DepthUpdateStatus.GAP,
                book_top=None,
                last_update_id=None,
                reason="empty_or_crossed_snapshot",
            )
        self._bids = candidate_bids
        self._asks = candidate_asks
        self._last_update_id = last_update_id
        self._synchronized = True
        self._has_processed_delta = False
        return DepthApplyResult(
            status=DepthUpdateStatus.APPLIED,
            book_top=book_top,
            last_update_id=self._last_update_id,
        )

    def apply_delta(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime | None
    ) -> DepthApplyResult:
        """Apply one absolute-quantity diff update or require a fresh snapshot."""

        if not self._synchronized or self._last_update_id is None:
            return DepthApplyResult(
                status=DepthUpdateStatus.AWAITING_SNAPSHOT,
                book_top=None,
                last_update_id=self._last_update_id,
                reason="snapshot_required",
            )
        first_update_id = _nonnegative_int(payload, "U")
        final_update_id = _nonnegative_int(payload, "u")
        if first_update_id > final_update_id:
            return self._gap_result("invalid_update_id_range")
        if final_update_id < self._last_update_id or (
            final_update_id == self._last_update_id
            and (self.source != "binance_perp" or self._has_processed_delta)
        ):
            return DepthApplyResult(
                status=DepthUpdateStatus.STALE,
                book_top=None,
                last_update_id=self._last_update_id,
                reason="already_covered",
            )
        if self.source == "binance_perp":
            if self._has_processed_delta:
                if payload.get("pu") is None:
                    return self._gap_result("previous_update_id_missing")
                if _nonnegative_int(payload, "pu") != self._last_update_id:
                    return self._gap_result("previous_update_id_mismatch")
            elif not first_update_id <= self._last_update_id <= final_update_id:
                return self._gap_result("snapshot_overlap_missing")
        else:
            expected_update_id = self._last_update_id + 1
            if not first_update_id <= expected_update_id <= final_update_id:
                return self._gap_result("update_id_gap")

        bid_changes = _levels(payload.get("b", ()))
        ask_changes = _levels(payload.get("a", ()))
        source_ts = _timestamp_from_millis(_first_present(payload, "T", "E"), "T/E")
        receive_ts = _normalize_receive_ts(collector_receive_ts)
        bid_undo = self._apply_levels(self._bids, bid_changes)
        ask_undo = self._apply_levels(self._asks, ask_changes)
        book_top = self._book_top(
            source_ts=source_ts,
            receive_ts=receive_ts,
            bids=self._bids,
            asks=self._asks,
        )
        if book_top is None:
            self._rollback_levels(self._asks, ask_undo)
            self._rollback_levels(self._bids, bid_undo)
            return self._gap_result("empty_or_crossed_book")
        self._last_update_id = final_update_id
        self._has_processed_delta = True
        return DepthApplyResult(
            status=DepthUpdateStatus.APPLIED,
            book_top=book_top,
            last_update_id=self._last_update_id,
        )

    def _gap_result(self, reason: str) -> DepthApplyResult:
        self._synchronized = False
        return DepthApplyResult(
            status=DepthUpdateStatus.GAP,
            book_top=None,
            last_update_id=self._last_update_id,
            reason=reason,
        )

    @staticmethod
    def _apply_levels(
        levels: dict[float, float],
        changes: Sequence[tuple[float, float]],
    ) -> tuple[tuple[float, float | None], ...]:
        undo: list[tuple[float, float | None]] = []
        for price, quantity in changes:
            undo.append((price, levels.get(price)))
            if quantity == 0.0:
                levels.pop(price, None)
            else:
                levels[price] = quantity
        return tuple(undo)

    @staticmethod
    def _rollback_levels(
        levels: dict[float, float],
        undo: Sequence[tuple[float, float | None]],
    ) -> None:
        for price, prior_quantity in reversed(undo):
            if prior_quantity is None:
                levels.pop(price, None)
            else:
                levels[price] = prior_quantity

    def _book_top(
        self,
        *,
        source_ts: datetime,
        receive_ts: datetime | None,
        bids: Mapping[float, float],
        asks: Mapping[float, float],
    ) -> BtcBookTop | None:
        if not bids or not asks:
            return None
        bid = max(bids)
        ask = min(asks)
        if bid >= ask:
            return None
        available_ts = _available_time(source_ts, receive_ts, self.availability_delay)
        return BtcBookTop(
            source_ts_ns=_datetime_to_ns(source_ts),
            available_ts_ns=_datetime_to_ns(available_ts),
            bid=bid,
            ask=ask,
            bid_size=bids[bid],
            ask_size=asks[ask],
            source=self.source,
            instrument=self.instrument,
        )


@dataclass(frozen=True, slots=True)
class _BufferedDepthUpdate:
    payload: Mapping[str, object]
    collector_receive_ts: datetime | None


class BinanceDepthSynchronizer:
    """Buffers diff-depth updates until one REST snapshot proves continuity."""

    def __init__(
        self,
        *,
        instrument: str,
        source: str = "binance_spot",
        availability_delay: timedelta = timedelta(0),
        max_buffered_events: int = 10_000,
    ) -> None:
        if max_buffered_events < 1:
            raise ValueError("max_buffered_events must be >= 1")
        _validate_depth_source(source)
        self.instrument = instrument
        self.source = source
        self.availability_delay = availability_delay
        self.max_buffered_events = max_buffered_events
        self._book = self._new_book()
        self._buffer: deque[_BufferedDepthUpdate] = deque()
        self._synchronized = False
        self._snapshot_loaded = False

    @property
    def synchronized(self) -> bool:
        return self._synchronized

    @property
    def needs_snapshot(self) -> bool:
        return not self._synchronized

    @property
    def requires_snapshot(self) -> bool:
        return not self._synchronized and not self._snapshot_loaded

    @property
    def buffered_event_count(self) -> int:
        return len(self._buffer)

    def invalidate(self) -> None:
        """Discard a local book after a disconnect or externally detected gap."""

        self._book = self._new_book()
        self._buffer.clear()
        self._synchronized = False
        self._snapshot_loaded = False

    def observe_delta(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime | None
    ) -> DepthApplyResult:
        """Apply an update only after snapshot-to-delta continuity is established."""

        if not self._synchronized and not self._snapshot_loaded:
            return self._buffer_update(payload, collector_receive_ts=collector_receive_ts)
        result = self._book.apply_delta(payload, collector_receive_ts=collector_receive_ts)
        if self._snapshot_loaded and result.status is DepthUpdateStatus.APPLIED:
            self._snapshot_loaded = False
            self._synchronized = True
            self._buffer.clear()
            return result
        if self._snapshot_loaded and result.status is DepthUpdateStatus.STALE:
            return result
        if result.status is not DepthUpdateStatus.GAP:
            return result
        self._book = self._new_book()
        self._synchronized = False
        self._snapshot_loaded = False
        self._buffer.clear()
        self._buffer.append(
            _BufferedDepthUpdate(payload=dict(payload), collector_receive_ts=collector_receive_ts)
        )
        return result

    def apply_snapshot(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime
    ) -> DepthApplyResult:
        """Apply a snapshot and the buffered sequence only when it overlaps safely."""

        snapshot_update_id = _nonnegative_int(payload, "lastUpdateId")
        retained = tuple(
            item
            for item in self._buffer
            if _retains_depth_update(
                source=self.source,
                final_update_id=_nonnegative_int(item.payload, "u"),
                snapshot_update_id=snapshot_update_id,
            )
        )
        if retained:
            first_update_id = _nonnegative_int(retained[0].payload, "U")
            first_final_update_id = _nonnegative_int(retained[0].payload, "u")
            expected_update_id = (
                snapshot_update_id if self.source == "binance_perp" else snapshot_update_id + 1
            )
            if not first_update_id <= expected_update_id <= first_final_update_id:
                self._book = self._new_book()
                self._synchronized = False
                self._snapshot_loaded = False
                return DepthApplyResult(
                    status=DepthUpdateStatus.AWAITING_SNAPSHOT,
                    book_top=None,
                    last_update_id=None,
                    reason="snapshot_too_old",
                )

        candidate = self._new_book()
        result = candidate.apply_snapshot(payload, collector_receive_ts=collector_receive_ts)
        if result.status is not DepthUpdateStatus.APPLIED:
            self._book = self._new_book()
            self._synchronized = False
            self._snapshot_loaded = False
            return result
        if not retained:
            self._book = candidate
            self._buffer.clear()
            self._synchronized = False
            self._snapshot_loaded = True
            return DepthApplyResult(
                status=DepthUpdateStatus.AWAITING_SNAPSHOT,
                book_top=None,
                last_update_id=snapshot_update_id,
                reason="delta_bridge_required",
            )
        last_applied = result
        for index, item in enumerate(retained):
            result = candidate.apply_delta(
                item.payload,
                collector_receive_ts=item.collector_receive_ts,
            )
            if result.status is DepthUpdateStatus.GAP:
                self._book = self._new_book()
                self._buffer = deque(retained[index:])
                self._synchronized = False
                self._snapshot_loaded = False
                return result
            if result.status is DepthUpdateStatus.APPLIED:
                last_applied = result

        self._book = candidate
        self._buffer.clear()
        self._synchronized = True
        self._snapshot_loaded = False
        return last_applied

    def _buffer_update(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime | None
    ) -> DepthApplyResult:
        self._buffer.append(
            _BufferedDepthUpdate(payload=dict(payload), collector_receive_ts=collector_receive_ts)
        )
        if len(self._buffer) <= self.max_buffered_events:
            return DepthApplyResult(
                status=DepthUpdateStatus.AWAITING_SNAPSHOT,
                book_top=None,
                last_update_id=None,
                reason="snapshot_required",
            )
        self._buffer.clear()
        return DepthApplyResult(
            status=DepthUpdateStatus.GAP,
            book_top=None,
            last_update_id=None,
            reason="snapshot_buffer_overflow",
        )

    def _new_book(self) -> BinanceDiffDepthBook:
        return BinanceDiffDepthBook(
            instrument=self.instrument,
            source=self.source,
            availability_delay=self.availability_delay,
        )


def _validate_depth_source(source: str) -> None:
    if source not in {"binance_spot", "binance_perp"}:
        raise ValueError("depth source must be 'binance_spot' or 'binance_perp'")


def _retains_depth_update(
    *,
    source: str,
    final_update_id: int,
    snapshot_update_id: int,
) -> bool:
    if source == "binance_perp":
        return final_update_id >= snapshot_update_id
    return final_update_id > snapshot_update_id


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"payload field {key!r} must be a non-empty string")
    return value.strip()


def _positive_float(payload: Mapping[str, object], key: str) -> float:
    value = _float(payload.get(key), key)
    if value <= 0.0:
        raise ValueError(f"payload field {key!r} must be > 0")
    return value


def _nonnegative_float(payload: Mapping[str, object], key: str) -> float:
    value = _float(payload.get(key), key)
    if value < 0.0:
        raise ValueError(f"payload field {key!r} must be >= 0")
    return value


def _float(value: object, key: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"payload field {key!r} must be numeric") from exc
    if not isfinite(numeric):
        raise ValueError(f"payload field {key!r} must be finite")
    return numeric


def _bool(value: object, key: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"payload field {key!r} must be bool")
    return value


def _nonnegative_int(payload: Mapping[str, object], key: str) -> int:
    try:
        value = int(payload[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"payload field {key!r} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"payload field {key!r} must be a non-negative integer")
    return value


def _levels(value: object) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError("book levels must be a sequence")
    normalized: list[tuple[float, float]] = []
    for level in value:
        if not isinstance(level, Sequence) or isinstance(level, str | bytes) or len(level) != 2:
            raise ValueError("each book level must contain price and quantity")
        price = _float(level[0], "price")
        quantity = _float(level[1], "quantity")
        if price <= 0.0 or quantity < 0.0:
            raise ValueError("book price must be > 0 and quantity must be >= 0")
        normalized.append((price, quantity))
    return tuple(normalized)


def _first_present(payload: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _timestamp_from_millis(
    value: object | None, field_name: str, *, default: datetime | None = None
) -> datetime:
    if value is None:
        if default is None:
            raise ValueError(f"payload field {field_name!r} is required")
        return default
    try:
        millis = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"payload field {field_name!r} must be an epoch milliseconds integer"
        ) from exc
    if millis < 0:
        raise ValueError(f"payload field {field_name!r} must be non-negative")
    return datetime.fromtimestamp(millis / 1_000, UTC)


def _normalize_receive_ts(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("collector_receive_ts must be timezone-aware")
    return value.astimezone(UTC)


def _available_time(source_ts: datetime, receive_ts: datetime | None, delay: timedelta) -> datetime:
    delayed_source = source_ts + delay
    if receive_ts is None:
        return delayed_source
    return max(delayed_source, receive_ts)


def _datetime_to_ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)
