from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from btc_short_horizon.config import load_btc_project_config
from btc_short_horizon.live.forward_runtime import (
    ForwardCollectorRuntimeConfig,
    run_forward_collector_runtime,
)
from btc_short_horizon.live.runtime import RuntimeControl, RuntimeStatusStore
from scripts.btc_forward_runtime import _initialize_research_paper


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


def test_paper_startup_failure_is_persisted_without_blocking_collector(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_runtime(**_kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("bad model artifact")

    monkeypatch.setattr("scripts.btc_forward_runtime.ResearchPaperRuntime", fail_runtime)
    buffer, runtime = _initialize_research_paper(
        project=load_btc_project_config(Path("configs/btc_short_horizon/baseline.toml")),
        model_directory=tmp_path / "model",
        runtime_root=tmp_path,
        rule_epoch="btc-chainlink-v1",
        starting_balance=1_000.0,
        max_events=100,
    )

    assert buffer is None
    assert runtime is None
    status = RuntimeStatusStore(tmp_path).read("research_paper")
    assert status is not None
    assert status.state == "failed"
    assert status.details["real_orders_enabled"] is False
    assert "bad model artifact" in str(status.details["last_error"])
