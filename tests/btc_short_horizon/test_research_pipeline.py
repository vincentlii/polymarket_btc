from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models import DirectionModelConfig
from btc_short_horizon.research import (
    DirectionDataset,
    ResearchSample,
    WalkForwardConfig,
    run_walk_forward_model,
    run_sealed_holdout_model,
)
from btc_short_horizon.research.pipeline import _split_training_indices


def _dataset() -> DirectionDataset:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = tuple(
        ResearchSample(
            sample_id=f"sample-{index}",
            feature_ts=start + timedelta(hours=index),
            label_available_ts=start + timedelta(hours=index, minutes=15),
            label=index % 2,
        )
        for index in range(48)
    )
    vectors = np.asarray([[float(index % 2), float(index)] for index in range(48)])
    return DirectionDataset(
        samples=samples,
        vectors=vectors,
        schema=FeatureSchema(version="test", names=("signal", "sequence")),
    )


def _split_config() -> WalkForwardConfig:
    return WalkForwardConfig(
        train_duration=timedelta(hours=12),
        calibration_duration=timedelta(hours=4),
        test_duration=timedelta(hours=4),
        step_duration=timedelta(hours=4),
        embargo_duration=timedelta(hours=1),
        sealed_holdout_duration=timedelta(hours=8),
    )


def test_walk_forward_pipeline_produces_only_oof_development_predictions() -> None:
    result = run_walk_forward_model(
        dataset=_dataset(),
        split_config=_split_config(),
        model_config=DirectionModelConfig(calibration_method="identity"),
    )

    expected = sum(len(fold.test_indices) for fold in result.plan.folds)
    assert len(result.predictions) == expected
    assert all(0.0 < prediction.p_up < 1.0 for prediction in result.predictions)
    assert not {prediction.sample_index for prediction in result.predictions} & set(
        result.plan.sealed_holdout_indices
    )


def test_walk_forward_pipeline_rejects_invalid_internal_early_stop_fraction() -> None:
    with pytest.raises(ValueError, match="early_stopping_fraction"):
        run_walk_forward_model(
            dataset=_dataset(),
            split_config=_split_config(),
            early_stopping_fraction=0.5,
        )


def test_sealed_holdout_model_does_not_use_holdout_for_fitting() -> None:
    result = run_sealed_holdout_model(
        dataset=_dataset(),
        split_config=_split_config(),
        model_config=DirectionModelConfig(calibration_method="identity"),
    )

    assert result.predictions
    assert set(result.holdout_indices).isdisjoint(result.train_indices)
    assert set(result.holdout_indices).isdisjoint(result.calibration_indices)
    assert set(result.fit_indices) | set(result.early_stopping_indices) == set(result.train_indices)


def test_direction_dataset_copies_and_freezes_numeric_inputs() -> None:
    source_vectors = np.asarray([[0.0, 1.0], [1.0, 2.0]])
    source_weights = np.asarray([0.5, 0.5])
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = tuple(
        ResearchSample(
            sample_id=f"sample-{index}",
            feature_ts=start + timedelta(hours=index),
            label_available_ts=start + timedelta(hours=index, minutes=15),
            label=index,
        )
        for index in range(2)
    )
    dataset = DirectionDataset(
        samples=samples,
        vectors=source_vectors,
        schema=FeatureSchema(version="immutable", names=("a", "b")),
        sample_weights=source_weights,
    )

    source_vectors[0, 0] = 99.0
    source_weights[0] = 99.0
    assert dataset.vectors[0, 0] == 0.0
    assert dataset.sample_weights[0] == 0.5
    with pytest.raises(ValueError, match="read-only"):
        dataset.vectors[0, 0] = 2.0


def test_lightgbm_early_stopping_reserves_complete_chronological_groups() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = tuple(
        ResearchSample(
            sample_id=f"market-{market}@{snapshot}",
            group_id=f"market-{market}",
            feature_ts=start + timedelta(hours=market, seconds=snapshot),
            label_available_ts=start + timedelta(hours=market, minutes=15),
            label=market % 2,
        )
        for market in range(10)
        for snapshot in range(3)
    )
    labels = np.asarray([sample.label for sample in samples])

    fit, early = _split_training_indices(
        indices=tuple(reversed(range(len(samples)))),
        labels=labels,
        samples=samples,
        fraction=0.2,
        require_early_stopping=True,
    )

    assert {samples[index].group_id for index in early} == {"market-8", "market-9"}
    assert len(early) == 6
    assert max(samples[index].feature_ts for index in fit) < min(
        samples[index].feature_ts for index in early
    )
