from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY
from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.market_relative_dataset_io import (
    anchored_dataset_sha256,
    load_anchored_direction_dataset,
    save_anchored_direction_dataset,
)
from btc_short_horizon.research.market_relative_stage_oof import AnchoredDirectionDataset
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample
from scripts.btc_market_relative_dataset_research import _walk_forward_config, parse_args, run


def test_anchored_dataset_round_trip_is_non_pickle_hash_bound_and_read_only(tmp_path) -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    samples = tuple(
        ResearchSample(
            sample_id=f"market-{index}@{index}",
            group_id=f"market-{index}",
            feature_ts=now + timedelta(minutes=index * 15),
            label_available_ts=now + timedelta(minutes=index * 15 + 15),
            label=index % 2,
        )
        for index in range(4)
    )
    anchored = AnchoredDirectionDataset(
        dataset=DirectionDataset(
            samples=samples,
            vectors=np.arange(8, dtype=float).reshape(4, 2),
            schema=FeatureSchema(version="dataset-io-test", names=("a", "b")),
            sample_weights=np.full(4, 0.25),
        ),
        market_up_probabilities=np.asarray([0.4, 0.6, 0.45, 0.55]),
        up_best_asks=np.full(4, 0.51),
        down_best_asks=np.full(4, 0.50),
        up_best_ask_sizes=np.full(4, 10.0),
        down_best_ask_sizes=np.full(4, 11.0),
        fee_rates=np.zeros(4),
        fee_rule_hash="fee-test",
    )
    path = tmp_path / "anchored.npz"

    saved_hash = save_anchored_direction_dataset(
        path=path,
        dataset=anchored,
        lineage={"profile": "core", "rule_epoch": "epoch"},
    )
    restored, lineage = load_anchored_direction_dataset(path)

    assert saved_hash == anchored_dataset_sha256(path)
    assert lineage == {"profile": "core", "rule_epoch": "epoch"}
    assert restored.dataset.samples == anchored.dataset.samples
    assert np.array_equal(restored.dataset.vectors, anchored.dataset.vectors)
    assert np.array_equal(restored.market_up_probabilities, anchored.market_up_probabilities)
    assert restored.market_up_probabilities.flags.writeable is False


def test_dataset_research_quick_profile_runs_and_stays_non_promotable(tmp_path) -> None:
    quick = _walk_forward_config("quick-development")
    assert quick.train_duration == timedelta(days=3)
    assert quick.calibration_duration == timedelta(days=1)
    assert quick.sealed_holdout_duration == timedelta(days=1)
    start = datetime(2026, 6, 1, tzinfo=UTC)
    samples = []
    vectors = []
    for day in range(35):
        for market_number in range(4):
            t0 = start + timedelta(days=day, minutes=market_number * 15)
            market = BTC_15M_MARKET_FAMILY.slug_for(t0)
            for elapsed, stage in ((5, 1.0), (35, 2.0), (95, 3.0)):
                feature_ts = t0 + timedelta(seconds=elapsed)
                samples.append(
                    ResearchSample(
                        sample_id=f"{market}@{int(feature_ts.timestamp() * 1_000_000_000)}",
                        group_id=market,
                        feature_ts=feature_ts,
                        label_available_ts=feature_ts + timedelta(minutes=15),
                        label=(day + market_number) % 2,
                    )
                )
                vectors.append((stage,))
    count = len(samples)
    anchored = AnchoredDirectionDataset(
        dataset=DirectionDataset(
            samples=tuple(samples),
            vectors=np.asarray(vectors),
            schema=FeatureSchema(version="quick-research-test", names=("stage",)),
            sample_weights=np.full(count, 1.0 / 3.0),
        ),
        market_up_probabilities=np.asarray([0.55 if sample.label else 0.45 for sample in samples]),
        up_best_asks=np.full(count, 0.50),
        down_best_asks=np.full(count, 0.50),
        up_best_ask_sizes=np.full(count, 10.0),
        down_best_ask_sizes=np.full(count, 10.0),
        fee_rates=np.zeros(count),
        fee_rule_hash="fee-test",
    )
    dataset = tmp_path / "research.npz"
    output = tmp_path / "receipt.json"
    save_anchored_direction_dataset(
        path=dataset,
        dataset=anchored,
        lineage={"profile": "core", "sealed_forward_start": "2026-08-20T00:00:00+00:00"},
    )

    receipt = run(
        parse_args(
            (
                "--dataset",
                str(dataset),
                "--output",
                str(output),
                "--model-search",
                "logistic",
                "--bootstrap-resamples",
                "100",
                "--minimum-side-opportunities",
                "1",
                "--maximum-side-share",
                "0.99",
            )
        )
    )

    assert receipt["status"] == "development_complete_no_go"
    assert receipt["sealed_holdout_evaluated"] is False
    assert receipt["runtime_promotion_eligible"] is False
    assert receipt["research_contract"]["walk_forward_profile"] == "quick-development"
