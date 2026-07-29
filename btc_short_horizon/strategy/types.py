"""Validated, framework-independent types for the opening-mispricing maker strategy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class TokenSide(StrEnum):
    UP = "up"
    DOWN = "down"


class LayerStructure(StrEnum):
    SINGLE = "single"
    TWO_LEVEL = "two_level"
    THREE_LEVEL = "three_level"

    @property
    def allocations(self) -> tuple[float, ...]:
        if self is LayerStructure.SINGLE:
            return (1.0,)
        if self is LayerStructure.TWO_LEVEL:
            return (0.5, 0.5)
        return (0.5, 0.3, 0.2)


def _require_probability(name: str, value: float) -> None:
    if isinstance(value, bool) or not isfinite(value) or not 0.0 < value < 1.0:
        raise ValueError(f"{name} must be finite and in (0, 1), got {value!r}")


def _require_nonnegative(name: str, value: float) -> None:
    if isinstance(value, bool) or not isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and >= 0, got {value!r}")


def _require_positive(name: str, value: float) -> None:
    if isinstance(value, bool) or not isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and > 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class VisibleBookLevel:
    """One validated L2 price level used for capacity-aware passive planning."""

    price: float
    size: float

    def __post_init__(self) -> None:
        _require_probability("price", self.price)
        _require_positive("size", self.size)


@dataclass(frozen=True, slots=True)
class SideBook:
    """The validated L2 view and venue minimum for one outcome token."""

    token_id: str
    bids: tuple[VisibleBookLevel, ...]
    asks: tuple[VisibleBookLevel, ...]
    tick_size: float
    minimum_order_size: float

    def __post_init__(self) -> None:
        if not self.token_id:
            raise ValueError("token_id is required")
        _require_positive("tick_size", self.tick_size)
        _require_positive("minimum_order_size", self.minimum_order_size)
        if not self.bids or not self.asks:
            raise ValueError("both bid and ask depth are required")
        if any(not isinstance(level, VisibleBookLevel) for level in (*self.bids, *self.asks)):
            raise TypeError("bids and asks must contain VisibleBookLevel values")
        bid_prices = tuple(level.price for level in self.bids)
        ask_prices = tuple(level.price for level in self.asks)
        if bid_prices != tuple(sorted(set(bid_prices), reverse=True)):
            raise ValueError("bids must be unique and sorted from highest to lowest")
        if ask_prices != tuple(sorted(set(ask_prices))):
            raise ValueError("asks must be unique and sorted from lowest to highest")
        for price in (*bid_prices, *ask_prices):
            ticks = price / self.tick_size
            if abs(ticks - round(ticks)) > 1e-8:
                raise ValueError("every book price must conform to tick_size")
        if self.best_bid > self.best_ask:
            raise ValueError("best_bid must not exceed best_ask")

    @property
    def best_bid(self) -> float:
        return self.bids[0].price

    @property
    def best_ask(self) -> float:
        return self.asks[0].price

    @property
    def midpoint(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    def visible_bid_size_at(self, price: float) -> float:
        return next((level.size for level in self.bids if abs(level.price - price) <= 1e-12), 0.0)


@dataclass(frozen=True, slots=True)
class OutcomeBooks:
    """Both mutually exclusive outcome books for one binary market."""

    up: SideBook
    down: SideBook

    def __post_init__(self) -> None:
        if self.up.token_id == self.down.token_id:
            raise ValueError("up and down token IDs must differ")

    def for_side(self, side: TokenSide) -> SideBook:
        return self.up if side is TokenSide.UP else self.down

    @property
    def implied_up_midpoint(self) -> float:
        return (self.up.midpoint + 1.0 - self.down.midpoint) / 2.0


@dataclass(frozen=True, slots=True)
class MakerOrderLayer:
    price: float
    size: float
    visible_size: float

    def __post_init__(self) -> None:
        _require_probability("price", self.price)
        _require_positive("size", self.size)
        _require_positive("visible_size", self.visible_size)
        if self.size > self.visible_size + 1e-12:
            raise ValueError("order size cannot exceed visible same-side depth")

    @property
    def notional(self) -> float:
        return self.price * self.size

    @property
    def visible_depth_fraction(self) -> float:
        return self.size / self.visible_size


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """One passive order cycle selected by fair probability versus executable price."""

    market_slug: str
    token_id: str
    side: TokenSide
    p_boundary: float
    p_fair: float
    p_market: float
    safety_buffer: float
    minimum_edge: float
    created_ts_ns: int
    expires_ts_ns: int
    layers: tuple[MakerOrderLayer, ...]

    def __post_init__(self) -> None:
        if not self.market_slug or not self.token_id:
            raise ValueError("market_slug and token_id are required")
        _require_probability("p_boundary", self.p_boundary)
        _require_probability("p_fair", self.p_fair)
        _require_probability("p_market", self.p_market)
        _require_nonnegative("safety_buffer", self.safety_buffer)
        _require_nonnegative("minimum_edge", self.minimum_edge)
        if self.created_ts_ns < 0 or self.expires_ts_ns <= self.created_ts_ns:
            raise ValueError("order plan timestamps must be ordered and non-negative")
        if not self.layers:
            raise ValueError("order plan requires at least one layer")
        if any(self.net_edge(layer.price) + 1e-12 < self.minimum_edge for layer in self.layers):
            raise ValueError("every layer must satisfy the configured executable edge")

    @property
    def total_size(self) -> float:
        return sum(layer.size for layer in self.layers)

    @property
    def total_notional(self) -> float:
        return sum(layer.notional for layer in self.layers)

    @property
    def model_edge(self) -> float:
        return self.p_fair - self.p_market

    def net_edge(self, price: float) -> float:
        return self.p_fair - price - self.safety_buffer
