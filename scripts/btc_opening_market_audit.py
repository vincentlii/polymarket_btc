"""Build causal dual-token BTC opening-market evidence from forward raw data."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data import read_market_catalog  # noqa: E402
from btc_short_horizon.research.opening_evidence import (  # noqa: E402
    ForwardBookEventLoad,
    build_opening_market_observations,
    load_forward_polymarket_book_events,
)
from btc_short_horizon.research.opening_proxy import (  # noqa: E402
    opening_proxy_decision_offsets_ms,
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
    parser.add_argument(
        "--raw-data-root",
        type=Path,
        help="Forward collector root; defaults to paths.raw_data_root from --config.",
    )
    parser.add_argument(
        "--lookback-seconds",
        type=int,
        default=300,
        help="CLOB history required before t0 to establish a valid L2 snapshot.",
    )
    parser.add_argument("--entry-start-seconds", type=int)
    parser.add_argument("--entry-end-seconds", type=int)
    parser.add_argument("--cadence-milliseconds", type=int)
    parser.add_argument("--output-directory", type=Path, required=True)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    config = load_btc_project_config(args.config)
    market = read_market_catalog(args.market_catalog).require(args.market_slug)
    if market.family != config.primary_family:
        raise ValueError("market must belong to the configured 15m primary family")
    if args.output_directory.exists():
        raise FileExistsError(f"output directory already exists: {args.output_directory}")
    raw_data_root = args.raw_data_root or config.paths.raw_data_root
    entry_start = (
        args.entry_start_seconds
        if args.entry_start_seconds is not None
        else config.research_timing.entry_start_seconds
    )
    entry_end = (
        args.entry_end_seconds
        if args.entry_end_seconds is not None
        else config.research_timing.entry_end_seconds
    )
    cadence_ms = (
        args.cadence_milliseconds
        if args.cadence_milliseconds is not None
        else config.research_timing.model_cadence_ms
    )
    _validate_inputs(
        lookback_seconds=args.lookback_seconds,
        entry_start_seconds=entry_start,
        entry_end_seconds=entry_end,
        cadence_milliseconds=cadence_ms,
        market_window_seconds=market.family.window_seconds,
    )
    collection_start = market.t0 - _seconds(args.lookback_seconds)
    analysis_end = market.t0 + _seconds(entry_end)
    up = load_forward_polymarket_book_events(
        raw_data_root=raw_data_root,
        token_id=market.up_token_id,
        start_time=collection_start,
        end_time=analysis_end,
        ingest_version=config.collection.ingest_version,
        expected_source_timestamp_regression_tolerance_seconds=(
            config.collection.polymarket_source_timestamp_regression_tolerance_seconds
        ),
    )
    down = load_forward_polymarket_book_events(
        raw_data_root=raw_data_root,
        token_id=market.down_token_id,
        start_time=collection_start,
        end_time=analysis_end,
        ingest_version=config.collection.ingest_version,
        expected_source_timestamp_regression_tolerance_seconds=(
            config.collection.polymarket_source_timestamp_regression_tolerance_seconds
        ),
    )
    if (
        up.polymarket_source_timestamp_regression_tolerance_seconds
        != down.polymarket_source_timestamp_regression_tolerance_seconds
    ):
        raise ValueError("Up/Down raw manifests use different Polymarket timestamp tolerances")
    decisions = tuple(
        _ns(market.t0) + offset_ms * 1_000_000
        for offset_ms in opening_proxy_decision_offsets_ms(
            cadence_ms=cadence_ms,
            entry_start_seconds=entry_start,
            entry_end_seconds=entry_end,
        )
    )
    observations = build_opening_market_observations(
        market=market,
        up_events=up.events,
        down_events=down.events,
        decision_ts_ns=decisions,
    )
    output = args.output_directory
    output.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([asdict(observation) for observation in observations]),
        output / "opening_market_observations.parquet",
        compression="zstd",
    )
    result = {
        "market_slug": market.slug,
        "raw_data_root": str(raw_data_root),
        "ingest_version": config.collection.ingest_version,
        "collection_window": {
            "start": collection_start.isoformat(),
            "end": analysis_end.isoformat(),
        },
        "entry_protocol": {
            "entry_start_seconds": entry_start,
            "entry_end_seconds": entry_end,
            "cadence_milliseconds": cadence_ms,
        },
        "decision_coverage": {
            "requested": len(decisions),
            "observed": len(observations),
            "missing": len(decisions) - len(observations),
            "gap_flagged": sum(item.has_data_gap for item in observations),
            "tick_change_flagged": sum(not item.tick_unchanged for item in observations),
        },
        "up_raw": _load_summary(up),
        "down_raw": _load_summary(down),
    }
    (output / "metrics.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def _load_summary(load: ForwardBookEventLoad) -> dict[str, int]:
    return {
        "raw_parts": load.raw_part_count,
        "raw_rows": load.raw_row_count,
        "duplicate_rows": load.duplicate_row_count,
        "awaiting_snapshot_rows": load.awaiting_snapshot_count,
        "state_events": len(load.events),
    }


def _validate_inputs(
    *,
    lookback_seconds: int,
    entry_start_seconds: int,
    entry_end_seconds: int,
    cadence_milliseconds: int,
    market_window_seconds: int,
) -> None:
    if lookback_seconds < 0:
        raise ValueError("lookback_seconds must be >= 0")
    if entry_start_seconds < 0 or entry_end_seconds < entry_start_seconds:
        raise ValueError("entry window is invalid")
    if entry_end_seconds >= market_window_seconds:
        raise ValueError("entry_end_seconds must lie before market resolution")
    if cadence_milliseconds < 1:
        raise ValueError("cadence_milliseconds must be >= 1")


def _seconds(value: int) -> timedelta:
    return timedelta(seconds=value)


def _ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def main(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
