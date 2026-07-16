"""Serve the read-only local dashboard for a BTC runtime directory."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.live.dashboard import DashboardConfig, serve_dashboard  # noqa: E402
from scripts._runtime_helpers import resolve_runtime_root  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
    )
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--health-service", default="forward_collector")
    parser.add_argument("--max-age-seconds", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    runtime_root = resolve_runtime_root(config_path=args.config, runtime_root=args.runtime_root)
    try:
        serve_dashboard(
            DashboardConfig(
                runtime_root=runtime_root,
                health_service=args.health_service,
                max_age_seconds=args.max_age_seconds,
            ),
            host=args.host,
            port=args.port,
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
