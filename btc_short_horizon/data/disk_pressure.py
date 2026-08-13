"""Pure disk-pressure policy used by collector orchestration and observability."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
import asyncio
from collections.abc import Awaitable, Callable


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


@dataclass(slots=True)
class DiskFeedSupervisor:
    """Hysteretic optional-feed state; core feeds are never disabled."""

    policy: DiskProtectionPolicy
    recovery_margin_gib: float = 1.0
    state: DiskPressureState = DiskPressureState.NORMAL

    def update(self, free_gib: float) -> DiskPressureState:
        candidate = self.policy.evaluate_free_gib(free_gib)
        severity = {
            DiskPressureState.NORMAL: 0,
            DiskPressureState.WARNING: 1,
            DiskPressureState.SHED_OPTIONAL_FEEDS: 2,
            DiskPressureState.SUSPEND_EXTENDED_CAPTURE: 3,
        }
        if severity[candidate] > severity[self.state]:
            self.state = candidate
            return self.state
        if (
            self.state
            in {
                DiskPressureState.SHED_OPTIONAL_FEEDS,
                DiskPressureState.SUSPEND_EXTENDED_CAPTURE,
            }
            and free_gib < self.policy.optional_feeds_free_gib + self.recovery_margin_gib
        ):
            return self.state
        self.state = candidate
        return self.state

    @property
    def optional_feeds_enabled(self) -> bool:
        return self.state not in {
            DiskPressureState.SHED_OPTIONAL_FEEDS,
            DiskPressureState.SUSPEND_EXTENDED_CAPTURE,
        }

    def subscriptions(
        self,
        *,
        core_spot: tuple[str, ...],
        optional_perp: tuple[str, ...],
        optional_okx: tuple[dict[str, str], ...],
    ) -> dict[str, tuple[object, ...]]:
        return {
            "binance_spot": core_spot,
            "binance_perp": optional_perp if self.optional_feeds_enabled else (),
            "okx": optional_okx if self.optional_feeds_enabled else (),
        }


async def supervise_optional_feed(
    *,
    stop_event: asyncio.Event,
    enabled_event: asyncio.Event,
    run_once: Callable[[asyncio.Event], Awaitable[None]],
    retry_seconds: float = 1.0,
    on_disabled: Callable[[], Awaitable[None]] | None = None,
    on_recovered: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Cancel an optional socket when disabled and restart it after safe recovery."""
    while not stop_event.is_set():
        if not enabled_event.is_set():
            enable_task = asyncio.create_task(enabled_event.wait())
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                (enable_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop_task in done:
                return
        local_stop = asyncio.Event()
        worker = asyncio.create_task(run_once(local_stop))
        disabled = asyncio.create_task(_wait_until_cleared(enabled_event, stop_event))
        done, pending = await asyncio.wait((worker, disabled), return_when=asyncio.FIRST_COMPLETED)
        if disabled in done:
            local_stop.set()
            await worker
            if on_disabled is not None and not stop_event.is_set():
                await on_disabled()
        elif not stop_event.is_set():
            await asyncio.gather(worker, return_exceptions=True)
            if retry_seconds:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=retry_seconds)
                except TimeoutError:
                    pass
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if enabled_event.is_set() and not stop_event.is_set() and on_recovered is not None:
            await on_recovered()


async def _wait_until_cleared(enabled: asyncio.Event, stopped: asyncio.Event) -> None:
    while enabled.is_set() and not stopped.is_set():
        await asyncio.sleep(0.1)
