"""Run the fail-closed BTC market-relative V2 research chain from raw evidence."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import timedelta
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from typing import Sequence

import numpy as np

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data import read_market_catalog  # noqa: E402
from btc_short_horizon.data.rule_contract import rule_contract_sha256  # noqa: E402
from btc_short_horizon.research.market_relative_stage_oof import (  # noqa: E402
    AnchoredDirectionDataset,
    run_market_relative_stage_oof,
)
from btc_short_horizon.research.market_relative_model_selection import (  # noqa: E402
    make_oof_candidate_evaluator,
    select_market_relative_models,
)
from btc_short_horizon.research.exit_replay import fee_rule_sha256  # noqa: E402
from btc_short_horizon.research.market_relative_v2 import (  # noqa: E402
    MarketRelativeV2FactorFamily,
    materialize_market_relative_v2,
)
from btc_short_horizon.research.market_relative_workflow import (  # noqa: E402
    evaluate_research_workflow,
)
from btc_short_horizon.research.opening_features import (  # noqa: E402
    build_forward_opening_feature_observations,
)
from btc_short_horizon.research.opening_proxy import (  # noqa: E402
    opening_proxy_decision_offsets_ms,
)
from btc_short_horizon.research.pipeline import DirectionDataset  # noqa: E402
from btc_short_horizon.strategy import OpeningStage  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/btc_short_horizon/baseline.toml")
    )
    parser.add_argument("--market-catalog", type=Path, required=True)
    parser.add_argument("--raw-data-root", type=Path)
    parser.add_argument("--legacy-probabilities-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument(
        "--family", action="append", choices=[value.value for value in MarketRelativeV2FactorFamily]
    )
    parser.add_argument("--minimum-eligible-markets", type=int)
    parser.add_argument("--bootstrap-resamples", type=int, default=5_000)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--random-seed", type=int, default=17)
    parser.add_argument("--logistic-c", type=float, default=0.1)
    parser.add_argument(
        "--calibration-method",
        choices=("identity", "sigmoid", "isotonic", "temperature", "beta", "auto"),
        default="auto",
    )
    parser.add_argument("--execution-cost-per-share", type=float, default=0.01)
    parser.add_argument("--minimum-trade-edge", type=float, default=0.0)
    parser.add_argument("--fee-rate", type=float, required=True)
    parser.add_argument("--fee-exponent", type=int, required=True)
    parser.add_argument("--minimum-order-size", type=float, default=5.0)
    parser.add_argument("--tail-quarantine-price", type=float)
    parser.add_argument(
        "--model-search", choices=("none", "logistic", "bounded"), default="bounded"
    )
    parser.add_argument("--minimum-markets-per-leaf", type=int, default=100)
    parser.add_argument("--familywise-alpha", type=float, default=0.05)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_btc_project_config(args.config)
    minimum_eligible_markets = (
        config.paper_research.evidence_target_markets
        if args.minimum_eligible_markets is None
        else args.minimum_eligible_markets
    )
    tail_quarantine_price = (
        config.paper_research.tail_entry_price_threshold
        if args.tail_quarantine_price is None
        else args.tail_quarantine_price
    )
    if minimum_eligible_markets < 1:
        raise ValueError("--minimum-eligible-markets must be >= 1")
    if args.bootstrap_resamples < 100:
        raise ValueError("--bootstrap-resamples must be >= 100")
    if not isfinite(args.confidence) or not 0.5 < args.confidence < 1.0:
        raise ValueError("--confidence must be in (0.5, 1)")
    if args.random_seed < 0:
        raise ValueError("--random-seed must be non-negative")
    if not isfinite(args.logistic_c) or args.logistic_c <= 0.0:
        raise ValueError("--logistic-c must be finite and > 0")
    if not isfinite(args.execution_cost_per_share) or args.execution_cost_per_share < 0.0:
        raise ValueError("--execution-cost-per-share must be finite and non-negative")
    if not isfinite(args.minimum_trade_edge) or args.minimum_trade_edge < 0.0:
        raise ValueError("--minimum-trade-edge must be finite and non-negative")
    if not isfinite(args.fee_rate) or not 0.0 <= args.fee_rate < 1.0:
        raise ValueError("--fee-rate must be finite and in [0, 1)")
    if args.fee_exponent != 1:
        raise ValueError("--fee-exponent must match the supported exponent-1 fee curve")
    if not isfinite(args.minimum_order_size) or args.minimum_order_size <= 0.0:
        raise ValueError("--minimum-order-size must be finite and > 0")
    if not isfinite(tail_quarantine_price) or not 0.0 < tail_quarantine_price < 0.5:
        raise ValueError("--tail-quarantine-price must be finite and in (0, 0.5)")
    if args.minimum_markets_per_leaf < 1:
        raise ValueError("--minimum-markets-per-leaf must be >= 1")
    if not isfinite(args.familywise_alpha) or not 0.0 < args.familywise_alpha < 1.0:
        raise ValueError("--familywise-alpha must be finite and in (0, 1)")
    fee_hash = fee_rule_sha256(args.fee_rate, args.fee_exponent)
    if args.output_json.exists():
        raise FileExistsError(f"output already exists: {args.output_json}")
    probability_bytes = args.legacy_probabilities_json.read_bytes()
    probabilities_by_market = json.loads(probability_bytes)
    if not isinstance(probabilities_by_market, dict):
        raise ValueError("Legacy probabilities must be a slug -> decision_ns -> probability object")
    families = tuple(
        MarketRelativeV2FactorFamily(value)
        for value in (args.family or [value.value for value in MarketRelativeV2FactorFamily])
    )
    required_venues = ["binance_spot", "binance_perp"]
    if any(
        family
        in {MarketRelativeV2FactorFamily.OKX_FLOW_BOOK, MarketRelativeV2FactorFamily.CROSS_VENUE}
        for family in families
    ):
        required_venues.extend(("okx_spot", "okx_swap"))
    records = []
    errors: dict[str, str] = {}
    for market in read_market_catalog(args.market_catalog).windows():
        if market.resolution is None or market.resolution.value == "void":
            continue
        supplied = probabilities_by_market.get(market.slug)
        if not isinstance(supplied, dict):
            errors[market.slug] = "missing_legacy_probabilities"
            continue
        try:
            direction = {int(key): float(value) for key, value in supplied.items()}
        except (TypeError, ValueError) as exc:
            errors[market.slug] = f"invalid_legacy_probabilities:{exc}"
            continue
        protocol_offsets = opening_proxy_decision_offsets_ms(
            cadence_ms=config.research_timing.model_cadence_ms,
            entry_start_seconds=config.research_timing.entry_start_seconds,
            entry_end_seconds=config.research_timing.entry_end_seconds,
        )
        if len(protocol_offsets) != 36:
            raise ValueError("market-relative V2 requires the frozen 36-snapshot protocol")
        expected = tuple(
            int(market.t0.timestamp() * 1_000_000_000) + offset_ms * 1_000_000
            for offset_ms in protocol_offsets
        )
        if tuple(sorted(direction)) != expected or any(
            not isfinite(value) or not 0.0 < value < 1.0 for value in direction.values()
        ):
            errors[market.slug] = "legacy_probability_protocol_mismatch"
            continue
        try:
            raw_build = build_forward_opening_feature_observations(
                raw_data_root=args.raw_data_root or config.paths.raw_data_root,
                market=market,
                start_time=market.t0
                - timedelta(seconds=config.research_timing.max_feature_lookback_seconds),
                end_time=market.t0 + timedelta(seconds=config.research_timing.entry_end_seconds),
                decision_ts_ns=expected,
                ingest_version=config.collection.ingest_version,
                polymarket_source_timestamp_regression_tolerance_seconds=(
                    config.collection.polymarket_source_timestamp_regression_tolerance_seconds
                ),
                required_venue_sources=tuple(required_venues),
            )
            records.append((market, direction, raw_build))
        except (OSError, ValueError) as exc:
            errors[market.slug] = f"{type(exc).__name__}:{exc}"

    rule_epochs = {market.rule_epoch for market, _direction, _raw_build in records}
    if len(rule_epochs) > 1:
        raise ValueError("one research run cannot mix multiple rule epochs")

    datasets_by_ablation: dict[str, tuple[AnchoredDirectionDataset, ...]] = {}
    coverage = []
    for end in range(1, len(families) + 1):
        selected = families[:end]
        builds = [_materialize(record, selected) for record in records]
        datasets = tuple(
            AnchoredDirectionDataset(
                dataset=build.dataset,
                market_up_probabilities=np.asarray(build.market_up_probabilities, dtype=float),
                up_best_asks=np.asarray(build.up_best_asks, dtype=float),
                down_best_asks=np.asarray(build.down_best_asks, dtype=float),
                up_best_ask_sizes=np.asarray(build.up_best_ask_sizes, dtype=float),
                down_best_ask_sizes=np.asarray(build.down_best_ask_sizes, dtype=float),
                fee_rates=np.full(len(build.market_up_probabilities), args.fee_rate),
                fee_rule_hash=fee_hash,
            )
            for build in builds
            if build.dataset is not None
        )
        key = "+".join(value.value for value in selected)
        coverage = (
            [asdict(build.coverage) for build in builds] if end == len(families) else coverage
        )
        datasets_by_ablation[key] = datasets

    full_key = "+".join(value.value for value in families)
    workflow = evaluate_research_workflow(
        datasets_by_ablation=datasets_by_ablation,
        selected_ablation=full_key,
        minimum_eligible_markets=minimum_eligible_markets,
        merge_datasets=_merge,
        evaluate_stages=lambda dataset: run_market_relative_stage_oof(
            dataset,
            confidence=args.confidence,
            resamples=args.bootstrap_resamples,
            seed=args.random_seed,
            logistic_c=args.logistic_c,
            calibration_method=args.calibration_method,
            execution_cost_per_share=args.execution_cost_per_share,
            minimum_trade_edge=args.minimum_trade_edge,
            minimum_order_size=args.minimum_order_size,
            tail_quarantine_price=tail_quarantine_price,
            stage_policy=config.stage_policy,
        ),
    )
    model_selection: dict[str, object] = {"status": "not_requested", "mode": "none"}
    selected_datasets = datasets_by_ablation.get(full_key, ())
    if args.model_search != "none" and selected_datasets:
        try:
            selected_dataset = _merge(selected_datasets)
            model_selection = select_market_relative_models(
                stage_inputs={stage: selected_dataset for stage in OpeningStage},
                evaluate_candidate=make_oof_candidate_evaluator(
                    confidence=args.confidence,
                    resamples=args.bootstrap_resamples,
                    seed=args.random_seed,
                    execution_cost_per_share=args.execution_cost_per_share,
                    minimum_trade_edge=args.minimum_trade_edge,
                    minimum_order_size=args.minimum_order_size,
                    tail_quarantine_price=tail_quarantine_price,
                    stage_policy=config.stage_policy,
                ),
                mode=args.model_search,
                random_seed=args.random_seed,
                minimum_markets_per_leaf=args.minimum_markets_per_leaf,
                familywise_alpha=args.familywise_alpha,
            )
        except ValueError as exc:
            model_selection = {
                "status": "no_go",
                "mode": args.model_search,
                "reason": f"{type(exc).__name__}:{exc}",
            }
    payload = {
        "schema_version": "btc-market-relative-v2-research-v2",
        "status": workflow.status,
        "failed_stage": workflow.failed_stage,
        "families": [value.value for value in families],
        "coverage": coverage,
        "errors": errors,
        "ablations": workflow.ablations,
        "exit_replay": {"status": "not_requested", "artifact_sha256": None},
        "promotion": workflow.promotion,
        "model_selection": model_selection,
        "research_contract": {
            "minimum_independent_markets_required": minimum_eligible_markets,
            "tail_quarantine_price": tail_quarantine_price,
            "confidence": args.confidence,
            "bootstrap_resamples": args.bootstrap_resamples,
            "random_seed": args.random_seed,
            "fee_rule_hash": fee_hash,
        },
        "input_hashes": {
            "catalog": _hash(args.market_catalog),
            "legacy_probabilities": sha256(probability_bytes).hexdigest(),
        },
        "rule_fingerprints": [
            {
                "market_slug": market.slug,
                "rule_epoch": market.rule_epoch,
                "rule_hash": market.rule_hash,
                "rule_contract_sha256": rule_contract_sha256(market.rule_epoch),
            }
            for market, _direction, _raw_build in records
        ],
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, sort_keys=True))
    return 2


def _materialize(record, families):  # type: ignore[no-untyped-def]
    market, direction, raw_build = record
    return materialize_market_relative_v2(
        market_slug=market.slug,
        up_token_id=market.up_token_id,
        down_token_id=market.down_token_id,
        label=int(market.resolution.value == "up"),
        label_available_ts=market.label_available_ts or market.t1,
        direction_p_up_by_decision=direction,
        raw_build=raw_build,
        families=families,
    )


def _merge(datasets: Sequence[AnchoredDirectionDataset]) -> AnchoredDirectionDataset:
    schema = datasets[0].dataset.schema
    if any(value.dataset.schema.hash != schema.hash for value in datasets):
        raise ValueError("eligible market datasets use different feature schemas")
    if len({value.fee_rule_hash for value in datasets}) != 1:
        raise ValueError("eligible market datasets use different fee contracts")
    direction = DirectionDataset(
        samples=tuple(sample for value in datasets for sample in value.dataset.samples),
        vectors=np.vstack([value.dataset.vectors for value in datasets]),
        schema=schema,
        sample_weights=np.concatenate([value.dataset.sample_weights for value in datasets]),
    )
    return AnchoredDirectionDataset(
        dataset=direction,
        market_up_probabilities=np.concatenate(
            [value.market_up_probabilities for value in datasets]
        ),
        up_best_asks=np.concatenate([value.up_best_asks for value in datasets]),
        down_best_asks=np.concatenate([value.down_best_asks for value in datasets]),
        up_best_ask_sizes=np.concatenate([value.up_best_ask_sizes for value in datasets]),
        down_best_ask_sizes=np.concatenate([value.down_best_ask_sizes for value in datasets]),
        fee_rates=np.concatenate([value.fee_rates for value in datasets]),
        fee_rule_hash=datasets[0].fee_rule_hash,
    )


def _hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
