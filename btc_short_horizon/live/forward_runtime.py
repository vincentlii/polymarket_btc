"""Supervise the public forward collector without granting any trading authority."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

from btc_short_horizon.live.runtime import RuntimeControl, RuntimeStatus, RuntimeStatusStore


type CollectorEntrypoint = Callable[[asyncio.Event], Awaitable[None]]
type StatusDetailsProvider = Callable[[], Mapping[str, object]]
type StatusHealthProvider = Callable[[], tuple[bool, str]]


@dataclass(frozen=True, slots=True)
class ForwardCollectorRuntimeConfig:
    runtime_root: Path
    service: str = "forward_collector"
    mode: str = "forward_collection"
    status_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.service.strip():
            raise ValueError("service is required")
        if not self.mode.strip():
            raise ValueError("mode is required")
        if not isfinite(self.status_interval_seconds) or self.status_interval_seconds <= 0.0:
            raise ValueError("status_interval_seconds must be finite and > 0")


async def run_forward_collector_runtime(
    *,
    config: ForwardCollectorRuntimeConfig,
    collect: CollectorEntrypoint,
    status_details: StatusDetailsProvider = lambda: {},
    status_health: StatusHealthProvider = lambda: (True, "ok"),
    stop_event: asyncio.Event | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Run one collector with a durable kill switch and fail-closed status reporting."""

    current_time = _utc(now())
    store = RuntimeStatusStore(config.runtime_root)
    control = RuntimeControl(config.runtime_root)
    service_stop = stop_event or asyncio.Event()
    stop_request = control.stop_request()
    if stop_request is not None or service_stop.is_set():
        _write_status(
            store=store,
            config=config,
            state="stopped",
            healthy=True,
            started_at=current_time,
            now=now,
            status_details=status_details,
            status_health=status_health,
            stop_reason=(stop_request.reason if stop_request is not None else "external_stop"),
        )
        return

    _write_status(
        store=store,
        config=config,
        state="starting",
        healthy=True,
        started_at=current_time,
        now=now,
        status_details=status_details,
        status_health=status_health,
        stop_reason=None,
    )
    collector_task = asyncio.create_task(collect(service_stop), name=config.service)
    try:
        while True:
            stop_request = control.stop_request()
            if stop_request is not None:
                service_stop.set()
            if service_stop.is_set():
                _write_status(
                    store=store,
                    config=config,
                    state="stopping",
                    healthy=True,
                    started_at=current_time,
                    now=now,
                    status_details=status_details,
                    status_health=status_health,
                    stop_reason=(
                        stop_request.reason if stop_request is not None else "external_stop"
                    ),
                )
                await collector_task
                _write_status(
                    store=store,
                    config=config,
                    state="stopped",
                    healthy=True,
                    started_at=current_time,
                    now=now,
                    status_details=status_details,
                    status_health=status_health,
                    stop_reason=(
                        stop_request.reason if stop_request is not None else "external_stop"
                    ),
                )
                return
            if collector_task.done():
                await collector_task
                raise RuntimeError("forward collector exited without a stop request")
            _write_status(
                store=store,
                config=config,
                state="running",
                healthy=True,
                started_at=current_time,
                now=now,
                status_details=status_details,
                status_health=status_health,
                apply_status_health=True,
                stop_reason=None,
            )
            await _wait_for_activity(
                collector_task=collector_task,
                stop_event=service_stop,
                timeout_seconds=config.status_interval_seconds,
            )
    except asyncio.CancelledError:
        service_stop.set()
        await _await_shutdown(collector_task)
        _write_status(
            store=store,
            config=config,
            state="stopped",
            healthy=True,
            started_at=current_time,
            now=now,
            status_details=status_details,
            status_health=status_health,
            stop_reason="task_cancelled",
        )
        raise
    except Exception as exc:
        service_stop.set()
        await _await_shutdown(collector_task)
        _write_status(
            store=store,
            config=config,
            state="failed",
            healthy=False,
            started_at=current_time,
            now=now,
            status_details=status_details,
            status_health=status_health,
            stop_reason=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise


async def _wait_for_activity(
    *,
    collector_task: asyncio.Task[None],
    stop_event: asyncio.Event,
    timeout_seconds: float,
) -> None:
    stop_waiter = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait(
            (collector_task, stop_waiter),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        stop_waiter.cancel()
        await asyncio.gather(stop_waiter, return_exceptions=True)


async def _await_shutdown(collector_task: asyncio.Task[None]) -> None:
    if collector_task.done():
        return
    try:
        await collector_task
    except Exception:
        return


def _write_status(
    *,
    store: RuntimeStatusStore,
    config: ForwardCollectorRuntimeConfig,
    state: str,
    healthy: bool,
    started_at: datetime,
    now: Callable[[], datetime],
    status_details: StatusDetailsProvider,
    status_health: StatusHealthProvider,
    stop_reason: str | None,
    error: str | None = None,
    apply_status_health: bool = False,
) -> None:
    service_healthy, health_reason = status_health()
    details = {
        **dict(status_details()),
        "stop_requested": stop_reason is not None,
        "health_reason": health_reason,
    }
    if stop_reason is not None:
        details["stop_reason"] = stop_reason
    if error is not None:
        details["error"] = error
    store.write(
        RuntimeStatus(
            service=config.service,
            mode=config.mode,
            state=state,
            healthy=healthy and (service_healthy or not apply_status_health),
            started_at=started_at,
            updated_at=_utc(now()),
            details=details,
        )
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime clock must be timezone-aware")
    return value.astimezone(UTC)
