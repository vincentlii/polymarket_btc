from __future__ import annotations

import numpy as np
import pytest

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.live.paper_runtime import MarketRelativePaperAdapter
from btc_short_horizon.models import OpeningMispricingPrediction
from btc_short_horizon.models.market_relative import (
    ProbabilityInterval,
    fit_market_relative_offset_model,
    market_relative_runtime_feature_schema,
    market_relative_runtime_feature_values,
    relative_probability,
)
from btc_short_horizon.models.artifacts import ModelArtifactMetadata
from btc_short_horizon.models.market_relative_artifacts import MarketRelativeArtifactStore
from btc_short_horizon.models.market_relative_lightgbm import fit_market_relative_lightgbm


def test_zero_residual_reproduces_causal_market_anchor() -> None:
    anchors = np.asarray([0.2, 0.49, 0.8])

    assert relative_probability(anchors, np.zeros(3)) == pytest.approx(anchors)


def test_residual_offset_signs_move_probability_in_the_expected_direction() -> None:
    features = np.asarray([[-2.0], [-1.0], [-0.5], [0.5], [1.0], [2.0]])
    labels = np.asarray([0, 0, 0, 1, 1, 1])
    anchors = np.full(6, 0.5)
    model = fit_market_relative_offset_model(
        train_vectors=features,
        train_labels=labels,
        train_market_up_probability=anchors,
        calibration_vectors=features,
        calibration_labels=labels,
        calibration_market_up_probability=anchors,
        schema=FeatureSchema(version="test-v1", names=("momentum",)),
        logistic_c=1.0,
    )

    probabilities = model.predict_up_probability(
        np.asarray([[-1.0], [1.0]]), np.asarray([0.5, 0.5])
    )

    assert probabilities[0] < 0.5 < probabilities[1]


def test_identity_calibration_preserves_the_market_relative_probability() -> None:
    features = np.asarray([[-2.0], [-1.0], [-0.5], [0.5], [1.0], [2.0]])
    labels = np.asarray([0, 0, 0, 1, 1, 1])
    anchors = np.full(6, 0.5)
    model = fit_market_relative_offset_model(
        train_vectors=features,
        train_labels=labels,
        train_market_up_probability=anchors,
        calibration_vectors=features,
        calibration_labels=labels,
        calibration_market_up_probability=anchors,
        schema=FeatureSchema(version="identity-v1", names=("momentum",)),
        logistic_c=1.0,
        calibration_method="identity",
    )

    assert model.predict_up_probability(features, anchors) == pytest.approx(
        model.predict_raw_up_probability(features, anchors)
    )


def test_probability_interval_has_complementary_conservative_side_bounds() -> None:
    interval = ProbabilityInterval(up_lower=0.47, up_point=0.52, up_upper=0.58)

    assert interval.robust_fair("up") == pytest.approx(0.47)
    assert interval.point_fair("down") == pytest.approx(0.48)
    assert interval.robust_fair("down") == pytest.approx(0.42)


def test_runtime_feature_contract_is_stage_explicit_and_market_anchored() -> None:
    schema = market_relative_runtime_feature_schema()
    values = market_relative_runtime_feature_values(
        direction_p_up=0.60,
        boundary_p_up=0.55,
        market_p_up=0.50,
        elapsed_seconds=45.0,
        btc_data_age_seconds=0.25,
    )

    vector = schema.vector_from(values)
    assert len(vector) == 7
    assert values["direction_market_logit_gap"] > values["boundary_market_logit_gap"] > 0
    assert values["stage_discovery"] == 1.0
    assert values["stage_mid_early"] == 0.0


def test_market_relative_artifact_is_immutable_and_rule_epoch_bound(tmp_path) -> None:
    features = np.asarray([[-2.0], [-1.0], [-0.5], [0.5], [1.0], [2.0]])
    labels = np.asarray([0, 0, 0, 1, 1, 1])
    anchors = np.full(6, 0.5)
    schema = FeatureSchema(version="market-relative-artifact-v1", names=("momentum",))
    model = fit_market_relative_offset_model(
        train_vectors=features,
        train_labels=labels,
        train_market_up_probability=anchors,
        calibration_vectors=features,
        calibration_labels=labels,
        calibration_market_up_probability=anchors,
        schema=schema,
        logistic_c=1.0,
    )
    metadata = ModelArtifactMetadata(
        model_id="market-relative-test-v1",
        feature_schema_hash=schema.hash,
        training_start_ns=1,
        training_end_ns=2,
        calibration_start_ns=3,
        calibration_end_ns=4,
        data_hash="a" * 64,
        code_revision="test-revision",
        config={
            "opening_model_family": "market_relative_offset_v1",
            "rule_epoch": "chainlink-btc-usd-point-v1",
        },
    )

    saved = MarketRelativeArtifactStore.save(
        directory=tmp_path / "model",
        model=model,
        metadata=metadata,
    )
    loaded, loaded_metadata = MarketRelativeArtifactStore.load(
        directory=tmp_path / "model",
        expected_schema_hash=schema.hash,
        expected_rule_epoch="chainlink-btc-usd-point-v1",
    )

    assert saved.model_sha256 == loaded_metadata.model_sha256
    assert loaded.predict_up_probability(features, anchors) == pytest.approx(
        model.predict_up_probability(features, anchors)
    )
    with pytest.raises(ValueError, match="rule epoch mismatch"):
        MarketRelativeArtifactStore.load(
            directory=tmp_path / "model",
            expected_schema_hash=schema.hash,
            expected_rule_epoch="chainlink-btc-usd-twap-60s-v1",
        )


