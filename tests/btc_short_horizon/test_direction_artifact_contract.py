from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models import DirectionModelConfig, fit_direction_model
from btc_short_horizon.research.direction_artifacts import build_direction_artifact_contract
from btc_short_horizon.research.opening_dataset import market_stage_weighting_hash
from btc_short_horizon.research.walk_forward import WalkForwardConfig


def _model():
    schema = FeatureSchema(version="test-v1", names=("x",))
    vectors = np.asarray([[-2.0], [-1.0], [1.0], [2.0]])
    labels = np.asarray([0, 0, 1, 1])
    return fit_direction_model(
        train_vectors=vectors,
        train_labels=labels,
        calibration_vectors=vectors,
        calibration_labels=labels,
        schema=schema,
        config=DirectionModelConfig(calibration_method="auto"),
    )


def test_direction_artifact_contract_records_probability_and_weighting_lineage() -> None:
    contract = build_direction_artifact_contract(
        model=_model(),
        rule_epoch="chainlink-btc-usd-point-v1",
        training_start_ns=1,
        training_end_ns=2,
        calibration_start_ns=3,
        calibration_end_ns=4,
        market_count=10,
        up_market_count=6,
        down_market_count=4,
        weighting_hash=market_stage_weighting_hash(),
        split_config=WalkForwardConfig(train_duration=timedelta(days=90)),
        source_hashes={"dataset": "a" * 64},
    )

    assert contract["rule_epoch"] == "chainlink-btc-usd-point-v1"
    assert contract["market_counts"] == {"all": 10, "up": 6, "down": 4}
    assert contract["weighting"]["hash"] == market_stage_weighting_hash()
    assert contract["calibration"]["selected_method"] in {"identity", "sigmoid", "beta"}


def test_direction_artifact_contract_rejects_rule_or_weighting_mismatch() -> None:
    kwargs = dict(
        model=_model(),
        rule_epoch="chainlink-btc-usd-point-v1",
        training_start_ns=1,
        training_end_ns=2,
        calibration_start_ns=3,
        calibration_end_ns=4,
        market_count=10,
        up_market_count=6,
        down_market_count=4,
        weighting_hash=market_stage_weighting_hash(),
        split_config=WalkForwardConfig(),
        source_hashes={"dataset": "a" * 64},
    )
    with pytest.raises(ValueError, match="rule_epoch"):
        build_direction_artifact_contract(**{**kwargs, "rule_epoch": ""})
    with pytest.raises(ValueError, match="weighting contract"):
        build_direction_artifact_contract(**{**kwargs, "weighting_hash": "b" * 64})
