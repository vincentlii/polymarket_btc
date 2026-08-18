# Derived from NautilusTrader prediction-market example code.
# Distributed under the GNU Lesser General Public License Version 3.0 or later.
# Modified in this repository on 2026-04-05.
# See the repository NOTICE file for provenance and licensing scope.

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from math import isfinite
import sys
from pathlib import Path
from typing import Any, TypeVar


_T = TypeVar("_T")


def ensure_repo_root(script_path: str | Path) -> Path:
    path = Path(script_path).resolve()
    for parent in path.parents:
        if (parent / "backtests").is_dir() and (parent / "strategies").is_dir():
            repo_root = parent
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            return repo_root
    raise RuntimeError(f"Could not determine repository root for {path}")


def run_async_entrypoint(
    coroutine: Coroutine[Any, Any, _T],
    *,
    shutdown_timeout_seconds: float,
) -> _T:
    """Run a CLI coroutine without letting cancellation-resistant tasks hang process exit."""

    if not isfinite(shutdown_timeout_seconds) or shutdown_timeout_seconds <= 0.0:
        coroutine.close()
        raise ValueError("shutdown_timeout_seconds must be finite and > 0")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    primary_error: BaseException | None = None
    result: _T | None = None
    try:
        try:
            result = loop.run_until_complete(coroutine)
        except BaseException as exc:
            primary_error = exc

        pending, cleanup_errors = _cancel_tasks_with_deadline(
            loop,
            deadline=loop.time() + shutdown_timeout_seconds,
        )
        shutdown_error: RuntimeError | None = None
        if pending:
            names = ", ".join(sorted(task.get_name() for task in pending))
            shutdown_error = RuntimeError(
                f"asyncio tasks did not stop before shutdown timeout: {names}"
            )
        if primary_error is not None:
            if shutdown_error is not None:
                primary_error.add_note(str(shutdown_error))
            for error in cleanup_errors:
                primary_error.add_note(f"asyncio task cleanup also failed: {error!r}")
            raise primary_error
        if shutdown_error is not None:
            for error in cleanup_errors:
                shutdown_error.add_note(f"asyncio task cleanup also failed: {error!r}")
            raise shutdown_error
        if cleanup_errors:
            first, *additional = cleanup_errors
            for error in additional:
                first.add_note(f"additional asyncio task cleanup failure: {error!r}")
            raise first
        return result  # type: ignore[return-value]
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _cancel_tasks_with_deadline(
    loop: asyncio.AbstractEventLoop,
    *,
    deadline: float,
) -> tuple[set[asyncio.Task[Any]], tuple[BaseException, ...]]:
    tasks = {task for task in asyncio.all_tasks(loop) if not task.done()}
    for task in tasks:
        task.cancel()
    if not tasks:
        return set(), ()

    remaining_seconds = max(0.0, deadline - loop.time())
    if remaining_seconds:
        _, pending = loop.run_until_complete(asyncio.wait(tasks, timeout=remaining_seconds))
    else:
        pending = tasks
    cleanup_errors = tuple(
        error
        for task in tasks - pending
        if not task.cancelled()
        for error in (task.exception(),)
        if error is not None
    )
    for task in pending:
        task.add_done_callback(_consume_task_exception)
        # The timeout is already reported by run_async_entrypoint. Suppress the
        # redundant interpreter warning when the deliberately abandoned loop closes.
        task._log_destroy_pending = False  # type: ignore[attr-defined]  # noqa: SLF001
    return pending, cleanup_errors


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()
