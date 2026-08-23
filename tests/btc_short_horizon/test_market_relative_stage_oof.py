from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY
from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.models.market_relative import relative_probability
from btc_short_horizon.research.market_relative_stage_oof import (
    AnchoredDirectionDataset,
    _earliest_market_opportunity_mask,
    _execution_scores,
    _market_first_mean,
    _paired_scores,
    run_market_relative_stage_oof,
)
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample, WalkForwardConfig


def test_zero_residual_is_exactly_the_market_anchor() -> None:
    anchors = np.asarray([0.2, 0.49, 0.8])
    assert relative_probability(anchors, np.zeros(3)) == pytest.approx(anchors)


def test_paired_metrics_compare_candidate_to_q_pm_not_half() -> None:
    labels = np.asarray([1.0, 0.0])
    market = np.asarray([0.8, 0.2])
    identical = _paired_scores(labels=labels, model=market, market=market)

    assert np.allclose(identical["brier"][0], identical["brier"][1])
    assert np.allclose(identical["log_loss"][0], identical["log_loss"][1])


def test_1x5_contract_keeps_only_earliest_opportunity_per_market() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    mask = _earliest_market_opportunity_mask(
        candidate_mask=np.asarray([True, True, False, True, True]),
        market_ids=("a", "a", "b", "b", "a"),
        market_times=(
            start + timedelta(seconds=10),
            start + timedelta(seconds=5),
            start,
            start + timedelta(seconds=15),
            start + timedelta(seconds=20),
        ),
    )

    assert mask.tolist() == [False, True, False, True, False]


def test_point_metrics_give_each_market_one_vote() -> None:
    assert _market_first_mean(
        np.asarray([1.0, 1.0, 1.0, 0.0]),
        ("many", "many", "many", "one"),
    ) == pytest.approx(0.5)


def test_execution_uses_robust_probability_not_point_probability() -> None:
    mask, _realized, sides, _tail = _execution_scores(
        labels=np.asarray([1.0]),
        up_fair=np.asarray([0.52]),
        down_fair=np.asarray([0.38]),
        up_asks=np.asarray([0.50]),
        down_asks=np.asarray([0.50]),
        up_sizes=np.asarray([10.0]),
        down_sizes=np.asarray([10.0]),
        fee_rates=np.asarray([0.0]),
        minimum_edge=0.04,
        execution_cost=0.0,
        minimum_order_size=5.0,
        tail_quarantine_price=0.35,
    )

    assert mask.tolist() == [False]
    assert sides.tolist() == [0]


def test_three_stages_fit_independently_and_emit_all_q_pm_objectives() -> None:
    anchored = _anchored_dataset()
    fitted_stage_values: list[float] = []

    class IdentityModel:
        def predict_up_probability(self, vectors, anchors):  # type: ignore[no-untyped-def]
            return np.asarray(anchors, dtype=float)

    def fit_model(**kwargs):  # type: ignore[no-untyped-def]
        fitted_stage_values.append(float(np.unique(kwargs["train_vectors"][:, 0])[0]))
        return IdentityModel()

    receipt = run_market_relative_stage_oof(
        anchored,
        split_config=WalkForwardConfig(
            train_duration=timedelta(days=8),
            calibration_duration=timedelta(days=3),
            test_duration=timedelta(days=3),
            step_duration=timedelta(days=3),
            embargo_duration=timedelta(minutes=15),
            sealed_holdout_duration=timedelta(days=3),
        ),
        resamples=100,
        fit_model=fit_model,
    )

    assert set(receipt) == {
        "early_3s_to_30s",
        "price_discovery_35s_to_90s",
        "mid_early_95s_to_180s",
    }
    assert set(fitted_stage_values) == {1.0, 2.0, 3.0}
    expected_edges = {
        "early_3s_to_30s": 0.04,
        "price_discovery_35s_to_90s": 0.035,
        "mid_early_95s_to_180s": 0.03,
    }
    for name, stage in receipt.items():
        assert stage["execution"]["minimum_net_edge"] == expected_edges[name]
        assert set(stage["metrics"]) == {"brier", "log_loss", "net_ev"}
        assert set(stage["p_lower"]) == {"brier", "log_loss", "net_ev"}
        assert stage["fold_metrics"]
        assert all("execution_score" in fold for fold in stage["fold_metrics"])
        assert all(
            0.0 < unit["p_value"] <= 1.0
            for objective in stage["p_lower"].values()
            for unit in objective.values()
        )
        assert stage["metrics"]["brier"]["candidate"] >= 0.0
        assert stage["metrics"]["log_loss"]["candidate"] >= 0.0
        point = stage["point_probability_diagnostic"]
        assert point["status"] == "diagnostic_not_execution_evidence"
        assert set(point["p_lower"]) == {"market", "day", "week"}
        assert (
            point["direction_balance"]["opportunity_count"]
            == (stage["execution"]["point_probability_opportunity_count"])
        )


