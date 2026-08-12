from __future__ import annotations

import numpy as np
import pytest

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models.market_relative import (
    ProbabilityInterval,
    fit_market_relative_offset_model,
    relative_probability,
)


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


def test_probability_interval_has_complementary_conservative_side_bounds() -> None:
    interval = ProbabilityInterval(up_lower=0.47, up_point=0.52, up_upper=0.58)

    assert interval.robust_fair("up") == pytest.approx(0.47)
    assert interval.point_fair("down") == pytest.approx(0.48)
    assert interval.robust_fair("down") == pytest.approx(0.42)
