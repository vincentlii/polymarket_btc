"""Label-isolated training datasets from causal BTC opening-feature snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from numbers import Integral

import numpy as np

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.features import OpeningFeatureObservation, opening_feature_schema
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample


_NANOS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class OpeningDirectionDatasetBuild:
    """One group-safe dataset plus explicit whole-market exclusion counts."""

    dataset: DirectionDataset
    requested_markets: int
    included_markets: int
    excluded_unresolved_markets: int
    excluded_void_markets: int
    excluded_incomplete_markets: int
    excluded_ineligible_markets: int
    snapshots_per_market: int


def build_opening_direction_dataset(
    *,
    markets: Sequence[MarketWindow],
    observations_by_market: Mapping[str, Sequence[OpeningFeatureObservation]],
    snapshot_seconds: int = 5,
    entry_start_seconds: int = 3,
    entry_end_seconds: int = 180,
) -> OpeningDirectionDatasetBuild:
    """Build one weighted dataset without partially retaining a bad market window."""

    ordered_markets = tuple(sorted(markets, key=lambda market: (market.t0, market.slug)))
    if not ordered_markets:
        raise ValueError("markets must not be empty")
    market_slugs = {market.slug for market in ordered_markets}
    if len(market_slugs) != len(ordered_markets):
        raise ValueError("market slugs must be unique")
    unknown_slugs = set(observations_by_market) - market_slugs
    if unknown_slugs:
        raise ValueError(f"observations contain unknown markets: {sorted(unknown_slugs)!r}")
    if any(market.family != BTC_15M_MARKET_FAMILY for market in ordered_markets):
        raise ValueError("opening direction datasets require the BTC 15m market family")
    if len({market.rule_epoch for market in ordered_markets}) != 1:
        raise ValueError("one dataset cannot mix market rule epochs")

    offsets = _snapshot_offsets(
        snapshot_seconds=snapshot_seconds,
        entry_start_seconds=entry_start_seconds,
        entry_end_seconds=entry_end_seconds,
    )
    schema = opening_feature_schema()
    samples: list[ResearchSample] = []
    vectors: list[tuple[float, ...]] = []
    weights: list[float] = []
    included_markets = 0
    excluded_unresolved = 0
    excluded_void = 0
    excluded_incomplete = 0
    excluded_ineligible = 0

    for market in ordered_markets:
        if market.resolution is None:
            excluded_unresolved += 1
            continue
        if market.resolution is MarketOutcome.VOID:
            excluded_void += 1
            continue
        if market.label_available_ts is None:
            raise ValueError(f"resolved market {market.slug!r} is missing label availability")
        market_start_ns = _datetime_to_ns(market.t0)
        observations = tuple(observations_by_market.get(market.slug, ()))
        indexed = _index_market_observations(
            market=market,
            observations=observations,
            expected_schema_hash=schema.hash,
        )
        expected_timestamps = tuple(
            market_start_ns + offset * _NANOS_PER_SECOND for offset in offsets
        )
        if any(timestamp not in indexed for timestamp in expected_timestamps):
            excluded_incomplete += 1
            continue
        selected = tuple(indexed[timestamp] for timestamp in expected_timestamps)
        if any(not observation.eligible for observation in selected):
            excluded_ineligible += 1
            continue

        per_snapshot_weight = 1.0 / len(selected)
        label = 1 if market.resolution is MarketOutcome.UP else 0
        for observation in selected:
            samples.append(
                ResearchSample(
                    sample_id=f"{market.slug}@{observation.ts_init}",
                    feature_ts=_datetime_from_ns(observation.ts_init),
                    label_available_ts=market.label_available_ts,
                    label=label,
                    group_id=market.slug,
                )
            )
            vectors.append(observation.values)
            weights.append(per_snapshot_weight)
        included_markets += 1

    if not samples:
        raise ValueError("no resolved markets have complete eligible opening features")
    return OpeningDirectionDatasetBuild(
        dataset=DirectionDataset(
            samples=tuple(samples),
            vectors=np.asarray(vectors, dtype=float),
            schema=schema,
            sample_weights=np.asarray(weights, dtype=float),
        ),
        requested_markets=len(ordered_markets),
        included_markets=included_markets,
        excluded_unresolved_markets=excluded_unresolved,
        excluded_void_markets=excluded_void,
        excluded_incomplete_markets=excluded_incomplete,
        excluded_ineligible_markets=excluded_ineligible,
        snapshots_per_market=len(offsets),
    )


def _index_market_observations(
    *,
    market: MarketWindow,
    observations: Sequence[OpeningFeatureObservation],
    expected_schema_hash: str,
) -> dict[int, OpeningFeatureObservation]:
    market_start_ns = _datetime_to_ns(market.t0)
    market_end_ns = _datetime_to_ns(market.t1)
    indexed: dict[int, OpeningFeatureObservation] = {}
    for observation in observations:
        if observation.market_window_start_ns != market_start_ns:
            raise ValueError(f"observation does not belong to market {market.slug!r}")
        if observation.feature_schema_hash != expected_schema_hash:
            raise ValueError("opening feature schema hash does not match the training schema")
        if observation.ts_init < observation.ts_event:
            raise ValueError("feature availability cannot precede its event timestamp")
        if not market_start_ns <= observation.ts_init < market_end_ns:
            raise ValueError("feature availability must lie within its market window")
        if observation.ts_init in indexed:
            raise ValueError(f"duplicate feature availability timestamp for {market.slug!r}")
        indexed[observation.ts_init] = observation
    return indexed


def _snapshot_offsets(
    *, snapshot_seconds: int, entry_start_seconds: int, entry_end_seconds: int
) -> tuple[int, ...]:
    if any(
        isinstance(value, bool) or not isinstance(value, Integral)
        for value in (snapshot_seconds, entry_start_seconds, entry_end_seconds)
    ) or (
        snapshot_seconds < 1
        or entry_start_seconds < 0
        or entry_end_seconds < entry_start_seconds
        or entry_end_seconds > 180
    ):
        raise ValueError("opening training snapshot timing is invalid")
    first = ((entry_start_seconds + snapshot_seconds - 1) // snapshot_seconds) * snapshot_seconds
    offsets = tuple(range(first, entry_end_seconds + 1, snapshot_seconds))
    if not offsets:
        raise ValueError("opening training timing produces no snapshots")
    return offsets


def _datetime_to_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


def _datetime_from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / _NANOS_PER_SECOND, tz=UTC)


__all__ = ["OpeningDirectionDatasetBuild", "build_opening_direction_dataset"]
