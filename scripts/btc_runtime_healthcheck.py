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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    store = RuntimeStatusStore(
        resolve_runtime_root(config_path=args.config, runtime_root=args.runtime_root)
    )
    try:
        health = check_runtime_health(
            store.read(args.service),
            now=datetime.now(UTC),
            max_age_seconds=args.max_age_seconds,
        )
    except ValueError as exc:
        print(json.dumps({"healthy": False, "reason": f"invalid_runtime_status: {exc}"}))
        return 1
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
