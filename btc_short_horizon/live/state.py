"""Immutable live order and trade lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite


class LiveOrderStatus(StrEnum):
    CREATED = "created"
    SIGNED = "signed"
    SUBMITTED = "submitted"
    LIVE = "live"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    NOT_CANCELED = "not_canceled"
    FILLED = "filled"


class LiveOrderFillStatus(StrEnum):
    UNFILLED = "unfilled"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"


class LiveTradeStatus(StrEnum):
    MATCHED = "matched"
    MINED = "mined"
    CONFIRMED = "confirmed"
    RETRYING = "retrying"
    FAILED = "failed"


_ORDER_TRANSITIONS: dict[LiveOrderStatus, frozenset[LiveOrderStatus]] = {
    LiveOrderStatus.CREATED: frozenset({LiveOrderStatus.SIGNED, LiveOrderStatus.REJECTED}),
    LiveOrderStatus.SIGNED: frozenset({LiveOrderStatus.SUBMITTED, LiveOrderStatus.REJECTED}),
    LiveOrderStatus.SUBMITTED: frozenset(
        {
            LiveOrderStatus.LIVE,
            LiveOrderStatus.REJECTED,
            LiveOrderStatus.UNKNOWN,
            LiveOrderStatus.CANCELED,
            LiveOrderStatus.FILLED,
        }
    ),
    LiveOrderStatus.LIVE: frozenset(
        {
            LiveOrderStatus.CANCEL_REQUESTED,
            LiveOrderStatus.CANCELED,
            LiveOrderStatus.NOT_CANCELED,
            LiveOrderStatus.UNKNOWN,
            LiveOrderStatus.FILLED,
        }
    ),
    LiveOrderStatus.CANCEL_REQUESTED: frozenset(
        {
            LiveOrderStatus.CANCELED,
            LiveOrderStatus.NOT_CANCELED,
            LiveOrderStatus.UNKNOWN,
            LiveOrderStatus.FILLED,
        }
    ),
    LiveOrderStatus.NOT_CANCELED: frozenset(
        {
            LiveOrderStatus.CANCEL_REQUESTED,
            LiveOrderStatus.CANCELED,
            LiveOrderStatus.UNKNOWN,
            LiveOrderStatus.FILLED,
        }
    ),
    LiveOrderStatus.UNKNOWN: frozenset(
        {
            LiveOrderStatus.LIVE,
            LiveOrderStatus.CANCELED,
            LiveOrderStatus.REJECTED,
            LiveOrderStatus.FILLED,
        }
    ),
    LiveOrderStatus.REJECTED: frozenset(),
    LiveOrderStatus.CANCELED: frozenset({LiveOrderStatus.FILLED}),
    LiveOrderStatus.FILLED: frozenset(),
}

_TRADE_TRANSITIONS: dict[LiveTradeStatus, frozenset[LiveTradeStatus]] = {
    LiveTradeStatus.MATCHED: frozenset(
        {
            LiveTradeStatus.MINED,
            LiveTradeStatus.CONFIRMED,
            LiveTradeStatus.RETRYING,
            LiveTradeStatus.FAILED,
        }
    ),
    LiveTradeStatus.MINED: frozenset(
        {LiveTradeStatus.CONFIRMED, LiveTradeStatus.RETRYING, LiveTradeStatus.FAILED}
    ),
    LiveTradeStatus.RETRYING: frozenset(
        {LiveTradeStatus.MINED, LiveTradeStatus.CONFIRMED, LiveTradeStatus.FAILED}
    ),
    LiveTradeStatus.CONFIRMED: frozenset(),
    LiveTradeStatus.FAILED: frozenset(),
}


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _require_nonnegative(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be finite and >= 0")
    if not isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and >= 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class LiveOrder:
    client_order_id: str
    market_id: str
    token_id: str
    price: float
    size: float
    status: LiveOrderStatus = LiveOrderStatus.CREATED
    venue_order_id: str | None = None
    expected_venue_order_id: str | None = None
    order_channel_matched_size: float = 0.0
    trade_matched_size: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("client_order_id", self.client_order_id),
            ("market_id", self.market_id),
            ("token_id", self.token_id),
        ):
            _require_nonempty(name, value)
        if isinstance(self.price, bool) or not isinstance(self.price, int | float):
            raise ValueError("price must be finite and in (0, 1)")
        if not isfinite(self.price) or not 0.0 < self.price < 1.0:
            raise ValueError(f"price must be finite and in (0, 1), got {self.price!r}")
        _require_nonnegative("size", self.size)
        _require_nonnegative("order_channel_matched_size", self.order_channel_matched_size)
        _require_nonnegative("trade_matched_size", self.trade_matched_size)
        if self.size <= 0.0:
            raise ValueError("size must be > 0")
        if self.matched_size > self.size + 1e-12:
            raise ValueError("matched size must not exceed order size")
        if self.venue_order_id is not None:
            _require_nonempty("venue_order_id", self.venue_order_id)
        if self.expected_venue_order_id is not None:
            _require_nonempty("expected_venue_order_id", self.expected_venue_order_id)
        if (
            self.venue_order_id is not None
            and self.expected_venue_order_id is not None
            and self.venue_order_id.casefold() != self.expected_venue_order_id.casefold()
        ):
            raise ValueError("venue_order_id does not match the expected signed order ID")
        if not isinstance(self.status, LiveOrderStatus):
            object.__setattr__(self, "status", LiveOrderStatus(self.status))
        if self.status is LiveOrderStatus.FILLED and self.matched_size < self.size - 1e-12:
            raise ValueError("filled order must have its complete matched size")

    @property
    def matched_size(self) -> float:
        return max(self.order_channel_matched_size, self.trade_matched_size)

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.size - self.matched_size)

    @property
    def fill_status(self) -> LiveOrderFillStatus:
        if self.matched_size <= 1e-12:
            return LiveOrderFillStatus.UNFILLED
        if self.matched_size >= self.size - 1e-12:
            return LiveOrderFillStatus.FILLED
        return LiveOrderFillStatus.PARTIALLY_FILLED

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            LiveOrderStatus.REJECTED,
            LiveOrderStatus.CANCELED,
            LiveOrderStatus.FILLED,
        }

    def transition(
        self, status: LiveOrderStatus, *, venue_order_id: str | None = None
    ) -> LiveOrder:
        if not isinstance(status, LiveOrderStatus):
            status = LiveOrderStatus(status)
        if status == self.status:
            return self
        if status not in _ORDER_TRANSITIONS[self.status]:
            raise ValueError(f"invalid order transition: {self.status} -> {status}")
        if status is LiveOrderStatus.FILLED and self.fill_status is not LiveOrderFillStatus.FILLED:
            raise ValueError("order cannot transition to filled before its full size is matched")
        return replace(self, status=status, venue_order_id=venue_order_id or self.venue_order_id)

    def expect_venue_order_id(self, venue_order_id: str) -> LiveOrder:
        _require_nonempty("expected_venue_order_id", venue_order_id)
        if (
            self.expected_venue_order_id is not None
            and self.expected_venue_order_id.casefold() != venue_order_id.casefold()
        ):
            raise ValueError("expected venue order ID cannot change")
        if (
            self.venue_order_id is not None
            and self.venue_order_id.casefold() != venue_order_id.casefold()
        ):
            raise ValueError("expected venue order ID does not match the venue order")
        return replace(self, expected_venue_order_id=venue_order_id)

    def record_order_cumulative_match(self, matched_size: float) -> LiveOrder:
        return self._record_cumulative("order_channel_matched_size", matched_size)

    def record_trade_cumulative_match(self, matched_size: float) -> LiveOrder:
        return self._record_cumulative("trade_matched_size", matched_size)

    def _record_cumulative(self, field_name: str, matched_size: float) -> LiveOrder:
        _require_nonnegative(field_name, matched_size)
        current = getattr(self, field_name)
        if matched_size < current - 1e-12:
            raise ValueError(f"{field_name} cannot decrease")
        if matched_size > self.size + 1e-12:
            raise ValueError("cumulative match would exceed order size")
        updated = replace(self, **{field_name: min(self.size, matched_size)})
        if (
            updated.fill_status is LiveOrderFillStatus.FILLED
            and updated.status is not LiveOrderStatus.FILLED
        ):
            updated = updated.transition(LiveOrderStatus.FILLED)
        return updated


@dataclass(frozen=True, slots=True)
class LiveTrade:
    trade_id: str
    client_order_id: str
    price: float
    size: float
    status: LiveTradeStatus = LiveTradeStatus.MATCHED
    transaction_hash: str | None = None
    match_time_ns: int | None = None
    last_update_ns: int | None = None

    def __post_init__(self) -> None:
        _require_nonempty("trade_id", self.trade_id)
        _require_nonempty("client_order_id", self.client_order_id)
        if isinstance(self.price, bool) or not isinstance(self.price, int | float):
            raise ValueError("price must be finite and in (0, 1)")
        if not isfinite(self.price) or not 0.0 < self.price < 1.0:
            raise ValueError(f"price must be finite and in (0, 1), got {self.price!r}")
        if isinstance(self.size, bool) or not isinstance(self.size, int | float):
            raise ValueError("size must be finite and > 0")
        if not isfinite(self.size) or self.size <= 0.0:
            raise ValueError(f"size must be finite and > 0, got {self.size!r}")
        if not isinstance(self.status, LiveTradeStatus):
            object.__setattr__(self, "status", LiveTradeStatus(self.status))
        for name in ("match_time_ns", "last_update_ns"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if (self.match_time_ns is None) != (self.last_update_ns is None):
            raise ValueError("trade match and update timestamps must be provided together")
        if (
            self.match_time_ns is not None
            and self.last_update_ns is not None
            and self.last_update_ns < self.match_time_ns
        ):
            raise ValueError("trade last_update_ns cannot precede match_time_ns")

    @property
    def is_terminal(self) -> bool:
        return self.status in {LiveTradeStatus.CONFIRMED, LiveTradeStatus.FAILED}

    def transition(
        self,
        status: LiveTradeStatus,
        *,
        transaction_hash: str | None = None,
        last_update_ns: int | None = None,
    ) -> LiveTrade:
        if not isinstance(status, LiveTradeStatus):
            status = LiveTradeStatus(status)
        if last_update_ns is not None:
            if (
                isinstance(last_update_ns, bool)
                or not isinstance(last_update_ns, int)
                or last_update_ns < 0
            ):
                raise ValueError("last_update_ns must be a non-negative integer")
            if self.last_update_ns is not None and last_update_ns < self.last_update_ns:
                raise ValueError("trade last_update_ns cannot decrease")
        next_last_update_ns = self.last_update_ns if last_update_ns is None else last_update_ns
        if status == self.status:
            return replace(
                self,
                transaction_hash=transaction_hash or self.transaction_hash,
                last_update_ns=next_last_update_ns,
            )
        if status not in _TRADE_TRANSITIONS[self.status]:
            raise ValueError(f"invalid trade transition: {self.status} -> {status}")
        return replace(
            self,
            status=status,
            transaction_hash=transaction_hash or self.transaction_hash,
            last_update_ns=next_last_update_ns,
        )
