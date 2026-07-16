"""Create or clear the local filesystem stop request for a BTC runtime."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.live.runtime import RuntimeControl  # noqa: E402
from scripts._runtime_helpers import resolve_runtime_root  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
    )
    parser.add_argument("--runtime-root", type=Path)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--request-stop", metavar="REASON")
    actions.add_argument("--clear-stop", action="store_true")
    actions.add_argument("--show", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    control = RuntimeControl(
        resolve_runtime_root(config_path=args.config, runtime_root=args.runtime_root)
    )
    if args.request_stop is not None:
        request = control.request_stop(reason=args.request_stop, requested_at=datetime.now(UTC))
        print(f"stop requested at {request.requested_at.isoformat()}: {request.reason}")
        return 0
    if args.clear_stop:
        print("stop request cleared" if control.clear_stop() else "no stop request was present")
        return 0
    request = control.stop_request()
    if request is None:
        print("no stop request")
        return 0
    print(f"stop requested at {request.requested_at.isoformat()}: {request.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
