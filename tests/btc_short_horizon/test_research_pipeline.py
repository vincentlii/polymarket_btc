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
