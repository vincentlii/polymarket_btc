from __future__ import annotations

import pytest

from btc_short_horizon.strategy import OpeningStage, StagePolicyConfig


def test_default_stage_policy_has_non_overlapping_frozen_windows() -> None:
    policy = StagePolicyConfig.default()

    assert [rule.stage for rule in policy.rules] == [
        OpeningStage.EARLY,
        OpeningStage.PRICE_DISCOVERY,
        OpeningStage.MID_EARLY,
    ]
    assert policy.rule_for(5.0).stage is OpeningStage.EARLY
    assert policy.rule_for(60.0).stage is OpeningStage.PRICE_DISCOVERY
    assert policy.rule_for(120.0).stage is OpeningStage.MID_EARLY


def test_stage_policy_rejects_a_gap_outside_tolerance() -> None:
    policy = StagePolicyConfig.default()

    with pytest.raises(ValueError, match="outside stage policy"):
        policy.rule_for(31.0)
