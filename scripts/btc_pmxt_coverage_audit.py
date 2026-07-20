"""Audit PMXT L2 and trade-tick coverage for one verified BTC 15-minute market."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
import json
from pathlib import Path

import pandas as pd

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from prediction_market_extensions.backtesting.data_sources.pmxt import (  # noqa: E402
    RunnerPolymarketPMXTDataLoader,
    configured_pmxt_data_source,
)
from prediction_market_extensions.adapters.prediction_market import (  # noqa: E402
    replay_records_sha256,
)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data import read_market_catalog  # noqa: E402
from btc_short_horizon.research.opening_evidence import (  # noqa: E402
    PmxtBookEventLoad,
    pmxt_order_book_state_events,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
        help="BTC project TOML configuration.",
    )
    parser.add_argument("--market-catalog", type=Path, required=True)
    parser.add_argument("--market-slug", required=True)
    parser.add_argument("--start-time", required=True, type=_parse_utc_datetime)
    parser.add_argument("--end-time", required=True, type=_parse_utc_datetime)
    parser.add_argument("--up-token-index", type=int, default=0)
    parser.add_argument("--down-token-index", type=int, default=1)
    parser.add_argument("--output-directory", type=Path, required=True)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    config = load_btc_project_config(args.config)
    market = read_market_catalog(args.market_catalog).require(args.market_slug)
    if market.family != config.primary_family:
        raise ValueError("market must belong to the configured 15m primary family")
    if args.start_time >= args.end_time:
        raise ValueError("start-time must precede end-time")
    if min(args.up_token_index, args.down_token_index) < 0:
        raise ValueError("token indexes must be non-negative")
    if args.up_token_index == args.down_token_index:
        raise ValueError("token indexes must differ")
    if args.output_directory.exists():
        raise FileExistsError(f"output directory already exists: {args.output_directory}")
    result = asyncio.run(
        _audit(
            market_slug=market.slug,
            expected_up_token_id=market.up_token_id,
            expected_down_token_id=market.down_token_id,
            start_time=args.start_time,
            end_time=args.end_time,
            up_token_index=args.up_token_index,
            down_token_index=args.down_token_index,
            sources=config.data_sources,
        )
    )
    args.output_directory.mkdir(parents=True)
    (args.output_directory / "pmxt_coverage.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


async def _audit(
    *,
    market_slug: str,
    expected_up_token_id: str,
    expected_down_token_id: str,
    start_time: datetime,
    end_time: datetime,
    up_token_index: int,
    down_token_index: int,
    sources: Sequence[str],
) -> dict[str, object]:
    with configured_pmxt_data_source(sources=sources):
        up_loader, down_loader = await asyncio.gather(
            RunnerPolymarketPMXTDataLoader.from_market_slug(
                market_slug, token_index=up_token_index
            ),
            RunnerPolymarketPMXTDataLoader.from_market_slug(
                market_slug, token_index=down_token_index
            ),
        )
        _validate_token_mapping(
            expected_up_token_id=expected_up_token_id,
            expected_down_token_id=expected_down_token_id,
            actual_up_token_id=up_loader.token_id,
            actual_down_token_id=down_loader.token_id,
        )
        start = pd.Timestamp(start_time)
        end = pd.Timestamp(end_time)
        up_records = up_loader.load_order_book_deltas(start, end)
        up_gap_hours = tuple(up_loader.last_load_gap_hours)
        down_records = down_loader.load_order_book_deltas(start, end)
        down_gap_hours = tuple(down_loader.last_load_gap_hours)
        up_trades = up_loader.load_pmxt_trade_ticks(start, end)
        down_trades = down_loader.load_pmxt_trade_ticks(start, end)
    up = pmxt_order_book_state_events(
        token_id=expected_up_token_id,
        records=up_records,
        gap_hours=up_gap_hours,
    )
    down = pmxt_order_book_state_events(
        token_id=expected_down_token_id,
        records=down_records,
        gap_hours=down_gap_hours,
    )
    books_covered = bool(up.events and down.events)
    trades_covered = bool(up_trades and down_trades)
    no_archive_gaps = not up_gap_hours and not down_gap_hours
    return {
        "market_slug": market_slug,
        "window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
        "tokens": {
            "up": _token_summary(
                up,
                book_records=up_records,
                trades=up_trades,
                gap_hours=up_gap_hours,
            ),
            "down": _token_summary(
                down,
                book_records=down_records,
                trades=down_trades,
                gap_hours=down_gap_hours,
            ),
        },
        "coverage": {
            "books_covered": books_covered,
            "trade_ticks_covered": trades_covered,
            "no_archive_gaps": no_archive_gaps,
            "eligible_for_joint_replay": books_covered and trades_covered and no_archive_gaps,
        },
        "limitation": (
            "Coverage eligibility validates data availability only; it is not a maker-profitability "
            "or exact-FIFO-queue conclusion."
        ),
    }


def _token_summary(
    load: PmxtBookEventLoad,
    *,
    book_records: Sequence[object],
    trades: Sequence[object],
    gap_hours: Sequence[object],
) -> dict[str, object]:
    return {
        "token_id": load.token_id,
        "source_book_event_count": load.source_book_event_count,
        "reconstructed_book_state_event_count": len(load.events),
        "trade_tick_count": len(trades),
        "records_sha256": replay_records_sha256((*book_records, *trades)),
        "gap_hours": [str(item) for item in gap_hours],
    }


def _validate_token_mapping(
    *,
    expected_up_token_id: str,
    expected_down_token_id: str,
    actual_up_token_id: str | None,
    actual_down_token_id: str | None,
) -> None:
    if actual_up_token_id != expected_up_token_id or actual_down_token_id != expected_down_token_id:
        raise ValueError(
            "PMXT token indexes do not match the immutable market catalog; "
            "set --up-token-index/--down-token-index only after verifying the mapping"
        )


def _parse_utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a UTC offset")
    return parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
