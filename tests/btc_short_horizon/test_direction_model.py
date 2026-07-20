from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import warnings

import numpy as np
import pytest

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models import (
    DirectionModelConfig,
    FittedDirectionModel,
    ModelArtifactMetadata,
    ModelArtifactStore,
    fit_direction_model,
)
from btc_short_horizon.models import artifacts as artifact_module


@pytest.fixture
def schema() -> FeatureSchema:
    return FeatureSchema(version="test-v1", names=("return_1s", "volume_1s"))


def _datasets() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    train_x = np.array([[0.0, 0.0], [0.1, 1.0], [0.9, 1.0], [1.0, 2.0]])
    train_y = np.array([0, 0, 1, 1])
    calibration_x = np.array([[0.05, 0.0], [0.2, 1.0], [0.8, 1.0], [0.95, 2.0]])
    calibration_y = np.array([0, 0, 1, 1])
    return train_x, train_y, calibration_x, calibration_y


def test_logistic_model_predicts_calibrated_probabilities(schema: FeatureSchema) -> None:
    model = fit_direction_model(
        train_vectors=_datasets()[0],
        train_labels=_datasets()[1],
        calibration_vectors=_datasets()[2],
        calibration_labels=_datasets()[3],
        schema=schema,
        config=DirectionModelConfig(kind="logistic", calibration_method="sigmoid"),
    )

    probabilities = model.predict_up_probability(np.array([[0.0, 0.0], [1.0, 2.0]]))
    assert probabilities.shape == (2,)
    assert 0.0 < probabilities[0] < probabilities[1] < 1.0


def test_model_rejects_schema_width_mismatch(schema: FeatureSchema) -> None:
    with pytest.raises(ValueError, match="width"):
        fit_direction_model(
            train_vectors=np.array([[0.0], [1.0]]),
            train_labels=np.array([0, 1]),
            calibration_vectors=np.array([[0.0], [1.0]]),
            calibration_labels=np.array([0, 1]),
            schema=schema,
        )


def test_artifact_store_checks_schema_and_hash(tmp_path: Path, schema: FeatureSchema) -> None:
    train_x, train_y, calibration_x, calibration_y = _datasets()
    model = fit_direction_model(
        train_vectors=train_x,
        train_labels=train_y,
        calibration_vectors=calibration_x,
        calibration_labels=calibration_y,
        schema=schema,
    )
    metadata = ModelArtifactMetadata(
        model_id="direction-test",
        feature_schema_hash=schema.hash,
        training_start_ns=1,
        training_end_ns=2,
        calibration_start_ns=3,
        calibration_end_ns=4,
        data_hash="d" * 64,
        code_revision="code-revision",
        config=model.config_dict,
    )
    stored = ModelArtifactStore.save(
        directory=tmp_path / "artifact", model=model, metadata=metadata
    )
    loaded, loaded_metadata = ModelArtifactStore.load(
        directory=tmp_path / "artifact", expected_schema_hash=schema.hash
    )

    assert stored.model_sha256 == loaded_metadata.model_sha256
    assert loaded.predict_up_probability(np.array([[1.0, 2.0]])).shape == (1,)


def test_isotonic_calibration_requires_independent_minimum_sample_size(
    schema: FeatureSchema,
) -> None:
    train_x, train_y, calibration_x, calibration_y = _datasets()

    with pytest.raises(ValueError, match="independent calibration samples"):
        fit_direction_model(
            train_vectors=train_x,
            train_labels=train_y,
            calibration_vectors=calibration_x,
            calibration_labels=calibration_y,
            schema=schema,
            config=DirectionModelConfig(calibration_method="isotonic"),
        )

    with pytest.raises(ValueError, match="independent calibration samples"):
        fit_direction_model(
            train_vectors=train_x,
            train_labels=train_y,
            calibration_vectors=calibration_x,
            calibration_labels=calibration_y,
            schema=schema,
            config=DirectionModelConfig(
                calibration_method="isotonic",
                min_isotonic_calibration_samples=4,
            ),
            calibration_independent_sample_count=2,
        )


def test_early_stopping_inputs_must_be_complete_pairs(schema: FeatureSchema) -> None:
    train_x, train_y, calibration_x, calibration_y = _datasets()

    with pytest.raises(ValueError, match="provided together"):
        fit_direction_model(
            train_vectors=train_x,
            train_labels=train_y,
            calibration_vectors=calibration_x,
            calibration_labels=calibration_y,
            schema=schema,
            early_stopping_vectors=calibration_x,
        )


