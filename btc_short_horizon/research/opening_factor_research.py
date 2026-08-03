"""Development-only frozen BTC opening-factor challenger matrix."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from math import isfinite
from typing import Sequence

import numpy as np

from btc_short_horizon.models import DirectionModelConfig
from btc_short_horizon.research.opening_factor_challenge import (
    OpeningFactorDatasetBuild,
    OpeningFactorFamily,
    opening_factor_feature_schema,
)
from btc_short_horizon.research.opening_model_gate import (
    calibration_slope,
    paired_daily_block_bootstrap,
    weighted_calibration_error,
)
from btc_short_horizon.research.pipeline import (
    DirectionDataset,
    OofPrediction,
    run_walk_forward_model,
)
from btc_short_horizon.research.walk_forward import WalkForwardConfig


@dataclass(frozen=True, slots=True)
class FactorCandidate:
    name: str
    families: tuple[OpeningFactorFamily, ...]
    kind: str


FACTOR_CANDIDATES = (
    FactorCandidate("control_logistic", (), "logistic"),
    FactorCandidate("boundary_logistic", (OpeningFactorFamily.BOUNDARY_TIME,), "logistic"),
    FactorCandidate("state_logistic", (OpeningFactorFamily.PATH_STATE,), "logistic"),
    FactorCandidate("flow_logistic", (OpeningFactorFamily.FLOW_VALUE,), "logistic"),
    FactorCandidate("cross_market_logistic", (OpeningFactorFamily.CROSS_MARKET,), "logistic"),
    FactorCandidate("all_logistic", tuple(OpeningFactorFamily), "logistic"),
    FactorCandidate("all_lightgbm", tuple(OpeningFactorFamily), "lightgbm"),
)
DEVELOPMENT_COMPARISON_COUNT = len(FACTOR_CANDIDATES)


@dataclass(frozen=True, slots=True)
class FactorCandidateResult:
    candidate: FactorCandidate
    predictions: tuple[OofPrediction, ...]
    log_loss: float
    brier: float
    calibration_error: float
    calibration_slope: float
    paired_log_loss_ci_lower: float | None = None
    paired_log_loss_ci_upper: float | None = None
    paired_brier_ci_lower: float | None = None
    paired_brier_ci_upper: float | None = None
    replacement_baseline_name: str | None = None
    replacement_log_loss_ci_lower: float | None = None
    replacement_log_loss_ci_upper: float | None = None
    replacement_brier_ci_lower: float | None = None
    replacement_brier_ci_upper: float | None = None


@dataclass(frozen=True, slots=True)
class OpeningFactorDevelopmentRun:
    accepted: bool
    selected_name: str | None
    failed_conditions: tuple[str, ...]
    candidates: tuple[FactorCandidateResult, ...]
    eligible_market_count: int
    excluded_incomplete_cross_market: int
    sealed_holdout_evaluated: bool = False

    def __post_init__(self) -> None:
        if self.sealed_holdout_evaluated:
            raise ValueError("factor development must not evaluate the sealed holdout")


def adjusted_alpha(challenger_count: int) -> float:
    if challenger_count < 1:
        raise ValueError("challenger_count must be >= 1")
    return 0.05 / challenger_count


def run_opening_factor_development(
    *,
    build: OpeningFactorDatasetBuild,
    split_config: WalkForwardConfig,
    bootstrap_iterations: int = 10_000,
    minimum_eligible_markets: int = 1,
    progress_callback: Callable[[FactorCandidateResult], None] | None = None,
) -> OpeningFactorDevelopmentRun:
    """Fit the pre-registered matrix; no sealed-holdout API is called here."""

    if build.eligible_market_count < minimum_eligible_markets:
        return OpeningFactorDevelopmentRun(
            False,
            None,
            ("eligible_market_coverage_below_minimum",),
            (),
            build.eligible_market_count,
            build.excluded_incomplete_cross_market,
        )
    datasets = {
        candidate.name: _candidate_dataset(build, candidate) for candidate in FACTOR_CANDIDATES
    }
    control = datasets["control_logistic"]
    results: list[FactorCandidateResult] = []
    control_run = run_walk_forward_model(
        dataset=control, split_config=split_config, model_config=_model_config("logistic")
    )
    control_result = _result(FACTOR_CANDIDATES[0], control_run.predictions, control)
    results.append(control_result)
    if progress_callback is not None:
        progress_callback(control_result)
    for candidate in FACTOR_CANDIDATES[1:]:
        dataset = datasets[candidate.name]
        run = run_walk_forward_model(
            dataset=dataset, split_config=split_config, model_config=_model_config(candidate.kind)
        )
        _validate_prediction_lineage(control_run.predictions, run.predictions)
        evidence = paired_daily_block_bootstrap(
            candidate_predictions=run.predictions,
            baseline_predictions=control_run.predictions,
            weights=control.sample_weights,
            baseline_name="control_logistic",
            iterations=bootstrap_iterations,
            alpha=adjusted_alpha(DEVELOPMENT_COMPARISON_COUNT),
        )
        metric = _result(candidate, run.predictions, dataset)
        candidate_result = replace(
            metric,
            paired_log_loss_ci_lower=evidence.log_loss_ci_lower,
            paired_log_loss_ci_upper=evidence.log_loss_ci_upper,
            paired_brier_ci_lower=evidence.brier_ci_lower,
            paired_brier_ci_upper=evidence.brier_ci_upper,
        )
        if candidate.kind == "lightgbm":
            best_logistic = min(
                (item for item in results if item.candidate.kind == "logistic"),
                key=lambda item: (
                    item.log_loss,
                    item.brier,
                    item.calibration_error,
                    item.candidate.name,
                ),
            )
            replacement_evidence = paired_daily_block_bootstrap(
                candidate_predictions=run.predictions,
                baseline_predictions=best_logistic.predictions,
                weights=control.sample_weights,
                baseline_name=best_logistic.candidate.name,
                iterations=bootstrap_iterations,
                alpha=adjusted_alpha(DEVELOPMENT_COMPARISON_COUNT),
            )
            candidate_result = replace(
                candidate_result,
                replacement_baseline_name=replacement_evidence.baseline_name,
                replacement_log_loss_ci_lower=replacement_evidence.log_loss_ci_lower,
                replacement_log_loss_ci_upper=replacement_evidence.log_loss_ci_upper,
                replacement_brier_ci_lower=replacement_evidence.brier_ci_lower,
                replacement_brier_ci_upper=replacement_evidence.brier_ci_upper,
            )
        results.append(candidate_result)
        if progress_callback is not None:
            progress_callback(candidate_result)
    return _select(tuple(results), build)


def development_report(run: OpeningFactorDevelopmentRun) -> dict[str, object]:
    return {
        "development_only": True,
        "sealed_holdout_evaluated": False,
        "coverage": {
            "eligible_market_count": run.eligible_market_count,
            "excluded_incomplete_cross_market": run.excluded_incomplete_cross_market,
        },
        "decision": {
            "accepted": run.accepted,
            "selected_name": run.selected_name,
            "failed_conditions": list(run.failed_conditions),
        },
        "protocol": {
            "family_wise_error_rate": 0.05,
            "comparison_count": DEVELOPMENT_COMPARISON_COUNT,
            "per_comparison_alpha": adjusted_alpha(DEVELOPMENT_COMPARISON_COUNT),
            "candidates": [
                {"name": c.name, "families": [f.value for f in c.families], "kind": c.kind}
                for c in FACTOR_CANDIDATES
            ],
        },
        "candidates": [
            {
                "name": item.candidate.name,
                "families": [family.value for family in item.candidate.families],
                "kind": item.candidate.kind,
                "log_loss": item.log_loss,
                "brier": item.brier,
                "calibration_error": item.calibration_error,
                "calibration_slope": item.calibration_slope,
                "oof_prediction_count": len(item.predictions),
                "paired_vs_control": None
                if item.candidate.name == "control_logistic"
                else {
                    "log_loss_ci_lower": item.paired_log_loss_ci_lower,
                    "log_loss_ci_upper": item.paired_log_loss_ci_upper,
                    "brier_ci_lower": item.paired_brier_ci_lower,
                    "brier_ci_upper": item.paired_brier_ci_upper,
                },
                "paired_vs_best_logistic": None
                if item.replacement_baseline_name is None
                else {
                    "baseline_name": item.replacement_baseline_name,
                    "log_loss_ci_lower": item.replacement_log_loss_ci_lower,
                    "log_loss_ci_upper": item.replacement_log_loss_ci_upper,
                    "brier_ci_lower": item.replacement_brier_ci_lower,
                    "brier_ci_upper": item.replacement_brier_ci_upper,
                },
            }
            for item in run.candidates
        ],
    }


def _candidate_dataset(
    build: OpeningFactorDatasetBuild, candidate: FactorCandidate
) -> DirectionDataset:
    if not candidate.families:
        return build.paired_control_dataset
    target = opening_factor_feature_schema(
        base_schema=build.paired_control_dataset.schema, families=candidate.families
    )
    all_names = {name: index for index, name in enumerate(build.factor_dataset.schema.names)}
    indices = np.asarray([all_names[name] for name in target.names], dtype=int)
    return DirectionDataset(
        samples=build.paired_control_dataset.samples,
        vectors=build.factor_dataset.vectors[:, indices],
        schema=target,
        sample_weights=build.paired_control_dataset.sample_weights,
    )


def _model_config(kind: str) -> DirectionModelConfig:
    if kind == "logistic":
        return DirectionModelConfig(kind="logistic", logistic_c=0.1, calibration_method="sigmoid")
    return DirectionModelConfig(
        kind="lightgbm",
        calibration_method="sigmoid",
        lightgbm_num_leaves=7,
        lightgbm_max_depth=3,
        lightgbm_min_child_samples=200,
    )


def _result(
    candidate: FactorCandidate, predictions: Sequence[OofPrediction], dataset: DirectionDataset
) -> FactorCandidateResult:
    indices = np.asarray([item.sample_index for item in predictions], dtype=int)
    labels = np.asarray([item.label for item in predictions], dtype=float)
    probabilities = np.clip(
        np.asarray([item.p_up for item in predictions], dtype=float), 1e-6, 1.0 - 1e-6
    )
    weights = dataset.sample_weights[indices]
    log_loss = float(
        np.average(
            -(labels * np.log(probabilities) + (1 - labels) * np.log(1 - probabilities)),
            weights=weights,
        )
    )
    brier = float(np.average((labels - probabilities) ** 2, weights=weights))
    calibration_error = weighted_calibration_error(
        labels=labels, probabilities=probabilities, weights=weights
    )
    slope = calibration_slope(labels=labels, probabilities=probabilities, weights=weights)
    if not all(isfinite(v) for v in (log_loss, brier, calibration_error, slope)):
        raise ValueError("factor candidate metrics must be finite")
    return FactorCandidateResult(
        candidate, tuple(predictions), log_loss, brier, calibration_error, slope
    )


def _validate_prediction_lineage(
    control: Sequence[OofPrediction], challenger: Sequence[OofPrediction]
) -> None:
    left = tuple(
        (p.fold_index, p.sample_index, p.sample_id, p.feature_ts_ns, p.label) for p in control
    )
    right = tuple(
        (p.fold_index, p.sample_index, p.sample_id, p.feature_ts_ns, p.label) for p in challenger
    )
    if left != right:
        raise ValueError("all factor candidates must emit exact paired OOF lineage")


def _select(
    results: tuple[FactorCandidateResult, ...], build: OpeningFactorDatasetBuild
) -> OpeningFactorDevelopmentRun:
    control = results[0]
    logistics = tuple(item for item in results if item.candidate.kind == "logistic")
    best_logistic = min(
        logistics,
        key=lambda item: (item.log_loss, item.brier, item.calibration_error, item.candidate.name),
    )
    lightgbm = next(item for item in results if item.candidate.kind == "lightgbm")
    if _improves(lightgbm, control) and _replaces(lightgbm, best_logistic):
        return OpeningFactorDevelopmentRun(
            True,
            lightgbm.candidate.name,
            (),
            results,
            build.eligible_market_count,
            build.excluded_incomplete_cross_market,
        )
    if best_logistic is not control and _improves(best_logistic, control):
        return OpeningFactorDevelopmentRun(
            True,
            best_logistic.candidate.name,
            (),
            results,
            build.eligible_market_count,
            build.excluded_incomplete_cross_market,
        )
    return OpeningFactorDevelopmentRun(
        False,
        None,
        ("no_challenger_passed_paired_development_gate",),
        results,
        build.eligible_market_count,
        build.excluded_incomplete_cross_market,
    )


def _improves(candidate: FactorCandidateResult, baseline: FactorCandidateResult) -> bool:
    return (
        candidate.log_loss < baseline.log_loss
        and candidate.brier < baseline.brier
        and candidate.calibration_error < baseline.calibration_error
        and candidate.paired_log_loss_ci_lower is not None
        and candidate.paired_log_loss_ci_lower > 0.0
        and candidate.paired_brier_ci_lower is not None
        and candidate.paired_brier_ci_lower > 0.0
    )


def _replaces(candidate: FactorCandidateResult, baseline: FactorCandidateResult) -> bool:
    return (
        candidate.replacement_baseline_name == baseline.candidate.name
        and candidate.log_loss < baseline.log_loss
        and candidate.brier < baseline.brier
        and candidate.calibration_error < baseline.calibration_error
        and candidate.replacement_log_loss_ci_lower is not None
        and candidate.replacement_log_loss_ci_lower > 0.0
        and candidate.replacement_brier_ci_lower is not None
        and candidate.replacement_brier_ci_lower > 0.0
    )


__all__ = [
    "DEVELOPMENT_COMPARISON_COUNT",
    "FACTOR_CANDIDATES",
    "FactorCandidate",
    "FactorCandidateResult",
    "OpeningFactorDevelopmentRun",
    "adjusted_alpha",
    "development_report",
    "run_opening_factor_development",
]
