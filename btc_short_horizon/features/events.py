from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Literal

AggressorSide = Literal["buy", "sell", "unknown"]


def _require_timestamp(name: str, value: int) -> None:
    if int(value) < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")


def _require_positive(name: str, value: float) -> None:
    if not isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and > 0, got {value!r}")


@dataclass(frozen=True, slots=True)
class BtcTrade:
    source_ts_ns: int
    available_ts_ns: int
    price: float
    quantity: float
    aggressor_side: AggressorSide = "unknown"
    source: str = "binance_spot"
    instrument: str = "BTCUSDT"

    def __post_init__(self) -> None:
        _require_timestamp("source_ts_ns", self.source_ts_ns)
        _require_timestamp("available_ts_ns", self.available_ts_ns)
        _require_positive("price", self.price)
        _require_positive("quantity", self.quantity)
        if self.aggressor_side not in {"buy", "sell", "unknown"}:
            raise ValueError(f"unsupported aggressor_side={self.aggressor_side!r}")
        if not self.source or not self.instrument:
            raise ValueError("source and instrument are required")


@dataclass(frozen=True, slots=True)
class BtcBookTop:
    source_ts_ns: int
    available_ts_ns: int
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    source: str = "binance_spot"
    instrument: str = "BTCUSDT"

    def __post_init__(self) -> None:
        _require_timestamp("source_ts_ns", self.source_ts_ns)
        _require_timestamp("available_ts_ns", self.available_ts_ns)
        _require_positive("bid", self.bid)
        _require_positive("ask", self.ask)
        _require_positive("bid_size", self.bid_size)
        _require_positive("ask_size", self.ask_size)
        if self.ask < self.bid:
            raise ValueError(f"ask must be >= bid, got ask={self.ask}, bid={self.bid}")
        if not self.source or not self.instrument:
            raise ValueError("source and instrument are required")


@dataclass(frozen=True, slots=True)
class BtcReferencePrice:
    source_ts_ns: int
    available_ts_ns: int
    price: float
    source: str = "chainlink"
    instrument: str = "btc/usd"

    def __post_init__(self) -> None:
        _require_timestamp("source_ts_ns", self.source_ts_ns)
        _require_timestamp("available_ts_ns", self.available_ts_ns)
        _require_positive("price", self.price)
        if not self.source or not self.instrument:
            raise ValueError("source and instrument are required")
