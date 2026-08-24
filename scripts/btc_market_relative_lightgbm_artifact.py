"""Fit and publish the frozen Paper-only market-relative LightGBM artifact."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import subprocess

import numpy as np

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data.catalog_io import read_market_catalog  # noqa: E402
from btc_short_horizon.data.rule_contract import rule_contract_sha256  # noqa: E402
from btc_short_horizon.models.artifacts import ModelArtifactMetadata  # noqa: E402
from btc_short_horizon.models.market_relative import (  # noqa: E402
    market_relative_runtime_feature_schema,
)
from btc_short_horizon.models.market_relative_artifacts import (  # noqa: E402
    MarketRelativeArtifactStore,
)
from btc_short_horizon.models.market_relative_lightgbm import (  # noqa: E402
    fit_market_relative_lightgbm,
    require_minimum_leaf_unique_markets,
)
from btc_short_horizon.research.market_relative_shadow import (  # noqa: E402
    load_market_relative_shadow_dataset,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--market-catalog", type=Path, action="append", required=True)
    parser.add_argument("--end-before", required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--rule-epoch", required=True)
    parser.add_argument("--maximum-pair-age-seconds", type=float, default=1.0)
    parser.add_argument("--probability-uncertainty-radius", type=float)
    parser.add_argument("--uncertainty-confidence", type=float, default=0.95)
    parser.add_argument("--minimum-markets-per-leaf", type=int, default=100)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    end_before = datetime.fromisoformat(args.end_before.replace("Z", "+00:00"))
    if end_before.tzinfo is None or end_before.utcoffset() is None:
        raise ValueError("--end-before must include a timezone")
    if not isfinite(args.uncertainty_confidence) or not 0.5 < args.uncertainty_confidence < 1.0:
        raise ValueError("--uncertainty-confidence must be in (0.5, 1)")
    labels: dict[str, int] = {}
    for path in args.market_catalog:
        for market in read_market_catalog(path).windows():
            if market.resolution is None or market.resolution.value == "void":
                continue
            label = int(market.resolution.value == "up")
            previous = labels.setdefault(market.slug, label)
            if previous != label:
                raise ValueError(f"catalogs disagree on outcome for {market.slug}")
    build = load_market_relative_shadow_dataset(
        shadow_root=args.shadow_root,
        labels_by_market=labels,
        end_before_epoch_seconds=int(end_before.timestamp()),
        maximum_pair_age_seconds=args.maximum_pair_age_seconds,
    )
    dataset = build.dataset
    if dataset.market_count < 1_000:
        raise ValueError("Paper LightGBM publication requires at least 1,000 eligible markets")
    days = np.asarray(
        [
            datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).date()
            for value in dataset.trigger_ts_ns
        ],
        dtype=object,
    )
    validation_day = max(days)
    train = np.flatnonzero(days < validation_day)
    validation = np.flatnonzero(days == validation_day)
    if len({dataset.market_slugs[index] for index in validation}) < 50:
        raise ValueError("final validation day requires at least 50 independent markets")
    weights = np.asarray(dataset.sample_weights, dtype=float)
    schema = market_relative_runtime_feature_schema()
    model = fit_market_relative_lightgbm(
        train_vectors=dataset.vectors[train],
        train_labels=dataset.labels[train],
        train_market_up_probability=dataset.market_up_probabilities[train],
        validation_vectors=dataset.vectors[validation],
        validation_labels=dataset.labels[validation],
        validation_market_up_probability=dataset.market_up_probabilities[validation],
        schema=schema,
        probability_uncertainty_radius=args.probability_uncertainty_radius,
        num_leaves=7,
        max_depth=3,
        min_child_samples=500,
        learning_rate=0.01,
        n_estimators=1_000,
        early_stopping_rounds=100,
        random_seed=17,
        train_weights=weights[train],
        validation_weights=weights[validation],
        validation_market_ids=tuple(dataset.market_slugs[index] for index in validation),
        uncertainty_confidence=args.uncertainty_confidence,
    )
    minimum_leaf_markets = require_minimum_leaf_unique_markets(
        leaf_indices=np.asarray(
            model.booster.predict(
                dataset.vectors[train],
                pred_leaf=True,
                num_iteration=model.booster.best_iteration,
            )
        ),
        market_slugs=tuple(dataset.market_slugs[index] for index in train),
        minimum_markets_per_leaf=args.minimum_markets_per_leaf,
    )
    source_paths = [
        *args.market_catalog,
        *sorted(args.shadow_root.glob("*/predictions.parquet")),
    ]
    metadata = ModelArtifactMetadata(
        model_id=args.model_id,
        feature_schema_hash=schema.hash,
        training_start_ns=min(dataset.trigger_ts_ns[index] for index in train),
        training_end_ns=max(dataset.trigger_ts_ns[index] for index in train),
        calibration_start_ns=min(dataset.trigger_ts_ns[index] for index in validation),
        calibration_end_ns=max(dataset.trigger_ts_ns[index] for index in validation),
        data_hash=_source_hash(source_paths),
        code_revision=_code_revision(),
        config={
            "opening_model_family": "market_relative_lightgbm_v1",
            "rule_epoch": args.rule_epoch,
            "rule_contract_sha256": rule_contract_sha256(args.rule_epoch),
            "paper_experiment_only": True,
            "runtime_promotion_eligible": False,
            "sealed_holdout_evaluated": False,
            "multiple_comparison_gate_passed": False,
            "probability_uncertainty_radius": model.probability_uncertainty_radius,
            "probability_uncertainty_method": (
                "operator_override"
                if args.probability_uncertainty_radius is not None
                else "market_first_wilson_calibration_envelope"
            ),
            "probability_uncertainty_confidence": args.uncertainty_confidence,
            "eligible_market_count": dataset.market_count,
            "training_market_count": len({dataset.market_slugs[index] for index in train}),
            "validation_market_count": len({dataset.market_slugs[index] for index in validation}),
            "validation_day": validation_day.isoformat(),
            "best_iteration": model.booster.best_iteration,
            "minimum_leaf_unique_market_count": minimum_leaf_markets,
            "minimum_markets_per_leaf_required": args.minimum_markets_per_leaf,
        },
    )
    saved = MarketRelativeArtifactStore.save(
        directory=args.output_directory,
        model=model,
        metadata=metadata,
    )
    print(
        json.dumps(
            {
                "artifact": str(args.output_directory),
                "model_id": saved.model_id,
                "model_sha256": saved.model_sha256,
                "eligible_markets": dataset.market_count,
                "best_iteration": model.booster.best_iteration,
                "minimum_leaf_unique_market_count": minimum_leaf_markets,
                "paper_experiment_only": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _source_hash(paths: Sequence[Path]) -> str:
    digest = sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        digest.update(path.name.encode())
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _code_revision() -> str:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(["git", "diff", "--quiet"], check=False).returncode != 0
    return f"{revision}-dirty" if dirty else revision


if __name__ == "__main__":
    raise SystemExit(main())
