from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btc_short_horizon.research import (
    ResearchSample,
    WalkForwardConfig,
    build_walk_forward_plan,
)


def _samples(*, count: int, spacing: timedelta = timedelta(hours=1)) -> tuple[ResearchSample, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(
        ResearchSample(
            sample_id=f"sample-{index}",
            feature_ts=start + (index * spacing),
            label_available_ts=start + (index * spacing) + timedelta(minutes=15),
            label=index % 2,
        )
        for index in range(count)
    )


def _config() -> WalkForwardConfig:
    return WalkForwardConfig(
        train_duration=timedelta(hours=12),
        calibration_duration=timedelta(hours=4),
        test_duration=timedelta(hours=4),
        step_duration=timedelta(hours=4),
        embargo_duration=timedelta(hours=1),
        sealed_holdout_duration=timedelta(hours=8),
    )


def test_walk_forward_partitions_are_chronological_disjoint_and_holdout_is_sealed() -> None:
    samples = _samples(count=48)
    plan = build_walk_forward_plan(samples, config=_config())

    assert len(plan.folds) > 1
    development = set()
    for fold in plan.folds:
        train = set(fold.train_indices)
        calibration = set(fold.calibration_indices)
        test = set(fold.test_indices)
        assert not train & calibration
        assert not train & test
        assert not calibration & test
        assert fold.train_end <= fold.calibration_start
        assert fold.calibration_end <= fold.test_start
        for index in (*fold.train_indices, *fold.calibration_indices):
            assert samples[index].label_available_ts <= (
                fold.train_end if index in train else fold.calibration_end
            )
        development.update(train | calibration | test)

    assert not development & set(plan.sealed_holdout_indices)
    assert all(
        samples[index].feature_ts >= plan.sealed_holdout_start
        for index in plan.sealed_holdout_indices
    )


def test_late_labels_are_excluded_from_development_training_and_calibration() -> None:
    samples = list(_samples(count=48))
    late = samples[12]
    samples[12] = ResearchSample(
        sample_id=late.sample_id,
        feature_ts=late.feature_ts,
        label_available_ts=late.feature_ts + timedelta(days=3),
        label=late.label,
    )

    plan = build_walk_forward_plan(samples, config=_config())

    assert all(12 not in fold.train_indices for fold in plan.folds)
    assert all(12 not in fold.calibration_indices for fold in plan.folds)


def test_protocol_rejects_future_unsafe_samples_and_insufficient_history() -> None:
    time = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="strictly after"):
        ResearchSample(
            sample_id="unsafe",
            feature_ts=time,
            label_available_ts=time,
            label=1,
        )
    with pytest.raises(ValueError, match="walk-forward plan requires"):
        build_walk_forward_plan(_samples(count=12), config=_config())


def test_walk_forward_keeps_an_entire_market_group_on_one_side_of_every_boundary() -> None:
    samples = list(_samples(count=48))
    for index in (38, 39):
        sample = samples[index]
        samples[index] = ResearchSample(
            sample_id=sample.sample_id,
            feature_ts=sample.feature_ts,
            label_available_ts=sample.label_available_ts,
            label=1,
            group_id="market-crossing-holdout-boundary",
        )

    plan = build_walk_forward_plan(samples, config=_config())

    holdout = set(plan.sealed_holdout_indices)
    assert {38, 39}.issubset(holdout)
    for fold in plan.folds:
        partitions = (
            set(fold.train_indices),
            set(fold.calibration_indices),
            set(fold.test_indices),
            holdout,
        )
        memberships = [
            position
            for position, indices in enumerate(partitions)
            if 38 in indices or 39 in indices
        ]
        assert len(memberships) == 1


def test_walk_forward_rejects_overlapping_oof_windows_and_boolean_labels() -> None:
    with pytest.raises(ValueError, match="overlapping OOF"):
        WalkForwardConfig(
            test_duration=timedelta(hours=2),
            step_duration=timedelta(hours=1),
        )
    time = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="binary"):
        ResearchSample(
            sample_id="sample",
            feature_ts=time,
            label_available_ts=time + timedelta(minutes=15),
            label=True,
        )
    with pytest.raises(ValueError, match="trimmed"):
        ResearchSample(
            sample_id=" sample ",
            feature_ts=time,
            label_available_ts=time + timedelta(minutes=15),
            label=1,
        )
