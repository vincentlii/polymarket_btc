"""Fit one development-only stage artifact from a frozen V2 anchored dataset."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
import subprocess
from typing import Sequence

import numpy as np

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data.rule_contract import rule_contract_sha256  # noqa: E402
from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.models import DirectionModelConfig  # noqa: E402
from btc_short_horizon.models.artifacts import ModelArtifactMetadata  # noqa: E402
from btc_short_horizon.models.market_relative import fit_market_relative_offset_model  # noqa: E402
from btc_short_horizon.models.market_relative_artifacts import (  # noqa: E402
    MarketRelativeArtifactStore,
)
from btc_short_horizon.models.market_relative_lightgbm import (  # noqa: E402
    fit_market_relative_lightgbm,
    require_minimum_leaf_unique_markets,
)
from btc_short_horizon.research.market_relative_dataset_io import (  # noqa: E402
    anchored_dataset_sha256,
    load_anchored_direction_dataset,
)
from btc_short_horizon.research.market_relative_model_selection import (  # noqa: E402
    validate_selection_receipt,
)
from btc_short_horizon.research.stage_aware_models import select_stage_dataset  # noqa: E402
from btc_short_horizon.strategy import OpeningStage  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/btc_short_horizon/baseline.toml")
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--research-receipt", type=Path, required=True)
    parser.add_argument("--stage", choices=[value.value for value in OpeningStage], required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--minimum-calibration-markets", type=int, default=200)
    parser.add_argument("--calibration-fraction", type=float, default=0.20)
    parser.add_argument("--uncertainty-confidence", type=float, default=0.95)
    parser.add_argument(
        "--observation-only",
        action="store_true",
        help="Fit the development champion for no-order forward comparison only.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    project = load_btc_project_config(args.config)
    if args.minimum_calibration_markets < 50:
        raise ValueError("--minimum-calibration-markets must be >= 50")
    if not 0.10 <= args.calibration_fraction <= 0.40:
        raise ValueError("--calibration-fraction must be in [0.10, 0.40]")
    if not 0.5 < args.uncertainty_confidence < 1.0:
        raise ValueError("--uncertainty-confidence must be in (0.5, 1)")
    anchored, lineage = load_anchored_direction_dataset(args.dataset)
    receipt = json.loads(args.research_receipt.read_text(encoding="utf-8"))
    dataset_receipt = receipt.get("dataset_artifact")
    if not isinstance(dataset_receipt, dict) or dataset_receipt.get(
        "sha256"
    ) != anchored_dataset_sha256(args.dataset):
        raise ValueError("research receipt does not bind the supplied dataset")
    selection = validate_selection_receipt(receipt.get("model_selection", {}))
    stage = OpeningStage(args.stage)
    stages = selection["stages"]
    assert isinstance(stages, dict)
    stage_selection = stages[stage.value]
    if not isinstance(stage_selection, dict):
        raise ValueError("selected stage receipt must be an object")
    if args.observation_only:
        config_payload = stage_selection.get("champion_config")
        selection_role = "observation_only_diagnostic"
    else:
        if stage_selection.get("paper_experiment_gate_passed") is not True:
            raise ValueError("selected stage did not pass Paper experiment sanity gates")
        config_payload = stage_selection.get("paper_experiment_config")
        selection_role = "paper_experiment_leader"
    if not isinstance(config_payload, dict):
        raise ValueError("selected stage receipt is missing champion config")
    config = DirectionModelConfig(**config_payload)
    if lineage.get("profile") != project.paper_research.market_relative_feature_profile:
        raise ValueError("dataset feature profile does not match project config")
    if lineage.get("project_config_sha256") != _file_sha256(args.config):
        raise ValueError("dataset project config hash does not match the supplied config")
    if (
        lineage.get("sealed_forward_start")
        != project.paper_research.sealed_forward_start.isoformat()
    ):
        raise ValueError("dataset sealed-forward boundary does not match project config")
    selected, indices = _selected_stage(anchored, stage, project.stage_policy)
    market_order = _market_order(selected.samples)
    calibration_count = max(
        args.minimum_calibration_markets,
        ceil(len(market_order) * args.calibration_fraction),
    )
    if calibration_count >= len(market_order):
        raise ValueError("development dataset cannot provide independent train/calibration markets")
    calibration_markets = set(market_order[-calibration_count:])
    train = np.asarray(
        [
            index
            for index, sample in enumerate(selected.samples)
            if sample.group_id not in calibration_markets
        ],
        dtype=int,
    )
    calibration = np.asarray(
        [
            index
            for index, sample in enumerate(selected.samples)
            if sample.group_id in calibration_markets
        ],
        dtype=int,
    )
    if len({selected.samples[index].group_id for index in train}) < 300:
        raise ValueError("development artifact requires at least 300 independent training markets")
    anchors = anchored.market_up_probabilities[indices]
    weights = selected.sample_weights
    if weights is None:
        raise ValueError("market-relative dataset requires explicit market-balanced weights")
    minimum_leaf_markets: int | None = None
    if config.kind == "lightgbm":
        model = fit_market_relative_lightgbm(
            train_vectors=selected.vectors[train],
            train_labels=selected.labels[train],
            train_market_up_probability=anchors[train],
            validation_vectors=selected.vectors[calibration],
            validation_labels=selected.labels[calibration],
            validation_market_up_probability=anchors[calibration],
            schema=selected.schema,
            probability_uncertainty_radius=None,
            num_leaves=config.lightgbm_num_leaves,
            max_depth=config.lightgbm_max_depth,
            min_child_samples=config.lightgbm_min_child_samples,
            learning_rate=config.lightgbm_learning_rate,
            n_estimators=config.lightgbm_n_estimators,
            early_stopping_rounds=config.lightgbm_early_stopping_rounds,
            random_seed=config.random_seed,
            train_weights=weights[train],
            validation_weights=weights[calibration],
            validation_market_ids=tuple(selected.samples[index].group_id for index in calibration),
            uncertainty_confidence=args.uncertainty_confidence,
        )
        minimum_required = int(selection["minimum_markets_per_leaf_required"])
        minimum_leaf_markets = require_minimum_leaf_unique_markets(
            leaf_indices=np.asarray(
                model.booster.predict(
                    selected.vectors[train],
                    pred_leaf=True,
                    num_iteration=model.booster.best_iteration,
                )
            ),
            market_slugs=tuple(selected.samples[index].group_id for index in train),
            minimum_markets_per_leaf=minimum_required,
        )
        family = "market_relative_lightgbm_v1"
    else:
        model = fit_market_relative_offset_model(
            train_vectors=selected.vectors[train],
            train_labels=selected.labels[train],
            train_market_up_probability=anchors[train],
            calibration_vectors=selected.vectors[calibration],
            calibration_labels=selected.labels[calibration],
            calibration_market_up_probability=anchors[calibration],
            schema=selected.schema,
            logistic_c=config.logistic_c,
            train_weights=weights[train],
            calibration_weights=weights[calibration],
            random_seed=config.random_seed,
            calibration_method=config.calibration_method,
            calibration_independent_market_count=len(calibration_markets),
        )
        family = "market_relative_offset_v1"
    rule_epoch = lineage.get("rule_epoch")
    if not isinstance(rule_epoch, str):
        raise ValueError("dataset lineage requires one rule_epoch")
    development_cutoff = lineage.get("sealed_forward_start")
    metadata_config = {
        "opening_model_family": family,
        "rule_epoch": rule_epoch,
        "rule_contract_sha256": rule_contract_sha256(rule_epoch),
        "feature_profile": lineage.get("profile"),
        "factor_families": lineage.get("families"),
        "stage": stage.value,
        "selected_config": asdict(config),
        "selection_role": selection_role,
        "selection_receipt_sha256": selection["selection_receipt_sha256"],
        "dataset_sha256": anchored_dataset_sha256(args.dataset),
        "development_cutoff": development_cutoff,
        "observation_only": bool(args.observation_only),
        "paper_experiment_only": True,
        "paper_experiment_gate_passed": not args.observation_only,
        "runtime_promotion_eligible": False,
        "sealed_holdout_evaluated": False,
        "multiple_comparison_gate_passed": False,
        "training_market_count": len({selected.samples[index].group_id for index in train}),
        "calibration_market_count": len(calibration_markets),
        "minimum_leaf_unique_market_count": minimum_leaf_markets,
    }
    radius = getattr(model, "probability_uncertainty_radius", None)
    if radius is not None:
        metadata_config.update(
            {
                "probability_uncertainty_radius": radius,
                "probability_uncertainty_method": "market_first_wilson_calibration_envelope",
                "probability_uncertainty_confidence": args.uncertainty_confidence,
            }
        )
    metadata = ModelArtifactMetadata(
        model_id=args.model_id,
        feature_schema_hash=selected.schema.hash,
        training_start_ns=min(_datetime_ns(selected.samples[index].feature_ts) for index in train),
        training_end_ns=max(_datetime_ns(selected.samples[index].feature_ts) for index in train),
        calibration_start_ns=min(
            _datetime_ns(selected.samples[index].feature_ts) for index in calibration
        ),
        calibration_end_ns=max(
            _datetime_ns(selected.samples[index].feature_ts) for index in calibration
        ),
        data_hash=sha256(
            (
                anchored_dataset_sha256(args.dataset) + str(selection["selection_receipt_sha256"])
            ).encode()
        ).hexdigest(),
        code_revision=_code_revision(),
        config=metadata_config,
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
                "stage": stage.value,
                "model_kind": config.kind,
                "training_markets": metadata_config["training_market_count"],
                "calibration_markets": metadata_config["calibration_market_count"],
                "runtime_promotion_eligible": False,
                "observation_only": bool(args.observation_only),
            },
            sort_keys=True,
        )
    )
    return 0


def _selected_stage(anchored, stage, policy):  # type: ignore[no-untyped-def]
    selected = select_stage_dataset(
        anchored.dataset,
        stage=stage,
        policy=policy,
    )
    positions = {sample.sample_id: index for index, sample in enumerate(anchored.dataset.samples)}
    return selected, np.asarray(
        [positions[sample.sample_id] for sample in selected.samples], dtype=int
    )


def _market_order(samples) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    first: dict[str, datetime] = {}
    for sample in samples:
        first[sample.group_id] = min(
            first.get(sample.group_id, sample.feature_ts), sample.feature_ts
        )
    return tuple(sorted(first, key=lambda market: (first[market], market)))


def _datetime_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact timestamps must be timezone-aware")
    return int(value.timestamp() * 1_000_000_000)


def _code_revision() -> str:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    return f"{revision}-dirty" if dirty else revision


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
