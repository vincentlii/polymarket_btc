from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from scripts import btc_forward_collector, btc_forward_runtime
from scripts._script_helpers import run_async_entrypoint


def test_async_entrypoint_preserves_keyboard_interrupt() -> None:
    async def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_async_entrypoint(interrupt(), shutdown_timeout_seconds=0.1)


def test_async_entrypoint_process_exits_with_original_error_and_stuck_task() -> None:
    script = textwrap.dedent(
        """
        import asyncio

        from scripts._script_helpers import run_async_entrypoint

        async def resist_cancellation():
            never = asyncio.Event()
            while True:
                try:
                    await never.wait()
                except asyncio.CancelledError:
                    continue

        async def fail():
            started = asyncio.Event()

            async def run():
                started.set()
                await resist_cancellation()

            asyncio.create_task(run(), name="stuck-producer")
            await started.wait()
            raise LookupError("primary failure")

        try:
            run_async_entrypoint(fail(), shutdown_timeout_seconds=0.05)
        except LookupError as exc:
            assert str(exc) == "primary failure"
            assert any("stuck-producer" in note for note in exc.__notes__)
            print("bounded-original-error")
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "bounded-original-error" in completed.stdout


def test_async_entrypoint_process_fails_boundedly_when_success_leaves_stuck_task() -> None:
    script = textwrap.dedent(
        """
        import asyncio

        from scripts._script_helpers import run_async_entrypoint

        async def resist_cancellation():
            never = asyncio.Event()
            while True:
                try:
                    await never.wait()
                except asyncio.CancelledError:
                    continue

        async def succeed():
            started = asyncio.Event()

            async def run():
                started.set()
                await resist_cancellation()

            asyncio.create_task(run(), name="stuck-producer")
            await started.wait()
            return 7

        try:
            run_async_entrypoint(succeed(), shutdown_timeout_seconds=0.05)
        except RuntimeError as exc:
            assert "stuck-producer" in str(exc)
            print("bounded-shutdown-error")
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "bounded-shutdown-error" in completed.stdout


def test_async_entrypoint_propagates_background_cleanup_failure() -> None:
    async def succeed() -> int:
        started = asyncio.Event()

        async def fail_during_cleanup() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise RuntimeError("background cleanup failed")

        asyncio.create_task(fail_during_cleanup(), name="cleanup-failure")
        await started.wait()
        return 7

    with pytest.raises(RuntimeError, match="background cleanup failed"):
        run_async_entrypoint(succeed(), shutdown_timeout_seconds=0.1)


@pytest.mark.parametrize(
    ("module", "coroutine_name"),
    (
        (btc_forward_collector, "collect"),
        (btc_forward_runtime, "run_async"),
    ),
)
def test_btc_entrypoints_use_configured_bounded_asyncio_runner(
    module,
    coroutine_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = SimpleNamespace(config=Path("config.toml"))
    observed: dict[str, object] = {}

    async def run(_args) -> None:
        assert _args is args

    def bounded_runner(coroutine, *, shutdown_timeout_seconds: float) -> None:
        observed["timeout"] = shutdown_timeout_seconds
        asyncio.run(coroutine)

    monkeypatch.setattr(module, "parse_args", lambda _argv: args)
    monkeypatch.setattr(
        module,
        "load_btc_project_config",
        lambda _path: SimpleNamespace(
            collection=SimpleNamespace(shutdown_flush_timeout_seconds=1.25)
        ),
    )
    monkeypatch.setattr(module, coroutine_name, run)
    monkeypatch.setattr(module, "run_async_entrypoint", bounded_runner)

    assert module.main([]) == 0
    assert observed == {"timeout": 1.25}
