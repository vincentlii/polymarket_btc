"""Download only BTC 15-minute market rows from the PMXT v2 archive."""

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

from btc_short_horizon.data import read_market_catalog  # noqa: E402
from btc_short_horizon.research.pmxt_btc_history import extract_btc_pmxt_history  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-catalog", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--start", type=_datetime, required=True)
    parser.add_argument("--end", type=_datetime, required=True)
    parser.add_argument("--archive-base-url", default="https://r2v2.pmxt.dev")
    parser.add_argument("--lookback-seconds", type=int, default=90)
    parser.add_argument("--entry-end-seconds", type=int, default=180)
    parser.add_argument("--memory-limit", default="1GB")
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.start >= args.end:
        raise ValueError("start must precede end")
    catalog = read_market_catalog(args.market_catalog, allow_unproven_legacy=True)
    markets = tuple(market for market in catalog.windows() if args.start <= market.t0 < args.end)
    if not markets:
        raise ValueError("catalog contains no market in the requested interval")
    return extract_btc_pmxt_history(
        markets=markets,
        destination=args.destination,
        archive_base_url=args.archive_base_url,
        lookback_seconds=args.lookback_seconds,
        entry_end_seconds=args.entry_end_seconds,
        memory_limit=args.memory_limit,
        workers=args.workers,
    )


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
