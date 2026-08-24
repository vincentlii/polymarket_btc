"""Paired uncertainty estimates whose smallest independent unit is one market."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Sequence

import numpy as np


class BlockUnit(StrEnum):
    MARKET = "market"
    DAY = "day"
    WEEK = "week"


@dataclass(frozen=True, slots=True)
class PairedBlockLowerBound:
    unit: BlockUnit
    confidence: float
    point_estimate: float
    lower_bound: float
    p_value: float
    observation_count: int
    independent_market_count: int
    block_count: int
    resamples: int
    seed: int
    aggregation: str = "snapshot_mean_within_market_then_market_weighted_block_resample"


def paired_block_lower_bound(
    *,
    candidate: np.ndarray,
    baseline: np.ndarray,
    market_ids: Sequence[str],
    market_times: Sequence[datetime],
    unit: BlockUnit,
    confidence: float,
    resamples: int,
    seed: int,
) -> PairedBlockLowerBound:
    """Return a one-sided paired lower bound without treating snapshots as IID."""

    candidate_values = np.asarray(candidate, dtype=float)
    baseline_values = np.asarray(baseline, dtype=float)
    identifiers = tuple(market_ids)
    timestamps = tuple(market_times)
    if (
        candidate_values.ndim != 1
        or baseline_values.shape != candidate_values.shape
        or len(identifiers) != len(candidate_values)
        or len(timestamps) != len(candidate_values)
    ):
        raise ValueError("candidate, baseline, market_ids, and market_times must align")
    if not np.isfinite(candidate_values).all() or not np.isfinite(baseline_values).all():
        raise ValueError("paired values must be finite")
    if any(not value or value.strip() != value for value in identifiers):
        raise ValueError("market_ids must be non-empty and trimmed")
    if not isfinite(confidence) or not 0.5 < confidence < 1.0:
        raise ValueError("confidence must be in (0.5, 1)")
    if isinstance(resamples, bool) or resamples < 100:
        raise ValueError("resamples must be an integer >= 100")
    normalized_times = tuple(_utc(value) for value in timestamps)

    differences = candidate_values - baseline_values
    markets: dict[str, list[float]] = {}
    market_time: dict[str, datetime] = {}
    for identifier, timestamp, difference in zip(
        identifiers, normalized_times, differences, strict=True
    ):
        previous = market_time.setdefault(identifier, timestamp)
        if previous.date() != timestamp.date():
            raise ValueError("one market cannot span multiple UTC dates")
        markets.setdefault(identifier, []).append(float(difference))
    if len(markets) < 2:
        raise ValueError("at least two independent markets are required")

    market_values = {key: float(np.mean(values)) for key, values in markets.items()}
    blocks: dict[str, list[float]] = {}
    selected_unit = BlockUnit(unit)
    for identifier, value in market_values.items():
        timestamp = market_time[identifier]
        if selected_unit is BlockUnit.MARKET:
            block = identifier
        elif selected_unit is BlockUnit.DAY:
            block = timestamp.date().isoformat()
        else:
            year, week, _weekday = timestamp.isocalendar()
            block = f"{year:04d}-W{week:02d}"
        blocks.setdefault(block, []).append(value)
    if len(blocks) < 2:
        raise ValueError(f"{selected_unit.value} bootstrap requires at least two blocks")

    ordered = tuple(tuple(blocks[key]) for key in sorted(blocks))
    rng = np.random.default_rng(seed)
    estimates = np.empty(resamples, dtype=float)
    null_estimates = np.empty(resamples, dtype=float)
    for index in range(resamples):
        selected = rng.integers(0, len(ordered), size=len(ordered))
        sampled = [value for block_index in selected for value in ordered[int(block_index)]]
        estimates[index] = float(np.mean(sampled))
        signs = rng.choice((-1.0, 1.0), size=len(ordered))
        null_sample = [
            sign * value for sign, block in zip(signs, ordered, strict=True) for value in block
        ]
        null_estimates[index] = float(np.mean(null_sample))
    point_estimate = float(np.mean(tuple(market_values.values())))
    return PairedBlockLowerBound(
        unit=selected_unit,
        confidence=confidence,
        point_estimate=point_estimate,
        lower_bound=float(np.quantile(estimates, 1.0 - confidence)),
        p_value=float((np.count_nonzero(null_estimates >= point_estimate) + 1) / (resamples + 1)),
        observation_count=len(candidate_values),
        independent_market_count=len(markets),
        block_count=len(blocks),
        resamples=resamples,
        seed=seed,
    )


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("market_times must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["BlockUnit", "PairedBlockLowerBound", "paired_block_lower_bound"]
