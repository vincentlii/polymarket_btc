"""Causal OKX public trade and order-book normalization for BTC research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from math import isfinite

from btc_short_horizon.data.binance import DepthApplyResult, DepthUpdateStatus
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.features.events import BtcBookTop, BtcTrade


@dataclass(frozen=True, slots=True)
class OkxTradeRecord:
    timing: TimedMarketEvent
    trade: BtcTrade


def normalize_okx_trade(
    payload: Mapping[str, object],
    *,
    collector_receive_ts: datetime | None,
    source: str,
    quantity_multiplier: float = 1.0,
    availability_delay: timedelta = timedelta(0),
) -> OkxTradeRecord:
    """Normalize an OKX `trades` item without assuming swap contract size."""

    _require_source(source)
    if not isfinite(quantity_multiplier) or quantity_multiplier <= 0.0:
        raise ValueError("quantity_multiplier must be finite and > 0")
    if availability_delay < timedelta(0):
        raise ValueError("availability_delay must be non-negative")
    instrument = _text(payload, "instId")
    source_ts = _timestamp_from_millis(payload.get("ts"), "ts")
    receive_ts = _receive_time(collector_receive_ts)
    available_ts = _available_time(source_ts, receive_ts, availability_delay)
    side = _text(payload, "side").casefold()
    if side not in {"buy", "sell"}:
        raise ValueError("OKX trade side must be 'buy' or 'sell'")
    timing = TimedMarketEvent(
        source_ts=source_ts,
        collector_receive_ts=receive_ts,
        available_ts=available_ts,
        sequence_or_hash=_text(payload, "tradeId"),
        source=source,
        instrument=instrument,
        schema_version="okx-trade-v1",
        ingest_version="btc-short-horizon-v1",
    )
    return OkxTradeRecord(
        timing=timing,
        trade=BtcTrade(
            source_ts_ns=_datetime_to_ns(source_ts),
            available_ts_ns=_datetime_to_ns(available_ts),
            price=_positive(payload, "px"),
            quantity=_positive(payload, "sz") * quantity_multiplier,
            aggressor_side=side,
            source=source,
            instrument=instrument,
        ),
    )


def normalize_okx_book_update(
    payload: Mapping[str, object],
    *,
    action: str,
    instrument: str,
    collector_receive_ts: datetime | None,
    source: str,
) -> TimedMarketEvent:
    """Normalize one `books` payload before applying its sequence to local state."""

    _require_source(source)
    if action not in {"snapshot", "update"}:
        raise ValueError("OKX book action must be 'snapshot' or 'update'")
    if not instrument:
        raise ValueError("instrument is required")
    sequence = _nonnegative(payload, "seqId")
    previous_sequence = _signed_integer(payload, "prevSeqId")
    _levels(payload.get("bids"), "bids")
    _levels(payload.get("asks"), "asks")
    source_ts = _timestamp_from_millis(payload.get("ts"), "ts")
    receive_ts = _receive_time(collector_receive_ts)
    return TimedMarketEvent(
        source_ts=source_ts,
        collector_receive_ts=receive_ts,
        available_ts=_available_time(source_ts, receive_ts, timedelta(0)),
        sequence_or_hash=(f"{action}:{previous_sequence}:{sequence}:{_payload_hash(payload)}"),
        source=source,
        instrument=instrument,
        schema_version="okx-books-v2",
        ingest_version="btc-short-horizon-v1",
    )


class OkxBookSynchronizer:
    """Snapshot-gated OKX `books` state verified with seqId/prevSeqId."""

    def __init__(self, *, instrument: str, source: str) -> None:
        if not instrument:
            raise ValueError("instrument is required")
        _require_source(source)
        self.instrument = instrument
        self.source = source
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._last_seq_id: int | None = None
        self._synchronized = False

    @property
    def synchronized(self) -> bool:
        return self._synchronized

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self._last_seq_id = None
        self._synchronized = False

    def apply(
        self,
        *,
        action: str,
        payload: Mapping[str, object],
        collector_receive_ts: datetime | None,
    ) -> DepthApplyResult:
        if action not in {"snapshot", "update"}:
            raise ValueError("OKX book action must be 'snapshot' or 'update'")
        sequence = _nonnegative(payload, "seqId")
        previous_sequence = _signed_integer(payload, "prevSeqId")
        bids = _levels(payload.get("bids"), "bids")
        asks = _levels(payload.get("asks"), "asks")
        source_ts = _timestamp_from_millis(payload.get("ts"), "ts")
        receive_ts = _receive_time(collector_receive_ts)

        if action == "snapshot":
            if previous_sequence != -1:
                self.reset()
                return DepthApplyResult(
                    status=DepthUpdateStatus.GAP,
                    book_top=None,
                    last_update_id=None,
                    reason="snapshot_previous_sequence_not_minus_one",
                )
            candidate_bids: dict[float, float] = {}
            candidate_asks: dict[float, float] = {}
            _apply_levels(candidate_bids, bids)
            _apply_levels(candidate_asks, asks)
            book_top = _book_top(
                bids=candidate_bids,
                asks=candidate_asks,
                source_ts=source_ts,
                receive_ts=receive_ts,
                source=self.source,
                instrument=self.instrument,
            )
            if book_top is None:
                self.reset()
                return DepthApplyResult(
                    status=DepthUpdateStatus.GAP,
                    book_top=None,
                    last_update_id=None,
                    reason="empty_or_crossed_snapshot",
                )
            self._bids = candidate_bids
            self._asks = candidate_asks
            self._last_seq_id = sequence
            self._synchronized = True
            return DepthApplyResult(
                status=DepthUpdateStatus.APPLIED,
                book_top=book_top,
                last_update_id=self._last_seq_id,
            )

        if not self._synchronized or self._last_seq_id is None:
            return DepthApplyResult(
                status=DepthUpdateStatus.AWAITING_SNAPSHOT,
                book_top=None,
                last_update_id=self._last_seq_id,
                reason="snapshot_required",
            )
        if (
            sequence == self._last_seq_id
            and previous_sequence == self._last_seq_id
            and not bids
            and not asks
        ):
            return DepthApplyResult(
                status=DepthUpdateStatus.STALE,
                book_top=None,
                last_update_id=self._last_seq_id,
                reason="heartbeat",
            )
        if previous_sequence != self._last_seq_id:
            last_sequence = self._last_seq_id
            self.reset()
            return DepthApplyResult(
                status=DepthUpdateStatus.GAP,
                book_top=None,
                last_update_id=last_sequence,
                reason="previous_sequence_mismatch",
            )
        if sequence == self._last_seq_id:
            last_sequence = self._last_seq_id
            self.reset()
            return DepthApplyResult(
                status=DepthUpdateStatus.GAP,
                book_top=None,
                last_update_id=last_sequence,
                reason="nonempty_same_sequence",
            )
        _apply_levels(self._bids, bids)
        _apply_levels(self._asks, asks)
        book_top = _book_top(
            bids=self._bids,
            asks=self._asks,
            source_ts=source_ts,
            receive_ts=receive_ts,
            source=self.source,
            instrument=self.instrument,
        )
        if book_top is None:
            last_sequence = self._last_seq_id
            self.reset()
            return DepthApplyResult(
                status=DepthUpdateStatus.GAP,
                book_top=None,
                last_update_id=last_sequence,
                reason="empty_or_crossed_book",
            )
        self._last_seq_id = sequence
        return DepthApplyResult(
            status=DepthUpdateStatus.APPLIED,
            book_top=book_top,
            last_update_id=self._last_seq_id,
        )


def _book_top(
    *,
    bids: Mapping[float, float],
    asks: Mapping[float, float],
    source_ts: datetime,
    receive_ts: datetime | None,
    source: str,
    instrument: str,
) -> BtcBookTop | None:
    if not bids or not asks:
        return None
    bid = max(bids)
    ask = min(asks)
    if bid >= ask:
        return None
    available_ts = _available_time(source_ts, receive_ts, timedelta(0))
    return BtcBookTop(
        source_ts_ns=_datetime_to_ns(source_ts),
        available_ts_ns=_datetime_to_ns(available_ts),
        bid=bid,
        ask=ask,
        bid_size=bids[bid],
        ask_size=asks[ask],
        source=source,
        instrument=instrument,
    )


def _apply_levels(levels: dict[float, float], changes: Sequence[tuple[float, float]]) -> None:
    for price, quantity in changes:
        if quantity == 0.0:
            levels.pop(price, None)
        else:
            levels[price] = quantity


def _levels(value: object, name: str) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"OKX {name} must be an array")
    levels: list[tuple[float, float]] = []
    for row in value:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) < 2:
            raise ValueError(f"OKX {name} levels must have price and size")
        price = _finite(row[0], f"{name}.price", positive=True)
        quantity = _finite(row[1], f"{name}.size", positive=False)
        levels.append((price, quantity))
    return tuple(levels)


def _require_source(source: str) -> None:
    if source not in {"okx_spot", "okx_swap"}:
        raise ValueError("source must be 'okx_spot' or 'okx_swap'")


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"OKX payload requires non-empty {field}")
    return value


def _finite(value: object, name: str, *, positive: bool) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"OKX {name} must be numeric") from exc
    if not isfinite(result) or (result <= 0.0 if positive else result < 0.0):
        qualifier = "> 0" if positive else ">= 0"
        raise ValueError(f"OKX {name} must be finite and {qualifier}")
    return result


def _positive(payload: Mapping[str, object], field: str) -> float:
    return _finite(payload.get(field), field, positive=True)


def _nonnegative(payload: Mapping[str, object], field: str) -> int:
    value = _signed_integer(payload, field)
    if value < 0:
        raise ValueError(f"OKX {field} must be >= 0")
    return value


def _signed_integer(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    try:
        result = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"OKX {field} must be an integer") from exc
    return result


def _timestamp_from_millis(value: object, field: str) -> datetime:
    milliseconds = _nonnegative({field: value}, field)
    return datetime.fromtimestamp(milliseconds / 1_000.0, tz=UTC)


def _receive_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("collector_receive_ts must be timezone-aware")
    return value.astimezone(UTC)


def _available_time(
    source_ts: datetime, receive_ts: datetime | None, availability_delay: timedelta
) -> datetime:
    delayed_source = source_ts + availability_delay
    return delayed_source if receive_ts is None else max(delayed_source, receive_ts)


def _payload_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _datetime_to_ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


__all__ = [
    "OkxBookSynchronizer",
    "OkxTradeRecord",
    "normalize_okx_book_update",
    "normalize_okx_trade",
]