def test_calibration_isotonic_eligibility_counts_unique_markets_not_snapshots() -> None:
    anchored = _anchored_dataset()
    observed: list[int] = []

    class IdentityModel:
        calibration_selection = None

        def predict_up_probability(self, vectors, anchors):  # type: ignore[no-untyped-def]
            return np.asarray(anchors, dtype=float)

    def fit_model(**kwargs):  # type: ignore[no-untyped-def]
        observed.append(kwargs["calibration_independent_market_count"])
        return IdentityModel()

    run_market_relative_stage_oof(
        anchored,
        split_config=WalkForwardConfig(
            train_duration=timedelta(days=8),
            calibration_duration=timedelta(days=3),
            test_duration=timedelta(days=3),
            step_duration=timedelta(days=3),
            embargo_duration=timedelta(minutes=15),
            sealed_holdout_duration=timedelta(days=3),
        ),
        resamples=100,
        fit_model=fit_model,
    )

    assert observed
    assert max(observed) <= 3


def test_no_executable_opportunities_fail_closed_without_aborting_research() -> None:
    class IdentityModel:
        calibration_selection = None

        def predict_up_probability(self, vectors, anchors):  # type: ignore[no-untyped-def]
            return np.asarray(anchors, dtype=float)

    receipt = run_market_relative_stage_oof(
        _anchored_dataset(),
        split_config=WalkForwardConfig(
            train_duration=timedelta(days=8),
            calibration_duration=timedelta(days=3),
            test_duration=timedelta(days=3),
            step_duration=timedelta(days=3),
            embargo_duration=timedelta(minutes=15),
            sealed_holdout_duration=timedelta(days=3),
        ),
        minimum_trade_edge=0.99,
        resamples=100,
        fit_model=lambda **_kwargs: IdentityModel(),
    )

    for stage in receipt.values():
        assert stage["execution"]["opportunity_count"] == 0
        assert stage["metrics"]["net_ev"]["improvement"] == 0.0
        assert stage["p_lower"]["net_ev"]["week"]["lower_bound"] == 0.0
        assert stage["p_lower"]["net_ev"]["week"]["p_value"] == 1.0


def _anchored_dataset() -> AnchoredDirectionDataset:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = []
    vectors = []
    anchors = []
    for day in range(35):
        t0 = start + timedelta(days=day)
        slug = BTC_15M_MARKET_FAMILY.slug_for(t0)
        label = day % 2
        for elapsed, stage_value in ((5, 1.0), (35, 2.0), (95, 3.0)):
            feature_ts = t0 + timedelta(seconds=elapsed)
            samples.append(
                ResearchSample(
                    sample_id=f"{slug}@{int(feature_ts.timestamp() * 1e9)}",
                    feature_ts=feature_ts,
                    label_available_ts=t0 + timedelta(minutes=15),
                    label=label,
                    group_id=slug,
                )
            )
            vectors.append((stage_value,))
            anchors.append(0.6 if label else 0.4)
    return AnchoredDirectionDataset(
        dataset=DirectionDataset(
            samples=tuple(samples),
            vectors=np.asarray(vectors),
            schema=FeatureSchema(version="stage-relative-test", names=("stage",)),
        ),
        market_up_probabilities=np.asarray(anchors),
        up_best_asks=np.full(len(anchors), 0.55),
        down_best_asks=np.full(len(anchors), 0.55),
        up_best_ask_sizes=np.full(len(anchors), 10.0),
        down_best_ask_sizes=np.full(len(anchors), 10.0),
        fee_rates=np.zeros(len(anchors)),
        fee_rule_hash="fee-test",
    )
