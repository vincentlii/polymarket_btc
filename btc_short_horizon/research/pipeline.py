"""Executable model fitting over the leakage-resistant walk-forward protocol."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from numbers import Integral

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
        samples = tuple(self.samples)
        if not samples or any(not isinstance(sample, ResearchSample) for sample in samples):
            raise ValueError("samples must be a non-empty sequence of ResearchSample values")
        if len({sample.sample_id for sample in samples}) != len(samples):
            raise ValueError("dataset sample IDs must be unique")
        object.__setattr__(self, "samples", samples)
        matrix = np.array(self.vectors, dtype=float, copy=True)
        if matrix.ndim != 2 or len(matrix) != len(samples):
            raise ValueError("vectors must be a 2D matrix aligned with samples")
        if matrix.shape[1] != len(self.schema.names):
            raise ValueError("vectors width must match feature schema")
        if not np.isfinite(matrix).all():
            raise ValueError("vectors must contain only finite values")
        matrix.setflags(write=False)
        object.__setattr__(self, "vectors", matrix)
        weights = (
            np.ones(len(samples), dtype=float)
            if self.sample_weights is None
            else np.array(self.sample_weights, dtype=float, copy=True)
        )
        if weights.ndim != 1 or len(weights) != len(samples):
            raise ValueError("sample_weights must align one-to-one with samples")
        if not np.isfinite(weights).all() or np.any(weights <= 0.0):
            raise ValueError("sample_weights must contain finite values > 0")
        weights.setflags(write=False)
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
        if any(
            isinstance(value, bool) or not isinstance(value, Integral) or value < 0
            for value in (self.fold_index, self.sample_index, self.feature_ts_ns)
        ):
            raise ValueError("OOF indexes and timestamp must be non-negative")
        if not self.sample_id or self.sample_id.strip() != self.sample_id:
            raise ValueError("sample_id must be non-empty and trimmed")
        if not isfinite(self.p_up) or not 0.0 < self.p_up < 1.0:
            raise ValueError("p_up must be finite and in (0, 1)")
        if (
            isinstance(self.label, bool)
            or not isinstance(self.label, Integral)
            or self.label
            not in {
                0,
                1,
            }
        ):
            raise ValueError("label must be binary")


@dataclass(frozen=True, slots=True)
class WalkForwardModelRun:
    """OOF predictions and split lineage for one frozen model configuration."""

    plan: WalkForwardPlan
    config: DirectionModelConfig
    predictions: tuple[OofPrediction, ...]

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "predictions", tuple(self.predictions))
        except TypeError as exc:
            raise ValueError("walk-forward predictions must be iterable") from exc
        if any(not isinstance(value, OofPrediction) for value in self.predictions):
            raise ValueError("walk-forward predictions must contain OofPrediction values")
        if not self.predictions:
            raise ValueError("walk-forward model run requires OOF predictions")
        sealed_indices = set(self.plan.sealed_holdout_indices)
        if any(prediction.sample_index in sealed_indices for prediction in self.predictions):
            raise ValueError("sealed holdout samples cannot appear in OOF predictions")
        expected = {
            (fold.index, sample_index)
            for fold in self.plan.folds
            for sample_index in fold.test_indices
        }
        actual = {
            (prediction.fold_index, prediction.sample_index) for prediction in self.predictions
        }
        if len(actual) != len(self.predictions):
            raise ValueError(
                "walk-forward predictions must not contain duplicate fold/sample pairs"
            )
        if actual != expected:
            raise ValueError("walk-forward predictions must exactly cover every OOF test partition")


@dataclass(frozen=True, slots=True)
class HoldoutPrediction:
    sample_index: int
    sample_id: str
    feature_ts_ns: int
    p_up: float
    label: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, Integral) or value < 0
            for value in (self.sample_index, self.feature_ts_ns)
        ):
            raise ValueError("holdout index and timestamp must be non-negative integers")
        if not self.sample_id or self.sample_id.strip() != self.sample_id:
            raise ValueError("sample_id must be non-empty and trimmed")
        if not isfinite(self.p_up) or not 0.0 < self.p_up < 1.0:
            raise ValueError("p_up must be finite and in (0, 1)")
        if (
            isinstance(self.label, bool)
            or not isinstance(self.label, Integral)
            or self.label
            not in {
                0,
                1,
            }
        ):
            raise ValueError("label must be binary")


@dataclass(frozen=True, slots=True)
class SealedHoldoutModelRun:
    """One final fit whose model selection was completed before the holdout."""

    train_indices: tuple[int, ...]
    fit_indices: tuple[int, ...]
    early_stopping_indices: tuple[int, ...]
    calibration_indices: tuple[int, ...]
    holdout_indices: tuple[int, ...]
    model: object
    predictions: tuple[HoldoutPrediction, ...]

    def __post_init__(self) -> None:
        for name in (
            "train_indices",
            "fit_indices",
            "early_stopping_indices",
            "calibration_indices",
            "holdout_indices",
            "predictions",
        ):
            try:
                object.__setattr__(self, name, tuple(getattr(self, name)))
            except TypeError as exc:
                raise ValueError(f"sealed holdout {name} must be iterable") from exc
        if any(not isinstance(value, HoldoutPrediction) for value in self.predictions):
            raise ValueError("sealed holdout predictions must contain HoldoutPrediction values")
        partitions = (
            self.train_indices,
            self.fit_indices,
            self.early_stopping_indices,
            self.calibration_indices,
            self.holdout_indices,
        )
        if any(
            any(
                isinstance(index, bool) or not isinstance(index, Integral) or index < 0
                for index in partition
            )
            for partition in partitions
        ):
            raise ValueError("sealed holdout lineage indices must be non-negative integers")
        if any(len(partition) != len(set(partition)) for partition in partitions):
            raise ValueError("sealed holdout lineage partitions must contain unique indices")
        if not self.train_indices or not self.fit_indices or not self.calibration_indices:
            raise ValueError("sealed holdout training lineage must not be empty")
        if set(self.fit_indices) & set(self.early_stopping_indices):
            raise ValueError("fit and early-stopping indices must not overlap")
        if set(self.fit_indices) | set(self.early_stopping_indices) != set(self.train_indices):
            raise ValueError("fit and early-stopping indices must exactly partition training")
        if set(self.train_indices) & set(self.calibration_indices):
            raise ValueError("training and calibration indices must not overlap")
        if (set(self.train_indices) | set(self.calibration_indices)) & set(self.holdout_indices):
            raise ValueError("holdout indices must remain sealed from fitting")
        if not self.predictions:
            raise ValueError("sealed holdout predictions must not be empty")
        prediction_indices = [prediction.sample_index for prediction in self.predictions]
        if len(prediction_indices) != len(set(prediction_indices)):
            raise ValueError("sealed holdout predictions must be unique")
        if set(prediction_indices) != set(self.holdout_indices):
            raise ValueError("sealed holdout predictions must exactly cover holdout indices")


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
    if weights is None:  # defensive; DirectionDataset materializes weights in __post_init__
        raise RuntimeError("direction dataset weights were not initialized")
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
            calibration_independent_sample_count=_independent_group_count(
                dataset.samples,
                fold.calibration_indices,
            ),
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
                    feature_ts_ns=_datetime_ns(sample.feature_ts),
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
        sorted(
            (
                index
                for index in range(len(dataset.samples))
                if index not in sealed_holdout_index_set
            ),
            key=lambda index: (
                dataset.samples[index].feature_ts,
                dataset.samples[index].sample_id,
            ),
        )
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
    if weights is None:  # defensive; DirectionDataset materializes weights in __post_init__
        raise RuntimeError("direction dataset weights were not initialized")
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
        calibration_independent_sample_count=_independent_group_count(
            dataset.samples,
            calibration_indices,
        ),
    )
    holdout_indices = plan.sealed_holdout_indices
    probabilities = model.predict_up_probability(
        dataset.vectors[np.asarray(holdout_indices, dtype=int)]
    )
    predictions = tuple(
        HoldoutPrediction(
            sample_index=index,
            sample_id=dataset.samples[index].sample_id,
            feature_ts_ns=_datetime_ns(dataset.samples[index].feature_ts),
            p_up=float(probability),
            label=dataset.samples[index].label,
        )
        for index, probability in zip(holdout_indices, probabilities.tolist(), strict=True)
    )
    return SealedHoldoutModelRun(
        train_indices=train_indices,
        fit_indices=fit_indices,
        early_stopping_indices=early_stopping_indices,
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
    ordered_indices = tuple(
        sorted(
            indices,
            key=lambda index: (samples[index].feature_ts, samples[index].sample_id),
        )
    )
    if not require_early_stopping:
        return ordered_indices, ()
    ordered_group_ids = tuple(dict.fromkeys(samples[index].group_id for index in ordered_indices))
    validation_group_count = max(2, round(len(ordered_group_ids) * fraction))
    if validation_group_count >= len(ordered_group_ids):
        raise ValueError("training fold is too small to reserve early-stopping data")
    early_stopping_group_ids = set(ordered_group_ids[-validation_group_count:])
    train_indices = tuple(
        index
        for index in ordered_indices
        if samples[index].group_id not in early_stopping_group_ids
    )
    early_stopping_indices = tuple(
        index for index in ordered_indices if samples[index].group_id in early_stopping_group_ids
    )
    if len(set(labels[list(train_indices)].tolist())) != 2:
        raise ValueError("training partition must retain both classes after early-stop split")
    if len(set(labels[list(early_stopping_indices)].tolist())) != 2:
        raise ValueError("early-stopping partition must contain both classes")
    return train_indices, early_stopping_indices


def _independent_group_count(samples: tuple[ResearchSample, ...], indices: tuple[int, ...]) -> int:
    return len({samples[index].group_id for index in indices})


def _datetime_ns(value: datetime) -> int:
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000
