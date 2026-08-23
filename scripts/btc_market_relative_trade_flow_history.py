"""Append causal Binance Spot trade-flow features to a frozen Core dataset."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path

import numpy as np
import pandas as pd

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data.storage import sha256_file, write_atomic_json  # noqa: E402
from btc_short_horizon.research.market_relative_dataset_io import (  # noqa: E402
    anchored_dataset_sha256,
    load_anchored_direction_dataset,
    save_anchored_direction_dataset,
)
from btc_short_horizon.research.market_relative_stage_oof import (  # noqa: E402
    AnchoredDirectionDataset,
)
from btc_short_horizon.research.market_relative_v2 import (  # noqa: E402
    market_relative_v2_profile_families,
    market_relative_v2_research_schema,
)
from btc_short_horizon.research.pipeline import DirectionDataset  # noqa: E402


_FLOW_COLUMNS = (
    "binance_spot_return_5s",
    "binance_spot_rv_5s",
    "binance_spot_flow_5s",
    "binance_spot_return_15s",
    "binance_spot_rv_15s",
    "binance_spot_flow_15s",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-dataset", type=Path, required=True)
    parser.add_argument("--legacy-flow-dataset", type=Path, required=True)
    parser.add_argument("--output-dataset", type=Path, required=True)
    parser.add_argument("--output-receipt", type=Path, required=True)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    core, parent_lineage = load_anchored_direction_dataset(args.core_dataset)
    core_schema = market_relative_v2_research_schema(market_relative_v2_profile_families("core"))
    if core.dataset.schema != core_schema or parent_lineage.get("profile") != "core":
        raise ValueError("input must be a verified market-relative Core dataset")

    flow = pd.read_parquet(
        args.legacy_flow_dataset,
        columns=["sample_id", *_FLOW_COLUMNS],
    )
    if flow["sample_id"].duplicated().any():
        raise ValueError("Legacy flow dataset sample IDs must be unique")
    flow = flow.set_index("sample_id")
    sample_ids = [sample.sample_id for sample in core.dataset.samples]
    missing = sorted(set(sample_ids) - set(flow.index))
    if missing:
        raise ValueError(f"Legacy flow dataset is missing {len(missing)} anchored samples")
    flow_matrix = flow.loc[sample_ids, list(_FLOW_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(flow_matrix).all():
        raise ValueError("Legacy flow features must be finite")

    families = market_relative_v2_profile_families("trade_flow")
    schema = market_relative_v2_research_schema(families)
    direction = DirectionDataset(
        samples=core.dataset.samples,
        vectors=np.column_stack((core.dataset.vectors, flow_matrix)),
        schema=schema,
        sample_weights=core.dataset.sample_weights,
    )
    augmented = AnchoredDirectionDataset(
        dataset=direction,
        market_up_probabilities=core.market_up_probabilities,
        up_best_asks=core.up_best_asks,
        down_best_asks=core.down_best_asks,
        up_best_ask_sizes=core.up_best_ask_sizes,
        down_best_ask_sizes=core.down_best_ask_sizes,
        fee_rates=core.fee_rates,
        fee_rule_hash=core.fee_rule_hash,
    )
    lineage = dict(parent_lineage)
    lineage.update(
        {
            "builder": "btc-market-relative-trade-flow-history-v1",
            "profile": "trade_flow",
            "families": [value.value for value in families],
            "parent_core_dataset_sha256": anchored_dataset_sha256(args.core_dataset),
            "legacy_flow_dataset_sha256": sha256_file(args.legacy_flow_dataset),
            "flow_columns": list(_FLOW_COLUMNS),
        }
    )
    dataset_sha = save_anchored_direction_dataset(
        path=args.output_dataset,
        dataset=augmented,
        lineage=lineage,
    )
    receipt = {
        "schema_version": "btc-market-relative-trade-flow-history-receipt-v1",
        "status": "ready_for_development_oof",
        "dataset": str(args.output_dataset),
        "dataset_sha256": dataset_sha,
        "market_count": len({sample.group_id for sample in direction.samples}),
        "sample_count": len(direction.samples),
        "feature_schema_hash": schema.hash,
        "lineage": lineage,
        "sealed_holdout_evaluated": False,
        "runtime_promotion_eligible": False,
    }
    write_atomic_json(args.output_receipt, receipt)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
