"""Shared causal confirmation state for replay and real-time adapters."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from btc_short_horizon.strategy.types import TokenSide


@dataclass(slots=True)
class ConsecutiveSignalConfirmation:
    required_signals: int
    cadence_seconds: float
    tolerance_seconds: float
    side: TokenSide | None = None
    last_signal_ts_ns: int | None = None
    count: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.required_signals, bool)
            or not isinstance(self.required_signals, int)
            or self.required_signals < 1
        ):
            raise ValueError("required_signals must be an integer >= 1")
        if not isfinite(self.cadence_seconds) or self.cadence_seconds <= 0.0:
            raise ValueError("cadence_seconds must be finite and > 0")
        if (
            not isfinite(self.tolerance_seconds)
            or self.tolerance_seconds < 0.0
            or self.tolerance_seconds >= self.cadence_seconds
        ):
            raise ValueError("tolerance_seconds must be in [0, cadence_seconds)")

    def observe(self, side: TokenSide, *, signal_ts_ns: int) -> bool:
        if not isinstance(side, TokenSide):
            side = TokenSide(side)
        if isinstance(signal_ts_ns, bool) or not isinstance(signal_ts_ns, int) or signal_ts_ns < 0:
            raise ValueError("signal_ts_ns must be a non-negative integer")
        cadence_ns = round(self.cadence_seconds * 1_000_000_000)
        tolerance_ns = round(self.tolerance_seconds * 1_000_000_000)
        consecutive = (
            self.side is side
            and self.last_signal_ts_ns is not None
            and cadence_ns - tolerance_ns
            <= signal_ts_ns - self.last_signal_ts_ns
            <= cadence_ns + tolerance_ns
        )
        self.count = self.count + 1 if consecutive else 1
        self.side = side
        self.last_signal_ts_ns = signal_ts_ns
        return self.count >= self.required_signals

    def reset(self) -> None:
        self.side = None
        self.last_signal_ts_ns = None
        self.count = 0


__all__ = ["ConsecutiveSignalConfirmation"]
