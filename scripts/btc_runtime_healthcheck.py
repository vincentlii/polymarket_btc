"""Return a non-zero status when a BTC runtime service is not healthy and fresh."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
import json
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.live.runtime import (  # noqa: E402
    RuntimeStatusStore,
    RuntimeHealth,
    check_runtime_health,
)
from scripts._runtime_helpers import resolve_runtime_root  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
    )
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--service", default="forward_collector")
    parser.add_argument("--max-age-seconds", type=float, default=30.0)
    parser.add_argument(
        "--require-process",
        action="store_true",
        help="also require the service PID recorded in status details to be alive",
    )
    return parser.parse_args(argv)


def _process_is_alive(pid: object, *, proc_root: Path = Path("/proc")) -> bool:
    """Return whether a Linux process exists and is not a zombie."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    status_path = proc_root / str(pid) / "status"
    try:
        state_line = next(
            line
            for line in status_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("State:")
        )
    except (FileNotFoundError, OSError, StopIteration):
        return False
    state = state_line.split("\t", 1)[-1].strip().split(" ", 1)[0]
    return state not in {"Z", "X"}


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    store = RuntimeStatusStore(
        resolve_runtime_root(config_path=args.config, runtime_root=args.runtime_root)
    )
    try:
        status = store.read(args.service)
        health = check_runtime_health(
            status,
            now=datetime.now(UTC),
            max_age_seconds=args.max_age_seconds,
        )
    except ValueError as exc:
        print(json.dumps({"healthy": False, "reason": f"invalid_runtime_status: {exc}"}))
        return 1
    if health.healthy and args.require_process:
        process_id = status.details.get("process_id") if status is not None else None
        if not _process_is_alive(process_id):
            health = RuntimeHealth(False, "process_not_alive", health.age_seconds)
    print(
        json.dumps(
            {
                "healthy": health.healthy,
                "reason": health.reason,
                "age_seconds": health.age_seconds,
            },
            sort_keys=True,
        )
    )
    return 0 if health.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
