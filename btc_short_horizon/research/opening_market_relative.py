"""Paired causal datasets for market-relative BTC opening-model research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite, log

import numpy as np

from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.opening_evidence import OpeningMarketObservation
from btc_short_horizon.research.opening_proxy import OpeningProxyDatasetBuild
from btc_short_horizon.research.pipeline import DirectionDataset


_PROBABILITY_EPSILON = 1e-6
_MAX_MARKET_DATA_AGE_SECONDS = 1.0


class MarketRelativeFeatureFamily(StrEnum):
    """Frozen additive factor groups, ordered by research complexity."""

    MARKET_ANCHOR = "market_anchor"
    BOUNDARY_RESIDUAL = "boundary_residual"
    TIME_QUALITY = "time_quality"


_FAMILY_NAMES = {
    MarketRelativeFeatureFamily.MARKET_ANCHOR: ("market_logit",),
    MarketRelativeFeatureFamily.BOUNDARY_RESIDUAL: ("boundary_market_logit_gap",),
    MarketRelativeFeatureFamily.TIME_QUALITY: (
        "market_logit_x_remaining_fraction",
        "boundary_market_logit_gap_x_remaining_fraction",
        "polymarket_data_age_seconds",
    ),
}


@dataclass(frozen=True, slots=True)
class OpeningMarketRelativeDatasetBuild:
    """Paired control/challenger samples on the exact same CLOB-covered markets."""

    paired_control_dataset: DirectionDataset | None
    challenger_dataset: DirectionDataset | None
    eligible_market_count: int
    excluded_missing_market_observation: int
    excluded_invalid_market_observation: int
    snapshots_per_market: int
    families: tuple[MarketRelativeFeatureFamily, ...]


def opening_market_relative_feature_schema(
    *,
    base_schema: FeatureSchema,
    families: Sequence[MarketRelativeFeatureFamily],
) -> FeatureSchema:
    """Return a deterministic schema without mutating the deployed control schema."""

    normalized = _normalize_families(families)
    appended = tuple(name for family in normalized for name in _FAMILY_NAMES[family])
    family_version = "+".join(family.value for family in normalized)
    return FeatureSchema(
        version=(
            f"btc-opening-market-relative-v1:{base_schema.hash}:"
            f"eps={_PROBABILITY_EPSILON:g}:{family_version}"
        ),
        names=base_schema.names + appended,
    )


def build_opening_market_relative_datasets(
    *,
    control_build: OpeningProxyDatasetBuild,
    observations_by_market: Mapping[str, Sequence[OpeningMarketObservation]],
    families: Sequence[MarketRelativeFeatureFamily],
    max_market_data_age_seconds: float = _MAX_MARKET_DATA_AGE_SECONDS,
) -> OpeningMarketRelativeDatasetBuild:
    """Join exact causal CLOB observations and drop incomplete markets as a whole."""

    normalized = _normalize_families(families)
    if not isfinite(max_market_data_age_seconds) or max_market_data_age_seconds <= 0.0:
        raise ValueError("max_market_data_age_seconds must be finite and > 0")
    control = control_build.dataset
    control_groups = {sample.group_id for sample in control.samples}
    unknown = set(observations_by_market) - control_groups
    if unknown:
        raise ValueError(f"market observations contain unknown groups: {sorted(unknown)!r}")

    indices_by_group: dict[str, list[int]] = {}
    for index, sample in enumerate(control.samples):
        indices_by_group.setdefault(sample.group_id, []).append(index)

    accepted_indices: list[int] = []
    factors_by_index: dict[int, tuple[float, ...]] = {}
    excluded_missing = 0
    excluded_invalid = 0
    base_names = {name: index for index, name in enumerate(control.schema.names)}
    required_base = {"p_boundary_up", "remaining_seconds"}
    if not required_base.issubset(base_names):
        raise ValueError("control schema lacks market-relative base features")

    for group_id, group_indices in indices_by_group.items():
        observations = tuple(observations_by_market.get(group_id, ()))
        if any(observation.market_slug != group_id for observation in observations):
            excluded_invalid += 1
            continue
        indexed: dict[int, OpeningMarketObservation] = {}
        duplicate = False
        for observation in observations:
            if observation.decision_ts_ns in indexed:
                duplicate = True
                break
            indexed[observation.decision_ts_ns] = observation
        if duplicate:
            excluded_invalid += 1
            continue

        selected: list[tuple[int, OpeningMarketObservation]] = []
        missing = False
        invalid = False
        for index in group_indices:
            decision_ts_ns = _datetime_ns(control.samples[index].feature_ts)
            observation = indexed.get(decision_ts_ns)
            if observation is None:
                missing = True
                break
            if not _valid_observation(
                observation,
                decision_ts_ns=decision_ts_ns,
                max_data_age_seconds=max_market_data_age_seconds,
            ):
                invalid = True
                break
            selected.append((index, observation))
        if missing:
            excluded_missing += 1
            continue
        if invalid:
            excluded_invalid += 1
            continue
        for index, observation in selected:
            factors_by_index[index] = _factor_vector(
                control_vector=control.vectors[index],
                base_names=base_names,
                observation=observation,
                families=normalized,
            )
            accepted_indices.append(index)

    if not accepted_indices:
        return OpeningMarketRelativeDatasetBuild(
            paired_control_dataset=None,
            challenger_dataset=None,
            eligible_market_count=0,
            excluded_missing_market_observation=excluded_missing,
            excluded_invalid_market_observation=excluded_invalid,
            snapshots_per_market=control_build.snapshots_per_market,
            families=normalized,
        )

    selected_indices = np.asarray(sorted(accepted_indices), dtype=int)
    selected_samples = tuple(control.samples[index] for index in selected_indices)
    selected_weights = control.sample_weights[selected_indices]
    paired_control = DirectionDataset(
        samples=selected_samples,
        vectors=control.vectors[selected_indices],
        schema=control.schema,
        sample_weights=selected_weights,
    )
    factor_matrix = np.asarray(
        [factors_by_index[int(index)] for index in selected_indices],
        dtype=float,
    )
    challenger = DirectionDataset(
        samples=selected_samples,
        vectors=np.column_stack((control.vectors[selected_indices], factor_matrix)),
        schema=opening_market_relative_feature_schema(
            base_schema=control.schema,
            families=normalized,
        ),
        sample_weights=selected_weights,
    )
    return OpeningMarketRelativeDatasetBuild(
        paired_control_dataset=paired_control,
        challenger_dataset=challenger,
        eligible_market_count=len({sample.group_id for sample in selected_samples}),
        excluded_missing_market_observation=excluded_missing,
        excluded_invalid_market_observation=excluded_invalid,
        snapshots_per_market=control_build.snapshots_per_market,
        families=normalized,
    )


def _normalize_families(
    families: Sequence[MarketRelativeFeatureFamily],
) -> tuple[MarketRelativeFeatureFamily, ...]:
    converted = tuple(MarketRelativeFeatureFamily(value) for value in families)
    if not converted:
        raise ValueError("at least one market-relative feature family is required")
    if len(set(converted)) != len(converted):
        raise ValueError("market-relative feature families must be unique")
    return tuple(family for family in MarketRelativeFeatureFamily if family in converted)


def _valid_observation(
    observation: OpeningMarketObservation,
    *,
    decision_ts_ns: int,
    max_data_age_seconds: float,
) -> bool:
    return (
        observation.decision_ts_ns == decision_ts_ns
        and observation.up_available_ts_ns <= decision_ts_ns
        and observation.down_available_ts_ns <= decision_ts_ns
        and observation.data_age_seconds <= max_data_age_seconds
        and not observation.has_data_gap
        and observation.structure_valid
        and observation.tick_unchanged
    )


def _factor_vector(
    *,
    control_vector: np.ndarray,
    base_names: Mapping[str, int],
    observation: OpeningMarketObservation,
    families: tuple[MarketRelativeFeatureFamily, ...],
) -> tuple[float, ...]:
    market_logit = _probability_logit(observation.p_market_mid_up)
    boundary_logit = _probability_logit(control_vector[base_names["p_boundary_up"]])
    residual = boundary_logit - market_logit
    remaining_fraction = min(
        1.0,
        max(0.0, control_vector[base_names["remaining_seconds"]] / 900.0),
    )
    values = {
        "market_logit": market_logit,
        "boundary_market_logit_gap": residual,
        "market_logit_x_remaining_fraction": market_logit * remaining_fraction,
        "boundary_market_logit_gap_x_remaining_fraction": residual * remaining_fraction,
        "polymarket_data_age_seconds": observation.data_age_seconds,
    }
    return tuple(values[name] for family in families for name in _FAMILY_NAMES[family])


def _probability_logit(value: float) -> float:
    clipped = min(1.0 - _PROBABILITY_EPSILON, max(_PROBABILITY_EPSILON, float(value)))
    return log(clipped / (1.0 - clipped))


def _datetime_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("feature timestamp must be timezone-aware")
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


__all__ = [
    "MarketRelativeFeatureFamily",
    "OpeningMarketRelativeDatasetBuild",
    "build_opening_market_relative_datasets",
    "opening_market_relative_feature_schema",
]
