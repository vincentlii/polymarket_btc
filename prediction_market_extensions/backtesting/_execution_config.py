from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from nautilus_trader.backtest.models import LatencyModel

_NANOS_PER_MILLISECOND = 1_000_000


class SameTimestampPriority(StrEnum):
    """Deterministic ordering for book/trade evidence with equal ``ts_init``."""

    BOOK_BEFORE_TRADE = "book_before_trade"
    TRADE_BEFORE_BOOK = "trade_before_book"


def _validate_milliseconds(*, name: str, value: float) -> None:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric")
    if not isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value!r}")


def _milliseconds_to_nanos(value: float) -> int:
    return int(round(value * _NANOS_PER_MILLISECOND))


@dataclass(frozen=True)
class StaticLatencyConfig:
    base_latency_ms: float = 0.0
    insert_latency_ms: float = 0.0
    update_latency_ms: float = 0.0
    cancel_latency_ms: float = 0.0

    def __post_init__(self) -> None:
        _validate_milliseconds(name="base_latency_ms", value=self.base_latency_ms)
        _validate_milliseconds(name="insert_latency_ms", value=self.insert_latency_ms)
        _validate_milliseconds(name="update_latency_ms", value=self.update_latency_ms)
        _validate_milliseconds(name="cancel_latency_ms", value=self.cancel_latency_ms)

    def build_latency_model(self) -> LatencyModel | None:
        if (
            self.base_latency_ms == 0.0
            and self.insert_latency_ms == 0.0
            and self.update_latency_ms == 0.0
            and self.cancel_latency_ms == 0.0
        ):
            return None

        return LatencyModel(
            base_latency_nanos=_milliseconds_to_nanos(self.base_latency_ms),
            insert_latency_nanos=_milliseconds_to_nanos(self.insert_latency_ms),
            update_latency_nanos=_milliseconds_to_nanos(self.update_latency_ms),
            cancel_latency_nanos=_milliseconds_to_nanos(self.cancel_latency_ms),
        )


@dataclass(frozen=True)
class ExecutionModelConfig:
    queue_position: bool = False
    latency_model: StaticLatencyConfig | None = None
    maker_rebates_enabled: bool = True
    trade_execution_size_multiplier: float = 1.0
    same_timestamp_priority: SameTimestampPriority = SameTimestampPriority.BOOK_BEFORE_TRADE
    slippage_ticks: int = 1
    entry_slippage_pct: float = 0.0
    exit_slippage_pct: float = 0.0
    prob_fill_on_limit: float = 0.25
    min_synthetic_book_size: float = 10.0
    synthetic_book_depth_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.queue_position, bool):
            raise TypeError("queue_position must be bool")
        if not isinstance(self.maker_rebates_enabled, bool):
            raise TypeError("maker_rebates_enabled must be bool")
        if not isinstance(self.same_timestamp_priority, SameTimestampPriority):
            object.__setattr__(
                self,
                "same_timestamp_priority",
                SameTimestampPriority(self.same_timestamp_priority),
            )
        if (
            isinstance(self.trade_execution_size_multiplier, bool)
            or not isfinite(self.trade_execution_size_multiplier)
            or not 0.0 < self.trade_execution_size_multiplier <= 1.0
        ):
            raise ValueError("trade_execution_size_multiplier must be finite and within (0.0, 1.0]")
        if isinstance(self.slippage_ticks, bool) or not isinstance(self.slippage_ticks, int):
            raise TypeError("slippage_ticks must be an integer")
        if self.slippage_ticks < 0:
            raise ValueError(f"slippage_ticks must be >= 0, got {self.slippage_ticks}")
        for name, value in (
            ("entry_slippage_pct", self.entry_slippage_pct),
            ("exit_slippage_pct", self.exit_slippage_pct),
            ("prob_fill_on_limit", self.prob_fill_on_limit),
        ):
            if isinstance(value, bool) or not isfinite(value):
                raise ValueError(f"{name} must be finite and numeric")
        if self.entry_slippage_pct < 0.0:
            raise ValueError(f"entry_slippage_pct must be >= 0, got {self.entry_slippage_pct}")
        if self.entry_slippage_pct > 1.0:
            raise ValueError(
                f"entry_slippage_pct must be within [0.0, 1.0], got {self.entry_slippage_pct}"
            )
        if self.exit_slippage_pct < 0.0:
            raise ValueError(f"exit_slippage_pct must be >= 0, got {self.exit_slippage_pct}")
        if self.exit_slippage_pct > 1.0:
            raise ValueError(
                f"exit_slippage_pct must be within [0.0, 1.0], got {self.exit_slippage_pct}"
            )
        if not 0.0 <= self.prob_fill_on_limit <= 1.0:
            raise ValueError(
                f"prob_fill_on_limit must be within [0.0, 1.0], got {self.prob_fill_on_limit}"
            )
        if (
            isinstance(self.min_synthetic_book_size, bool)
            or not isfinite(self.min_synthetic_book_size)
            or self.min_synthetic_book_size <= 0.0
        ):
            raise ValueError(
                "min_synthetic_book_size must be finite and > 0.0, got "
                f"{self.min_synthetic_book_size}"
            )
        if (
            isinstance(self.synthetic_book_depth_multiplier, bool)
            or not isfinite(self.synthetic_book_depth_multiplier)
            or self.synthetic_book_depth_multiplier <= 0.0
        ):
            raise ValueError(
                "synthetic_book_depth_multiplier must be finite and > 0.0, got "
                f"{self.synthetic_book_depth_multiplier}"
            )

    def build_latency_model(self) -> LatencyModel | None:
        if self.latency_model is None:
            return None
        return self.latency_model.build_latency_model()

    def build_fill_model_kwargs(self) -> dict[str, int | float]:
        kwargs: dict[str, int | float] = {
            "slippage_ticks": self.slippage_ticks,
            "entry_slippage_pct": self.entry_slippage_pct,
            "exit_slippage_pct": self.exit_slippage_pct,
            "prob_fill_on_limit": self.prob_fill_on_limit,
        }
        if self.min_synthetic_book_size != type(self).min_synthetic_book_size:
            kwargs["min_synthetic_book_size"] = self.min_synthetic_book_size
        if self.synthetic_book_depth_multiplier != type(self).synthetic_book_depth_multiplier:
            kwargs["synthetic_book_depth_multiplier"] = self.synthetic_book_depth_multiplier
        return kwargs


__all__ = ["ExecutionModelConfig", "SameTimestampPriority", "StaticLatencyConfig"]
