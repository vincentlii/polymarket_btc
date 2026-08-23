"""Run stage-aware OOF and bounded model selection on an anchored dataset."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data.storage import write_atomic_json  # noqa: E402
from btc_short_horizon.research.market_relative_dataset_io import (  # noqa: E402
    anchored_dataset_sha256,
    load_anchored_direction_dataset,
)
from btc_short_horizon.research.market_relative_model_selection import (  # noqa: E402
    make_oof_candidate_evaluator,
    select_market_relative_models,
)
from btc_short_horizon.research.market_relative_stage_oof import (  # noqa: E402
    run_market_relative_stage_oof,
)
from btc_short_horizon.research.walk_forward import WalkForwardConfig  # noqa: E402
from btc_short_horizon.strategy import OpeningStage  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/btc_short_horizon/baseline.toml")
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-search", choices=("logistic", "bounded"), default="bounded")
    parser.add_argument(
        "--walk-forward-profile",
        choices=("quick-development", "full"),
        default="quick-development",
    )
    parser.add_argument("--bootstrap-resamples", type=int, default=2_000)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--random-seed", type=int, default=17)
    parser.add_argument("--execution-cost-per-share", type=float, default=0.01)
    parser.add_argument("--minimum-trade-edge", type=float, default=0.01)
    parser.add_argument("--minimum-order-size", type=float, default=5.0)
    parser.add_argument("--minimum-side-opportunities", type=int, default=30)
    parser.add_argument("--maximum-side-share", type=float, default=0.80)
    parser.add_argument("--minimum-markets-per-leaf", type=int, default=100)
    parser.add_argument("--familywise-alpha", type=float, default=0.05)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.output.exists():
        raise FileExistsError(args.output)
    if args.bootstrap_resamples < 100:
        raise ValueError("bootstrap-resamples must be >= 100")
    config = load_btc_project_config(args.config)
    anchored, lineage = load_anchored_direction_dataset(args.dataset)
    tail = config.paper_research.tail_entry_price_threshold
    split_config = _walk_forward_config(args.walk_forward_profile)
    shared = dict(
        confidence=args.confidence,
        resamples=args.bootstrap_resamples,
        seed=args.random_seed,
        execution_cost_per_share=args.execution_cost_per_share,
        minimum_trade_edge=args.minimum_trade_edge,
        minimum_order_size=args.minimum_order_size,
        tail_quarantine_price=tail,
        minimum_side_opportunities=args.minimum_side_opportunities,
        maximum_side_share=args.maximum_side_share,
        stage_policy=config.stage_policy,
        split_config=split_config,
    )
    baseline = run_market_relative_stage_oof(anchored, **shared)
    selection = select_market_relative_models(
        stage_inputs={stage: anchored for stage in OpeningStage},
        evaluate_candidate=make_oof_candidate_evaluator(**shared),
        mode=args.model_search,
        random_seed=args.random_seed,
        minimum_markets_per_leaf=args.minimum_markets_per_leaf,
        familywise_alpha=args.familywise_alpha,
    )
    stage_gates = {
        stage.value: bool(selection["stages"][stage.value]["selection_gate_passed"])
        for stage in OpeningStage
    }
    payload = {
        "schema_version": "btc-market-relative-dataset-research-v1",
        "status": "development_complete_no_go",
        "dataset_artifact": {
            "path": str(args.dataset),
            "sha256": anchored_dataset_sha256(args.dataset),
            "market_count": len({sample.group_id for sample in anchored.dataset.samples}),
            "sample_count": len(anchored.dataset.samples),
        },
        "lineage": lineage,
        "baseline_stage_oof": baseline,
        "model_selection": selection,
        "stage_selection_gates": stage_gates,
        "runtime_promotion_eligible": False,
        "sealed_holdout_evaluated": False,
        "promotion": {
            "status": "blocked",
            "reason": "sealed_forward_holdout_required",
        },
        "research_contract": {
            "tail_quarantine_price": tail,
            "bootstrap_resamples": args.bootstrap_resamples,
            "confidence": args.confidence,
            "execution_cost_per_share": args.execution_cost_per_share,
            "minimum_trade_edge": args.minimum_trade_edge,
            "minimum_side_opportunities": args.minimum_side_opportunities,
            "maximum_side_share": args.maximum_side_share,
            "minimum_markets_per_leaf": args.minimum_markets_per_leaf,
            "familywise_alpha": args.familywise_alpha,
            "walk_forward_profile": args.walk_forward_profile,
            "walk_forward": {
                name: value.total_seconds() if isinstance(value, timedelta) else value
                for name, value in asdict(split_config).items()
            },
        },
    }
    write_atomic_json(args.output, payload)
    return payload


def _walk_forward_config(profile: str) -> WalkForwardConfig:
    if profile == "full":
        return WalkForwardConfig()
    if profile != "quick-development":
        raise ValueError(f"unsupported walk-forward profile: {profile}")
    return WalkForwardConfig(
        train_duration=timedelta(days=3),
        calibration_duration=timedelta(days=1),
        test_duration=timedelta(days=1),
        step_duration=timedelta(days=1),
        embargo_duration=timedelta(hours=4, minutes=15),
        sealed_holdout_duration=timedelta(days=1),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = run(args)
    print(
        json.dumps(
            {
                "status": result["status"],
                "output": str(args.output),
                "stage_selection_gates": result["stage_selection_gates"],
                "runtime_promotion_eligible": result["runtime_promotion_eligible"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
