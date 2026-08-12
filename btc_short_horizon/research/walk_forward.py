"""Chronological, embargoed splits for BTC probability-model research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from numbers import Integral
from typing import Literal, Mapping, Sequence


def _as_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _require_positive_duration(name: str, value: timedelta) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class ResearchSample:
    """A single causal prediction observation with an isolated final label."""

    sample_id: str
    feature_ts: datetime
    label_available_ts: datetime
    label: int
    group_id: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sample_id, str)
            or not self.sample_id
            or self.sample_id.strip() != self.sample_id
        ):
            raise ValueError("sample_id must be non-empty and trimmed")
        feature_ts = _as_utc(self.feature_ts, "feature_ts")
        label_available_ts = _as_utc(self.label_available_ts, "label_available_ts")
        if label_available_ts <= feature_ts:
            raise ValueError("label_available_ts must be strictly after feature_ts")
        if (
            isinstance(self.label, bool)
            or not isinstance(self.label, Integral)
            or self.label
            not in {
                0,
                1,
            }
        ):
            raise ValueError("label must be binary")
        group_id = self.group_id or self.sample_id
        if not isinstance(group_id, str) or not group_id or group_id.strip() != group_id:
            raise ValueError("group_id must be non-empty and trimmed")
        object.__setattr__(self, "feature_ts", feature_ts)
        object.__setattr__(self, "label_available_ts", label_available_ts)
        object.__setattr__(self, "group_id", group_id)


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    """Default protocol: 90d train, 21d calibration, 14d test/step, 28d holdout."""

    train_duration: timedelta = timedelta(days=90)
    calibration_duration: timedelta = timedelta(days=21)
    test_duration: timedelta = timedelta(days=14)
    step_duration: timedelta = timedelta(days=14)
    embargo_duration: timedelta = timedelta(hours=4, minutes=15)
    sealed_holdout_duration: timedelta = timedelta(days=28)
    training_window: Literal["rolling", "expanding"] = "rolling"

    def __post_init__(self) -> None:
        for name, value in (
            ("train_duration", self.train_duration),
            ("calibration_duration", self.calibration_duration),
            ("test_duration", self.test_duration),
            ("step_duration", self.step_duration),
            ("embargo_duration", self.embargo_duration),
            ("sealed_holdout_duration", self.sealed_holdout_duration),
        ):
            _require_positive_duration(name, value)
        if self.step_duration < self.test_duration:
            raise ValueError(
                "step_duration must be >= test_duration to prevent overlapping OOF tests"
            )
        if self.training_window not in {"rolling", "expanding"}:
            raise ValueError("training_window must be 'rolling' or 'expanding'")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """Indices and decision-time boundaries for one OOS model evaluation fold."""

    index: int
    train_indices: tuple[int, ...]
    calibration_indices: tuple[int, ...]
    test_indices: tuple[int, ...]
    train_start: datetime
    train_end: datetime
    calibration_start: datetime
    calibration_end: datetime
    test_start: datetime
    test_end: datetime

    def __post_init__(self) -> None:
        for name in ("train_indices", "calibration_indices", "test_indices"):
            try:
                object.__setattr__(self, name, tuple(getattr(self, name)))
            except TypeError as exc:
                raise ValueError(f"{name} must be an iterable of indices") from exc
        if isinstance(self.index, bool) or not isinstance(self.index, Integral) or self.index < 0:
            raise ValueError("fold index must be a non-negative integer")
        partitions = (self.train_indices, self.calibration_indices, self.test_indices)
        for partition in partitions:
            if len(partition) != len(set(partition)):
                raise ValueError("fold partitions must not contain duplicate indices")
            if any(
                isinstance(value, bool) or not isinstance(value, Integral) or value < 0
                for value in partition
            ):
                raise ValueError("fold indices must be non-negative integers")
        sets = [set(self.train_indices), set(self.calibration_indices), set(self.test_indices)]
        if any(
            left & right for position, left in enumerate(sets) for right in sets[position + 1 :]
        ):
            raise ValueError("fold partitions must not overlap")
        named_times = (
            ("train_start", self.train_start),
            ("train_end", self.train_end),
            ("calibration_start", self.calibration_start),
            ("calibration_end", self.calibration_end),
            ("test_start", self.test_start),
            ("test_end", self.test_end),
        )
        times = tuple(_as_utc(value, name) for name, value in named_times)
        for (name, _), value in zip(named_times, times, strict=True):
            object.__setattr__(self, name, value)
        if not (times[0] < times[1] <= times[2] < times[3] <= times[4] < times[5]):
            raise ValueError("fold time boundaries must be chronologically ordered")
        if not self.train_indices or not self.calibration_indices or not self.test_indices:
            raise ValueError("every fold partition must contain at least one sample")


@dataclass(frozen=True, slots=True)
class WalkForwardPlan:
    """Development folds plus a final untouched holdout partition."""

    folds: tuple[WalkForwardFold, ...]
    sealed_holdout_indices: tuple[int, ...]
    sealed_holdout_start: datetime

    def __post_init__(self) -> None:
        try:
            folds = tuple(self.folds)
            sealed_holdout_indices = tuple(self.sealed_holdout_indices)
        except TypeError as exc:
            raise ValueError("walk-forward plan partitions must be iterable") from exc
        if any(not isinstance(fold, WalkForwardFold) for fold in folds):
            raise ValueError("walk-forward folds must contain WalkForwardFold values")
        object.__setattr__(self, "folds", folds)
        object.__setattr__(self, "sealed_holdout_indices", sealed_holdout_indices)
        if not self.folds:
            raise ValueError("walk-forward plan requires at least one development fold")
        if not self.sealed_holdout_indices:
            raise ValueError("walk-forward plan requires a sealed holdout")
        if len(self.sealed_holdout_indices) != len(set(self.sealed_holdout_indices)):
            raise ValueError("sealed holdout indices must be unique")
        if any(
            isinstance(value, bool) or not isinstance(value, Integral) or value < 0
            for value in self.sealed_holdout_indices
        ):
            raise ValueError("sealed holdout indices must be non-negative integers")
        object.__setattr__(
            self,
            "sealed_holdout_start",
            _as_utc(self.sealed_holdout_start, "sealed_holdout_start"),
        )
        fold_indices = [fold.index for fold in self.folds]
        if len(fold_indices) != len(set(fold_indices)):
            raise ValueError("walk-forward fold indices must be unique")
        if fold_indices != list(range(len(self.folds))):
            raise ValueError("walk-forward folds must be ordered with contiguous indices")
        if any(
            current.test_end > following.test_start
            for current, following in zip(self.folds, self.folds[1:])
        ):
            raise ValueError("walk-forward test windows must not overlap")
        if self.folds[-1].test_end > self.sealed_holdout_start:
            raise ValueError("development test windows must end before the sealed holdout")
        test_indices = [index for fold in self.folds for index in fold.test_indices]
        if len(test_indices) != len(set(test_indices)):
            raise ValueError("development OOF test partitions must not overlap")
        development_indices = {
            index
            for fold in self.folds
            for index in (*fold.train_indices, *fold.calibration_indices, *fold.test_indices)
        }
        if development_indices & set(self.sealed_holdout_indices):
            raise ValueError("sealed holdout must not overlap development partitions")


def build_walk_forward_plan(
    samples: Sequence[ResearchSample], *, config: WalkForwardConfig | None = None
) -> WalkForwardPlan:
    """Build leakage-resistant development folds from fully labelled research data.

    Each partition uses feature timestamps for membership. Training and calibration
    samples additionally require their labels to have been available before the
    next decision stage begins. The final holdout is never assigned to a fold.
    """

    effective_config = config or WalkForwardConfig()
    if not samples:
        raise ValueError("samples must not be empty")
    if any(not isinstance(sample, ResearchSample) for sample in samples):
        raise ValueError("samples must contain only ResearchSample values")
    if len({sample.sample_id for sample in samples}) != len(samples):
        raise ValueError("sample_id values must be unique")
    ordered_indices = tuple(
        sorted(
            range(len(samples)),
            key=lambda index: (samples[index].feature_ts, samples[index].sample_id),
        )
    )
    first_feature_ts = samples[ordered_indices[0]].feature_ts
    last_feature_ts = samples[ordered_indices[-1]].feature_ts
    nominal_holdout_start = last_feature_ts - effective_config.sealed_holdout_duration
    all_groups = _grouped_indices(samples, ordered_indices)
    sealed_group_ids = {
        group_id
        for group_id, indices in all_groups.items()
        if any(samples[index].feature_ts >= nominal_holdout_start for index in indices)
    }
    sealed_holdout_indices = tuple(
        index for index in ordered_indices if samples[index].group_id in sealed_group_ids
    )
    development_indices = tuple(
        index for index in ordered_indices if samples[index].group_id not in sealed_group_ids
    )
    if not development_indices or not sealed_holdout_indices:
        raise ValueError("insufficient samples for a separate sealed holdout")

    sealed_holdout_start = min(samples[index].feature_ts for index in sealed_holdout_indices)
    development_groups = _grouped_indices(samples, development_indices)
    earliest_test_start = (
        first_feature_ts
        + effective_config.train_duration
        + effective_config.calibration_duration
        + (2 * effective_config.embargo_duration)
    )
    folds: list[WalkForwardFold] = []
    test_start = earliest_test_start
    while test_start + effective_config.test_duration <= sealed_holdout_start:
        calibration_end = test_start - effective_config.embargo_duration
        calibration_start = calibration_end - effective_config.calibration_duration
        train_end = calibration_start - effective_config.embargo_duration
        train_start = (
            first_feature_ts
            if effective_config.training_window == "expanding"
            else train_end - effective_config.train_duration
        )
        train_indices = _partition_indices(
            samples,
            development_indices,
            development_groups,
            start=train_start,
            end=train_end,
            label_deadline=train_end,
        )
        calibration_indices = _partition_indices(
            samples,
            development_indices,
            development_groups,
            start=calibration_start,
            end=calibration_end,
            label_deadline=calibration_end,
        )
        test_indices = _partition_indices(
            samples,
            development_indices,
            development_groups,
            start=test_start,
            end=test_start + effective_config.test_duration,
            label_deadline=None,
        )
        if train_indices and calibration_indices and test_indices:
            folds.append(
                WalkForwardFold(
                    index=len(folds),
                    train_indices=train_indices,
                    calibration_indices=calibration_indices,
                    test_indices=test_indices,
                    train_start=train_start,
                    train_end=train_end,
                    calibration_start=calibration_start,
                    calibration_end=calibration_end,
                    test_start=test_start,
                    test_end=test_start + effective_config.test_duration,
                )
            )
        test_start += effective_config.step_duration
    return WalkForwardPlan(
        folds=tuple(folds),
        sealed_holdout_indices=sealed_holdout_indices,
        sealed_holdout_start=sealed_holdout_start,
    )


def direction_horizon_protocols() -> dict[str, WalkForwardConfig]:
    """Frozen grouped protocols for development-only horizon comparison."""

    shared = {
        "calibration_duration": timedelta(days=21),
        "test_duration": timedelta(days=14),
        "step_duration": timedelta(days=14),
        "embargo_duration": timedelta(hours=4, minutes=15),
        "sealed_holdout_duration": timedelta(days=28),
    }
    return {
        "rolling_90d": WalkForwardConfig(train_duration=timedelta(days=90), **shared),
        "rolling_180d": WalkForwardConfig(train_duration=timedelta(days=180), **shared),
        "expanding_90d_minimum": WalkForwardConfig(
            train_duration=timedelta(days=90),
            training_window="expanding",
            **shared,
        ),
    }


def _partition_indices(
    samples: Sequence[ResearchSample],
    candidates: Sequence[int],
    groups: Mapping[str, tuple[int, ...]],
    *,
    start: datetime,
    end: datetime,
    label_deadline: datetime | None,
) -> tuple[int, ...]:
    accepted_groups = {
        group_id
        for group_id, indices in groups.items()
        if all(start <= samples[index].feature_ts < end for index in indices)
        and (
            label_deadline is None
            or all(samples[index].label_available_ts <= label_deadline for index in indices)
        )
    }
    return tuple(index for index in candidates if samples[index].group_id in accepted_groups)


def select_complete_group_indices(
    samples: Sequence[ResearchSample],
    candidates: Sequence[int],
    *,
    start: datetime,
    end: datetime,
    label_deadline: datetime | None,
) -> tuple[int, ...]:
    """Select only groups wholly contained by a causal time partition."""

    start = _as_utc(start, "start")
    end = _as_utc(end, "end")
    if start >= end:
        raise ValueError("partition start must be before end")
    if label_deadline is not None:
        label_deadline = _as_utc(label_deadline, "label_deadline")

    return _partition_indices(
        samples,
        candidates,
        _grouped_indices(samples, candidates),
        start=start,
        end=end,
        label_deadline=label_deadline,
    )


def _grouped_indices(
    samples: Sequence[ResearchSample], candidates: Sequence[int]
) -> dict[str, tuple[int, ...]]:
    if len(candidates) != len(set(candidates)):
        raise ValueError("candidate indices must be unique")
    if any(
        isinstance(index, bool)
        or not isinstance(index, Integral)
        or index < 0
        or index >= len(samples)
        for index in candidates
    ):
        raise ValueError("candidate indices must be valid non-negative sample indices")
    groups: dict[str, list[int]] = {}
    for index in candidates:
        groups.setdefault(samples[index].group_id, []).append(index)
    result: dict[str, tuple[int, ...]] = {}
    for group_id, indices in groups.items():
        labels = {samples[index].label for index in indices}
        if len(labels) != 1:
            raise ValueError(f"group {group_id!r} contains conflicting labels")
        result[group_id] = tuple(indices)
    return result
