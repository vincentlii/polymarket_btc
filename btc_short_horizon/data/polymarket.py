"""Public Polymarket CLOB market-channel L2 normalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
from typing import Mapping, Sequence

from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.features.events import BtcBookTop, BtcTrade


class PolymarketL2Status(StrEnum):
    APPLIED = "applied"
    IGNORED = "ignored"
    AWAITING_SNAPSHOT = "awaiting_snapshot"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class PolymarketL2Result:
    status: PolymarketL2Status
    timing: TimedMarketEvent | None
    event_type: str
    book_top: BtcBookTop | None = None
    trade: BtcTrade | None = None
    tick_size: float | None = None
    tick_size_changed: bool = False
    reason: str | None = None


class PolymarketL2Normalizer:
    """Maintains per-token L2 state from public CLOB market-channel events."""

    def __init__(
        self,
        *,
        token_id: str,
        source: str = "polymarket_clob",
        source_timestamp_regression_tolerance: timedelta = timedelta(seconds=1),
    ) -> None:
        if not token_id:
            raise ValueError("token_id is required")
        if source_timestamp_regression_tolerance < timedelta(0):
            raise ValueError("source_timestamp_regression_tolerance must be non-negative")
        self.token_id = token_id
        self.source = source
        self.source_timestamp_regression_tolerance = source_timestamp_regression_tolerance
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._has_snapshot = False
        self._tick_size: float | None = None
        self._last_available_ts: datetime | None = None
        self._last_source_ts: datetime | None = None

    @property
    def tick_size(self) -> float | None:
        return self._tick_size

    def reset(self) -> None:
        """Discard L2 state after a connection gap until a fresh snapshot arrives."""

        self._bids.clear()
        self._asks.clear()
        self._has_snapshot = False
        self._tick_size = None
        self._last_available_ts = None
        self._last_source_ts = None

    def apply(
        self, payload: Mapping[str, object], *, collector_receive_ts: datetime
    ) -> PolymarketL2Result:
        """Apply one market-channel payload and preserve source/receive timing."""

        receive_ts = _as_utc(collector_receive_ts, "collector_receive_ts")
        event_type = _text(payload.get("event_type"), "event_type")
        if event_type == "book":
            return self._apply_book(payload, receive_ts=receive_ts)
        if event_type == "price_change":
            return self._apply_price_change(payload, receive_ts=receive_ts)
        if event_type == "last_trade_price":
            return self._apply_trade(payload, receive_ts=receive_ts)
        if event_type == "tick_size_change":
            return self._apply_tick_size_change(payload, receive_ts=receive_ts)
        if event_type in {"new_market", "market_resolved"}:
            return self._apply_market_metadata(
                payload, receive_ts=receive_ts, event_type=event_type
            )
        return PolymarketL2Result(
            status=PolymarketL2Status.IGNORED,
            timing=self._timing(payload, receive_ts=receive_ts),
            event_type=event_type,
            reason="unsupported_event_type",
        )

    def _apply_book(
        self, payload: Mapping[str, object], *, receive_ts: datetime
    ) -> PolymarketL2Result:
        if _text(payload.get("asset_id"), "asset_id") != self.token_id:
            return self._ignored(payload, receive_ts, "other_token")
        self._bids = dict(_levels(payload.get("bids", ())))
        self._asks = dict(_levels(payload.get("asks", ())))
        self._has_snapshot = True
        timing = self._timing(payload, receive_ts=receive_ts)
        top = self._book_top(timing)
        if top is None:
            self._has_snapshot = False
            return PolymarketL2Result(
                status=PolymarketL2Status.INVALID,
                timing=timing,
                event_type="book",
                reason="empty_or_crossed_snapshot",
            )
        self._accept_timing(timing)
        return PolymarketL2Result(
            status=PolymarketL2Status.APPLIED,
            timing=timing,
            event_type="book",
            book_top=top,
            tick_size=self._tick_size,
        )

    def _apply_price_change(
        self, payload: Mapping[str, object], *, receive_ts: datetime
    ) -> PolymarketL2Result:
        timing = self._timing(payload, receive_ts=receive_ts)
        if not self._has_snapshot:
            return PolymarketL2Result(
                status=PolymarketL2Status.AWAITING_SNAPSHOT,
                timing=timing,
                event_type="price_change",
                reason="snapshot_required",
            )
        changes = payload.get("price_changes")
        if not isinstance(changes, Sequence) or isinstance(changes, str | bytes):
            raise ValueError("price_change payload requires price_changes sequence")
        matched = 0
        for change in changes:
            if not isinstance(change, Mapping):
                raise ValueError("each price change must be an object")
            if _text(change.get("asset_id"), "asset_id") != self.token_id:
                continue
            matched += 1
            side = _text(change.get("side"), "side").upper()
            if side not in {"BUY", "SELL"}:
                raise ValueError("price change side must be BUY or SELL")
            levels = self._bids if side == "BUY" else self._asks
            price = _probability(change.get("price"), "price")
            size = _nonnegative_float(change.get("size"), "size")
            if size == 0.0:
                levels.pop(price, None)
            else:
                levels[price] = size
        if matched == 0:
            return self._ignored(payload, receive_ts, "other_token")
        top = self._book_top(timing)
        if top is None:
            self._has_snapshot = False
            return PolymarketL2Result(
                status=PolymarketL2Status.INVALID,
                timing=timing,
                event_type="price_change",
                reason="empty_or_crossed_book",
            )
        self._accept_timing(timing)
        return PolymarketL2Result(
            status=PolymarketL2Status.APPLIED,
            timing=timing,
            event_type="price_change",
            book_top=top,
            tick_size=self._tick_size,
        )

    def _apply_trade(
        self, payload: Mapping[str, object], *, receive_ts: datetime
    ) -> PolymarketL2Result:
        if _text(payload.get("asset_id"), "asset_id") != self.token_id:
            return self._ignored(payload, receive_ts, "other_token")
        timing = self._timing(payload, receive_ts=receive_ts)
        side = _text(payload.get("side"), "side").casefold()
        if side not in {"buy", "sell"}:
            raise ValueError("last_trade_price side must be BUY or SELL")
        self._accept_timing(timing)
        return PolymarketL2Result(
            status=PolymarketL2Status.APPLIED,
            timing=timing,
            event_type="last_trade_price",
            trade=BtcTrade(
                source_ts_ns=_datetime_to_ns(timing.source_ts),
                available_ts_ns=_datetime_to_ns(timing.available_ts),
                price=_probability(payload.get("price"), "price"),
                quantity=_positive_float(payload.get("size"), "size"),
                aggressor_side=side,
                source=self.source,
                instrument=self.token_id,
            ),
            tick_size=self._tick_size,
        )

    def _apply_tick_size_change(
        self, payload: Mapping[str, object], *, receive_ts: datetime
    ) -> PolymarketL2Result:
        asset_id = payload.get("asset_id")
        if asset_id is not None and _text(asset_id, "asset_id") != self.token_id:
            return self._ignored(payload, receive_ts, "other_token")
        timing = self._timing(payload, receive_ts=receive_ts)
        new_tick_size = _probability(payload.get("new_tick_size"), "new_tick_size")
        changed = self._tick_size is not None and self._tick_size != new_tick_size
        self._tick_size = new_tick_size
        self._accept_timing(timing)
        return PolymarketL2Result(
            status=PolymarketL2Status.APPLIED,
            timing=timing,
            event_type="tick_size_change",
            tick_size=new_tick_size,
            tick_size_changed=changed,
        )

    def _apply_market_metadata(
        self, payload: Mapping[str, object], *, receive_ts: datetime, event_type: str
    ) -> PolymarketL2Result:
        if not _metadata_references_token(payload, self.token_id):
            return self._ignored(payload, receive_ts, "other_token")
        timing = self._timing(payload, receive_ts=receive_ts)
        self._accept_timing(timing)
        return PolymarketL2Result(
            status=PolymarketL2Status.APPLIED,
            timing=timing,
            event_type=event_type,
        )

    def _ignored(
        self, payload: Mapping[str, object], receive_ts: datetime, reason: str
    ) -> PolymarketL2Result:
        return PolymarketL2Result(
            status=PolymarketL2Status.IGNORED,
            timing=self._timing(payload, receive_ts=receive_ts),
            event_type=_text(payload.get("event_type"), "event_type"),
            reason=reason,
        )

    def _timing(self, payload: Mapping[str, object], *, receive_ts: datetime) -> TimedMarketEvent:
        source_ts = _timestamp_millis(payload.get("timestamp"), "timestamp")
        base_available_ts = max(source_ts, receive_ts)
        if self._has_material_source_timestamp_regression(source_ts):
            available_ts = base_available_ts
        else:
            available_ts = max(
                base_available_ts,
                self._last_available_ts or base_available_ts,
            )
        return TimedMarketEvent(
            source_ts=source_ts,
            collector_receive_ts=receive_ts,
            available_ts=available_ts,
            sequence_or_hash=str(payload.get("hash") or _payload_hash(payload)),
            source=self.source,
            instrument=self.token_id,
            schema_version="polymarket-market-ws-v1",
            ingest_version="btc-short-horizon-v1",
        )

    def _accept_timing(self, timing: TimedMarketEvent) -> None:
        """Advance only on an applied token event; small source-clock jitter may delay, never rewind."""

        self._last_available_ts = timing.available_ts
        self._last_source_ts = timing.source_ts

    def _has_material_source_timestamp_regression(self, source_ts: datetime) -> bool:
        if self._last_source_ts is None:
            return False
        return source_ts + self.source_timestamp_regression_tolerance <= self._last_source_ts

    def _book_top(self, timing: TimedMarketEvent) -> BtcBookTop | None:
        if not self._bids or not self._asks:
            return None
        bid = max(self._bids)
        ask = min(self._asks)
        if bid >= ask:
            return None
        return BtcBookTop(
            source_ts_ns=_datetime_to_ns(timing.source_ts),
            available_ts_ns=_datetime_to_ns(timing.available_ts),
            bid=bid,
            ask=ask,
            bid_size=self._bids[bid],
            ask_size=self._asks[ask],
            source=self.source,
            instrument=self.token_id,
        )


def _levels(value: object) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError("book levels must be a sequence")
    levels: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Polymarket book level must be an object")
        levels.append(
            (
                _probability(item.get("price"), "price"),
                _nonnegative_float(item.get("size"), "size"),
            )
        )
    return tuple(levels)


def _metadata_references_token(payload: Mapping[str, object], token_id: str) -> bool:
    asset_id = payload.get("asset_id")
    if asset_id is not None:
        return _text(asset_id, "asset_id") == token_id
    for key in ("assets_ids", "clob_token_ids"):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            if any(item == token_id for item in value):
                return True
    return False


def _payload_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(encoded).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _number(value: object, name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(numeric):
        raise ValueError(f"{name} must be finite")
    return numeric


def _probability(value: object, name: str) -> float:
    numeric = _number(value, name)
    if not 0.0 < numeric < 1.0:
        raise ValueError(f"{name} must be in (0, 1)")
    return numeric


def _positive_float(value: object, name: str) -> float:
    numeric = _number(value, name)
    if numeric <= 0.0:
        raise ValueError(f"{name} must be > 0")
    return numeric


def _nonnegative_float(value: object, name: str) -> float:
    numeric = _number(value, name)
    if numeric < 0.0:
        raise ValueError(f"{name} must be >= 0")
    return numeric


def _timestamp_millis(value: object, name: str) -> datetime:
    try:
        millis = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be epoch milliseconds") from exc
    if millis < 0:
        raise ValueError(f"{name} must be non-negative")
    return datetime.fromtimestamp(millis / 1_000, UTC)


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _datetime_to_ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)
