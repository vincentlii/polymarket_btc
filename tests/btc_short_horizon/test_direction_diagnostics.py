from __future__ import annotations

from math import sqrt

import pytest

from btc_short_horizon.research.direction_diagnostics import (
    DirectionMarketEvidence,
    rolling_direction_diagnostics,
)
from btc_short_horizon.strategy import OpeningStage


def _evidence(index: int, *, stage: OpeningStage = OpeningStage.EARLY) -> DirectionMarketEvidence:
    probability = 0.25 if index % 2 == 0 else 0.75
    label = 0 if index % 2 == 0 else 1
    return DirectionMarketEvidence(
        market_slug=f"btc-updown-15m-{index}",
        t0_ns=index,
        stage=stage,
        actual_label=label,
        raw_p_up=probability,
        calibrated_p_up=probability,
        hard_model_direction="down" if probability < 0.5 else "up",
        causal_pm_direction="down" if probability < 0.5 else "up",
        selected_trade_side=None,
        filled=False,
        trade_outcome=None,
    )


def test_direction_evidence_keeps_independent_semantic_fields() -> None:
    item = DirectionMarketEvidence(
        market_slug="btc-updown-15m-1",
        t0_ns=1,
        stage=OpeningStage.EARLY,
        actual_label=1,
        raw_p_up=0.49,
        calibrated_p_up=0.48,
        hard_model_direction="down",
        causal_pm_direction="up",
        selected_trade_side="up",
        filled=True,
        trade_outcome="win",
    )

    assert item.actual_label == 1
    assert item.raw_p_up == 0.49
    assert item.calibrated_p_up == 0.48
    assert item.hard_model_direction == "down"
    assert item.causal_pm_direction == "up"
    assert item.selected_trade_side == "up"


def test_rolling_diagnostics_use_unique_chronological_market_stage_rows() -> None:
    evidence = tuple(_evidence(index) for index in reversed(range(384)))

    summaries = rolling_direction_diagnostics(evidence)

    by_window = {item.window_markets: item for item in summaries}
    assert set(by_window) == {96, 384}
    assert by_window[96].first_t0_ns == 288
    assert by_window[96].last_t0_ns == 383
    assert by_window[96].actual_up_rate == pytest.approx(0.5)
    assert by_window[96].mean_p_up == pytest.approx(0.5)
    assert by_window[96].hard_up_ratio == pytest.approx(0.5)
    assert by_window[96].standard_error == pytest.approx(sqrt(96 * 0.1875) / 96)
    assert by_window[96].calibration_z == pytest.approx(0.0)

    with pytest.raises(ValueError, match="duplicate market/stage"):
        rolling_direction_diagnostics((*evidence, evidence[0]))
