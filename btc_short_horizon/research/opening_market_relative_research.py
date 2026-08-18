"""Frozen paired ablation protocol for causal market-relative opening models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from math import isfinite

import numpy as np

from btc_short_horizon.models import DirectionModelConfig
from btc_short_horizon.research.opening_market_relative import (
    MarketRelativeFeatureFamily,
    OpeningMarketRelativeDatasetBuild,
)
from btc_short_horizon.research.opening_model_gate import (
    calibration_slope,
    paired_daily_block_bootstrap,
    weighted_calibration_error,
)
from btc_short_horizon.research.pipeline import (
    DirectionDataset,
    HoldoutPrediction,
    OofPrediction,
    SealedHoldoutModelRun,
    WalkForwardModelRun,
    run_sealed_holdout_model,
    run_walk_forward_model,
)
from btc_short_horizon.research.walk_forward import WalkForwardConfig


_PROBABILITY_CLIP_EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class MarketRelativeAblation:
    """One pre-registered Logistic factor-set candidate."""

    name: str
    families: tuple[MarketRelativeFeatureFamily, ...]


MARKET_RELATIVE_LOGISTIC_ABLATIONS = (
    MarketRelativeAblation("control", ()),
    MarketRelativeAblation(
        "market_anchor",
        (MarketRelativeFeatureFamily.MARKET_ANCHOR,),
    ),
    MarketRelativeAblation(
        "market_anchor_boundary",
        (
            MarketRelativeFeatureFamily.MARKET_ANCHOR,
            MarketRelativeFeatureFamily.BOUNDARY_RESIDUAL,
        ),
    ),
    MarketRelativeAblation("market_relative_all", tuple(MarketRelativeFeatureFamily)),
)


@dataclass(frozen=True, slots=True)
class MarketRelativeCandidateMetrics:
    """Development-only OOF evidence for one paired Logistic candidate."""

    name: str
    families: tuple[MarketRelativeFeatureFamily, ...]
    log_loss: float
    brier: float
    calibration_error: float
    calibration_slope: float
    predictions: tuple[OofPrediction, ...]
    paired_log_loss_improvement: float | None = None
    paired_log_loss_ci_lower: float | None = None
    paired_log_loss_ci_upper: float | None = None
    paired_brier_improvement: float | None = None
    paired_brier_ci_lower: float | None = None
    paired_brier_ci_upper: float | None = None


@dataclass(frozen=True, slots=True)
class MarketRelativeDevelopmentRun:
    """Serializable gate result which never evaluates the sealed holdout."""

    accepted: bool
    selected_name: str | None
    failed_conditions: tuple[str, ...]
    coverage: Mapping[str, int]
    candidates: tuple[MarketRelativeCandidateMetrics, ...]
    sealed_holdout_evaluated: bool = False
    dataset_lineage_hash: str | None = None
    control_schema_hash: str | None = None
    challenger_schema_hashes: Mapping[str, str] | None = None
    protocol_hash: str | None = None
    split_config: Mapping[str, object] | None = None
    model_config: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.sealed_holdout_evaluated:
            raise ValueError("development selection cannot evaluate the sealed holdout")


@dataclass(frozen=True, slots=True)
class MarketRelativeSealedHoldoutRun:
    """Explicit one-shot paired holdout evidence for the selected challenger."""

    accepted: bool
    selected_name: str
    failed_conditions: tuple[str, ...]
    holdout_market_count: int
    control_run: SealedHoldoutModelRun
    challenger_run: SealedHoldoutModelRun
    log_loss: float
    brier: float
    calibration_error: float
    calibration_slope: float
    paired_log_loss_improvement: float
    paired_log_loss_ci_lower: float
    paired_log_loss_ci_upper: float
    paired_brier_improvement: float
    paired_brier_ci_lower: float
    paired_brier_ci_upper: float
    dataset_lineage_hash: str
    protocol_hash: str


def run_market_relative_development(
    *,
    datasets_by_ablation: Mapping[str, OpeningMarketRelativeDatasetBuild],
    minimum_eligible_markets: int,
    split_config: WalkForwardConfig | None = None,
    model_config: DirectionModelConfig | None = None,
    bootstrap_iterations: int = 10_000,
) -> MarketRelativeDevelopmentRun:
    """Run the four frozen Logistic candidates on one exact paired sample set."""

    if minimum_eligible_markets < 1:
        raise ValueError("minimum_eligible_markets must be >= 1")
    effective_split = split_config or WalkForwardConfig()
    effective_model = model_config or DirectionModelConfig(
        kind="logistic",
        calibration_method="sigmoid",
        logistic_c=0.1,
    )
    if effective_model.kind != "logistic":
        raise ValueError("market-relative ablation protocol is Logistic-only")
    expected = {item.name for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS[1:]}
    if set(datasets_by_ablation) != expected:
        raise ValueError("datasets must exactly match the frozen challenger ablations")
    builds = tuple(
        datasets_by_ablation[item.name] for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS[1:]
    )
    lineage = _development_lineage(
        datasets_by_ablation=datasets_by_ablation,
        split_config=effective_split,
        model_config=effective_model,
    )
    coverage = _coverage(builds)
    if coverage["eligible_market_count"] < minimum_eligible_markets:
        return MarketRelativeDevelopmentRun(
            accepted=False,
            selected_name=None,
            failed_conditions=("eligible_market_coverage_below_minimum",),
            coverage=coverage,
            candidates=(),
            **lineage,
        )
    if any(
        build.paired_control_dataset is None or build.challenger_dataset is None for build in builds
    ):
        return MarketRelativeDevelopmentRun(
            accepted=False,
            selected_name=None,
            failed_conditions=("paired_dataset_unavailable",),
            coverage=coverage,
            candidates=(),
            **lineage,
        )
    control = builds[0].paired_control_dataset
    assert control is not None
    challengers = {name: build.challenger_dataset for name, build in datasets_by_ablation.items()}
    _validate_paired_datasets(control, challengers)
    control_run = run_walk_forward_model(
        dataset=control,
        split_config=effective_split,
        model_config=effective_model,
    )
    candidates = [_candidate_metrics("control", (), control_run, control)]
    challenger_count = len(MARKET_RELATIVE_LOGISTIC_ABLATIONS) - 1
    for ablation in MARKET_RELATIVE_LOGISTIC_ABLATIONS[1:]:
        dataset = challengers[ablation.name]
        assert dataset is not None
        run = run_walk_forward_model(
            dataset=dataset,
            split_config=effective_split,
            model_config=effective_model,
        )
        _validate_paired_predictions(control_run.predictions, run.predictions)
        evidence = paired_daily_block_bootstrap(
            candidate_predictions=run.predictions,
            baseline_predictions=control_run.predictions,
            weights=control.sample_weights,
            baseline_name="control",
            iterations=bootstrap_iterations,
            alpha=0.05 / challenger_count,
        )
        metrics = _candidate_metrics(ablation.name, ablation.families, run, dataset)
        candidates.append(
            replace(
                metrics,
                paired_log_loss_improvement=evidence.log_loss_improvement,
                paired_log_loss_ci_lower=evidence.log_loss_ci_lower,
                paired_log_loss_ci_upper=evidence.log_loss_ci_upper,
                paired_brier_improvement=evidence.brier_improvement,
                paired_brier_ci_lower=evidence.brier_ci_lower,
                paired_brier_ci_upper=evidence.brier_ci_upper,
            )
        )
    return evaluate_market_relative_research_gate(
        MarketRelativeDevelopmentRun(
            accepted=False,
            selected_name=None,
            failed_conditions=(),
            coverage=coverage,
            candidates=tuple(candidates),
            **lineage,
        )
    )


def evaluate_market_relative_research_gate(
    development: MarketRelativeDevelopmentRun,
) -> MarketRelativeDevelopmentRun:
    """Select a challenger only when every paired development check improves."""

    if development.sealed_holdout_evaluated:
        raise ValueError("sealed holdout evidence cannot participate in development selection")
    if development.failed_conditions:
        return development
    control = next((item for item in development.candidates if item.name == "control"), None)
    if control is None:
        raise ValueError("development candidates require the frozen control")
    eligible = tuple(
        item
        for item in development.candidates
        if item.name != "control"
        and item.log_loss < control.log_loss
        and item.brier < control.brier
        and item.calibration_error <= control.calibration_error
        and 0.8 <= item.calibration_slope <= 1.2
        and item.paired_log_loss_ci_lower is not None
        and item.paired_log_loss_ci_lower > 0.0
        and item.paired_brier_ci_lower is not None
        and item.paired_brier_ci_lower > 0.0
    )
    if not eligible:
        return replace(
            development,
            accepted=False,
            selected_name=None,
            failed_conditions=("no_challenger_passed_paired_development_gate",),
        )
    selected = min(
        eligible,
        key=lambda item: (item.log_loss, item.brier, item.calibration_error, item.name),
    )
    return replace(development, accepted=True, selected_name=selected.name)


def run_market_relative_sealed_holdout(
    *,
    development: MarketRelativeDevelopmentRun,
    datasets_by_ablation: Mapping[str, OpeningMarketRelativeDatasetBuild],
    minimum_holdout_markets: int = 2_500,
    split_config: WalkForwardConfig | None = None,
    model_config: DirectionModelConfig | None = None,
    bootstrap_iterations: int = 10_000,
) -> MarketRelativeSealedHoldoutRun:
    """Run the sealed tail only after development has frozen one challenger."""

    if not development.accepted or development.selected_name is None:
        raise ValueError("development gate must pass before sealed holdout evaluation")
    if minimum_holdout_markets < 1:
        raise ValueError("minimum_holdout_markets must be >= 1")
    effective_split = split_config or WalkForwardConfig()
    effective_model = model_config or DirectionModelConfig(
        kind="logistic",
        calibration_method="sigmoid",
        logistic_c=0.1,
    )
    lineage = _development_lineage(
        datasets_by_ablation=datasets_by_ablation,
        split_config=effective_split,
        model_config=effective_model,
    )
    if (
        development.dataset_lineage_hash is None
        or lineage["dataset_lineage_hash"] != development.dataset_lineage_hash
    ):
        raise ValueError("sealed holdout does not match development dataset lineage")
    if development.protocol_hash is None or lineage["protocol_hash"] != development.protocol_hash:
        raise ValueError("sealed holdout does not match development protocol lineage")
    selected_build = datasets_by_ablation.get(development.selected_name)
    if (
        selected_build is None
        or selected_build.paired_control_dataset is None
        or selected_build.challenger_dataset is None
    ):
        raise ValueError("selected paired dataset is unavailable")
    control_dataset = selected_build.paired_control_dataset
    challenger_dataset = selected_build.challenger_dataset
    _validate_paired_datasets(control_dataset, {development.selected_name: challenger_dataset})
    if effective_model.kind != "logistic":
        raise ValueError("market-relative sealed protocol is Logistic-only")
    control_run = run_sealed_holdout_model(
        dataset=control_dataset,
        split_config=effective_split,
        model_config=effective_model,
    )
    challenger_run = run_sealed_holdout_model(
        dataset=challenger_dataset,
        split_config=effective_split,
        model_config=effective_model,
    )
    _validate_paired_predictions(control_run.predictions, challenger_run.predictions)
    evidence = paired_daily_block_bootstrap(
        candidate_predictions=challenger_run.predictions,
        baseline_predictions=control_run.predictions,
        weights=control_dataset.sample_weights,
        baseline_name="control",
        iterations=bootstrap_iterations,
    )
    metrics = _probability_metrics(
        predictions=challenger_run.predictions,
        dataset=challenger_dataset,
    )
    holdout_market_count = len(
        {challenger_dataset.samples[index].group_id for index in challenger_run.holdout_indices}
    )
    failures: list[str] = []
    if holdout_market_count < minimum_holdout_markets:
        failures.append("sealed_holdout_market_count_below_minimum")
    if evidence.log_loss_ci_lower <= 0.0:
        failures.append("sealed_log_loss_ci_lower_not_positive")
    if evidence.brier_ci_lower <= 0.0:
        failures.append("sealed_brier_ci_lower_not_positive")
    if not 0.8 <= metrics["calibration_slope"] <= 1.2:
        failures.append("sealed_calibration_slope_outside_range")
    return MarketRelativeSealedHoldoutRun(
        accepted=not failures,
        selected_name=development.selected_name,
        failed_conditions=tuple(failures),
        holdout_market_count=holdout_market_count,
        control_run=control_run,
        challenger_run=challenger_run,
        log_loss=metrics["log_loss"],
        brier=metrics["brier"],
        calibration_error=metrics["calibration_error"],
        calibration_slope=metrics["calibration_slope"],
        paired_log_loss_improvement=evidence.log_loss_improvement,
        paired_log_loss_ci_lower=evidence.log_loss_ci_lower,
        paired_log_loss_ci_upper=evidence.log_loss_ci_upper,
        paired_brier_improvement=evidence.brier_improvement,
        paired_brier_ci_lower=evidence.brier_ci_lower,
        paired_brier_ci_upper=evidence.brier_ci_upper,
        dataset_lineage_hash=development.dataset_lineage_hash,
        protocol_hash=development.protocol_hash,
    )


def build_market_relative_artifact_contract(
    *,
    development: MarketRelativeDevelopmentRun,
    sealed_holdout: MarketRelativeSealedHoldoutRun,
    source_hash: str,
    rule_epoch: str,
    ingest_version: str,
    join_cadence_seconds: int,
    opening_protocol: Mapping[str, object],
) -> dict[str, object]:
    """Build metadata only after both development and explicit sealed gates pass."""

    if not development.accepted or development.selected_name is None:
        raise ValueError("development gate must pass before artifact metadata is built")
    if (
        not isinstance(sealed_holdout, MarketRelativeSealedHoldoutRun)
        or not sealed_holdout.accepted
    ):
        raise ValueError("sealed holdout gate must pass before artifact metadata is built")
    if sealed_holdout.selected_name != development.selected_name:
        raise ValueError("sealed holdout selected challenger does not match development")
    if (
        development.dataset_lineage_hash is None
        or sealed_holdout.dataset_lineage_hash != development.dataset_lineage_hash
        or development.protocol_hash is None
        or sealed_holdout.protocol_hash != development.protocol_hash
    ):
        raise ValueError("sealed holdout lineage does not match development lineage")
    if (
        development.control_schema_hash is None
        or development.challenger_schema_hashes is None
        or development.split_config is None
        or development.model_config is None
    ):
        raise ValueError("development provenance is incomplete")
    if len(source_hash) != 64 or any(value not in "0123456789abcdef" for value in source_hash):
        raise ValueError("source_hash must be a lowercase SHA-256 digest")
    if join_cadence_seconds < 1:
        raise ValueError("join_cadence_seconds must be >= 1")
    if not rule_epoch or rule_epoch.strip() != rule_epoch:
        raise ValueError("rule_epoch must be non-empty and trimmed")
    if not ingest_version or ingest_version.strip() != ingest_version:
        raise ValueError("ingest_version must be non-empty and trimmed")
    selected = next(
        item for item in development.candidates if item.name == development.selected_name
    )
    return {
        "opening_model_family": "market_relative_v1",
        "market_relative_feature_families": [item.value for item in selected.families],
        "market_relative_protocol": {
            "probability_clip_epsilon": _PROBABILITY_CLIP_EPSILON,
            "join_cadence_seconds": join_cadence_seconds,
            "opening_protocol": dict(opening_protocol),
        },
        "source_hash": source_hash,
        "dataset_lineage_hash": development.dataset_lineage_hash,
        "control_feature_schema_hash": development.control_schema_hash,
        "selected_feature_schema_hash": development.challenger_schema_hashes[
            development.selected_name
        ],
        "protocol_hash": development.protocol_hash,
        "rule_epoch": rule_epoch,
        "ingest_version": ingest_version,
        "split_config": dict(development.split_config),
        "model_config": dict(development.model_config),
        "runtime_promotion_eligible": False,
        "promotion_blockers": ["pair_session_identity_not_proven"],
    }


def _development_lineage(
    *,
    datasets_by_ablation: Mapping[str, OpeningMarketRelativeDatasetBuild],
    split_config: WalkForwardConfig,
    model_config: DirectionModelConfig,
) -> dict[str, object]:
    dataset_payload: dict[str, object] = {}
    challenger_schema_hashes: dict[str, str] = {}
    control_schema_hash: str | None = None
    for name in sorted(datasets_by_ablation):
        build = datasets_by_ablation[name]
        if build.paired_control_dataset is not None:
            current_control_hash = build.paired_control_dataset.schema.hash
            if control_schema_hash is None:
                control_schema_hash = current_control_hash
            elif control_schema_hash != current_control_hash:
                raise ValueError("all ablations must share the control feature schema")
        if build.challenger_dataset is not None:
            challenger_schema_hashes[name] = build.challenger_dataset.schema.hash
        dataset_payload[name] = {
            "control": _dataset_payload(build.paired_control_dataset),
            "challenger": _dataset_payload(build.challenger_dataset),
            "families": [family.value for family in build.families],
            "snapshots_per_market": build.snapshots_per_market,
        }
    split_payload = {
        name: getattr(split_config, name).total_seconds()
        for name in (
            "train_duration",
            "calibration_duration",
            "test_duration",
            "step_duration",
            "embargo_duration",
            "sealed_holdout_duration",
        )
    }
    split_payload = {f"{name}_seconds": value for name, value in split_payload.items()}
    model_payload = asdict(model_config)
    protocol_payload = {
        "version": 1,
        "probability_clip_epsilon": _PROBABILITY_CLIP_EPSILON,
        "ablations": [
            {"name": item.name, "families": [family.value for family in item.families]}
            for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS
        ],
        "split_config": split_payload,
        "model_config": model_payload,
    }
    return {
        "dataset_lineage_hash": _canonical_hash(dataset_payload),
        "control_schema_hash": control_schema_hash,
        "challenger_schema_hashes": challenger_schema_hashes,
        "protocol_hash": _canonical_hash(protocol_payload),
        "split_config": split_payload,
        "model_config": model_payload,
    }


def _dataset_payload(dataset: DirectionDataset | None) -> object:
    if dataset is None:
        return None
    return {
        "schema_hash": dataset.schema.hash,
        "samples": [
            {
                "sample_id": sample.sample_id,
                "group_id": sample.group_id,
                "feature_ts": sample.feature_ts.isoformat(),
                "label_available_ts": sample.label_available_ts.isoformat(),
                "label": sample.label,
            }
            for sample in dataset.samples
        ],
        "vectors": dataset.vectors.tolist(),
        "sample_weights": dataset.sample_weights.tolist(),
    }


def _canonical_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def market_relative_development_report(
    development: MarketRelativeDevelopmentRun,
) -> dict[str, object]:
    """Serialize compact development evidence without embedding row-level predictions."""

    return {
        "protocol": {
            "model_kind": "logistic",
            "ablations": [
                {"name": item.name, "families": [family.value for family in item.families]}
                for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS
            ],
        },
        "decision": {
            "accepted": development.accepted,
            "selected_name": development.selected_name,
            "failed_conditions": list(development.failed_conditions),
            "sealed_holdout_evaluated": development.sealed_holdout_evaluated,
        },
        "coverage": dict(development.coverage),
        "candidates": [
            {
                "name": item.name,
                "families": [family.value for family in item.families],
                "log_loss": item.log_loss,
                "brier": item.brier,
                "calibration_error": item.calibration_error,
                "calibration_slope": item.calibration_slope,
                "oof_prediction_count": len(item.predictions),
                "paired_vs_control": (
                    None
                    if item.name == "control"
                    else {
                        "log_loss_improvement": item.paired_log_loss_improvement,
                        "log_loss_ci_lower": item.paired_log_loss_ci_lower,
                        "log_loss_ci_upper": item.paired_log_loss_ci_upper,
                        "brier_improvement": item.paired_brier_improvement,
                        "brier_ci_lower": item.paired_brier_ci_lower,
                        "brier_ci_upper": item.paired_brier_ci_upper,
                    }
                ),
            }
            for item in development.candidates
        ],
    }


def _coverage(builds: Sequence[OpeningMarketRelativeDatasetBuild]) -> dict[str, int]:
    signatures = {
        (
            item.eligible_market_count,
            item.excluded_missing_market_observation,
            item.excluded_invalid_market_observation,
            item.snapshots_per_market,
        )
        for item in builds
    }
    if len(signatures) != 1:
        raise ValueError("all ablations must have identical coverage counters")
    eligible, missing, invalid, snapshots = signatures.pop()
    return {
        "eligible_market_count": eligible,
        "excluded_missing_market_observation": missing,
        "excluded_invalid_market_observation": invalid,
        "snapshots_per_market": snapshots,
    }


def _validate_paired_datasets(
    control: DirectionDataset,
    challengers: Mapping[str, DirectionDataset | None],
) -> None:
    lineage = tuple(
        (item.sample_id, item.group_id, item.label, item.feature_ts) for item in control.samples
    )
    for name, challenger in challengers.items():
        if challenger is None:
            raise ValueError(f"challenger dataset is unavailable: {name}")
        observed = tuple(
            (item.sample_id, item.group_id, item.label, item.feature_ts)
            for item in challenger.samples
        )
        if observed != lineage or not np.array_equal(
            challenger.sample_weights,
            control.sample_weights,
        ):
            raise ValueError("all ablations must use identical paired samples and weights")


def _validate_paired_predictions(
    baseline: Sequence[OofPrediction], challenger: Sequence[OofPrediction]
) -> None:
    left = tuple(
        (item.sample_index, item.sample_id, item.feature_ts_ns, item.label) for item in baseline
    )
    right = tuple(
        (item.sample_index, item.sample_id, item.feature_ts_ns, item.label) for item in challenger
    )
    if left != right:
        raise ValueError("all ablations must emit identical grouped OOF lineage")


def _candidate_metrics(
    name: str,
    families: tuple[MarketRelativeFeatureFamily, ...],
    run: WalkForwardModelRun,
    dataset: DirectionDataset,
) -> MarketRelativeCandidateMetrics:
    predictions = run.predictions
    metrics = _probability_metrics(predictions=predictions, dataset=dataset)
    return MarketRelativeCandidateMetrics(
        name=name,
        families=families,
        log_loss=metrics["log_loss"],
        brier=metrics["brier"],
        calibration_error=metrics["calibration_error"],
        calibration_slope=metrics["calibration_slope"],
        predictions=tuple(predictions),
    )


def _probability_metrics(
    *,
    predictions: Sequence[OofPrediction] | Sequence[HoldoutPrediction],
    dataset: DirectionDataset,
) -> dict[str, float]:
    indices = np.asarray([item.sample_index for item in predictions], dtype=int)
    labels = np.asarray([item.label for item in predictions], dtype=float)
    probabilities = np.clip(
        np.asarray([item.p_up for item in predictions], dtype=float),
        _PROBABILITY_CLIP_EPSILON,
        1.0 - _PROBABILITY_CLIP_EPSILON,
    )
    weights = dataset.sample_weights[indices]
    log_loss = float(
        np.average(
            -(labels * np.log(probabilities) + (1.0 - labels) * np.log(1.0 - probabilities)),
            weights=weights,
        )
    )
    brier = float(np.average((labels - probabilities) ** 2, weights=weights))
    slope = calibration_slope(labels=labels, probabilities=probabilities, weights=weights)
    if not all(isfinite(value) for value in (log_loss, brier, slope)):
        raise ValueError("candidate metrics must be finite")
    return {
        "log_loss": log_loss,
        "brier": brier,
        "calibration_error": weighted_calibration_error(
            labels=labels,
            probabilities=probabilities,
            weights=weights,
        ),
        "calibration_slope": slope,
    }


__all__ = [
    "MARKET_RELATIVE_LOGISTIC_ABLATIONS",
    "MarketRelativeAblation",
    "MarketRelativeCandidateMetrics",
    "MarketRelativeDevelopmentRun",
    "MarketRelativeSealedHoldoutRun",
    "build_market_relative_artifact_contract",
    "evaluate_market_relative_research_gate",
    "market_relative_development_report",
    "run_market_relative_development",
    "run_market_relative_sealed_holdout",
]
