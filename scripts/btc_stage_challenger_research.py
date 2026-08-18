"""Run the offline stage-aware BTC 15m direction challenger study.

The command intentionally produces research artifacts only.  Candidate selection
uses development OOF predictions; the sealed holdout is evaluated once after
selection and is never used to tune the candidate grid.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root


ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data import read_market_catalog  # noqa: E402
from btc_short_horizon.models import (  # noqa: E402
    DirectionModelConfig,
    ModelArtifactMetadata,
    ModelArtifactStore,
)
from btc_short_horizon.research.opening_model_gate import (  # noqa: E402
    CandidateEvaluation,
    paired_daily_block_bootstrap,
    select_direction_candidate,
    weighted_calibration_error,
)
from btc_short_horizon.research.opening_proxy import (  # noqa: E402
    opening_proxy_protocol,
)
from btc_short_horizon.research.pipeline import (  # noqa: E402
    DirectionDataset,
    run_sealed_holdout_model,
    run_walk_forward_model,
)
from btc_short_horizon.research.provenance import git_provenance  # noqa: E402
from btc_short_horizon.research.stage_aware_models import select_stage_dataset  # noqa: E402
from btc_short_horizon.research.walk_forward import WalkForwardConfig  # noqa: E402
from btc_short_horizon.strategy import OpeningStage  # noqa: E402
from btc_short_horizon.research.lightgbm_tuning import (  # noqa: E402
    controlled_lightgbm_grid,
)

from btc_opening_mispricing_proxy import (  # noqa: E402
    load_materialized_opening_proxy_dataset,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--market-catalog", type=Path, required=True)
    parser.add_argument("--materialized-dataset", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--profile", choices=("quick", "full"), default="quick")
    parser.add_argument("--interval-seconds", type=int, default=1)
    parser.add_argument("--bootstrap-iterations", type=int, default=2_000)
    return parser.parse_args(argv)


def _split_config(profile: str) -> WalkForwardConfig:
    if profile == "full":
        return WalkForwardConfig()
    return WalkForwardConfig(
        train_duration=timedelta(days=35),
        calibration_duration=timedelta(days=10),
        test_duration=timedelta(days=7),
        step_duration=timedelta(days=7),
        embargo_duration=timedelta(hours=4, minutes=15),
        sealed_holdout_duration=timedelta(days=14),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metrics(predictions: Iterable[object], *, weights: np.ndarray) -> dict[str, float]:
    items = tuple(predictions)
    if not items:
        raise ValueError("predictions must not be empty")
    indices = np.asarray([item.sample_index for item in items], dtype=int)
    labels = np.asarray([item.label for item in items], dtype=float)
    probabilities = np.clip(
        np.asarray([item.p_up for item in items], dtype=float), 1e-6, 1.0 - 1e-6
    )
    selected_weights = np.asarray(weights[indices], dtype=float)
    log_loss = -np.log(np.where(labels == 1.0, probabilities, 1.0 - probabilities))
    brier = (labels - probabilities) ** 2
    return {
        "sample_count": int(len(items)),
        "market_count": int(len({item.sample_id.rsplit("@", 1)[0] for item in items})),
        "log_loss": float(np.average(log_loss, weights=selected_weights)),
        "brier": float(np.average(brier, weights=selected_weights)),
        "accuracy_at_0_5": float(
            np.average((probabilities >= 0.5) == (labels == 1.0), weights=selected_weights)
        ),
        "calibration_error": float(
            weighted_calibration_error(
                labels=labels.astype(int),
                probabilities=probabilities,
                weights=selected_weights,
            )
        ),
    }


def _prediction_frame(predictions: Iterable[object], *, split: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "split": split,
            "sample_index": [item.sample_index for item in predictions],
            "sample_id": [item.sample_id for item in predictions],
            "feature_ts_ns": [item.feature_ts_ns for item in predictions],
            "p_up": [item.p_up for item in predictions],
            "label": [item.label for item in predictions],
        }
    )


def _model_metadata(
    *,
    model: object,
    dataset: DirectionDataset,
    holdout: object,
    stage: OpeningStage,
    selected_name: str,
    args: argparse.Namespace,
    project: object,
    data_hash: str,
    provenance: dict[str, object],
) -> ModelArtifactMetadata:
    train_indices = holdout.train_indices
    calibration_indices = holdout.calibration_indices
    protocol = opening_proxy_protocol(
        entry_start_seconds=project.research_timing.entry_start_seconds,
        entry_end_seconds=project.research_timing.entry_end_seconds,
        snapshot_seconds=project.research_timing.training_snapshot_seconds,
    )
    return ModelArtifactMetadata(
        model_id=f"btc-15m-stage-{stage.value}-{selected_name}-{args.profile}",
        feature_schema_hash=dataset.schema.hash,
        training_start_ns=int(dataset.samples[train_indices[0]].feature_ts.timestamp() * 1e9),
        training_end_ns=int(dataset.samples[train_indices[-1]].feature_ts.timestamp() * 1e9),
        calibration_start_ns=int(
            dataset.samples[calibration_indices[0]].feature_ts.timestamp() * 1e9
        ),
        calibration_end_ns=int(
            dataset.samples[calibration_indices[-1]].feature_ts.timestamp() * 1e9
        ),
        data_hash=data_hash,
        code_revision=str(provenance["revision_label"]),
        config={
            **model.config_dict,
            "opening_proxy_protocol": protocol,
            "stage": stage.value,
            "research_profile": args.profile,
            "selected_on_development": True,
            "fit_sample_count": len(holdout.fit_indices),
            "early_stopping_sample_count": len(holdout.early_stopping_indices),
            "calibration_sample_count": len(holdout.calibration_indices),
        },
    )


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.output_directory.exists():
        raise FileExistsError(f"output directory already exists: {args.output_directory}")
    if args.bootstrap_iterations < 100:
        raise ValueError("--bootstrap-iterations must be >= 100")
    if args.interval_seconds < 1:
        raise ValueError("--interval-seconds must be >= 1")

    project = load_btc_project_config(args.config)
    catalog = read_market_catalog(args.market_catalog)
    windows = tuple(sorted(catalog.windows(), key=lambda item: (item.t0, item.slug)))
    if not windows:
        raise ValueError("market catalog contains no windows")
    dataset = load_materialized_opening_proxy_dataset(
        path=args.materialized_dataset,
        interval_seconds=args.interval_seconds,
        snapshot_seconds=project.research_timing.training_snapshot_seconds,
        entry_start_seconds=project.research_timing.entry_start_seconds,
        entry_end_seconds=project.research_timing.entry_end_seconds,
        expected_market_group_ids=tuple(window.slug for window in windows),
    )
    split_config = _split_config(args.profile)
    provenance = git_provenance()
    data_hash = _sha256_file(args.materialized_dataset)
    output = args.output_directory
    output.mkdir(parents=True)
    results: dict[str, object] = {
        "study_type": "btc_15m_stage_aware_direction_challenger",
        "profile": args.profile,
        "promotion_allowed": False,
        "dataset": {
            "path": str(args.materialized_dataset),
            "sha256": data_hash,
            "sample_count": len(dataset.samples),
            "market_count": len({sample.group_id for sample in dataset.samples}),
            "feature_count": len(dataset.schema.names),
            "feature_schema_hash": dataset.schema.hash,
        },
        "provenance": provenance,
        "stages": {},
    }

    for stage in OpeningStage:
        stage_dataset = select_stage_dataset(dataset, stage=stage, policy=project.stage_policy)
        candidates: list[tuple[str, DirectionModelConfig, object, dict[str, float]]] = []
        logistic_configs = (
            DirectionModelConfig(kind="logistic", calibration_method="sigmoid", logistic_c=0.1),
            DirectionModelConfig(kind="logistic", calibration_method="sigmoid", logistic_c=1.0),
        )
        for index, config in enumerate(logistic_configs):
            run_result = run_walk_forward_model(
                dataset=stage_dataset, split_config=split_config, model_config=config
            )
            candidates.append(
                (
                    f"logistic-c{config.logistic_c:g}",
                    config,
                    run_result,
                    _metrics(run_result.predictions, weights=stage_dataset.sample_weights),
                )
            )
        for candidate in controlled_lightgbm_grid(stage=stage.value):
            run_result = run_walk_forward_model(
                dataset=stage_dataset,
                split_config=split_config,
                model_config=candidate.config,
            )
            candidates.append(
                (
                    candidate.name,
                    candidate.config,
                    run_result,
                    _metrics(run_result.predictions, weights=stage_dataset.sample_weights),
                )
            )

        best_logistic = min(
            (item for item in candidates if item[1].kind == "logistic"),
            key=lambda item: (item[3]["log_loss"], item[3]["brier"], item[0]),
        )
        evaluations: list[CandidateEvaluation] = []
        for name, config, run_result, metrics in candidates:
            paired = None
            if config.kind == "lightgbm":
                paired = paired_daily_block_bootstrap(
                    candidate_predictions=run_result.predictions,
                    baseline_predictions=best_logistic[2].predictions,
                    weights=stage_dataset.sample_weights,
                    baseline_name=best_logistic[0],
                    iterations=args.bootstrap_iterations,
                    alpha=0.05 / 18.0,
                )
            evaluations.append(
                CandidateEvaluation(
                    name=name,
                    kind=config.kind,
                    log_loss=metrics["log_loss"],
                    brier=metrics["brier"],
                    calibration_error=metrics["calibration_error"],
                    paired_vs_best_logistic=paired,
                )
            )
        selection = select_direction_candidate(evaluations)
        selected = next(item for item in candidates if item[0] == selection.selected_name)
        baseline_holdout = run_sealed_holdout_model(
            dataset=stage_dataset,
            split_config=split_config,
            model_config=best_logistic[1],
        )
        selected_holdout = (
            baseline_holdout
            if selection.selected_name == best_logistic[0]
            else run_sealed_holdout_model(
                dataset=stage_dataset,
                split_config=split_config,
                model_config=selected[1],
            )
        )
        baseline_holdout_metrics = _metrics(
            baseline_holdout.predictions, weights=stage_dataset.sample_weights
        )
        selected_holdout_metrics = _metrics(
            selected_holdout.predictions, weights=stage_dataset.sample_weights
        )
        holdout_paired = (
            paired_daily_block_bootstrap(
                candidate_predictions=selected_holdout.predictions,
                baseline_predictions=baseline_holdout.predictions,
                weights=stage_dataset.sample_weights,
                baseline_name=best_logistic[0],
                iterations=args.bootstrap_iterations,
            )
            if selection.selected_name != best_logistic[0]
            else None
        )
        stage_output = output / stage.value
        stage_output.mkdir()
        artifact_metadata = ModelArtifactStore.save(
            directory=stage_output / "model",
            model=selected_holdout.model,
            metadata=_model_metadata(
                model=selected_holdout.model,
                dataset=stage_dataset,
                holdout=selected_holdout,
                stage=stage,
                selected_name=selection.selected_name,
                args=args,
                project=project,
                data_hash=data_hash,
                provenance=provenance,
            ),
        )
        pd.concat(
            (
                _prediction_frame(selected[2].predictions, split="development_oof"),
                _prediction_frame(selected_holdout.predictions, split="sealed_holdout"),
            ),
            ignore_index=True,
        ).to_parquet(stage_output / "predictions.parquet", index=False)
        stage_result = {
            "dataset": {
                "sample_count": len(stage_dataset.samples),
                "market_count": len({sample.group_id for sample in stage_dataset.samples}),
            },
            "development": {
                "candidate_count": len(candidates),
                "best_logistic": best_logistic[0],
                "selection": asdict(selection),
                "candidates": [
                    {
                        **asdict(evaluation),
                        "paired_vs_best_logistic": (
                            asdict(evaluation.paired_vs_best_logistic)
                            if evaluation.paired_vs_best_logistic is not None
                            else None
                        ),
                    }
                    for evaluation in evaluations
                ],
            },
            "sealed_holdout": {
                "baseline_logistic": baseline_holdout_metrics,
                "selected": selected_holdout_metrics,
                "paired_vs_baseline": (
                    asdict(holdout_paired) if holdout_paired is not None else None
                ),
            },
            "artifact": {
                "path": str(stage_output / "model"),
                "model_id": artifact_metadata.model_id,
                "model_sha256": artifact_metadata.model_sha256,
            },
        }
        (stage_output / "summary.json").write_text(
            json.dumps(stage_result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        results["stages"][stage.value] = stage_result

    (output / "report.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
