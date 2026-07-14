"""Executable model fitting over the leakage-resistant walk-forward protocol."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import numpy as np

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models import DirectionModelConfig, fit_direction_model
from btc_short_horizon.research.walk_forward import (
    ResearchSample,
    WalkForwardConfig,
    WalkForwardPlan,
    build_walk_forward_plan,
    select_complete_group_indices,
)


@dataclass(frozen=True, slots=True)
class DirectionDataset:
    """Feature matrix aligned one-to-one with causally labelled samples."""

    samples: tuple[ResearchSample, ...]
    vectors: np.ndarray
    schema: FeatureSchema
    sample_weights: np.ndarray | None = None

    def __post_init__(self) -> None:
        matrix = np.asarray(self.vectors, dtype=float)
        if matrix.ndim != 2 or len(matrix) != len(self.samples):
            raise ValueError("vectors must be a 2D matrix aligned with samples")
        if matrix.shape[1] != len(self.schema.names):
            raise ValueError("vectors width must match feature schema")
        if not np.isfinite(matrix).all():
            raise ValueError("vectors must contain only finite values")
        if not self.samples:
            raise ValueError("samples must not be empty")
        object.__setattr__(self, "vectors", matrix)
        weights = (
            np.ones(len(self.samples), dtype=float)
            if self.sample_weights is None
            else np.asarray(self.sample_weights, dtype=float)
        )
        if weights.ndim != 1 or len(weights) != len(self.samples):
            raise ValueError("sample_weights must align one-to-one with samples")
        if not np.isfinite(weights).all() or np.any(weights <= 0.0):
            raise ValueError("sample_weights must contain finite values > 0")
        object.__setattr__(self, "sample_weights", weights)

    @property
    def labels(self) -> np.ndarray:
        return np.asarray([sample.label for sample in self.samples], dtype=int)


@dataclass(frozen=True, slots=True)
class OofPrediction:
    """A prediction made by a model which did not train on that sample."""

    fold_index: int
    sample_index: int
    sample_id: str
    feature_ts_ns: int
    p_up: float
    label: int

    def __post_init__(self) -> None:
        if self.fold_index < 0 or self.sample_index < 0 or self.feature_ts_ns < 0:
            raise ValueError("OOF indexes and timestamp must be non-negative")
        if not self.sample_id:
            raise ValueError("sample_id is required")
        if not isfinite(self.p_up) or not 0.0 < self.p_up < 1.0:
            raise ValueError("p_up must be finite and in (0, 1)")
        if self.label not in {0, 1}:
            raise ValueError("label must be binary")


@dataclass(frozen=True, slots=True)
class WalkForwardModelRun:
    """OOF predictions and split lineage for one frozen model configuration."""

    plan: WalkForwardPlan
    config: DirectionModelConfig
    predictions: tuple[OofPrediction, ...]

    def __post_init__(self) -> None:
        if not self.predictions:
            raise ValueError("walk-forward model run requires OOF predictions")
        sealed_indices = set(self.plan.sealed_holdout_indices)
        if any(prediction.sample_index in sealed_indices for prediction in self.predictions):
            raise ValueError("sealed holdout samples cannot appear in OOF predictions")


@dataclass(frozen=True, slots=True)
class HoldoutPrediction:
    sample_index: int
    sample_id: str
    feature_ts_ns: int
    p_up: float
    label: int


@dataclass(frozen=True, slots=True)
class SealedHoldoutModelRun:
    """One final fit whose model selection was completed before the holdout."""

    train_indices: tuple[int, ...]
    calibration_indices: tuple[int, ...]
    holdout_indices: tuple[int, ...]
    model: object
    predictions: tuple[HoldoutPrediction, ...]


def run_walk_forward_model(
    *,
    dataset: DirectionDataset,
    split_config: WalkForwardConfig | None = None,
    model_config: DirectionModelConfig | None = None,
    early_stopping_fraction: float = 0.2,
) -> WalkForwardModelRun:
    """Fit one frozen model configuration across development folds only."""

    if not isfinite(early_stopping_fraction) or not 0.0 < early_stopping_fraction < 0.5:
        raise ValueError("early_stopping_fraction must be in (0, 0.5)")
    effective_model_config = model_config or DirectionModelConfig()
    plan = build_walk_forward_plan(dataset.samples, config=split_config)
    labels = dataset.labels
    weights = dataset.sample_weights
    assert weights is not None
    predictions: list[OofPrediction] = []
    for fold in plan.folds:
        train_indices, early_stopping_indices = _split_training_indices(
            indices=fold.train_indices,
            labels=labels,
            samples=dataset.samples,
            fraction=early_stopping_fraction,
            require_early_stopping=effective_model_config.kind == "lightgbm",
        )
        model = fit_direction_model(
            train_vectors=dataset.vectors[np.asarray(train_indices, dtype=int)],
            train_labels=labels[np.asarray(train_indices, dtype=int)],
            train_weights=weights[np.asarray(train_indices, dtype=int)],
            calibration_vectors=dataset.vectors[np.asarray(fold.calibration_indices, dtype=int)],
            calibration_labels=labels[np.asarray(fold.calibration_indices, dtype=int)],
            calibration_weights=weights[np.asarray(fold.calibration_indices, dtype=int)],
            early_stopping_vectors=(
                dataset.vectors[np.asarray(early_stopping_indices, dtype=int)]
                if early_stopping_indices
                else None
            ),
            early_stopping_labels=(
                labels[np.asarray(early_stopping_indices, dtype=int)]
                if early_stopping_indices
                else None
            ),
            early_stopping_weights=(
                weights[np.asarray(early_stopping_indices, dtype=int)]
                if early_stopping_indices
                else None
            ),
            schema=dataset.schema,
            config=effective_model_config,
        )
        test_indices = np.asarray(fold.test_indices, dtype=int)
        probabilities = model.predict_up_probability(dataset.vectors[test_indices])
        for sample_index, probability in zip(
            test_indices.tolist(), probabilities.tolist(), strict=True
        ):
            sample = dataset.samples[sample_index]
            predictions.append(
                OofPrediction(
                    fold_index=fold.index,
                    sample_index=sample_index,
                    sample_id=sample.sample_id,
                    feature_ts_ns=int(sample.feature_ts.timestamp() * 1_000_000_000),
                    p_up=float(probability),
                    label=sample.label,
                )
            )
    return WalkForwardModelRun(
        plan=plan,
        config=effective_model_config,
        predictions=tuple(
            sorted(predictions, key=lambda item: (item.sample_index, item.fold_index))
        ),
    )


def run_sealed_holdout_model(
    *,
    dataset: DirectionDataset,
    split_config: WalkForwardConfig | None = None,
    model_config: DirectionModelConfig | None = None,
    early_stopping_fraction: float = 0.2,
) -> SealedHoldoutModelRun:
    """Fit once on the final pre-holdout partitions and score the sealed tail."""

    if not isfinite(early_stopping_fraction) or not 0.0 < early_stopping_fraction < 0.5:
        raise ValueError("early_stopping_fraction must be in (0, 0.5)")
    effective_split = split_config or WalkForwardConfig()
    effective_model = model_config or DirectionModelConfig()
    plan = build_walk_forward_plan(dataset.samples, config=effective_split)
    holdout_start = plan.sealed_holdout_start
    calibration_end = holdout_start - effective_split.embargo_duration
    calibration_start = calibration_end - effective_split.calibration_duration
    train_end = calibration_start - effective_split.embargo_duration
    train_start = train_end - effective_split.train_duration
    sealed_holdout_index_set = set(plan.sealed_holdout_indices)
    development_indices = tuple(
        index for index in range(len(dataset.samples)) if index not in sealed_holdout_index_set
    )
    train_indices = select_complete_group_indices(
        dataset.samples,
        development_indices,
        start=train_start,
        end=train_end,
        label_deadline=train_end,
    )
    calibration_indices = select_complete_group_indices(
        dataset.samples,
        development_indices,
        start=calibration_start,
        end=calibration_end,
        label_deadline=calibration_end,
    )
    if not train_indices or not calibration_indices:
        raise ValueError("sealed holdout fit has an empty train or calibration partition")
    labels = dataset.labels
    weights = dataset.sample_weights
    assert weights is not None
    fit_indices, early_stopping_indices = _split_training_indices(
        indices=train_indices,
        labels=labels,
        samples=dataset.samples,
        fraction=early_stopping_fraction,
        require_early_stopping=effective_model.kind == "lightgbm",
    )
    model = fit_direction_model(
        train_vectors=dataset.vectors[np.asarray(fit_indices, dtype=int)],
        train_labels=labels[np.asarray(fit_indices, dtype=int)],
        train_weights=weights[np.asarray(fit_indices, dtype=int)],
        calibration_vectors=dataset.vectors[np.asarray(calibration_indices, dtype=int)],
        calibration_labels=labels[np.asarray(calibration_indices, dtype=int)],
        calibration_weights=weights[np.asarray(calibration_indices, dtype=int)],
        early_stopping_vectors=(
            dataset.vectors[np.asarray(early_stopping_indices, dtype=int)]
            if early_stopping_indices
            else None
        ),
        early_stopping_labels=(
            labels[np.asarray(early_stopping_indices, dtype=int)]
            if early_stopping_indices
            else None
        ),
        early_stopping_weights=(
            weights[np.asarray(early_stopping_indices, dtype=int)]
            if early_stopping_indices
            else None
        ),
        schema=dataset.schema,
        config=effective_model,
    )
    holdout_indices = plan.sealed_holdout_indices
    probabilities = model.predict_up_probability(
        dataset.vectors[np.asarray(holdout_indices, dtype=int)]
    )
    predictions = tuple(
        HoldoutPrediction(
            sample_index=index,
            sample_id=dataset.samples[index].sample_id,
            feature_ts_ns=int(dataset.samples[index].feature_ts.timestamp() * 1_000_000_000),
            p_up=float(probability),
            label=dataset.samples[index].label,
        )
        for index, probability in zip(holdout_indices, probabilities.tolist(), strict=True)
    )
    return SealedHoldoutModelRun(
        train_indices=train_indices,
        calibration_indices=calibration_indices,
        holdout_indices=holdout_indices,
        model=model,
        predictions=predictions,
    )


def _split_training_indices(
    *,
    indices: tuple[int, ...],
    labels: np.ndarray,
    samples: tuple[ResearchSample, ...],
    fraction: float,
    require_early_stopping: bool,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not require_early_stopping:
        return indices, ()
    validation_count = max(2, round(len(indices) * fraction))
    if validation_count >= len(indices) - 1:
        raise ValueError("training fold is too small to reserve early-stopping data")
    early_stopping_group_ids = {samples[index].group_id for index in indices[-validation_count:]}
    train_indices = tuple(
        index for index in indices if samples[index].group_id not in early_stopping_group_ids
    )
    early_stopping_indices = tuple(
        index for index in indices if samples[index].group_id in early_stopping_group_ids
    )
    if len(set(labels[list(train_indices)].tolist())) != 2:
        raise ValueError("training partition must retain both classes after early-stop split")
    if len(set(labels[list(early_stopping_indices)].tolist())) != 2:
        raise ValueError("early-stopping partition must contain both classes")
    return train_indices, early_stopping_indices
