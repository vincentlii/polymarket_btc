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
        {LiveOrderStatus.LIVE, LiveOrderStatus.REJECTED, LiveOrderStatus.UNKNOWN}
    ),
    LiveOrderStatus.LIVE: frozenset(
        {
            LiveOrderStatus.CANCEL_REQUESTED,
            LiveOrderStatus.CANCELED,
            LiveOrderStatus.NOT_CANCELED,
            LiveOrderStatus.UNKNOWN,
        }
    ),
    LiveOrderStatus.CANCEL_REQUESTED: frozenset(
        {LiveOrderStatus.CANCELED, LiveOrderStatus.NOT_CANCELED, LiveOrderStatus.UNKNOWN}
    ),
    LiveOrderStatus.NOT_CANCELED: frozenset(
        {LiveOrderStatus.CANCEL_REQUESTED, LiveOrderStatus.CANCELED, LiveOrderStatus.UNKNOWN}
    ),
    LiveOrderStatus.UNKNOWN: frozenset(
        {LiveOrderStatus.LIVE, LiveOrderStatus.CANCELED, LiveOrderStatus.REJECTED}
    ),
    LiveOrderStatus.REJECTED: frozenset(),
    LiveOrderStatus.CANCELED: frozenset(),
}

_TRADE_TRANSITIONS: dict[LiveTradeStatus, frozenset[LiveTradeStatus]] = {
    LiveTradeStatus.MATCHED: frozenset(
        {LiveTradeStatus.MINED, LiveTradeStatus.RETRYING, LiveTradeStatus.FAILED}
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
    if not value:
        raise ValueError(f"{name} is required")


def _require_nonnegative(name: str, value: float) -> None:
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
    matched_size: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("client_order_id", self.client_order_id),
            ("market_id", self.market_id),
            ("token_id", self.token_id),
        ):
            _require_nonempty(name, value)
        if not isfinite(self.price) or not 0.0 < self.price < 1.0:
            raise ValueError(f"price must be finite and in (0, 1), got {self.price!r}")
        _require_nonnegative("size", self.size)
        _require_nonnegative("matched_size", self.matched_size)
        if self.size <= 0.0:
            raise ValueError("size must be > 0")
        if self.matched_size > self.size:
            raise ValueError("matched_size must not exceed size")

    @property
    def remaining_size(self) -> float:
        return self.size - self.matched_size

    def transition(
        self, status: LiveOrderStatus, *, venue_order_id: str | None = None
    ) -> LiveOrder:
        if status == self.status:
            return self
        if status not in _ORDER_TRANSITIONS[self.status]:
            raise ValueError(f"invalid order transition: {self.status} -> {status}")
        return replace(self, status=status, venue_order_id=venue_order_id or self.venue_order_id)

    def record_match(self, matched_size: float) -> LiveOrder:
        _require_nonnegative("matched_size", matched_size)
        new_matched_size = self.matched_size + matched_size
        if new_matched_size > self.size + 1e-12:
            raise ValueError("match would exceed order size")
        return replace(self, matched_size=min(self.size, new_matched_size))


@dataclass(frozen=True, slots=True)
class LiveTrade:
    trade_id: str
    client_order_id: str
    price: float
    size: float
    status: LiveTradeStatus = LiveTradeStatus.MATCHED
    transaction_hash: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty("trade_id", self.trade_id)
        _require_nonempty("client_order_id", self.client_order_id)
        if not isfinite(self.price) or not 0.0 < self.price < 1.0:
            raise ValueError(f"price must be finite and in (0, 1), got {self.price!r}")
        if not isfinite(self.size) or self.size <= 0.0:
            raise ValueError(f"size must be finite and > 0, got {self.size!r}")

    @property
    def is_terminal(self) -> bool:
        return self.status in {LiveTradeStatus.CONFIRMED, LiveTradeStatus.FAILED}

    def transition(
        self, status: LiveTradeStatus, *, transaction_hash: str | None = None
    ) -> LiveTrade:
        if status == self.status:
            return self
        if status not in _TRADE_TRANSITIONS[self.status]:
            raise ValueError(f"invalid trade transition: {self.status} -> {status}")
        return replace(
            self, status=status, transaction_hash=transaction_hash or self.transaction_hash
        )
