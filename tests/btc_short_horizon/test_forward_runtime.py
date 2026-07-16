from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from btc_short_horizon.live.forward_runtime import (
    ForwardCollectorRuntimeConfig,
    run_forward_collector_runtime,
)
from btc_short_horizon.live.runtime import RuntimeControl, RuntimeStatusStore


async def _wait_for_state(store: RuntimeStatusStore, state: str) -> None:
    for _ in range(100):
        status = store.read("forward_collector")
        if status is not None and status.state == state:
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"runtime did not reach {state}")


def test_forward_runtime_stops_from_persisted_control_request(tmp_path) -> None:
    async def run() -> None:
        store = RuntimeStatusStore(tmp_path)
        started = asyncio.Event()

        async def collect(stop_event: asyncio.Event) -> None:
            started.set()
            await stop_event.wait()

        task = asyncio.create_task(
            run_forward_collector_runtime(
                config=ForwardCollectorRuntimeConfig(
                    runtime_root=tmp_path,
                    status_interval_seconds=0.01,
                ),
                collect=collect,
                status_details=lambda: {"source": "test"},
            )
        )
        await started.wait()
        await _wait_for_state(store, "running")
        RuntimeControl(tmp_path).request_stop(
            reason="operator_request",
            requested_at=datetime.now(UTC),
        )
        await task

        status = store.read("forward_collector")
        assert status is not None
        assert status.state == "stopped"
        assert status.healthy
        assert status.details["stop_reason"] == "operator_request"

    asyncio.run(run())


def test_forward_runtime_marks_unexpected_collector_exit_failed(tmp_path) -> None:
    async def collect(_stop_event: asyncio.Event) -> None:
        return

    async def run() -> None:
        with pytest.raises(RuntimeError, match="exited without"):
            await run_forward_collector_runtime(
                config=ForwardCollectorRuntimeConfig(
                    runtime_root=tmp_path,
                    status_interval_seconds=0.01,
                ),
                collect=collect,
            )

    asyncio.run(run())
    status = RuntimeStatusStore(tmp_path).read("forward_collector")
    assert status is not None
    assert status.state == "failed"
    assert not status.healthy


def test_forward_runtime_persists_unhealthy_required_feed_state(tmp_path) -> None:
    async def run() -> None:
        store = RuntimeStatusStore(tmp_path)
        started = asyncio.Event()

        async def collect(stop_event: asyncio.Event) -> None:
            started.set()
            await stop_event.wait()

        task = asyncio.create_task(
            run_forward_collector_runtime(
                config=ForwardCollectorRuntimeConfig(
                    runtime_root=tmp_path,
                    status_interval_seconds=0.01,
                ),
                collect=collect,
                status_health=lambda: (False, "required_feed_stale"),
            )
        )
        await started.wait()
        for _ in range(100):
            status = store.read("forward_collector")
            if status is not None and status.state == "running" and not status.healthy:
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("runtime did not persist unhealthy feed state")
        assert status.details["health_reason"] == "required_feed_stale"
        RuntimeControl(tmp_path).request_stop(
            reason="operator_request",
            requested_at=datetime.now(UTC),
        )
        await task

    asyncio.run(run())
