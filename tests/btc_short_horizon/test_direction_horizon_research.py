from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.research.direction_horizon_research import (
    collapse_oof_to_market_probabilities,
    paired_market_block_bootstrap,
)
from btc_short_horizon.research.pipeline import DirectionDataset, OofPrediction
from btc_short_horizon.research.walk_forward import ResearchSample
from btc_short_horizon.strategy import OpeningStage


def _dataset() -> DirectionDataset:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = tuple(
        ResearchSample(
            sample_id=f"market-{market}@{market * 10 + snapshot}",
            feature_ts=start + timedelta(days=market, seconds=snapshot),
            label_available_ts=start + timedelta(days=market, minutes=15),
            label=market % 2,
            group_id=f"market-{market}",
        )
        for market in range(4)
        for snapshot in range(2)
    )
    return DirectionDataset(
        samples=samples,
        vectors=np.zeros((8, 1)),
        schema=FeatureSchema(version="test-v1", names=("x",)),
        sample_weights=np.full(8, 0.5),
    )


def test_market_probability_collapse_uses_one_unique_chronological_market_unit() -> None:
    dataset = _dataset()
    predictions = tuple(
        OofPrediction(
            fold_index=0,
            sample_index=index,
            sample_id=sample.sample_id,
            feature_ts_ns=int(sample.feature_ts.timestamp() * 1_000_000_000),
            raw_p_up=0.4 + 0.1 * (index % 2),
            p_up=0.3 + 0.2 * (index % 2),
            label=sample.label,
        )
        for index, sample in enumerate(dataset.samples)
    )

    markets = collapse_oof_to_market_probabilities(
        dataset=dataset,
        predictions=predictions,
        stage=OpeningStage.EARLY,
    )

    assert [item.market_slug for item in markets] == [f"market-{index}" for index in range(4)]
    assert all(item.raw_p_up == 0.45 for item in markets)
    assert all(item.calibrated_p_up == 0.4 for item in markets)


def test_paired_market_bootstrap_uses_only_identical_market_units() -> None:
    dataset = _dataset()
    baseline_predictions = tuple(
        OofPrediction(
            fold_index=0,
            sample_index=index,
            sample_id=sample.sample_id,
            feature_ts_ns=int(sample.feature_ts.timestamp() * 1_000_000_000),
            raw_p_up=0.5,
            p_up=0.5,
            label=sample.label,
        )
        for index, sample in enumerate(dataset.samples)
    )
    candidate_predictions = tuple(
        OofPrediction(
            fold_index=0,
            sample_index=index,
            sample_id=sample.sample_id,
            feature_ts_ns=int(sample.feature_ts.timestamp() * 1_000_000_000),
            raw_p_up=0.2 if sample.label == 0 else 0.8,
            p_up=0.2 if sample.label == 0 else 0.8,
            label=sample.label,
        )
        for index, sample in enumerate(dataset.samples)
    )
    baseline = collapse_oof_to_market_probabilities(
        dataset=dataset,
        predictions=baseline_predictions,
        stage=OpeningStage.EARLY,
    )
    candidate = collapse_oof_to_market_probabilities(
        dataset=dataset,
        predictions=candidate_predictions,
        stage=OpeningStage.EARLY,
    )

    evidence = paired_market_block_bootstrap(
        baseline=baseline,
        candidate=candidate,
        iterations=200,
        random_seed=17,
    )

    assert evidence.market_count == 4
    assert evidence.log_loss_improvement > 0.0
    assert evidence.brier_improvement > 0.0
    assert evidence.log_loss_ci_lower > 0.0
