"""Run one bounded BTC opening-model shadow pass without submitting orders."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.backtest import (  # noqa: E402
    BtcOpeningMispricingSignal,
    to_opening_mispricing_signal,
)
from btc_short_horizon.backtest.signal_io import (  # noqa: E402
    write_opening_mispricing_signals,
)
from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data import MarketWindow, read_market_catalog  # noqa: E402
from btc_short_horizon.models import (  # noqa: E402
    FittedDirectionModel,
    ModelArtifactMetadata,
    ModelArtifactStore,
    OpeningMispricingPrediction,
)
from btc_short_horizon.research.binance_history import (  # noqa: E402
    BinanceKlineHistory,
    fetch_binance_spot_kline_history,
)
from btc_short_horizon.research.opening_evidence import (  # noqa: E402
    ForwardBookEventLoad,
    OpeningMarketObservation,
    build_opening_market_observations,
    load_forward_polymarket_book_events,
)
from btc_short_horizon.research.opening_proxy import (  # noqa: E402
    OPENING_REGIMES,
    opening_proxy_decision_offsets_ms,
    opening_proxy_feature_schema,
    opening_proxy_protocol,
    opening_regime_for_elapsed_seconds,
    validate_opening_proxy_protocol,
)
from btc_short_horizon.research.opening_runtime import (  # noqa: E402
    build_opening_proxy_prediction,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
    )
    parser.add_argument("--market-catalog", type=Path, required=True)
    parser.add_argument("--market-slug", required=True)
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--raw-data-root", type=Path)
    parser.add_argument("--book-lookback-seconds", type=int, default=300)
    parser.add_argument("--availability-delay-seconds", type=float, default=1.0)
    parser.add_argument(
        "--as-of",
        type=_datetime,
        help="Causal UTC cutoff; defaults to the current time.",
    )
    return parser.parse_args(argv)


def build_shadow_predictions(
    *,
    model: FittedDirectionModel,
    metadata: ModelArtifactMetadata,
    market: MarketWindow,
    klines: BinanceKlineHistory,
    observations: Sequence[OpeningMarketObservation],
    availability_delay: timedelta = timedelta(seconds=1),
) -> tuple[tuple[OpeningMispricingPrediction, ...], tuple[BtcOpeningMispricingSignal, ...]]:
    """Build replay-ready signals only; this function has no execution gateway."""

    if not observations:
        raise ValueError("shadow pass requires at least one causal market observation")
    ordered = tuple(sorted(observations, key=lambda item: item.decision_ts_ns))
    if len({item.decision_ts_ns for item in ordered}) != len(ordered):
        raise ValueError("shadow observations must have unique decision timestamps")
    predictions = tuple(
        build_opening_proxy_prediction(
            model=model,
            metadata=metadata,
            market=market,
            klines=klines,
            market_observation=observation,
            availability_delay=availability_delay,
        )
        for observation in ordered
    )
    return predictions, tuple(to_opening_mispricing_signal(item) for item in predictions)


def shadow_bootstrap_window(
    *,
    market_start: datetime,
    last_decision_time: datetime,
    max_lookback_seconds: int,
    availability_delay: timedelta,
) -> tuple[datetime, datetime]:
    if market_start.tzinfo is None or last_decision_time.tzinfo is None:
        raise ValueError("shadow bootstrap timestamps must be timezone-aware")
    if max_lookback_seconds < 1 or availability_delay < timedelta(0):
        raise ValueError("shadow bootstrap lookback and availability delay are invalid")
    interval = timedelta(seconds=1)
    start = market_start - timedelta(seconds=max_lookback_seconds)
    end = last_decision_time + interval + availability_delay
    if end - start >= timedelta(seconds=4_000):
        raise ValueError("shadow bootstrap exceeds the four-page REST safety bound")
    return start, end


async def run_async(args: argparse.Namespace) -> dict[str, object]:
    config = load_btc_project_config(args.config)
    market = read_market_catalog(args.market_catalog).require(args.market_slug)
    if market.family != config.primary_family:
        raise ValueError("market must belong to the configured BTC 15m primary family")
    if args.output_directory.exists():
        raise FileExistsError(f"output directory already exists: {args.output_directory}")
    if args.book_lookback_seconds < 0:
        raise ValueError("book_lookback_seconds must be >= 0")
    if args.availability_delay_seconds < 0.0:
        raise ValueError("availability_delay_seconds must be >= 0")
    as_of = args.as_of or datetime.now(UTC)
    timing = config.research_timing
    offsets_ms = opening_proxy_decision_offsets_ms(
        cadence_ms=timing.model_cadence_ms,
        entry_start_seconds=timing.entry_start_seconds,
        entry_end_seconds=timing.entry_end_seconds,
    )
    as_of_ns = _ns(as_of)
    decisions = tuple(
        _ns(market.t0) + offset_ms * 1_000_000
        for offset_ms in offsets_ms
        if _ns(market.t0) + offset_ms * 1_000_000 <= as_of_ns
    )
    if not decisions:
        raise ValueError("as_of precedes the first configured shadow decision")

    schema = opening_proxy_feature_schema(1)
    model, metadata = ModelArtifactStore.load(
        directory=args.model_directory,
        expected_schema_hash=schema.hash,
    )
    protocol = opening_proxy_protocol(
        entry_start_seconds=timing.entry_start_seconds,
        entry_end_seconds=timing.entry_end_seconds,
        snapshot_seconds=timing.training_snapshot_seconds,
    )
    validate_opening_proxy_protocol(metadata.config, expected=protocol)
    raw_root = args.raw_data_root or config.paths.raw_data_root
    book_start = market.t0 - timedelta(seconds=args.book_lookback_seconds)
    book_end = datetime.fromtimestamp(decisions[-1] / 1_000_000_000, tz=UTC) + timedelta(
        microseconds=1
    )
    up = load_forward_polymarket_book_events(
        raw_data_root=raw_root,
        token_id=market.up_token_id,
        start_time=book_start,
        end_time=book_end,
        ingest_version=config.collection.ingest_version,
        expected_source_timestamp_regression_tolerance_seconds=(
            config.collection.polymarket_source_timestamp_regression_tolerance_seconds
        ),
    )
    down = load_forward_polymarket_book_events(
        raw_data_root=raw_root,
        token_id=market.down_token_id,
        start_time=book_start,
        end_time=book_end,
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
    observations = build_opening_market_observations(
        market=market,
        up_events=up.events,
        down_events=down.events,
        decision_ts_ns=decisions,
    )
    if not observations:
        raise ValueError("forward CLOB data produced no causal shadow observations")

    availability_delay = timedelta(seconds=args.availability_delay_seconds)
    required_start, required_end = shadow_bootstrap_window(
        market_start=market.t0,
        last_decision_time=datetime.fromtimestamp(decisions[-1] / 1_000_000_000, tz=UTC),
        max_lookback_seconds=timing.max_feature_lookback_seconds,
        availability_delay=availability_delay,
    )
    bootstrap_end = min(required_end, as_of)
    klines = await fetch_binance_spot_kline_history(
        start_time=required_start,
        end_time=bootstrap_end,
    )
    predictions, signals = build_shadow_predictions(
        model=model,
        metadata=metadata,
        market=market,
        klines=klines,
        observations=observations,
        availability_delay=availability_delay,
    )

    output = args.output_directory
    output.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([asdict(item) for item in predictions]),
        output / "predictions.parquet",
        compression="zstd",
    )
    write_opening_mispricing_signals(output / "signals.parquet", signals)
    threshold = config.maker.minimum_edge + config.maker.safety_buffer
    mid_gaps = [abs(item.p_up - item.p_market_mid_up) for item in predictions]
    result = {
        "study_type": "opening_proxy_one_shot_shadow",
        "mode": "shadow",
        "market_slug": market.slug,
        "as_of": as_of.isoformat(),
        "ingest_version": config.collection.ingest_version,
        "model": {
            "model_id": metadata.model_id,
            "model_sha256": metadata.model_sha256,
            "feature_schema_hash": metadata.feature_schema_hash,
        },
        "entry_protocol": {
            **protocol,
            "decision_offsets_ms": list(offsets_ms),
            "availability_delay_seconds": args.availability_delay_seconds,
            "minimum_strategy_edge": config.maker.minimum_edge,
            "safety_buffer": config.maker.safety_buffer,
        },
        "coverage": {
            "requested_decisions": len(decisions),
            "market_observations": len(observations),
            "predictions": len(predictions),
            "quality_eligible": sum(
                not item.has_data_gap
                and item.structure_valid
                and item.tick_unchanged
                and item.data_age_seconds <= config.maker.stale_after_seconds
                for item in predictions
            ),
            "by_regime": _shadow_regime_coverage(
                market=market,
                decisions=decisions,
                observations=observations,
                predictions=predictions,
                stale_after_seconds=config.maker.stale_after_seconds,
            ),
        },
        "diagnostics": {
            "mean_p_up": sum(item.p_up for item in predictions) / len(predictions),
            "maximum_absolute_fair_to_mid_gap": max(mid_gaps),
            "mid_gap_above_strategy_threshold": sum(value >= threshold for value in mid_gaps),
        },
        "up_raw": _load_summary(up),
        "down_raw": _load_summary(down),
        "binance_bootstrap": {
            "start": required_start.isoformat(),
            "end": bootstrap_end.isoformat(),
            "bars": len(klines.open_ts_ns),
            "source_hash": klines.source_hash,
        },
        "orders_submitted": 0,
        "limitations": [
            "Shadow output does not submit, simulate, or claim an order fill.",
            "Fair-to-mid gaps are diagnostics; passive executable prices and queue are evaluated only in BookReplay.",
            "The current model/holdout evidence was previously inspected and is not a fresh sealed confirmation.",
        ],
    }
    (output / "metrics.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
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


def _shadow_regime_coverage(
    *,
    market: MarketWindow,
    decisions: Sequence[int],
    observations: Sequence[OpeningMarketObservation],
    predictions: Sequence[OpeningMispricingPrediction],
    stale_after_seconds: float,
) -> dict[str, dict[str, int]]:
    market_start_ns = _ns(market.t0)

    def regime_for(timestamp_ns: int) -> str:
        elapsed_seconds = (timestamp_ns - market_start_ns) / 1_000_000_000
        return opening_regime_for_elapsed_seconds(elapsed_seconds).value

    def counts(timestamps: Sequence[int]) -> dict[str, int]:
        return {
            regime.value: sum(
                regime_for(timestamp_ns) == regime.value for timestamp_ns in timestamps
            )
            for regime in OPENING_REGIMES
        }

    decision_counts = counts(decisions)
    observation_counts = counts([item.decision_ts_ns for item in observations])
    prediction_counts = counts([item.trigger_ts_ns for item in predictions])
    eligible_counts = counts(
        [
            item.trigger_ts_ns
            for item in predictions
            if not item.has_data_gap
            and item.structure_valid
            and item.tick_unchanged
            and item.data_age_seconds <= stale_after_seconds
        ]
    )
    return {
        regime.value: {
            "requested_decisions": decision_counts[regime.value],
            "market_observations": observation_counts[regime.value],
            "predictions": prediction_counts[regime.value],
            "quality_eligible": eligible_counts[regime.value],
        }
        for regime in OPENING_REGIMES
    }


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("datetime must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(UTC)


def _ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def main(argv: Sequence[str] | None = None) -> int:
    result = asyncio.run(run_async(parse_args(argv)))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