def test_lightgbm_prediction_uses_the_training_feature_names(schema: FeatureSchema) -> None:
    train_x, train_y, calibration_x, calibration_y = _datasets()
    model = fit_direction_model(
        train_vectors=train_x,
        train_labels=train_y,
        calibration_vectors=calibration_x,
        calibration_labels=calibration_y,
        schema=schema,
        config=DirectionModelConfig(
            kind="lightgbm",
            calibration_method="identity",
            lightgbm_min_child_samples=1,
            lightgbm_n_estimators=5,
        ),
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        model.predict_up_probability(np.array([[1.0, 2.0]]))

    assert not caught


def test_model_rejects_fractional_labels_before_integer_conversion(
    schema: FeatureSchema,
) -> None:
    train_x, _, calibration_x, calibration_y = _datasets()

    with pytest.raises(ValueError, match="binary"):
        fit_direction_model(
            train_vectors=train_x,
            train_labels=np.asarray([0.0, 0.9, 1.0, 1.0]),
            calibration_vectors=calibration_x,
            calibration_labels=calibration_y,
            schema=schema,
        )


def test_prediction_rejects_nonfinite_calibrator_output(schema: FeatureSchema) -> None:
    class Estimator:
        def predict_proba(self, matrix: np.ndarray) -> np.ndarray:
            return np.tile(np.asarray([[0.5, 0.5]]), (len(matrix), 1))

    class Calibrator:
        def transform(self, probabilities: np.ndarray) -> np.ndarray:
            return np.full(len(probabilities), np.nan)

    model = FittedDirectionModel(
        schema=schema,
        config=DirectionModelConfig(calibration_method="identity"),
        estimator=Estimator(),
        calibrator=Calibrator(),
    )

    with pytest.raises(ValueError, match="finite"):
        model.predict_up_probability(np.asarray([[0.0, 0.0]]))


def test_model_config_rejects_boolean_counts_and_impossible_leaf_depth() -> None:
    with pytest.raises(ValueError, match="integer"):
        DirectionModelConfig(lightgbm_n_estimators=True)
    with pytest.raises(ValueError, match=r"2 \*\*"):
        DirectionModelConfig(lightgbm_num_leaves=17, lightgbm_max_depth=4)
    with pytest.raises(ValueError, match="random_seed"):
        DirectionModelConfig(random_seed=-1)
    with pytest.raises(ValueError, match="learning_rate"):
        DirectionModelConfig(lightgbm_learning_rate=True)


def test_model_config_copies_temperature_grid_to_preserve_immutability() -> None:
    source = [0.5, 1.0, 2.0]
    config = DirectionModelConfig(temperature_grid=source)  # type: ignore[arg-type]

    source.append(3.0)

    assert config.temperature_grid == (0.5, 1.0, 2.0)
    with pytest.raises(ValueError, match="finite values"):
        DirectionModelConfig(temperature_grid=(True, 1.0))


def test_artifact_save_is_transactional_and_checks_model_metadata(
    tmp_path: Path,
    schema: FeatureSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    train_x, train_y, calibration_x, calibration_y = _datasets()
    model = fit_direction_model(
        train_vectors=train_x,
        train_labels=train_y,
        calibration_vectors=calibration_x,
        calibration_labels=calibration_y,
        schema=schema,
    )
    metadata = ModelArtifactMetadata(
        model_id="direction-test",
        feature_schema_hash=schema.hash,
        training_start_ns=1,
        training_end_ns=2,
        calibration_start_ns=3,
        calibration_end_ns=4,
        data_hash="d" * 64,
        code_revision="code-revision",
        config=model.config_dict,
    )
    destination = tmp_path / "artifact"

    def fail_dump(_model: object, path: Path) -> None:
        path.write_bytes(b"partial")
        raise OSError("simulated disk failure")

    monkeypatch.setattr(artifact_module.joblib, "dump", fail_dump)
    with pytest.raises(OSError, match="disk failure"):
        ModelArtifactStore.save(directory=destination, model=model, metadata=metadata)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".artifact.staging-*"))

    inconsistent = replace(metadata, feature_schema_hash="f" * 64)
    with pytest.raises(ValueError, match="feature schema"):
        ModelArtifactStore.save(
            directory=destination,
            model=model,
            metadata=inconsistent,
        )