def test_paper_adapter_attaches_a_separate_market_relative_interval(tmp_path) -> None:
    schema = market_relative_runtime_feature_schema()
    features = np.asarray(
        [
            [-0.4, -0.2, -0.3, -0.1, 0.1, 0.0, 0.0],
            [-0.2, -0.1, -0.1, -0.05, 0.2, 1.0, 0.0],
            [-0.1, -0.05, -0.05, -0.02, 0.3, 0.0, 1.0],
            [0.1, 0.05, 0.05, 0.02, 0.1, 0.0, 0.0],
            [0.2, 0.1, 0.1, 0.05, 0.2, 1.0, 0.0],
            [0.4, 0.2, 0.3, 0.1, 0.3, 0.0, 1.0],
        ]
    )
    labels = np.asarray([0, 0, 0, 1, 1, 1])
    anchors = np.full(6, 0.5)
    model = fit_market_relative_offset_model(
        train_vectors=features,
        train_labels=labels,
        train_market_up_probability=anchors,
        calibration_vectors=features,
        calibration_labels=labels,
        calibration_market_up_probability=anchors,
        schema=schema,
        calibration_method="identity",
    )
    directory = tmp_path / "market-relative"
    MarketRelativeArtifactStore.save(
        directory=directory,
        model=model,
        metadata=ModelArtifactMetadata(
            model_id="market-relative-v1",
            feature_schema_hash=schema.hash,
            training_start_ns=1,
            training_end_ns=2,
            calibration_start_ns=3,
            calibration_end_ns=4,
            data_hash="a" * 64,
            code_revision="test-revision",
            config={
                "opening_model_family": "market_relative_offset_v1",
                "rule_epoch": "chainlink-btc-usd-point-v1",
            },
        ),
    )
    prediction = OpeningMispricingPrediction(
        market_slug="btc-updown-15m-1",
        model_version="legacy-direction-v1",
        feature_schema_hash="b" * 64,
        market_window_start_ts_ns=0,
        trigger_ts_ns=45_000_000_000,
        p_up=0.60,
        p_boundary_up=0.55,
        p_market_mid_up=0.50,
        data_age_seconds=0.25,
    )

    attached = MarketRelativePaperAdapter(
        directory=directory,
        expected_rule_epoch="chainlink-btc-usd-point-v1",
    ).attach(prediction)

    assert attached.p_up == pytest.approx(0.60)
    assert attached.market_relative_model_version == "market-relative-v1"
    assert (
        attached.market_relative_p_up_lower
        <= attached.market_relative_p_up
        <= attached.market_relative_p_up_upper
    )


def test_paper_adapter_loads_a_paper_only_lightgbm_with_one_uncertainty_margin(
    tmp_path,
) -> None:
    schema = market_relative_runtime_feature_schema()
    features = np.asarray(
        [
            [-0.4, -0.2, -0.3, -0.1, 0.1, 0.0, 0.0],
            [-0.2, -0.1, -0.1, -0.05, 0.2, 1.0, 0.0],
            [-0.1, -0.05, -0.05, -0.02, 0.3, 0.0, 1.0],
            [0.1, 0.05, 0.05, 0.02, 0.1, 0.0, 0.0],
            [0.2, 0.1, 0.1, 0.05, 0.2, 1.0, 0.0],
            [0.4, 0.2, 0.3, 0.1, 0.3, 0.0, 1.0],
        ]
    )
    labels = np.asarray([0, 0, 0, 1, 1, 1])
    anchors = np.full(6, 0.5)
    model = fit_market_relative_lightgbm(
        train_vectors=features,
        train_labels=labels,
        train_market_up_probability=anchors,
        validation_vectors=features,
        validation_labels=labels,
        validation_market_up_probability=anchors,
        schema=schema,
        probability_uncertainty_radius=0.03,
        num_leaves=3,
        max_depth=2,
        min_child_samples=100,
        learning_rate=0.03,
        n_estimators=5,
        early_stopping_rounds=2,
        random_seed=17,
    )
    directory = tmp_path / "market-relative-lightgbm"
    MarketRelativeArtifactStore.save(
        directory=directory,
        model=model,
        metadata=ModelArtifactMetadata(
            model_id="market-relative-lightgbm-paper-v1",
            feature_schema_hash=schema.hash,
            training_start_ns=1,
            training_end_ns=2,
            calibration_start_ns=3,
            calibration_end_ns=4,
            data_hash="a" * 64,
            code_revision="test-revision",
            config={
                "opening_model_family": "market_relative_lightgbm_v1",
                "rule_epoch": "chainlink-btc-usd-point-v1",
                "paper_experiment_only": True,
                "runtime_promotion_eligible": False,
                "probability_uncertainty_radius": 0.03,
            },
        ),
    )
    prediction = OpeningMispricingPrediction(
        market_slug="btc-updown-15m-1",
        model_version="legacy-direction-v1",
        feature_schema_hash="b" * 64,
        market_window_start_ts_ns=0,
        trigger_ts_ns=45_000_000_000,
        p_up=0.60,
        p_boundary_up=0.55,
        p_market_mid_up=0.50,
        data_age_seconds=0.25,
    )

    attached = MarketRelativePaperAdapter(
        directory=directory,
        expected_rule_epoch="chainlink-btc-usd-point-v1",
    ).attach(prediction)

    assert attached.market_relative_model_version == "market-relative-lightgbm-paper-v1"
    assert attached.market_relative_p_up_lower == pytest.approx(
        attached.market_relative_p_up - 0.03
    )
    assert attached.market_relative_p_up_upper == pytest.approx(
        attached.market_relative_p_up + 0.03
    )
