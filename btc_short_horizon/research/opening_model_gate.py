"""Model replacement and formal direction-gate evidence for the opening proxy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from numbers import Integral

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
    confidence_level: float = 0.95

    def __post_init__(self) -> None:
        if not self.baseline_name or self.baseline_name.strip() != self.baseline_name:
            raise ValueError("baseline_name must be non-empty and trimmed")
        for name in (
            "log_loss_improvement",
            "log_loss_ci_lower",
            "log_loss_ci_upper",
            "brier_improvement",
            "brier_ci_lower",
            "brier_ci_upper",
        ):
            if not isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        if self.log_loss_ci_lower > self.log_loss_ci_upper:
            raise ValueError("log-loss confidence interval must be ordered")
        if self.brier_ci_lower > self.brier_ci_upper:
            raise ValueError("Brier confidence interval must be ordered")
        if not isfinite(self.confidence_level) or not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must lie in (0, 1)")


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
        if not self.name or self.name.strip() != self.name:
            raise ValueError("candidate name must be non-empty and trimmed")
        if self.kind not in {"logistic", "lightgbm"}:
            raise ValueError("candidate kind must be logistic or lightgbm")
        for field_name in ("log_loss", "brier", "calibration_error"):
            if not isfinite(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be finite")
        if self.log_loss < 0.0:
            raise ValueError("log_loss must be >= 0")
        if not 0.0 <= self.brier <= 1.0:
            raise ValueError("brier must lie in [0, 1]")
        if not 0.0 <= self.calibration_error <= 1.0:
            raise ValueError("calibration_error must lie in [0, 1]")


@dataclass(frozen=True, slots=True)
class CandidateSelectionDecision:
    selected_name: str
    best_logistic_name: str
    reason: str
    eligible_lightgbm_candidates: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.eligible_lightgbm_candidates, (str, bytes)):
            raise ValueError("eligible LightGBM candidate names must be an iterable of strings")
        try:
            object.__setattr__(
                self,
                "eligible_lightgbm_candidates",
                tuple(self.eligible_lightgbm_candidates),
            )
        except TypeError as exc:
            raise ValueError("eligible LightGBM candidate names must be iterable") from exc
        for name in ("selected_name", "best_logistic_name", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError(f"{name} must be non-empty and trimmed")
        if len(self.eligible_lightgbm_candidates) != len(
            set(self.eligible_lightgbm_candidates)
        ) or any(
            not isinstance(value, str) or not value or value.strip() != value
            for value in self.eligible_lightgbm_candidates
        ):
            raise ValueError("eligible LightGBM candidate names must be unique and valid")
        if self.reason == "no_lightgbm_candidate_met_replacement_rule":
            if self.selected_name != self.best_logistic_name or self.eligible_lightgbm_candidates:
                raise ValueError("fallback selection must select the best Logistic candidate")
        elif self.reason == "lightgbm_met_replacement_rule":
            if self.selected_name not in self.eligible_lightgbm_candidates:
                raise ValueError("LightGBM selection must be one of the eligible candidates")
        else:
            raise ValueError("unsupported candidate selection reason")


def select_direction_candidate(
    candidates: Sequence[CandidateEvaluation],
) -> CandidateSelectionDecision:
    """Select Logistic unless LightGBM clears every pre-registered replacement check."""

    names = [candidate.name for candidate in candidates]
    if len(names) != len(set(names)):
        raise ValueError("candidate names must be unique")

    logistic = tuple(candidate for candidate in candidates if candidate.kind == "logistic")
    if not logistic:
        raise ValueError("at least one Logistic candidate is required")
    best_logistic = min(
        logistic,
        key=lambda item: (item.log_loss, item.brier, item.calibration_error, item.name),
    )
    lightgbm_count = sum(candidate.kind == "lightgbm" for candidate in candidates)
    minimum_replacement_confidence = 1.0 - (0.05 / max(1, lightgbm_count))
    eligible = tuple(
        candidate
        for candidate in candidates
        if candidate.kind == "lightgbm"
        and candidate.log_loss < best_logistic.log_loss
        and candidate.brier < best_logistic.brier
        and candidate.calibration_error < best_logistic.calibration_error
        and candidate.paired_vs_best_logistic is not None
        and candidate.paired_vs_best_logistic.baseline_name == best_logistic.name
        and candidate.paired_vs_best_logistic.confidence_level >= minimum_replacement_confidence
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
    iterations: int = 10_000,
    seed: int = 17,
    block_length_days: int = 7,
    alpha: float = 0.05,
) -> CandidatePairedEvidence:
    """Estimate paired CIs with a circular moving bootstrap of complete UTC days."""

    if isinstance(iterations, bool) or not isinstance(iterations, Integral) or iterations < 100:
        raise ValueError("iterations must be >= 100")
    if isinstance(seed, bool) or not isinstance(seed, Integral):
        raise ValueError("seed must be an integer")
    if (
        isinstance(block_length_days, bool)
        or not isinstance(block_length_days, Integral)
        or block_length_days < 1
    ):
        raise ValueError("block_length_days must be a positive integer")
    if not isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie in (0, 1)")
    if not baseline_name or baseline_name.strip() != baseline_name:
        raise ValueError("baseline_name must be non-empty and trimmed")
    candidate = _prediction_map(candidate_predictions, name="candidate_predictions")
    baseline = _prediction_map(baseline_predictions, name="baseline_predictions")
    if candidate.keys() != baseline.keys() or not candidate:
        raise ValueError("paired candidates must contain the same non-empty sample set")
    ordered_indices = np.asarray(sorted(candidate), dtype=int)
    candidate_items = [candidate[index] for index in ordered_indices]
    baseline_items = [baseline[index] for index in ordered_indices]
    if any(
        candidate_item.sample_id != baseline_item.sample_id
        or candidate_item.feature_ts_ns != baseline_item.feature_ts_ns
        or candidate_item.label != baseline_item.label
        for candidate_item, baseline_item in zip(candidate_items, baseline_items, strict=True)
    ):
        raise ValueError("paired candidates must have identical sample lineage and labels")
    labels = np.asarray([item.label for item in candidate_items])
    sample_weights = _selected_weights(weights, ordered_indices)
    labels, candidate_probability, sample_weights = _validated_arrays(
        labels,
        np.asarray([item.p_up for item in candidate_items]),
        sample_weights,
    )
    _, baseline_probability, _ = _validated_arrays(
        labels,
        np.asarray([item.p_up for item in baseline_items]),
        sample_weights,
    )
    dates = np.asarray(
        [
            datetime.fromtimestamp(item.feature_ts_ns / 1_000_000_000, tz=UTC).date()
            for item in candidate_items
        ],
        dtype=object,
    )
    unique_dates = tuple(sorted(set(dates.tolist())))
    if len(unique_dates) < 2:
        raise ValueError("paired daily-block bootstrap requires at least two UTC days")
    log_loss_delta = _binary_log_loss(labels, baseline_probability) - _binary_log_loss(
        labels, candidate_probability
    )
    brier_delta = (labels - baseline_probability) ** 2 - (labels - candidate_probability) ** 2
    log_point = float(np.average(log_loss_delta, weights=sample_weights))
    brier_point = float(np.average(brier_delta, weights=sample_weights))
    first_day, last_day = unique_dates[0], unique_dates[-1]
    calendar_days = tuple(
        first_day + timedelta(days=offset) for offset in range((last_day - first_day).days + 1)
    )
    day_positions = {day: position for position, day in enumerate(calendar_days)}
    log_numerator = np.zeros(len(calendar_days), dtype=float)
    brier_numerator = np.zeros(len(calendar_days), dtype=float)
    weight_by_day = np.zeros(len(calendar_days), dtype=float)
    for day in unique_dates:
        mask = dates == day
        position = day_positions[day]
        day_weights = sample_weights[mask]
        weight_by_day[position] = float(np.sum(day_weights))
        log_numerator[position] = float(np.sum(log_loss_delta[mask] * day_weights))
        brier_numerator[position] = float(np.sum(brier_delta[mask] * day_weights))
    rng = np.random.default_rng(seed)
    block_length = min(int(block_length_days), len(calendar_days))
    block_offsets = np.arange(block_length, dtype=int)
    block_count = (len(calendar_days) + block_length - 1) // block_length
    samples = np.empty((int(iterations), len(calendar_days)), dtype=np.int64)
    unresolved = np.arange(int(iterations), dtype=np.int64)
    for _ in range(100):
        starts = rng.integers(0, len(calendar_days), size=(len(unresolved), block_count))
        choices = ((starts[:, :, None] + block_offsets) % len(calendar_days)).reshape(
            len(unresolved), -1
        )[:, : len(calendar_days)]
        valid = np.sum(weight_by_day[choices], axis=1) > 0.0
        samples[unresolved[valid]] = choices[valid]
        unresolved = unresolved[~valid]
        if not len(unresolved):
            break
    if len(unresolved):
        raise RuntimeError("bootstrap could not sample any weighted UTC day")
    sampled_weight = np.sum(weight_by_day[samples], axis=1)
    sampled_log = np.sum(log_numerator[samples], axis=1) / sampled_weight
    sampled_brier = np.sum(brier_numerator[samples], axis=1) / sampled_weight
    return CandidatePairedEvidence(
        baseline_name=baseline_name,
        log_loss_improvement=log_point,
        log_loss_ci_lower=float(np.quantile(sampled_log, alpha / 2.0)),
        log_loss_ci_upper=float(np.quantile(sampled_log, 1.0 - alpha / 2.0)),
        brier_improvement=brier_point,
        brier_ci_lower=float(np.quantile(sampled_brier, alpha / 2.0)),
        brier_ci_upper=float(np.quantile(sampled_brier, 1.0 - alpha / 2.0)),
        confidence_level=1.0 - alpha,
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
    indexed = _prediction_map(items, name="predictions")
    if len(indexed) != len(items):  # defensive; _prediction_map already rejects duplicates
        raise ValueError("predictions must be unique")
    indices = np.asarray([item.sample_index for item in items], dtype=int)
    sample_weights = _selected_weights(weights, indices)
    labels, probabilities, sample_weights = _validated_arrays(
        np.asarray([item.label for item in items]),
        np.asarray([item.p_up for item in items]),
        sample_weights,
    )
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
    confidence_level: float = 0.95,
) -> dict[str, object]:
    """Serialize formal direction evidence and a fail-closed gate decision."""

    if not isinstance(protocol_eligible, bool):
        raise ValueError("protocol_eligible must be boolean")
    if len(protocol_failures) != len(set(protocol_failures)) or any(
        not isinstance(value, str) or not value or value.strip() != value
        for value in protocol_failures
    ):
        raise ValueError("protocol failures must be unique, non-empty, trimmed strings")
    if protocol_eligible and protocol_failures:
        raise ValueError("protocol failures cannot accompany protocol_eligible=True")
    if not isfinite(confidence_level) or not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must lie in (0, 1)")
    for name, value in (
        ("log_loss_ci_upper", log_loss_ci_upper),
        ("brier_ci_upper", brier_ci_upper),
    ):
        if not isfinite(value):
            raise ValueError(f"{name} must be finite")
    if log_loss_ci_lower > log_loss_ci_upper or brier_ci_lower > brier_ci_upper:
        raise ValueError("paired confidence intervals must be ordered")
    expected_boundaries = ((0.55, 0.60), (0.60, 0.65), (0.65, 0.70), (0.70, 1.00))
    if len(target_bands) != len(expected_boundaries):
        raise ValueError("target bands must contain the four pre-registered confidence bands")
    for band, (expected_lower, expected_upper) in zip(
        target_bands,
        expected_boundaries,
        strict=True,
    ):
        try:
            lower = float(band["lower"])
            upper = float(band["upper"])
            snapshot_count = band["snapshot_count"]
            effective_market_count = band["effective_market_count"]
            error = float(band["calibration_error"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("target band evidence has an invalid schema") from exc
        if lower != expected_lower or upper != expected_upper:
            raise ValueError("target band boundaries do not match the pre-registered protocol")
        if (
            isinstance(snapshot_count, bool)
            or not isinstance(snapshot_count, Integral)
            or snapshot_count < 0
        ):
            raise ValueError("target band snapshot count must be a non-negative integer")
        if (
            isinstance(effective_market_count, bool)
            or not isinstance(effective_market_count, Integral)
            or effective_market_count < 0
        ):
            raise ValueError("target band effective market count must be a non-negative integer")
        if effective_market_count > snapshot_count:
            raise ValueError("target band market count cannot exceed its snapshot count")
        if not isfinite(error) or not 0.0 <= error <= 1.0:
            raise ValueError("target band calibration error must lie in [0, 1]")

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
                "confidence_level": confidence_level,
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
            "protocol_failures": list(protocol_failures),
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
    raw_labels = np.asarray(labels)
    probabilities = np.asarray(probabilities, dtype=float)
    weights = np.asarray(weights, dtype=float)
    if not (raw_labels.ndim == probabilities.ndim == weights.ndim == 1):
        raise ValueError("labels, probabilities, and weights must be one-dimensional")
    if not (len(raw_labels) == len(probabilities) == len(weights)) or not len(raw_labels):
        raise ValueError("labels, probabilities, and weights must be non-empty and aligned")
    if np.issubdtype(raw_labels.dtype, np.bool_) or not (
        np.issubdtype(raw_labels.dtype, np.integer) or np.issubdtype(raw_labels.dtype, np.floating)
    ):
        raise ValueError("labels must contain numeric binary classes 0 and 1")
    labels = np.asarray(raw_labels, dtype=float)
    if not np.isfinite(labels).all() or np.any((labels != 0.0) & (labels != 1.0)):
        raise ValueError("labels must contain only binary classes 0 and 1")
    if not np.isfinite(probabilities).all() or np.any(
        (probabilities < 0.0) | (probabilities > 1.0)
    ):
        raise ValueError("probabilities must be finite and lie in [0, 1]")
    if not np.isfinite(weights).all():
        raise ValueError("weights must be finite")
    if np.any(weights <= 0.0):
        raise ValueError("weights must be positive")
    return labels, np.clip(probabilities, 1e-6, 1.0 - 1e-6), weights


def _prediction_map(predictions: Iterable[object], *, name: str) -> dict[int, object]:
    result: dict[int, object] = {}
    for item in predictions:
        sample_index = getattr(item, "sample_index", None)
        if (
            isinstance(sample_index, bool)
            or not isinstance(sample_index, Integral)
            or sample_index < 0
        ):
            raise ValueError(f"{name} sample indices must be non-negative integers")
        if sample_index in result:
            raise ValueError(f"{name} must not contain duplicate sample indices")
        sample_id = getattr(item, "sample_id", None)
        feature_ts_ns = getattr(item, "feature_ts_ns", None)
        if not isinstance(sample_id, str) or not sample_id or sample_id.strip() != sample_id:
            raise ValueError(f"{name} sample IDs must be non-empty and trimmed")
        if (
            isinstance(feature_ts_ns, bool)
            or not isinstance(feature_ts_ns, Integral)
            or feature_ts_ns < 0
        ):
            raise ValueError(f"{name} timestamps must be non-negative integers")
        result[int(sample_index)] = item
    return result


def _selected_weights(weights: np.ndarray, indices: np.ndarray) -> np.ndarray:
    vector = np.asarray(weights, dtype=float)
    if vector.ndim != 1 or not len(vector):
        raise ValueError("weights must be a non-empty one-dimensional vector")
    if not np.isfinite(vector).all() or np.any(vector <= 0.0):
        raise ValueError("weights must contain finite values > 0")
    if np.any(indices < 0) or np.any(indices >= len(vector)):
        raise ValueError("prediction sample index is outside the weights vector")
    return vector[indices]


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
