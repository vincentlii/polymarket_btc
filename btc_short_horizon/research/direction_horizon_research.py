"""Paired market-level evidence for direction horizon comparisons."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

import numpy as np

from btc_short_horizon.research.pipeline import DirectionDataset, OofPrediction
from btc_short_horizon.strategy import OpeningStage


_EPSILON = 1e-6


@dataclass(frozen=True, slots=True)
class MarketProbabilityEvidence:
    market_slug: str
    feature_ts_ns: int
    stage: OpeningStage
    label: int
    raw_p_up: float
    calibrated_p_up: float


@dataclass(frozen=True, slots=True)
class PairedProbabilityEvidence:
    market_count: int
    log_loss_improvement: float
    log_loss_ci_lower: float
    log_loss_ci_upper: float
    brier_improvement: float
    brier_ci_lower: float
    brier_ci_upper: float
    block_count: int
    bootstrap_iterations: int


def collapse_oof_to_market_probabilities(
    *,
    dataset: DirectionDataset,
    predictions: Sequence[OofPrediction],
    stage: OpeningStage,
) -> tuple[MarketProbabilityEvidence, ...]:
    """Collapse repeated snapshots to one equal-weight OOS probability per market/stage."""

    if not predictions:
        raise ValueError("predictions must not be empty")
    grouped: dict[str, list[OofPrediction]] = defaultdict(list)
    for prediction in predictions:
        if prediction.sample_index >= len(dataset.samples):
            raise ValueError("prediction sample_index falls outside the dataset")
        sample = dataset.samples[prediction.sample_index]
        if prediction.sample_id != sample.sample_id or prediction.label != sample.label:
            raise ValueError("prediction identity or label does not match the dataset")
        grouped[sample.group_id].append(prediction)
    result: list[MarketProbabilityEvidence] = []
    for market_slug, items in grouped.items():
        labels = {item.label for item in items}
        if len(labels) != 1:
            raise ValueError("one market cannot contain conflicting OOF labels")
        result.append(
            MarketProbabilityEvidence(
                market_slug=market_slug,
                feature_ts_ns=min(item.feature_ts_ns for item in items),
                stage=OpeningStage(stage),
                label=items[0].label,
                raw_p_up=float(np.mean([item.raw_p_up for item in items])),
                calibrated_p_up=float(np.mean([item.p_up for item in items])),
            )
        )
    return tuple(sorted(result, key=lambda item: (item.feature_ts_ns, item.market_slug)))


def paired_market_block_bootstrap(
    *,
    baseline: Sequence[MarketProbabilityEvidence],
    candidate: Sequence[MarketProbabilityEvidence],
    iterations: int = 10_000,
    random_seed: int = 17,
    alpha: float = 0.05,
) -> PairedProbabilityEvidence:
    """Compare exact paired OOS markets by UTC-day blocks."""

    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if not isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError("alpha must lie in (0, 1)")
    baseline_by_slug = {item.market_slug: item for item in baseline}
    candidate_by_slug = {item.market_slug: item for item in candidate}
    if len(baseline_by_slug) != len(baseline) or len(candidate_by_slug) != len(candidate):
        raise ValueError("paired evidence must contain unique market slugs")
    if set(baseline_by_slug) != set(candidate_by_slug) or not baseline_by_slug:
        raise ValueError("baseline and candidate must cover identical non-empty markets")
    ordered = tuple(
        sorted(baseline_by_slug.values(), key=lambda item: (item.feature_ts_ns, item.market_slug))
    )
    candidate_ordered = tuple(candidate_by_slug[item.market_slug] for item in ordered)
    for base, challenger in zip(ordered, candidate_ordered, strict=True):
        if base.label != challenger.label or base.stage is not challenger.stage:
            raise ValueError("paired market labels and stages must match")
    labels = np.asarray([item.label for item in ordered], dtype=float)
    base_p = np.clip(
        np.asarray([item.calibrated_p_up for item in ordered]), _EPSILON, 1.0 - _EPSILON
    )
    candidate_p = np.clip(
        np.asarray([item.calibrated_p_up for item in candidate_ordered]),
        _EPSILON,
        1.0 - _EPSILON,
    )
    log_loss_delta = _binary_log_losses(labels, base_p) - _binary_log_losses(labels, candidate_p)
    brier_delta = (base_p - labels) ** 2 - (candidate_p - labels) ** 2
    block_indices: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(ordered):
        day = datetime.fromtimestamp(item.feature_ts_ns / 1_000_000_000, tz=UTC).date().isoformat()
        block_indices[day].append(index)
    blocks = tuple(np.asarray(indices, dtype=int) for _, indices in sorted(block_indices.items()))
    rng = np.random.default_rng(random_seed)
    log_samples = np.empty(iterations, dtype=float)
    brier_samples = np.empty(iterations, dtype=float)
    for iteration in range(iterations):
        selected_blocks = rng.integers(0, len(blocks), size=len(blocks))
        indices = np.concatenate([blocks[index] for index in selected_blocks])
        log_samples[iteration] = float(np.mean(log_loss_delta[indices]))
        brier_samples[iteration] = float(np.mean(brier_delta[indices]))
    lower = alpha / 2.0
    upper = 1.0 - lower
    return PairedProbabilityEvidence(
        market_count=len(ordered),
        log_loss_improvement=float(np.mean(log_loss_delta)),
        log_loss_ci_lower=float(np.quantile(log_samples, lower)),
        log_loss_ci_upper=float(np.quantile(log_samples, upper)),
        brier_improvement=float(np.mean(brier_delta)),
        brier_ci_lower=float(np.quantile(brier_samples, lower)),
        brier_ci_upper=float(np.quantile(brier_samples, upper)),
        block_count=len(blocks),
        bootstrap_iterations=iterations,
    )


def _binary_log_losses(labels: np.ndarray, probabilities: np.ndarray) -> np.ndarray:
    return -(labels * np.log(probabilities) + (1.0 - labels) * np.log1p(-probabilities))


__all__ = [
    "MarketProbabilityEvidence",
    "PairedProbabilityEvidence",
    "collapse_oof_to_market_probabilities",
    "paired_market_block_bootstrap",
]
