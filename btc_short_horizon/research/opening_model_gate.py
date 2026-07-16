"""Model replacement and formal direction-gate evidence for the opening proxy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import isfinite

import numpy as np
from sklearn.linear_model import LogisticRegression

from btc_short_horizon.research.gates import (
    CalibrationBinEvidence,
    DirectionGateEvidence,
    GateDecision,
    evaluate_direction_gate,
)


@dataclass(frozen=True, slots=True)
class CandidatePairedEvidence:
    """Paired daily-block loss improvements over the best Logistic candidate."""

    baseline_name: str
    log_loss_improvement: float
    log_loss_ci_lower: float
    log_loss_ci_upper: float
    brier_improvement: float
    brier_ci_lower: float
    brier_ci_upper: float


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """Development evidence used by the frozen candidate replacement rule."""

    name: str
    kind: str
    log_loss: float
    brier: float
    calibration_error: float
    paired_vs_best_logistic: CandidatePairedEvidence | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("candidate name is required")
        if self.kind not in {"logistic", "lightgbm"}:
            raise ValueError("candidate kind must be logistic or lightgbm")
        for field_name in ("log_loss", "brier", "calibration_error"):
            if not isfinite(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be finite")


@dataclass(frozen=True, slots=True)
class CandidateSelectionDecision:
    selected_name: str
    best_logistic_name: str
    reason: str
    eligible_lightgbm_candidates: tuple[str, ...]


def select_direction_candidate(
    candidates: Sequence[CandidateEvaluation],
) -> CandidateSelectionDecision:
    """Select Logistic unless LightGBM clears every pre-registered replacement check."""

    logistic = tuple(candidate for candidate in candidates if candidate.kind == "logistic")
    if not logistic:
        raise ValueError("at least one Logistic candidate is required")
    best_logistic = min(
        logistic,
        key=lambda item: (item.log_loss, item.brier, item.calibration_error, item.name),
    )
    eligible = tuple(
        candidate
        for candidate in candidates
        if candidate.kind == "lightgbm"
        and candidate.log_loss < best_logistic.log_loss
        and candidate.brier < best_logistic.brier
        and candidate.calibration_error < best_logistic.calibration_error
        and candidate.paired_vs_best_logistic is not None
        and candidate.paired_vs_best_logistic.baseline_name == best_logistic.name
        and candidate.paired_vs_best_logistic.log_loss_ci_lower > 0.0
        and candidate.paired_vs_best_logistic.brier_ci_lower > 0.0
    )
    if not eligible:
        return CandidateSelectionDecision(
            selected_name=best_logistic.name,
            best_logistic_name=best_logistic.name,
            reason="no_lightgbm_candidate_met_replacement_rule",
            eligible_lightgbm_candidates=(),
        )
    selected = min(
        eligible,
        key=lambda item: (item.log_loss, item.brier, item.calibration_error, item.name),
    )
    return CandidateSelectionDecision(
        selected_name=selected.name,
        best_logistic_name=best_logistic.name,
        reason="lightgbm_met_replacement_rule",
        eligible_lightgbm_candidates=tuple(candidate.name for candidate in eligible),
    )


def paired_daily_block_bootstrap(
    *,
    candidate_predictions: Iterable[object],
    baseline_predictions: Iterable[object],
    weights: np.ndarray,
    baseline_name: str,
    iterations: int = 2_000,
    seed: int = 17,
) -> CandidatePairedEvidence:
    """Estimate paired loss improvement CIs by resampling complete UTC days."""

    if iterations < 100:
        raise ValueError("iterations must be >= 100")
    candidate = {item.sample_index: item for item in candidate_predictions}
    baseline = {item.sample_index: item for item in baseline_predictions}
    if candidate.keys() != baseline.keys() or not candidate:
        raise ValueError("paired candidates must contain the same non-empty sample set")
    ordered_indices = np.asarray(sorted(candidate), dtype=int)
    candidate_items = [candidate[index] for index in ordered_indices]
    baseline_items = [baseline[index] for index in ordered_indices]
    labels = np.asarray([item.label for item in candidate_items], dtype=float)
    if labels.tolist() != [float(item.label) for item in baseline_items]:
        raise ValueError("paired candidates must have identical labels")
    candidate_probability = np.clip(
        np.asarray([item.p_up for item in candidate_items], dtype=float), 1e-6, 1.0 - 1e-6
    )
    baseline_probability = np.clip(
        np.asarray([item.p_up for item in baseline_items], dtype=float), 1e-6, 1.0 - 1e-6
    )
    sample_weights = np.asarray(weights, dtype=float)[ordered_indices]
    dates = np.asarray(
        [
            datetime.fromtimestamp(item.feature_ts_ns / 1_000_000_000, tz=UTC).date().isoformat()
            for item in candidate_items
        ]
    )
    unique_dates = np.unique(dates)
    if len(unique_dates) < 2:
        raise ValueError("paired daily-block bootstrap requires at least two UTC days")
    log_loss_delta = _binary_log_loss(labels, baseline_probability) - _binary_log_loss(
        labels, candidate_probability
    )
    brier_delta = (labels - baseline_probability) ** 2 - (labels - candidate_probability) ** 2
    log_point = float(np.average(log_loss_delta, weights=sample_weights))
    brier_point = float(np.average(brier_delta, weights=sample_weights))
    log_by_day, brier_by_day, weight_by_day = [], [], []
    for day in unique_dates:
        mask = dates == day
        day_weights = sample_weights[mask]
        weight_by_day.append(float(np.sum(day_weights)))
        log_by_day.append(float(np.average(log_loss_delta[mask], weights=day_weights)))
        brier_by_day.append(float(np.average(brier_delta[mask], weights=day_weights)))
    rng = np.random.default_rng(seed)
    sampled_log = np.empty(iterations, dtype=float)
    sampled_brier = np.empty(iterations, dtype=float)
    log_array = np.asarray(log_by_day)
    brier_array = np.asarray(brier_by_day)
    day_weights = np.asarray(weight_by_day)
    for iteration in range(iterations):
        sample = rng.integers(0, len(unique_dates), size=len(unique_dates))
        sampled_log[iteration] = np.average(log_array[sample], weights=day_weights[sample])
        sampled_brier[iteration] = np.average(brier_array[sample], weights=day_weights[sample])
    return CandidatePairedEvidence(
        baseline_name=baseline_name,
        log_loss_improvement=log_point,
        log_loss_ci_lower=float(np.quantile(sampled_log, 0.025)),
        log_loss_ci_upper=float(np.quantile(sampled_log, 0.975)),
        brier_improvement=brier_point,
        brier_ci_lower=float(np.quantile(sampled_brier, 0.025)),
        brier_ci_upper=float(np.quantile(sampled_brier, 0.975)),
    )


def weighted_calibration_error(
    *, labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray
) -> float:
    """Return ten-bin weighted expected calibration error."""

    labels, probabilities, weights = _validated_arrays(labels, probabilities, weights)
    error = 0.0
    total_weight = float(np.sum(weights))
    for lower in np.arange(0.0, 1.0, 0.1):
        upper = lower + 0.1
        mask = (probabilities >= lower) & (
            (probabilities < upper) if upper < 1.0 else (probabilities <= upper)
        )
        if np.any(mask):
            bin_weight = float(np.sum(weights[mask]))
            error += (
                bin_weight
                / total_weight
                * abs(
                    float(np.average(labels[mask], weights=weights[mask]))
                    - float(np.average(probabilities[mask], weights=weights[mask]))
                )
            )
    return error


def calibration_slope(
    *, labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray
) -> float:
    """Fit the standard weighted logistic calibration slope on prediction logits."""

    labels, probabilities, weights = _validated_arrays(labels, probabilities, weights)
    if len(np.unique(labels)) != 2:
        raise ValueError("calibration slope requires both labels")
    clipped = np.clip(probabilities, 1e-6, 1.0 - 1e-6)
    logits = np.log(clipped / (1.0 - clipped)).reshape(-1, 1)
    model = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2_000)
    model.fit(logits, labels.astype(int), sample_weight=weights)
    return float(model.coef_[0, 0])


def target_confidence_bands(
    *, predictions: Iterable[object], weights: np.ndarray
) -> tuple[dict[str, object], ...]:
    """Build directional confidence-band evidence without counting snapshots as markets."""

    items = tuple(predictions)
    probabilities = np.asarray([item.p_up for item in items], dtype=float)
    labels = np.asarray([item.label for item in items], dtype=int)
    sample_weights = np.asarray(weights, dtype=float)[
        np.asarray([item.sample_index for item in items], dtype=int)
    ]
    confidence = np.maximum(probabilities, 1.0 - probabilities)
    correct = ((probabilities >= 0.5) == labels).astype(float)
    market_ids = np.asarray([_market_id(item.sample_id) for item in items])
    bands: list[dict[str, object]] = []
    for lower, upper in ((0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 1.00)):
        mask = (confidence >= lower) & (
            (confidence < upper) if upper < 1.0 else (confidence <= upper)
        )
        if np.any(mask):
            mean_confidence = float(np.average(confidence[mask], weights=sample_weights[mask]))
            observed_accuracy = float(np.average(correct[mask], weights=sample_weights[mask]))
            market_count = len(np.unique(market_ids[mask]))
            calibration_error = abs(observed_accuracy - mean_confidence)
        else:
            mean_confidence = None
            observed_accuracy = None
            market_count = 0
            calibration_error = 1.0
        bands.append(
            {
                "lower": lower,
                "upper": upper,
                "snapshot_count": int(np.sum(mask)),
                "effective_market_count": market_count,
                "mean_confidence": mean_confidence,
                "observed_accuracy": observed_accuracy,
                "calibration_error": calibration_error,
            }
        )
    return tuple(bands)


def build_direction_gate_artifact(
    *,
    sealed_holdout_markets: int,
    log_loss_improvement: float,
    log_loss_ci_lower: float,
    log_loss_ci_upper: float,
    brier_improvement: float,
    brier_ci_lower: float,
    brier_ci_upper: float,
    calibration_slope: float,
    target_bands: Sequence[Mapping[str, object]],
    protocol_eligible: bool,
    protocol_failures: Sequence[str] = (),
) -> dict[str, object]:
    """Serialize formal direction evidence and a fail-closed gate decision."""

    evidence = DirectionGateEvidence(
        sealed_holdout_markets=sealed_holdout_markets,
        log_loss_improvement=log_loss_improvement,
        log_loss_ci_lower=log_loss_ci_lower,
        brier_improvement=brier_improvement,
        brier_ci_lower=brier_ci_lower,
        calibration_slope=calibration_slope,
        target_bins=tuple(
            CalibrationBinEvidence(
                sample_count=int(band["effective_market_count"]),
                calibration_error=float(band["calibration_error"]),
            )
            for band in target_bands
        ),
    )
    base_decision = evaluate_direction_gate(evidence)
    failures = list(base_decision.failed_conditions)
    if not protocol_eligible:
        failures.extend(protocol_failures or ("protocol_not_gate_eligible",))
    decision = GateDecision(accepted=not failures, failed_conditions=tuple(dict.fromkeys(failures)))
    return {
        "evidence": {
            "sealed_holdout_markets": sealed_holdout_markets,
            "paired_vs_training_prior": {
                "log_loss_improvement": log_loss_improvement,
                "log_loss_ci_lower": log_loss_ci_lower,
                "log_loss_ci_upper": log_loss_ci_upper,
                "brier_improvement": brier_improvement,
                "brier_ci_lower": brier_ci_lower,
                "brier_ci_upper": brier_ci_upper,
            },
            "calibration_slope": calibration_slope,
            "target_confidence_bands": [dict(band) for band in target_bands],
            "protocol_eligible": protocol_eligible,
        },
        "decision": {
            "accepted": decision.accepted,
            "failed_conditions": list(decision.failed_conditions),
        },
    }


def candidate_evaluation_dict(candidate: CandidateEvaluation) -> dict[str, object]:
    """Serialize one selection candidate including paired replacement evidence."""

    return asdict(candidate)


def _binary_log_loss(labels: np.ndarray, probabilities: np.ndarray) -> np.ndarray:
    return -(labels * np.log(probabilities) + (1.0 - labels) * np.log(1.0 - probabilities))


def _validated_arrays(
    labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    labels = np.asarray(labels, dtype=float)
    probabilities = np.clip(np.asarray(probabilities, dtype=float), 1e-6, 1.0 - 1e-6)
    weights = np.asarray(weights, dtype=float)
    if not (labels.ndim == probabilities.ndim == weights.ndim == 1):
        raise ValueError("labels, probabilities, and weights must be one-dimensional")
    if not (len(labels) == len(probabilities) == len(weights)) or not len(labels):
        raise ValueError("labels, probabilities, and weights must be non-empty and aligned")
    if not np.isfinite(probabilities).all() or not np.isfinite(weights).all():
        raise ValueError("probabilities and weights must be finite")
    if np.any(weights <= 0.0):
        raise ValueError("weights must be positive")
    return labels, probabilities, weights


def _market_id(sample_id: str) -> str:
    market_id, separator, timestamp = sample_id.rpartition("@")
    return market_id if separator and market_id and timestamp.isdigit() else sample_id


__all__ = [
    "CandidateEvaluation",
    "CandidatePairedEvidence",
    "CandidateSelectionDecision",
    "build_direction_gate_artifact",
    "calibration_slope",
    "candidate_evaluation_dict",
    "paired_daily_block_bootstrap",
    "select_direction_candidate",
    "target_confidence_bands",
    "weighted_calibration_error",
]
