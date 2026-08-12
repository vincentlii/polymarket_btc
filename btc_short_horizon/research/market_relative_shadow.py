"""Strict paired Shadow dataset for the Paper-only market-relative challenger."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from btc_short_horizon.models.market_relative import (
    fit_market_relative_offset_model,
    market_relative_runtime_feature_schema,
    market_relative_runtime_feature_values,
)
from btc_short_horizon.research.opening_model_gate import calibration_slope
from btc_short_horizon.research.opening_dataset import market_stage_sample_weights
from btc_short_horizon.strategy import OpeningStage


_REQUIRED_COLUMNS = {
    "market_slug",
    "model_version",
    "feature_schema_hash",
    "market_window_start_ts_ns",
    "trigger_ts_ns",
    "p_up",
    "p_boundary_up",
    "p_market_mid_up",
    "data_age_seconds",
    "has_data_gap",
    "structure_valid",
    "tick_unchanged",
    "fee_unchanged",
    "latency_healthy",
}


@dataclass(frozen=True, slots=True)
class MarketRelativeShadowDataset:
    vectors: np.ndarray
    labels: np.ndarray
    market_up_probabilities: np.ndarray
    direction_up_probabilities: np.ndarray
    sample_weights: tuple[float, ...]
    market_slugs: tuple[str, ...]
    trigger_ts_ns: tuple[int, ...]
    stages: tuple[OpeningStage, ...]

    @property
    def market_count(self) -> int:
        return len(set(self.market_slugs))


@dataclass(frozen=True, slots=True)
class MarketRelativeShadowDatasetBuild:
    dataset: MarketRelativeShadowDataset
    discovered_market_files: int
    excluded_missing_label_markets: int
    excluded_invalid_artifact_markets: int
    excluded_missing_stage_markets: int
    excluded_at_or_after_rule_boundary_markets: int


@dataclass(frozen=True, slots=True)
class MarketRelativeLogisticCandidate:
    logistic_c: float
    log_loss: float
    brier: float
    calibration_slope: float
    log_loss_improvement: float
    log_loss_ci_lower: float
    log_loss_ci_upper: float
    brier_improvement: float
    brier_ci_lower: float
    brier_ci_upper: float


@dataclass(frozen=True, slots=True)
class MarketRelativeDevelopmentResult:
    accepted: bool
    selected_logistic_c: float | None
    failed_conditions: tuple[str, ...]
    eligible_market_count: int
    oof_market_count: int
    oof_day_count: int
    candidates: tuple[MarketRelativeLogisticCandidate, ...]
    market_baseline_log_loss: float | None
    market_baseline_brier: float | None
    direction_baseline_log_loss: float | None
    direction_baseline_brier: float | None


def load_market_relative_shadow_dataset(
    *,
    shadow_root: Path,
    labels_by_market: Mapping[str, int],
    end_before_epoch_seconds: int,
    maximum_pair_age_seconds: float = 1.0,
) -> MarketRelativeShadowDatasetBuild:
    """Load causal rows only; one eligible row in every stage is mandatory."""

    if end_before_epoch_seconds < 1:
        raise ValueError("end_before_epoch_seconds must be positive")
    if not isfinite(maximum_pair_age_seconds) or maximum_pair_age_seconds <= 0.0:
        raise ValueError("maximum_pair_age_seconds must be finite and > 0")
    schema = market_relative_runtime_feature_schema()
    files = sorted(shadow_root.glob("*/predictions.parquet"))
    rows: list[tuple[str, int, OpeningStage, int, float, float, tuple[float, ...]]] = []
    missing_label = 0
    invalid_artifact = 0
    missing_stage = 0
    after_boundary = 0
    seen_slugs: set[str] = set()
    for path in files:
        try:
            table = pq.read_table(path)
            selected = _validated_market_rows(
                table=table,
                path=path,
                labels_by_market=labels_by_market,
                end_before_epoch_seconds=end_before_epoch_seconds,
                maximum_pair_age_seconds=maximum_pair_age_seconds,
                schema=schema,
            )
        except KeyError:
            missing_label += 1
            continue
        except (OSError, ValueError, pa.ArrowException):
            invalid_artifact += 1
            continue
        if selected is None:
            after_boundary += 1
            continue
        slug, market_rows = selected
        if slug in seen_slugs:
            invalid_artifact += 1
            continue
        seen_slugs.add(slug)
        stages = tuple(row[2] for row in market_rows)
        try:
            weights = market_stage_sample_weights(stages)
        except ValueError:
            missing_stage += 1
            continue
        rows.extend(
            (*row[:6], vector, weight)
            for row, vector, weight in zip(
                market_rows,
                (row[6] for row in market_rows),
                weights,
                strict=True,
            )
        )
    if not rows:
        vectors = np.empty((0, len(schema.names)), dtype=float)
    else:
        vectors = np.asarray([row[6] for row in rows], dtype=float)
    dataset = MarketRelativeShadowDataset(
        vectors=vectors,
        labels=np.asarray([row[3] for row in rows], dtype=int),
        market_up_probabilities=np.asarray([row[4] for row in rows], dtype=float),
        direction_up_probabilities=np.asarray([row[5] for row in rows], dtype=float),
        sample_weights=tuple(float(row[7]) for row in rows),
        market_slugs=tuple(row[0] for row in rows),
        trigger_ts_ns=tuple(row[1] for row in rows),
        stages=tuple(row[2] for row in rows),
    )
    return MarketRelativeShadowDatasetBuild(
        dataset=dataset,
        discovered_market_files=len(files),
        excluded_missing_label_markets=missing_label,
        excluded_invalid_artifact_markets=invalid_artifact,
        excluded_missing_stage_markets=missing_stage,
        excluded_at_or_after_rule_boundary_markets=after_boundary,
    )


def run_market_relative_logistic_development(
    *,
    dataset: MarketRelativeShadowDataset,
    logistic_cs: tuple[float, ...] = (0.01, 0.03, 0.1, 0.3, 1.0),
    train_days: int = 4,
    calibration_days: int = 1,
    test_days: int = 1,
    minimum_training_markets: int = 250,
    minimum_calibration_markets: int = 50,
    minimum_oof_markets: int = 300,
    bootstrap_iterations: int = 5_000,
    random_seed: int = 17,
) -> MarketRelativeDevelopmentResult:
    """Run the frozen Logistic grid on grouped daily OOS folds without a holdout."""

    if not logistic_cs or len(set(logistic_cs)) != len(logistic_cs):
        raise ValueError("logistic_cs must be non-empty and unique")
    if any(not isfinite(value) or value <= 0.0 for value in logistic_cs):
        raise ValueError("logistic_cs must contain finite positive values")
    if min(train_days, calibration_days, test_days) < 1:
        raise ValueError("development durations must be positive whole days")
    if bootstrap_iterations < 1:
        raise ValueError("bootstrap_iterations must be >= 1")
    if dataset.market_count < minimum_oof_markets:
        return _empty_development_result(
            dataset,
            failure="eligible_market_coverage_below_minimum",
        )
    days = np.asarray(
        [
            datetime.fromtimestamp(value / 1_000_000_000, tz=UTC).date()
            for value in dataset.trigger_ts_ns
        ],
        dtype=object,
    )
    unique_days = tuple(sorted(set(days.tolist())))
    folds: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    first_test = unique_days[0] + timedelta(days=train_days + calibration_days)
    last_test_start = unique_days[-1] - timedelta(days=test_days - 1)
    test_start = first_test
    while test_start <= last_test_start:
        train_start = test_start - timedelta(days=train_days + calibration_days)
        calibration_start = test_start - timedelta(days=calibration_days)
        test_end = test_start + timedelta(days=test_days)
        train = np.flatnonzero((days >= train_start) & (days < calibration_start))
        calibration = np.flatnonzero((days >= calibration_start) & (days < test_start))
        test = np.flatnonzero((days >= test_start) & (days < test_end))
        if (
            _market_count(dataset, train) >= minimum_training_markets
            and _market_count(dataset, calibration) >= minimum_calibration_markets
            and len(test)
            and len(np.unique(dataset.labels[train])) == 2
            and len(np.unique(dataset.labels[calibration])) == 2
        ):
            folds.append((train, calibration, test))
        test_start += timedelta(days=test_days)
    if not folds:
        return _empty_development_result(dataset, failure="no_valid_walk_forward_folds")
    weights = np.asarray(dataset.sample_weights, dtype=float)
    schema = market_relative_runtime_feature_schema()
    candidate_predictions: dict[float, tuple[np.ndarray, np.ndarray]] = {}
    for logistic_c in logistic_cs:
        indices: list[int] = []
        predictions: list[float] = []
        for train, calibration, test in folds:
            model = fit_market_relative_offset_model(
                train_vectors=dataset.vectors[train],
                train_labels=dataset.labels[train],
                train_market_up_probability=dataset.market_up_probabilities[train],
                calibration_vectors=dataset.vectors[calibration],
                calibration_labels=dataset.labels[calibration],
                calibration_market_up_probability=dataset.market_up_probabilities[calibration],
                schema=schema,
                logistic_c=logistic_c,
                train_weights=weights[train],
                calibration_weights=weights[calibration],
                random_seed=random_seed,
                calibration_method="identity",
            )
            indices.extend(test.tolist())
            predictions.extend(
                model.predict_up_probability(
                    dataset.vectors[test],
                    dataset.market_up_probabilities[test],
                ).tolist()
            )
        candidate_predictions[logistic_c] = (
            np.asarray(indices, dtype=int),
            np.asarray(predictions, dtype=float),
        )
    reference_indices = next(iter(candidate_predictions.values()))[0]
    if any(
        not np.array_equal(indices, reference_indices)
        for indices, _ in candidate_predictions.values()
    ):
        raise RuntimeError("Logistic candidates produced mismatched OOS lineage")
    oof_markets = _market_count(dataset, reference_indices)
    baseline_market = dataset.market_up_probabilities[reference_indices]
    baseline_direction = dataset.direction_up_probabilities[reference_indices]
    labels = dataset.labels[reference_indices].astype(float)
    oof_weights = weights[reference_indices]
    baseline_market_scores = _proper_scores(labels, baseline_market, oof_weights)
    baseline_direction_scores = _proper_scores(labels, baseline_direction, oof_weights)
    candidates = []
    for logistic_c, (_indices, probabilities) in candidate_predictions.items():
        scores = _proper_scores(labels, probabilities, oof_weights)
        evidence = _paired_daily_bootstrap(
            dataset=dataset,
            indices=reference_indices,
            baseline=baseline_market,
            candidate=probabilities,
            iterations=bootstrap_iterations,
            random_seed=random_seed,
        )
        candidates.append(
            MarketRelativeLogisticCandidate(
                logistic_c=logistic_c,
                log_loss=scores[0],
                brier=scores[1],
                calibration_slope=calibration_slope(
                    labels=labels,
                    probabilities=probabilities,
                    weights=oof_weights,
                ),
                log_loss_improvement=evidence[0],
                log_loss_ci_lower=evidence[1],
                log_loss_ci_upper=evidence[2],
                brier_improvement=evidence[3],
                brier_ci_lower=evidence[4],
                brier_ci_upper=evidence[5],
            )
        )
    eligible = tuple(
        item
        for item in candidates
        if oof_markets >= minimum_oof_markets
        and item.log_loss < baseline_market_scores[0]
        and item.brier < baseline_market_scores[1]
        and item.log_loss_ci_lower > 0.0
        and item.brier_ci_lower > 0.0
        and 0.8 <= item.calibration_slope <= 1.2
    )
    selected = (
        min(eligible, key=lambda item: (item.log_loss, item.brier, item.logistic_c))
        if eligible
        else None
    )
    return MarketRelativeDevelopmentResult(
        accepted=selected is not None,
        selected_logistic_c=None if selected is None else selected.logistic_c,
        failed_conditions=(
            () if selected is not None else ("no_logistic_candidate_passed_paired_oos_gate",)
        ),
        eligible_market_count=dataset.market_count,
        oof_market_count=oof_markets,
        oof_day_count=len(
            {
                datetime.fromtimestamp(
                    dataset.trigger_ts_ns[index] / 1_000_000_000,
                    tz=UTC,
                ).date()
                for index in reference_indices
            }
        ),
        candidates=tuple(candidates),
        market_baseline_log_loss=baseline_market_scores[0],
        market_baseline_brier=baseline_market_scores[1],
        direction_baseline_log_loss=baseline_direction_scores[0],
        direction_baseline_brier=baseline_direction_scores[1],
    )


def _proper_scores(
    labels: np.ndarray,
    probabilities: np.ndarray,
    weights: np.ndarray,
) -> tuple[float, float]:
    clipped = np.clip(probabilities, 1e-6, 1.0 - 1e-6)
    log_losses = -(labels * np.log(clipped) + (1.0 - labels) * np.log1p(-clipped))
    return (
        float(np.average(log_losses, weights=weights)),
        float(np.average((labels - clipped) ** 2, weights=weights)),
    )


def _paired_daily_bootstrap(
    *,
    dataset: MarketRelativeShadowDataset,
    indices: np.ndarray,
    baseline: np.ndarray,
    candidate: np.ndarray,
    iterations: int,
    random_seed: int,
) -> tuple[float, float, float, float, float, float]:
    labels = dataset.labels[indices].astype(float)
    weights = np.asarray(dataset.sample_weights, dtype=float)[indices]
    baseline = np.clip(baseline, 1e-6, 1.0 - 1e-6)
    candidate = np.clip(candidate, 1e-6, 1.0 - 1e-6)
    log_delta = (
        -(labels * np.log(baseline) + (1.0 - labels) * np.log1p(-baseline))
        + labels * np.log(candidate)
        + (1.0 - labels) * np.log1p(-candidate)
    )
    brier_delta = (labels - baseline) ** 2 - (labels - candidate) ** 2
    by_market: dict[str, list[int]] = {}
    for position, index in enumerate(indices):
        by_market.setdefault(dataset.market_slugs[index], []).append(position)
    market_rows = []
    for slug, positions in by_market.items():
        selected = np.asarray(positions, dtype=int)
        day = datetime.fromtimestamp(
            dataset.trigger_ts_ns[indices[selected[0]]] / 1_000_000_000,
            tz=UTC,
        ).date()
        market_rows.append(
            (
                day,
                float(np.average(log_delta[selected], weights=weights[selected])),
                float(np.average(brier_delta[selected], weights=weights[selected])),
            )
        )
    blocks: dict[date, list[tuple[date, float, float]]] = {}
    for row in market_rows:
        blocks.setdefault(row[0], []).append(row)
    ordered_blocks = tuple(blocks[key] for key in sorted(blocks))
    rng = np.random.default_rng(random_seed)
    samples = np.empty((iterations, 2), dtype=float)
    for iteration in range(iterations):
        selected_blocks = rng.integers(0, len(ordered_blocks), size=len(ordered_blocks))
        sample = [row for block in selected_blocks for row in ordered_blocks[block]]
        samples[iteration] = (
            np.mean([row[1] for row in sample]),
            np.mean([row[2] for row in sample]),
        )
    return (
        float(np.mean([row[1] for row in market_rows])),
        float(np.quantile(samples[:, 0], 0.025)),
        float(np.quantile(samples[:, 0], 0.975)),
        float(np.mean([row[2] for row in market_rows])),
        float(np.quantile(samples[:, 1], 0.025)),
        float(np.quantile(samples[:, 1], 0.975)),
    )


def _market_count(dataset: MarketRelativeShadowDataset, indices: np.ndarray) -> int:
    return len({dataset.market_slugs[index] for index in indices})


def _empty_development_result(
    dataset: MarketRelativeShadowDataset,
    *,
    failure: str,
) -> MarketRelativeDevelopmentResult:
    return MarketRelativeDevelopmentResult(
        accepted=False,
        selected_logistic_c=None,
        failed_conditions=(failure,),
        eligible_market_count=dataset.market_count,
        oof_market_count=0,
        oof_day_count=0,
        candidates=(),
        market_baseline_log_loss=None,
        market_baseline_brier=None,
        direction_baseline_log_loss=None,
        direction_baseline_brier=None,
    )


def _validated_market_rows(
    *,
    table: pa.Table,
    path: Path,
    labels_by_market: Mapping[str, int],
    end_before_epoch_seconds: int,
    maximum_pair_age_seconds: float,
    schema,
) -> (
    tuple[
        str,
        list[tuple[str, int, OpeningStage, int, float, float, tuple[float, ...]]],
    ]
    | None
):
    if table.num_rows != 36 or not _REQUIRED_COLUMNS.issubset(table.column_names):
        raise ValueError(f"invalid Shadow prediction schema: {path}")
    values = table.to_pydict()
    slugs = set(values["market_slug"])
    starts = set(values["market_window_start_ts_ns"])
    if len(slugs) != 1 or len(starts) != 1:
        raise ValueError(f"mixed Shadow market identity: {path}")
    slug = str(next(iter(slugs)))
    try:
        epoch_seconds = int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"invalid Shadow slug: {slug}") from exc
    if epoch_seconds >= end_before_epoch_seconds:
        return None
    if slug in labels_by_market:
        label = labels_by_market[slug]
    else:
        raise KeyError(slug)
    if isinstance(label, bool) or label not in {0, 1}:
        raise ValueError(f"invalid Shadow label for {slug}")
    start_ns = epoch_seconds * 1_000_000_000
    if starts != {start_ns}:
        raise ValueError(f"Shadow start timestamp disagrees with slug: {slug}")
    expected = tuple(start_ns + seconds * 1_000_000_000 for seconds in range(5, 181, 5))
    if tuple(values["trigger_ts_ns"]) != expected:
        raise ValueError(f"Shadow decision cadence is incomplete or unordered: {slug}")
    selected = []
    for index, trigger_ns in enumerate(expected):
        age = float(values["data_age_seconds"][index])
        eligible = (
            isfinite(age)
            and age <= maximum_pair_age_seconds
            and values["has_data_gap"][index] is False
            and values["structure_valid"][index] is True
            and values["tick_unchanged"][index] is True
            and values["fee_unchanged"][index] is True
            and values["latency_healthy"][index] is True
        )
        if not eligible:
            continue
        elapsed = (trigger_ns - start_ns) / 1_000_000_000
        stage = _stage(elapsed)
        market_probability = float(values["p_market_mid_up"][index])
        direction_probability = float(values["p_up"][index])
        feature_values = market_relative_runtime_feature_values(
            direction_p_up=direction_probability,
            boundary_p_up=float(values["p_boundary_up"][index]),
            market_p_up=market_probability,
            elapsed_seconds=elapsed,
            btc_data_age_seconds=age,
        )
        selected.append(
            (
                slug,
                trigger_ns,
                stage,
                int(label),
                market_probability,
                direction_probability,
                schema.vector_from(feature_values),
            )
        )
    return slug, selected


def _stage(elapsed_seconds: float) -> OpeningStage:
    if elapsed_seconds <= 30.0:
        return OpeningStage.EARLY
    if elapsed_seconds <= 90.0:
        return OpeningStage.PRICE_DISCOVERY
    return OpeningStage.MID_EARLY


__all__ = [
    "MarketRelativeShadowDataset",
    "MarketRelativeShadowDatasetBuild",
    "load_market_relative_shadow_dataset",
    "MarketRelativeDevelopmentResult",
    "MarketRelativeLogisticCandidate",
    "run_market_relative_logistic_development",
]
