"""Run one explicit BTC 15-minute dual-token Opening Mispricing maker replay."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
from btc_short_horizon.models import ModelArtifactMetadata  # noqa: E402
from btc_short_horizon.reporting import BtcRunArtifacts, RunArtifactWriter, RunManifest  # noqa: E402


@dataclass(frozen=True, slots=True)
class RunnerInputs:
    config: BtcProjectConfig
    market: MarketWindow
    signals: tuple[BtcOpeningMispricingSignal, ...]
    model_hash: str
    pmxt_coverage: Mapping[str, object]


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
    parser.add_argument("--scenario", default="p95")
    parser.add_argument("--up-token-index", type=int, default=0)
    parser.add_argument("--down-token-index", type=int, default=1)
    parser.add_argument("--initial-cash", type=float, default=100.0)
    parser.add_argument("--probability-window", type=int, default=30)
    parser.add_argument("--name-prefix", default="btc_15m_opening_mispricing_maker")
    parser.add_argument("--artifact-directory", type=Path, required=True)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--upstream-revision", required=True)
    parser.add_argument("--signal-manifest", type=Path, required=True)
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--pmxt-coverage", type=Path, required=True)
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
    manifest_model_hash = _validate_signal_manifest(
        path=args.signal_manifest,
        config=config,
        market=market,
        signals=signals,
    )
    model_hash = _validate_model_artifact(
        directory=args.model_directory,
        signal=signals[0],
        expected_model_hash=manifest_model_hash,
    )
    pmxt_coverage = _validate_pmxt_coverage(
        path=args.pmxt_coverage,
        market=market,
        replay_start=args.start_time,
        replay_book_end=min(args.end_time, market.t1),
    )
    return RunnerInputs(
        config=config,
        market=market,
        signals=signals,
        model_hash=model_hash,
        pmxt_coverage=pmxt_coverage,
    )


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
    if scenario.formal_grid_component:
        _validate_formal_results(results=results, order_events=order_events)
    else:
        _validate_fill_ledger(results=results, order_events=order_events)
    opening_paths, closed_trades = _flatten_results(
        market_slug=inputs.market.slug,
        results=results,
        order_events=order_events,
    )
    fills = _audit_fill_records(
        market_slug=inputs.market.slug,
        results=results,
        order_events=order_events,
    )
    prediction_records = tuple(_prediction_record(signal) for signal in inputs.signals)
    return BtcRunArtifacts(
        manifest=RunManifest(
            code_revision=_require_git_revision(args.code_revision, "code_revision"),
            upstream_revision=_require_git_revision(
                args.upstream_revision,
                "upstream_revision",
            ),
            data_hashes={
                "config": _sha256_file(args.config),
                **_market_input_hashes(args),
                "opening_mispricing_signals": _sha256_file(args.signals),
                "signal_manifest": _sha256_file(args.signal_manifest),
                "model_metadata": _sha256_file(args.model_directory / "metadata.json"),
                "pmxt_coverage": _sha256_file(args.pmxt_coverage),
            },
            model_hashes={inputs.signals[0].model_version: inputs.model_hash},
            scenario={
                "name": scenario.name,
                "formal_grid_component": scenario.formal_grid_component,
                "standalone_strategy_conclusion": False,
                "queue_position": execution.queue_position,
                "maker_rebates_enabled": execution.maker_rebates_enabled,
                "trade_execution_size_multiplier": execution.trade_execution_size_multiplier,
                "same_timestamp_priority": execution.same_timestamp_priority.value,
                "latency": {
                    "base_latency_ms": execution.latency_model.base_latency_ms,
                    "insert_latency_ms": execution.latency_model.insert_latency_ms,
                    "update_latency_ms": execution.latency_model.update_latency_ms,
                    "cancel_latency_ms": execution.latency_model.cancel_latency_ms,
                },
                "maker_fee_rate": 0.0,
                "rebate_model": "disabled",
                "max_visible_depth_fraction": (inputs.config.maker.max_visible_depth_fraction),
            },
            seed=int(args.seed),
        ),
        data_quality={
            "market_slug": inputs.market.slug,
            "token_replay_result_count": len(results),
            "configured_sources": list(inputs.config.data_sources),
            "pmxt_coverage_eligible": True,
            "pmxt_coverage": dict(
                _mapping_field(inputs.pmxt_coverage, "coverage", "PMXT coverage")
            ),
            "pmxt_tokens": {
                side: dict(
                    _mapping_field(
                        _mapping_field(inputs.pmxt_coverage, "tokens", "PMXT coverage"),
                        side,
                        "PMXT coverage tokens",
                    )
                )
                for side in ("up", "down")
            },
            "formal_grid_component": scenario.formal_grid_component,
            "settlement_complete": bool(results)
            and all(result.get("settlement_pnl_applied") is True for result in results),
            "execution_anomaly_free": not any(
                event.get("event_type") == "cancel_rejected"
                or (
                    event.get("event_type") == "fill"
                    and str(event.get("liquidity_side", "")).upper() != "MAKER"
                )
                for event in order_events
            ),
        },
        predictions=prediction_records,
        opening_paths=opening_paths,
        opportunities=prediction_records,
        orders=tuple(dict(event) for event in order_events),
        fills=fills,
        closed_trades=closed_trades,
        metrics=_result_metrics(results, order_events=order_events, fills=fills),
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
            up_records_sha256=_pmxt_records_sha256(inputs.pmxt_coverage, "up"),
            down_records_sha256=_pmxt_records_sha256(inputs.pmxt_coverage, "down"),
            initial_cash=args.initial_cash,
            probability_window=args.probability_window,
            formal_grid_component=scenario.formal_grid_component,
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
    return _read_json_object(path, "metadata")


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} JSON must contain one object")
    return payload


def _validate_signal_manifest(
    *,
    path: Path,
    config: BtcProjectConfig,
    market: MarketWindow,
    signals: Sequence[BtcOpeningMispricingSignal],
) -> str:
    payload = _read_json_object(path, "signal manifest")
    if payload.get("study_type") != "opening_proxy_one_shot_shadow":
        raise ValueError("signal manifest has an unexpected study_type")
    orders_submitted = payload.get("orders_submitted")
    if (
        payload.get("mode") != "shadow"
        or isinstance(orders_submitted, bool)
        or not isinstance(orders_submitted, int)
        or orders_submitted != 0
    ):
        raise ValueError("signal manifest must describe a zero-order Shadow run")
    if payload.get("market_slug") != market.slug:
        raise ValueError("signal manifest market_slug does not match the replay market")
    if payload.get("ingest_version") != config.collection.ingest_version:
        raise ValueError("signal manifest ingest_version does not match project configuration")
    model = _mapping_field(payload, "model", "signal manifest")
    signal_model = signals[0]
    if model.get("model_id") != signal_model.model_version:
        raise ValueError("signal manifest model_id does not match the signal rows")
    schema_hash = _require_sha256(
        signal_model.feature_schema_hash,
        "signal feature_schema_hash",
    )
    if model.get("feature_schema_hash") != schema_hash:
        raise ValueError("signal manifest feature schema does not match the signal rows")
    coverage = _mapping_field(payload, "coverage", "signal manifest")
    prediction_count = coverage.get("predictions")
    if (
        isinstance(prediction_count, bool)
        or not isinstance(prediction_count, int)
        or prediction_count != len(signals)
    ):
        raise ValueError("signal manifest prediction count does not match the signal rows")
    return _require_sha256(model.get("model_sha256"), "signal manifest model_sha256")


def _validate_pmxt_coverage(
    *,
    path: Path,
    market: MarketWindow,
    replay_start: datetime,
    replay_book_end: datetime,
) -> Mapping[str, object]:
    payload = _read_json_object(path, "PMXT coverage")
    if payload.get("market_slug") != market.slug:
        raise ValueError("PMXT coverage market_slug does not match the replay market")
    window = _mapping_field(payload, "window", "PMXT coverage")
    covered_start = _json_utc_datetime(window.get("start"), "PMXT coverage window start")
    covered_end = _json_utc_datetime(window.get("end"), "PMXT coverage window end")
    if covered_start > replay_start or covered_end < replay_book_end:
        raise ValueError("PMXT coverage window does not contain the requested book replay")
    tokens = _mapping_field(payload, "tokens", "PMXT coverage")
    for side, expected_token_id in (
        ("up", market.up_token_id),
        ("down", market.down_token_id),
    ):
        token = _mapping_field(tokens, side, "PMXT coverage tokens")
        if token.get("token_id") != expected_token_id:
            raise ValueError(f"PMXT coverage {side} token does not match the market catalog")
        for count_name in (
            "source_book_event_count",
            "reconstructed_book_state_event_count",
            "trade_tick_count",
        ):
            count = token.get(count_name)
            if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                raise ValueError(f"PMXT coverage {side} {count_name} must be a positive integer")
        _require_sha256(
            token.get("records_sha256"),
            f"PMXT coverage {side} records_sha256",
        )
        gap_hours = token.get("gap_hours")
        if not isinstance(gap_hours, list) or gap_hours:
            raise ValueError(f"PMXT coverage {side} gap_hours must be an empty list")
    coverage = _mapping_field(payload, "coverage", "PMXT coverage")
    for field in (
        "books_covered",
        "trade_ticks_covered",
        "no_archive_gaps",
        "eligible_for_joint_replay",
    ):
        if coverage.get(field) is not True:
            raise ValueError(f"PMXT coverage requires {field}=true")
    return payload


def _validate_model_artifact(
    *,
    directory: Path,
    signal: BtcOpeningMispricingSignal,
    expected_model_hash: str,
) -> str:
    payload = _read_json_object(directory / "metadata.json", "model metadata")
    try:
        metadata = ModelArtifactMetadata(**payload)
    except TypeError as exc:
        raise ValueError("model metadata has unexpected or missing fields") from exc
    if metadata.model_id != signal.model_version:
        raise ValueError("model artifact model_id does not match the signal rows")
    if metadata.feature_schema_hash != _require_sha256(
        signal.feature_schema_hash,
        "signal feature_schema_hash",
    ):
        raise ValueError("model artifact feature schema does not match the signal rows")
    model_path = directory / "model.joblib"
    actual_model_hash = _sha256_file(model_path)
    if metadata.model_sha256 != actual_model_hash:
        raise ValueError("model artifact SHA-256 does not match metadata.json")
    if actual_model_hash != expected_model_hash:
        raise ValueError("model artifact SHA-256 does not match the Shadow signal manifest")
    _require_git_revision(metadata.code_revision, "model metadata code_revision")
    return actual_model_hash


def _pmxt_records_sha256(payload: Mapping[str, object], side: str) -> str:
    tokens = _mapping_field(payload, "tokens", "PMXT coverage")
    token = _mapping_field(tokens, side, "PMXT coverage tokens")
    return _require_sha256(
        token.get("records_sha256"),
        f"PMXT coverage {side} records_sha256",
    )


def _mapping_field(
    payload: Mapping[str, object],
    field: str,
    label: str,
) -> Mapping[str, object]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} {field} must be an object")
    return value


def _json_utc_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    return parsed.astimezone(UTC)


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
    *,
    market_slug: str,
    results: Sequence[Mapping[str, object]],
    order_events: Sequence[Mapping[str, object]],
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    opening_paths: list[dict[str, object]] = []
    closed_trades: list[dict[str, object]] = []
    fill_event_count_by_instrument: dict[str, int] = {}
    for event in order_events:
        if event.get("event_type") == "fill":
            instrument_id = str(event.get("instrument_id", ""))
            fill_event_count_by_instrument[instrument_id] = (
                fill_event_count_by_instrument.get(instrument_id, 0) + 1
            )
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
        closed_trades.append(
            {
                "market_slug": market_slug,
                "instrument_id": instrument_id,
                "token_index": token_index,
                "filled_order_count": result.get("fills", 0),
                "fill_event_count": fill_event_count_by_instrument.get(instrument_id, 0),
                "pnl": result.get("pnl", 0.0),
                "outcome": result.get("outcome"),
                "realized_outcome": result.get("realized_outcome"),
                "settlement_observable_time": result.get("settlement_observable_time"),
                "settlement_pnl_applied": result.get("settlement_pnl_applied", False),
                "simulated_through": result.get("simulated_through"),
            }
        )
    return tuple(opening_paths), tuple(closed_trades)


def _audit_fill_records(
    *,
    market_slug: str,
    results: Sequence[Mapping[str, object]],
    order_events: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    token_index_by_instrument = {
        str(result.get("instrument_id", "")): result.get("token_index") for result in results
    }
    realized_outcome_by_instrument = {
        str(result.get("instrument_id", "")): result.get("realized_outcome") for result in results
    }
    markouts_by_fill: dict[tuple[str, str], dict[str, object]] = {}
    for event in order_events:
        if event.get("event_type") != "fill_markout":
            continue
        fill_key = (
            str(event.get("client_order_id", "")),
            str(event.get("trade_id", "")),
        )
        horizon_seconds = _as_int(event.get("horizon_seconds"))
        if horizon_seconds in {1, 3, 10, 30, 60}:
            prefix = f"markout_{horizon_seconds}s"
            markout = markouts_by_fill.setdefault(fill_key, {})
            markout[prefix] = event.get("markout")
            markout[f"{prefix}_midpoint"] = event.get("midpoint")
            markout[f"{prefix}_book_ts_ns"] = event.get("book_ts_ns")
            markout[f"{prefix}_book_age_seconds"] = event.get("book_age_seconds")
            markout[f"{prefix}_available"] = event.get("available")
    records: list[dict[str, object]] = []
    for event in order_events:
        if event.get("event_type") != "fill":
            continue
        instrument_id = str(event.get("instrument_id", ""))
        record = {
            "market_slug": market_slug,
            "token_index": token_index_by_instrument.get(instrument_id),
            **dict(event),
        }
        fill_price = _as_float(event.get("price"))
        fill_markouts = markouts_by_fill.get(
            (
                str(event.get("client_order_id", "")),
                str(event.get("trade_id", "")),
            ),
            {},
        )
        for horizon_seconds in (1, 3, 10, 30, 60):
            prefix = f"markout_{horizon_seconds}s"
            for suffix in (
                "",
                "_midpoint",
                "_book_ts_ns",
                "_book_age_seconds",
                "_available",
            ):
                field = f"{prefix}{suffix}"
                record[field] = fill_markouts.get(field)
        realized_outcome = realized_outcome_by_instrument.get(instrument_id)
        record["settlement_markout"] = (
            _as_float(realized_outcome) - fill_price
            if realized_outcome is not None and fill_price > 0.0
            else None
        )
        records.append(record)
    return tuple(records)


def _result_metrics(
    results: Sequence[Mapping[str, object]],
    *,
    order_events: Sequence[Mapping[str, object]],
    fills: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    portfolio_stats = results[0].get("portfolio_stats", {}) if results else {}
    audit_fills = tuple(event for event in order_events if event.get("event_type") == "fill")
    fill_metrics = _audit_fill_metrics(order_events)
    reported_pnl = sum(_as_float(result.get("pnl", 0.0)) for result in results)
    total_shares = float(fill_metrics["total_filled_shares"])
    return {
        "token_replay_result_count": len(results),
        "reported_filled_order_count": sum(_as_int(result.get("fills", 0)) for result in results),
        "audit_fill_event_count": len(audit_fills),
        "cancel_race_fill_count": _cancel_race_fill_count(order_events),
        **_order_lifecycle_metrics(order_events),
        **_order_plan_quality_metrics(order_events),
        **fill_metrics,
        **_markout_metrics(fills),
        "reported_pnl": reported_pnl,
        "realized_pnl_per_share": reported_pnl / total_shares if total_shares > 0.0 else None,
        "settled_token_result_count": sum(
            1 for result in results if result.get("settlement_pnl_applied") is True
        ),
        "portfolio_stats": dict(portfolio_stats) if isinstance(portfolio_stats, Mapping) else {},
    }


def _markout_metrics(fills: Sequence[Mapping[str, object]]) -> dict[str, object]:
    metrics: dict[str, object] = {}
    horizon_fields = (
        "markout_1s",
        "markout_3s",
        "markout_10s",
        "markout_30s",
        "markout_60s",
    )
    for field in (*horizon_fields, "settlement_markout"):
        observations = tuple(
            (_decimal_amount(fill.get("size")), _decimal_amount(fill[field]))
            for fill in fills
            if fill.get(field) is not None
        )
        total_shares = sum((size for size, _ in observations), Decimal(0))
        metrics[f"{field}_observation_count"] = len(observations)
        metrics[f"{field}_per_share"] = (
            float(sum((size * value for size, value in observations), Decimal(0)) / total_shares)
            if total_shares > 0
            else None
        )
        if field in horizon_fields:
            book_ages = tuple(
                _decimal_amount(fill[f"{field}_book_age_seconds"])
                for fill in fills
                if fill.get(f"{field}_book_age_seconds") is not None
            )
            metrics[f"{field}_book_age_seconds_max"] = float(max(book_ages)) if book_ages else None
    return metrics


def _order_lifecycle_metrics(
    order_events: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    names = {
        "plan": "placement_plan_count",
        "submit": "submitted_order_count",
        "accepted": "accepted_order_count",
        "rejected": "rejected_order_count",
        "denied": "denied_order_count",
        "cancel_request": "cancel_request_count",
        "cancel_ack": "cancel_ack_count",
        "cancel_rejected": "cancel_rejected_count",
        "expired": "expired_order_count",
        "fill": "fill_event_count",
        "fill_markout": "fill_markout_event_count",
    }
    counts = {metric: 0 for metric in names.values()}
    for event in order_events:
        metric = names.get(str(event.get("event_type", "")))
        if metric is not None:
            counts[metric] += 1
    return counts


def _order_plan_quality_metrics(
    order_events: Sequence[Mapping[str, object]],
) -> dict[str, float | None]:
    ages = tuple(
        _decimal_amount(event.get("entry_book_age_seconds_max"))
        for event in order_events
        if event.get("event_type") == "plan" and event.get("entry_book_age_seconds_max") is not None
    )
    return {"entry_book_age_seconds_max": float(max(ages)) if ages else None}


def _audit_fill_metrics(
    order_events: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    fills = tuple(event for event in order_events if event.get("event_type") == "fill")
    if not fills:
        return {
            "total_filled_shares": 0.0,
            "average_entry_price": None,
            "gross_expected_edge_per_share": None,
            "net_expected_edge_per_share": None,
            "buffered_net_expected_edge_per_share": None,
            "fee_total": 0.0,
            "rebate_total": 0.0,
            "up_fill_event_count": 0,
            "down_fill_event_count": 0,
            "partial_fill_order_count": 0,
            "maker_fill_event_count": 0,
        }
    required_fields = ("size", "price", "p_fair", "net_edge", "side", "liquidity_side")
    if any(event.get(field) is None for event in fills for field in required_fields):
        raise ValueError("audit fill is missing strategy attribution fields")
    total_shares = sum(_decimal_amount(event.get("size")) for event in fills)
    total_notional = sum(
        _decimal_amount(event.get("size")) * _decimal_amount(event.get("price")) for event in fills
    )
    gross_expected_value = sum(
        _decimal_amount(event.get("size"))
        * (_decimal_amount(event.get("p_fair")) - _decimal_amount(event.get("price")))
        for event in fills
    )
    net_expected_value = gross_expected_value - sum(
        (_decimal_amount(event.get("commission")) for event in fills), Decimal(0)
    )
    buffered_expected_value = sum(
        _decimal_amount(event.get("size")) * _decimal_amount(event.get("net_edge"))
        - _decimal_amount(event.get("commission"))
        for event in fills
    )
    commissions = tuple(_decimal_amount(event.get("commission")) for event in fills)
    submitted_size_by_order = {
        str(event.get("client_order_id", "")): _decimal_amount(event.get("size"))
        for event in order_events
        if event.get("event_type") == "submit"
    }
    filled_size_by_order: dict[str, Decimal] = {}
    for event in fills:
        order_id = str(event.get("client_order_id", ""))
        filled_size_by_order[order_id] = filled_size_by_order.get(order_id, Decimal(0)) + (
            _decimal_amount(event.get("size"))
        )
    partial_fill_orders = sum(
        1
        for order_id, filled_size in filled_size_by_order.items()
        if order_id in submitted_size_by_order
        and filled_size + Decimal("0.00000001") < submitted_size_by_order[order_id]
    )
    return {
        "total_filled_shares": float(total_shares),
        "average_entry_price": float(total_notional / total_shares),
        "gross_expected_edge_per_share": float(gross_expected_value / total_shares),
        "net_expected_edge_per_share": float(net_expected_value / total_shares),
        "buffered_net_expected_edge_per_share": float(buffered_expected_value / total_shares),
        "fee_total": float(sum((max(value, Decimal(0)) for value in commissions), Decimal(0))),
        "rebate_total": float(sum((max(-value, Decimal(0)) for value in commissions), Decimal(0))),
        "up_fill_event_count": sum(str(event.get("side", "")) == "up" for event in fills),
        "down_fill_event_count": sum(str(event.get("side", "")) == "down" for event in fills),
        "partial_fill_order_count": partial_fill_orders,
        "maker_fill_event_count": sum(
            str(event.get("liquidity_side", "")).upper() == "MAKER" for event in fills
        ),
    }


def _validate_formal_results(
    *,
    results: Sequence[Mapping[str, object]],
    order_events: Sequence[Mapping[str, object]],
) -> None:
    _validate_fill_ledger(results=results, order_events=order_events)
    if len(results) != 2:
        raise ValueError("formal BTC maker evidence requires both token replay results")
    if any(result.get("terminated_early") is not False for result in results):
        raise ValueError("formal BTC maker evidence cannot use an early-terminated replay")
    if any(result.get("settlement_pnl_applied") is not True for result in results):
        raise ValueError("formal BTC maker evidence must run through observable settlement")
    for result in results:
        _decimal_amount(result.get("pnl"))
    instrument_ids = tuple(str(result.get("instrument_id", "")).strip() for result in results)
    if any(not instrument_id for instrument_id in instrument_ids) or len(set(instrument_ids)) != 2:
        raise ValueError("formal BTC maker evidence requires two distinct instruments")
    token_indexes = tuple(result.get("token_index") for result in results)
    if (
        any(isinstance(index, bool) or not isinstance(index, int) for index in token_indexes)
        or len(set(token_indexes)) != 2
    ):
        raise ValueError("formal BTC maker evidence requires two distinct token indexes")
    realized_outcomes = {
        _strict_binary_outcome(result.get("realized_outcome")) for result in results
    }
    if realized_outcomes != {Decimal(0), Decimal(1)}:
        raise ValueError("formal BTC maker evidence requires complementary binary outcomes")
    if any(event.get("event_type") == "cancel_rejected" for event in order_events):
        raise ValueError("formal BTC maker evidence cannot contain a cancel rejection")
    audit_fills = tuple(event for event in order_events if event.get("event_type") == "fill")
    fill_ids = tuple(
        (
            str(event.get("client_order_id", "")).strip(),
            str(event.get("trade_id", "")).strip(),
        )
        for event in audit_fills
    )
    if any(not order_id or not trade_id for order_id, trade_id in fill_ids):
        raise ValueError("formal BTC maker fills require non-empty order IDs and trade_id values")
    if len(set(fill_ids)) != len(fill_ids):
        raise ValueError("formal BTC maker fills require unique order/trade_id pairs")
    if any(str(event.get("liquidity_side", "")).upper() != "MAKER" for event in audit_fills):
        raise ValueError("formal BTC maker evidence requires every fill to be maker-side")
    if any(_decimal_amount(event.get("commission")) != 0 for event in audit_fills):
        raise ValueError("formal BTC maker evidence requires zero maker commission and rebate")
    _validate_formal_markouts(audit_fills=audit_fills, order_events=order_events)


def _validate_formal_markouts(
    *,
    audit_fills: Sequence[Mapping[str, object]],
    order_events: Sequence[Mapping[str, object]],
) -> None:
    horizons = (1, 3, 10, 30, 60)
    fill_timestamps = {
        (
            str(fill.get("client_order_id", "")).strip(),
            str(fill.get("trade_id", "")).strip(),
        ): _strict_nonnegative_int(fill.get("ts_ns"), "formal fill ts_ns")
        for fill in audit_fills
    }
    observed: set[tuple[str, str, int]] = set()
    for event in order_events:
        if event.get("event_type") != "fill_markout":
            continue
        order_id = str(event.get("client_order_id", "")).strip()
        trade_id = str(event.get("trade_id", "")).strip()
        fill_key = (order_id, trade_id)
        if fill_key not in fill_timestamps:
            raise ValueError("formal markout does not match a replay fill")
        horizon = _strict_nonnegative_int(
            event.get("horizon_seconds"),
            "formal markout horizon_seconds",
        )
        if horizon not in horizons:
            raise ValueError("formal markout has an unexpected horizon")
        markout_key = (*fill_key, horizon)
        if markout_key in observed:
            raise ValueError("formal markouts must be unique per fill and horizon")
        observed.add(markout_key)
        markout_ts_ns = _strict_nonnegative_int(event.get("ts_ns"), "formal markout ts_ns")
        if markout_ts_ns != fill_timestamps[fill_key] + horizon * 1_000_000_000:
            raise ValueError("formal markout timestamp does not match its exact horizon")
        available = event.get("available")
        if not isinstance(available, bool):
            raise ValueError("formal markout available flag must be bool")
        if available:
            midpoint = _decimal_amount(event.get("midpoint"))
            markout = _decimal_amount(event.get("markout"))
            book_ts_ns = _strict_nonnegative_int(
                event.get("book_ts_ns"),
                "formal markout book_ts_ns",
            )
            book_age = _decimal_amount(event.get("book_age_seconds"))
            if not Decimal(0) < midpoint < Decimal(1):
                raise ValueError("formal markout midpoint must be within (0, 1)")
            if book_ts_ns > markout_ts_ns or book_age < 0:
                raise ValueError("formal markout book evidence must be causal")
            expected_book_age = Decimal(markout_ts_ns - book_ts_ns) / Decimal(1_000_000_000)
            if not _decimal_close(book_age, expected_book_age):
                raise ValueError("formal markout book age does not match its timestamp")
            if not _decimal_close(markout, midpoint - _decimal_amount(event.get("fill_price"))):
                raise ValueError("formal markout value does not match midpoint minus fill price")
    expected = {(*fill_key, horizon) for fill_key in fill_timestamps for horizon in horizons}
    if observed != expected:
        raise ValueError("formal fills require the complete 1/3/10/30/60-second markout grid")


def _validate_fill_ledger(
    *,
    results: Sequence[Mapping[str, object]],
    order_events: Sequence[Mapping[str, object]],
) -> None:
    engine_fills: dict[str, tuple[str, Mapping[str, object]]] = {}
    reported_filled_orders = 0
    for result in results:
        instrument_id = str(result.get("instrument_id", ""))
        filled_order_count = _strict_nonnegative_int(result.get("fills"), "result fills")
        raw_fill_events = result.get("fill_events")
        if not isinstance(raw_fill_events, list | tuple) or any(
            not isinstance(event, Mapping) for event in raw_fill_events
        ):
            raise ValueError("result fill_events must be a sequence of objects")
        fill_events = tuple(raw_fill_events)
        if filled_order_count != len(fill_events):
            raise ValueError("result filled-order count does not match its fill summaries")
        reported_filled_orders += filled_order_count
        for event in fill_events:
            order_id = str(event.get("order_id", "")).strip()
            if not order_id or order_id in engine_fills:
                raise ValueError("engine fill summaries require unique non-empty order IDs")
            engine_fills[order_id] = (instrument_id, event)
    if reported_filled_orders != len(engine_fills):
        raise ValueError("engine filled-order count does not match serialized fill summaries")

    audit_by_order: dict[str, list[Mapping[str, object]]] = {}
    for event in order_events:
        if event.get("event_type") != "fill":
            continue
        order_id = str(event.get("client_order_id", "")).strip()
        if not order_id:
            raise ValueError("audit fill requires client_order_id")
        audit_by_order.setdefault(order_id, []).append(event)
    if set(audit_by_order) != set(engine_fills):
        raise ValueError("audit fill orders do not match engine fill summaries")

    for order_id, audit_events in audit_by_order.items():
        instrument_id, engine_event = engine_fills[order_id]
        if any(str(event.get("instrument_id", "")) != instrument_id for event in audit_events):
            raise ValueError("audit fill instrument does not match engine fill summary")
        audit_quantity = sum(_decimal_amount(event.get("size")) for event in audit_events)
        if any(
            _decimal_amount(event.get("size")) <= 0
            or not Decimal(0) < _decimal_amount(event.get("price")) < Decimal(1)
            for event in audit_events
        ):
            raise ValueError("audit fill price and quantity must be exchange-valid")
        audit_notional = sum(
            _decimal_amount(event.get("size")) * _decimal_amount(event.get("price"))
            for event in audit_events
        )
        audit_commission = sum(_decimal_amount(event.get("commission")) for event in audit_events)
        engine_quantity = _decimal_amount(engine_event.get("quantity"))
        engine_price = _decimal_amount(engine_event.get("price"))
        engine_commission = _decimal_amount(engine_event.get("commission"))
        if audit_quantity <= 0 or engine_quantity <= 0:
            raise ValueError("fill quantities must be positive")
        if not Decimal(0) < engine_price < Decimal(1):
            raise ValueError("engine average fill price must be within (0, 1)")
        if not _decimal_close(audit_quantity, engine_quantity):
            raise ValueError("audit fill quantity does not match engine fill summary")
        if not _decimal_close(audit_notional, engine_quantity * engine_price):
            raise ValueError("audit fill notional does not match engine average fill price")
        if not _decimal_close(audit_commission, engine_commission):
            raise ValueError("audit fill commission does not match engine fill summary")


def _cancel_race_fill_count(order_events: Sequence[Mapping[str, object]]) -> int:
    pending_cancels: set[str] = set()
    count = 0
    for event in order_events:
        event_type = str(event.get("event_type", ""))
        order_id = str(event.get("client_order_id", ""))
        if event_type == "cancel_request":
            pending_cancels.add(order_id)
        elif event_type == "fill" and order_id in pending_cancels:
            count += 1
        elif event_type in {"cancel_ack", "cancel_rejected", "expired", "rejected", "denied"}:
            pending_cancels.discard(order_id)
    return count


def _decimal_amount(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or isinstance(value, bool):
        raise ValueError(f"numeric ledger value is required: {value!r}")
    text = str(value).strip().replace("_", "")
    token = text.split(maxsplit=1)[0]
    try:
        amount = Decimal(token)
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric ledger value: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"numeric ledger value must be finite: {value!r}")
    return amount


def _decimal_close(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= Decimal("0.00000001")


def _strict_binary_outcome(value: object) -> Decimal:
    outcome = _decimal_amount(value)
    if outcome not in {Decimal(0), Decimal(1)}:
        raise ValueError("formal realized_outcome must be exactly 0 or 1")
    return outcome


def _strict_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


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


def _require_git_revision(value: object, name: str) -> str:
    text = _require_text(value, name).casefold()
    if len(text) not in {40, 64} or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a full hexadecimal Git revision")
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
