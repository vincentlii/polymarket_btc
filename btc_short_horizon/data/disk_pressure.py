"""Pure disk-pressure policy used by collector orchestration and observability."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class DiskPressureState(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    SHED_OPTIONAL_FEEDS = "shed_optional_feeds"
    SUSPEND_EXTENDED_CAPTURE = "suspend_extended_capture"


@dataclass(frozen=True, slots=True)
class DiskProtectionPolicy:
    warning_free_gib: float
    optional_feeds_free_gib: float
    extended_capture_free_gib: float

    def __post_init__(self) -> None:
        values = (
            self.warning_free_gib,
            self.optional_feeds_free_gib,
            self.extended_capture_free_gib,
        )
        if any(isinstance(value, bool) or not isfinite(value) or value <= 0.0 for value in values):
            raise ValueError("disk protection thresholds must be finite and > 0")
        if not (
            self.warning_free_gib > self.optional_feeds_free_gib > self.extended_capture_free_gib
        ):
            raise ValueError(
                "disk protection thresholds must satisfy warning > optional feeds > extended capture"
            )

    def evaluate_free_gib(self, free_gib: float) -> DiskPressureState:
        if isinstance(free_gib, bool) or not isfinite(free_gib) or free_gib < 0.0:
            raise ValueError("free_gib must be finite and >= 0")
        if free_gib <= self.extended_capture_free_gib:
            return DiskPressureState.SUSPEND_EXTENDED_CAPTURE
        if free_gib <= self.optional_feeds_free_gib:
            return DiskPressureState.SHED_OPTIONAL_FEEDS
        if free_gib <= self.warning_free_gib:
            return DiskPressureState.WARNING
        return DiskPressureState.NORMAL
