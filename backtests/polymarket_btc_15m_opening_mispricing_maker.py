"""Run one explicit BTC 15-minute dual-token Opening Mispricing maker replay."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from prediction_market_extensions.backtesting._market_data_config import MarketDataConfig  # noqa: E402
from prediction_market_extensions.backtesting._timing_harness import timing_harness  # noqa: E402
from prediction_market_extensions.backtesting.data_sources import Book, PMXT, Polymarket  # noqa: E402

from btc_short_horizon.backtest import (  # noqa: E402
    BtcJointReplayConfig,
    BtcOpeningMispricingSignal,
    build_btc_joint_backtest,
    collect_btc_order_events,
)
from btc_short_horizon.backtest.signal_io import read_opening_mispricing_signals  # noqa: E402
from btc_short_horizon.config import BtcProjectConfig, load_btc_project_config  # noqa: E402
from btc_short_horizon.data import MarketWindow, read_market_catalog  # noqa: E402
from btc_short_horizon.data.gamma import gamma_market_to_window  # noqa: E402
from btc_short_horizon.reporting import BtcRunArtifacts, RunArtifactWriter, RunManifest  # noqa: E402


@dataclass(frozen=True, slots=True)
class RunnerInputs:
    config: BtcProjectConfig
    market: MarketWindow
    signals: tuple[BtcOpeningMispricingSignal, ...]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
        help="BTC project TOML configuration.",
    )
    market_input = parser.add_mutually_exclusive_group(required=True)
    market_input.add_argument("--metadata-json", type=Path)
    market_input.add_argument("--market-catalog", type=Path)
    parser.add_argument("--market-slug", help="Market slug to select from --market-catalog.")
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--rule-epoch", required=True)
    parser.add_argument("--start-time", required=True, type=_parse_utc_datetime)
    parser.add_argument("--end-time", required=True, type=_parse_utc_datetime)
    parser.add_argument("--scenario", default="p99_pessimistic")
    parser.add_argument("--up-token-index", type=int, default=0)
    parser.add_argument("--down-token-index", type=int, default=1)
    parser.add_argument("--initial-cash", type=float, default=100.0)
    parser.add_argument("--probability-window", type=int, default=30)
    parser.add_argument("--name-prefix", default="btc_15m_opening_mispricing_maker")
    parser.add_argument("--artifact-directory", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--upstream-revision", required=True)
    parser.add_argument("--model-hash", required=True)
    parser.add_argument("--raw-data-hash", required=True)
    parser.add_argument("--seed", type=int, default=0)
    return parser.parse_args(argv)


def load_runner_inputs(args: argparse.Namespace) -> RunnerInputs:
    config = load_btc_project_config(args.config)
    market = _load_market(args=args, config=config)
    signals = read_opening_mispricing_signals(args.signals, market_slug=market.slug)
    if not signals:
        raise ValueError(f"no Opening Mispricing signals found for market {market.slug!r}")
    if len({(signal.model_version, signal.feature_schema_hash) for signal in signals}) != 1:
        raise ValueError("one replay must use exactly one model version and feature schema")
    return RunnerInputs(config=config, market=market, signals=signals)


def build_backtest_from_args(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    """Construct the replay without loading PMXT data or running Nautilus."""

    return _build_backtest(args=args, inputs=load_runner_inputs(args))


def run(argv: Sequence[str] | None = None) -> list[dict[str, Any]]:
    args = parse_args(argv)
    inputs = load_runner_inputs(args)
    backtest = _build_backtest(args=args, inputs=inputs)

    @timing_harness
    def _run() -> list[dict[str, Any]]:
        return backtest.run()

    results = _run()
    RunArtifactWriter.write(
        directory=args.artifact_directory,
        artifacts=build_run_artifacts(
            args=args,
            inputs=inputs,
            results=results,
            order_events=collect_btc_order_events(backtest),
        ),
        title=f"BTC 15m Opening Mispricing maker replay: {inputs.market.slug}",
    )
    print(f"Completed {backtest.name}: {len(results)} token replay result(s).")
    return results


def build_run_artifacts(
    *,
    args: argparse.Namespace,
    inputs: RunnerInputs,
    results: Sequence[Mapping[str, object]],
    order_events: Sequence[Mapping[str, object]] = (),
) -> BtcRunArtifacts:
    scenario = inputs.config.require_scenario(args.scenario)
    execution = scenario.execution
    opening_paths, fills, closed_trades = _flatten_results(
        market_slug=inputs.market.slug,
        results=results,
    )
    prediction_records = tuple(_prediction_record(signal) for signal in inputs.signals)
    return BtcRunArtifacts(
        manifest=RunManifest(
            code_revision=_require_text(args.code_revision, "code_revision"),
            upstream_revision=_require_text(args.upstream_revision, "upstream_revision"),
            data_hashes={
                **_market_input_hashes(args),
                "opening_mispricing_signals": _sha256_file(args.signals),
                "raw_data": _require_sha256(args.raw_data_hash, "raw_data_hash"),
            },
            model_hashes={
                inputs.signals[0].model_version: _require_sha256(args.model_hash, "model_hash")
            },
            scenario={
                "name": scenario.name,
                "queue_position": execution.queue_position,
                "latency": {
                    "base_latency_ms": execution.latency_model.base_latency_ms,
                    "insert_latency_ms": execution.latency_model.insert_latency_ms,
                    "update_latency_ms": execution.latency_model.update_latency_ms,
                    "cancel_latency_ms": execution.latency_model.cancel_latency_ms,
                },
                "prob_fill_on_limit": execution.prob_fill_on_limit,
                "maker_fee_per_share": inputs.config.maker.maker_fee_per_share,
                "rebate_per_share": 0.0,
            },
            seed=int(args.seed),
        ),
        data_quality={
            "market_slug": inputs.market.slug,
            "token_replay_result_count": len(results),
            "configured_sources": list(inputs.config.data_sources),
            "raw_data_hash_provided": True,
        },
        predictions=prediction_records,
        opening_paths=opening_paths,
        opportunities=prediction_records,
        orders=tuple(dict(event) for event in order_events),
        fills=fills,
        closed_trades=closed_trades,
        metrics=_result_metrics(results),
    )


def _build_backtest(*, args: argparse.Namespace, inputs: RunnerInputs):  # type: ignore[no-untyped-def]
    scenario = inputs.config.require_scenario(args.scenario)
    return build_btc_joint_backtest(
        name=(
            f"{_require_text(args.name_prefix, 'name_prefix')}-{inputs.market.slug}-{scenario.name}"
        ),
        data=MarketDataConfig(
            platform=Polymarket,
            data_type=Book,
            vendor=PMXT,
            sources=inputs.config.data_sources,
        ),
        config=BtcJointReplayConfig(
            market=inputs.market,
            start_time=args.start_time,
            end_time=args.end_time,
            up_token_index=args.up_token_index,
            down_token_index=args.down_token_index,
            signals=inputs.signals,
            maker=inputs.config.maker,
            execution=scenario.execution,
            initial_cash=args.initial_cash,
            probability_window=args.probability_window,
        ),
    )


def _load_market(*, args: argparse.Namespace, config: BtcProjectConfig) -> MarketWindow:
    rule_epoch = _require_text(args.rule_epoch, "rule_epoch")
    if args.metadata_json is not None:
        if args.market_slug:
            raise ValueError("--market-slug requires --market-catalog")
        return gamma_market_to_window(
            _read_metadata(args.metadata_json),
            family=config.primary_family,
            rule_epoch=rule_epoch,
        )
    assert args.market_catalog is not None
    if not args.market_slug:
        raise ValueError("--market-slug is required with --market-catalog")
    market = read_market_catalog(args.market_catalog).require(args.market_slug)
    if market.family != config.primary_family:
        raise ValueError("market catalog entry must belong to the configured 15m primary family")
    if market.rule_epoch != rule_epoch:
        raise ValueError("market catalog rule_epoch must equal --rule-epoch")
    return market


def _read_metadata(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata JSON is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("metadata JSON must contain one Gamma market object")
    return payload


def _market_input_hashes(args: argparse.Namespace) -> dict[str, str]:
    if args.metadata_json is not None:
        return {"metadata_json": _sha256_file(args.metadata_json)}
    assert args.market_catalog is not None
    return {"market_catalog": _sha256_file(args.market_catalog)}


def _prediction_record(signal: BtcOpeningMispricingSignal) -> dict[str, object]:
    return {
        "market_slug": signal.market_slug,
        "model_version": signal.model_version,
        "feature_schema_hash": signal.feature_schema_hash,
        "market_window_start_ts_ns": signal.market_window_start_ts_ns,
        "p_up": signal.p_up,
        "p_boundary_up": signal.p_boundary_up,
        "p_market_mid_up": signal.p_market_mid_up,
        "data_age_seconds": signal.data_age_seconds,
        "has_data_gap": signal.has_data_gap,
        "structure_valid": signal.structure_valid,
        "tick_unchanged": signal.tick_unchanged,
        "fee_unchanged": signal.fee_unchanged,
        "latency_healthy": signal.latency_healthy,
        "ts_event": signal.ts_event,
        "ts_init": signal.ts_init,
    }


def _flatten_results(
    *, market_slug: str, results: Sequence[Mapping[str, object]]
) -> tuple[
    tuple[dict[str, object], ...], tuple[dict[str, object], ...], tuple[dict[str, object], ...]
]:
    opening_paths: list[dict[str, object]] = []
    fills: list[dict[str, object]] = []
    closed_trades: list[dict[str, object]] = []
    for result in results:
        instrument_id = str(result.get("instrument_id", ""))
        token_index = result.get("token_index")
        for timestamp, price in _price_points(result.get("price_series")):
            opening_paths.append(
                {
                    "market_slug": market_slug,
                    "instrument_id": instrument_id,
                    "token_index": token_index,
                    "timestamp": timestamp,
                    "price": price,
                }
            )
        for event in _mapping_sequence(result.get("fill_events")):
            fills.append(
                {
                    "market_slug": market_slug,
                    "instrument_id": instrument_id,
                    "token_index": token_index,
                    **dict(event),
                }
            )
        closed_trades.append(
            {
                "market_slug": market_slug,
                "instrument_id": instrument_id,
                "token_index": token_index,
                "fills": result.get("fills", 0),
                "pnl": result.get("pnl", 0.0),
                "outcome": result.get("outcome"),
                "realized_outcome": result.get("realized_outcome"),
                "settlement_observable_time": result.get("settlement_observable_time"),
            }
        )
    return tuple(opening_paths), tuple(fills), tuple(closed_trades)


def _result_metrics(results: Sequence[Mapping[str, object]]) -> dict[str, object]:
    portfolio_stats = results[0].get("portfolio_stats", {}) if results else {}
    return {
        "token_replay_result_count": len(results),
        "reported_fill_count": sum(_as_int(result.get("fills", 0)) for result in results),
        "reported_pnl": sum(_as_float(result.get("pnl", 0.0)) for result in results),
        "portfolio_stats": dict(portfolio_stats) if isinstance(portfolio_stats, Mapping) else {},
    }


def _price_points(value: object) -> tuple[tuple[object, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(
        (item[0], item[1]) for item in value if isinstance(item, list | tuple) and len(item) == 2
    )


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _as_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: object, name: str) -> str:
    text = _require_text(value, name).casefold()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a SHA-256 hexadecimal digest")
    return text


def _parse_utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include a UTC offset")
    return parsed.astimezone(UTC)


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


if __name__ == "__main__":
    run()
