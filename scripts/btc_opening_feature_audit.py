"""Build causal BTC opening-feature evidence from one versioned forward market window."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from datetime import timedelta
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
from btc_short_horizon.research.opening_features import (  # noqa: E402
    build_forward_opening_feature_observations,
)
from btc_short_horizon.research.opening_proxy import (  # noqa: E402
    opening_proxy_decision_offsets_ms,
)


_CORE_VENUE_SOURCES = ("binance_spot", "binance_perp")


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
    parser.add_argument("--lookback-seconds", type=int, default=300)
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
    start_time = market.t0 - timedelta(seconds=args.lookback_seconds)
    end_time = market.t0 + timedelta(seconds=entry_end)
    decisions = tuple(
        _ns(market.t0) + offset_ms * 1_000_000
        for offset_ms in opening_proxy_decision_offsets_ms(
            cadence_ms=cadence_ms,
            entry_start_seconds=entry_start,
            entry_end_seconds=entry_end,
        )
    )
    build = build_forward_opening_feature_observations(
        raw_data_root=args.raw_data_root or config.paths.raw_data_root,
        market=market,
        start_time=start_time,
        end_time=end_time,
        decision_ts_ns=decisions,
        ingest_version=config.collection.ingest_version,
        required_venue_sources=_CORE_VENUE_SOURCES,
    )
    output = args.output_directory
    output.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([_observation_row(item) for item in build.observations]),
        output / "opening_feature_observations.parquet",
        compression="zstd",
    )
    schema = build.observations[0].feature_schema_hash if build.observations else None
    quality_flags = Counter(
        flag for observation in build.observations for flag in observation.quality_flags
    )
    result = {
        "market_slug": market.slug,
        "ingest_version": config.collection.ingest_version,
        "collection_window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
        "entry_protocol": {
            "entry_start_seconds": entry_start,
            "entry_end_seconds": entry_end,
            "cadence_milliseconds": cadence_ms,
            "required_venue_sources": list(_CORE_VENUE_SOURCES),
        },
        "feature_schema_hash": schema,
        "decision_coverage": {
            "requested": len(decisions),
            "observed": len(build.observations),
            "eligible": sum(observation.eligible for observation in build.observations),
            "ineligible": sum(not observation.eligible for observation in build.observations),
            "quality_flags": dict(sorted(quality_flags.items())),
        },
        "source_summaries": [asdict(summary) for summary in build.source_summaries],
    }
    (output / "metrics.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def _observation_row(observation) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "market_window_start_ns": observation.market_window_start_ns,
        "ts_event": observation.ts_event,
        "ts_init": observation.ts_init,
        "feature_schema_hash": observation.feature_schema_hash,
        "values": list(observation.values),
        "p_boundary_up": observation.p_boundary_up,
        "p_market_mid_up": observation.p_market_mid_up,
        "quality_flags": sorted(observation.quality_flags),
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


def _ns(value) -> int:  # type: ignore[no-untyped-def]
    return int(value.timestamp() * 1_000_000_000)


def main(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
