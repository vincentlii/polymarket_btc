from __future__ import annotations

import pytest

from btc_short_horizon.features.market_relative import (
    DualTokenBookSnapshot,
    market_relative_feature_schema_v2,
    market_relative_feature_values_v2,
)


def test_v2_features_add_causal_dual_token_executable_book_information() -> None:
    values = market_relative_feature_values_v2(
        direction_p_up=0.60,
        boundary_p_up=0.55,
        market_p_up=0.50,
        elapsed_seconds=45.0,
        btc_data_age_seconds=0.2,
        up=DualTokenBookSnapshot(bid=0.48, ask=0.50, bid_size=12.0, ask_size=8.0),
        down=DualTokenBookSnapshot(bid=0.49, ask=0.52, bid_size=7.0, ask_size=13.0),
    )

    vector = market_relative_feature_schema_v2().vector_from(values)
    assert len(vector) == 15
    assert values["pm_up_depth_imbalance"] == pytest.approx(0.2)
    assert values["pm_down_depth_imbalance"] == pytest.approx(-0.3)
    assert values["pm_complement_ask_excess"] == pytest.approx(0.02)
    assert values["pm_complement_bid_shortfall"] == pytest.approx(0.03)


def test_v2_features_reject_crossed_or_incomplete_books() -> None:
    with pytest.raises(ValueError, match="bid must not exceed ask"):
        DualTokenBookSnapshot(bid=0.51, ask=0.50, bid_size=1.0, ask_size=1.0)
