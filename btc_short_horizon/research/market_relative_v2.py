"""Fail-closed V2 market-relative dataset materialization from raw-derived evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import log
from typing import Mapping, Sequence

import numpy as np

from btc_short_horizon.features import FeatureSchema, opening_feature_schema
from btc_short_horizon.features.events import BtcBookTop, BtcTrade
from btc_short_horizon.features.market_relative import (
    DualTokenBookSnapshot,
    market_relative_feature_schema_v2,
    market_relative_feature_values_v2,
)
from btc_short_horizon.research.opening_features import ForwardOpeningFeatureBuild
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample


class MarketRelativeV2FactorFamily(StrEnum):
    ANCHOR = "anchor"
    PM_DUAL_TOKEN = "pm_dual_token"
    BINANCE_FLOW_BOOK = "binance_flow_book"
    OKX_FLOW_BOOK = "okx_flow_book"
    CROSS_VENUE = "cross_venue"


_V2_NAMES = market_relative_feature_schema_v2().names
_FAMILY_NAMES: dict[MarketRelativeV2FactorFamily, tuple[str, ...]] = {
    MarketRelativeV2FactorFamily.ANCHOR: _V2_NAMES[:7],
    MarketRelativeV2FactorFamily.PM_DUAL_TOKEN: _V2_NAMES[7:],
    MarketRelativeV2FactorFamily.BINANCE_FLOW_BOOK: tuple(
        f"{source}_{suffix}"
        for source in ("binance_spot", "binance_perp")
        for suffix in (
            "return_1s",
            "return_5s",
            "return_15s",
            "flow_1s",
            "flow_5s",
            "flow_15s",
            "book_imbalance",
            "microprice_distance",
            "spread_bps",
        )
    ),
    MarketRelativeV2FactorFamily.OKX_FLOW_BOOK: tuple(
        f"{source}_{suffix}"
        for source in ("okx_spot", "okx_swap")
        for suffix in (
            "return_1s",
            "return_5s",
            "return_15s",
            "flow_1s",
            "flow_5s",
            "flow_15s",
            "book_imbalance",
            "microprice_distance",
            "spread_bps",
        )
    ),
    MarketRelativeV2FactorFamily.CROSS_VENUE: (
        "consensus_dispersion_bps",
        "chainlink_consensus_basis_bps",
    ),
}
_FAMILY_SOURCES: dict[MarketRelativeV2FactorFamily, tuple[str, ...]] = {
    MarketRelativeV2FactorFamily.BINANCE_FLOW_BOOK: ("binance_spot", "binance_perp"),
    MarketRelativeV2FactorFamily.OKX_FLOW_BOOK: ("okx_spot", "okx_swap"),
    MarketRelativeV2FactorFamily.CROSS_VENUE: (
        "binance_spot",
        "binance_perp",
        "okx_spot",
        "okx_swap",
    ),
}


@dataclass(frozen=True, slots=True)
class MarketRelativeV2Coverage:
    input_market_count: int
    eligible_market_count: int
    input_snapshot_count: int
    eligible_snapshot_count: int
    excluded_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketRelativeV2DatasetBuild:
    dataset: DirectionDataset | None
    market_up_probabilities: tuple[float, ...]
    up_best_asks: tuple[float, ...]
    down_best_asks: tuple[float, ...]
    up_best_ask_sizes: tuple[float, ...]
    down_best_ask_sizes: tuple[float, ...]
    coverage: MarketRelativeV2Coverage
    families: tuple[MarketRelativeV2FactorFamily, ...]


def market_relative_v2_research_schema(
    families: Sequence[MarketRelativeV2FactorFamily],
) -> FeatureSchema:
    normalized = _families(families)
    names = tuple(name for family in normalized for name in _FAMILY_NAMES[family])
    return FeatureSchema(
        version="btc-market-relative-research-v2:" + "+".join(value.value for value in normalized),
        names=names,
    )


def materialize_market_relative_v2(
    *,
    market_slug: str,
    up_token_id: str,
    down_token_id: str,
    label: int,
    label_available_ts: datetime,
    direction_p_up_by_decision: Mapping[int, float],
    raw_build: ForwardOpeningFeatureBuild,
    families: Sequence[MarketRelativeV2FactorFamily],
) -> MarketRelativeV2DatasetBuild:
    """Materialize one whole market or exclude it; snapshots never count as markets."""

    normalized = _families(families)
    reasons = _missing_source_reasons(raw_build, normalized)
    observations = raw_build.observations
    if not observations:
        reasons.append("missing_observations")
    if any(not observation.eligible for observation in observations):
        reasons.append("ineligible_observation")
    if any(observation.ts_event not in direction_p_up_by_decision for observation in observations):
        reasons.append("missing_direction_probability")
    snapshots = _dual_books(
        raw_build=raw_build,
        up_token_id=up_token_id,
        down_token_id=down_token_id,
    )
    if any(observation.ts_event not in snapshots for observation in observations):
        reasons.append("missing_dual_token_book")
    reasons = sorted(set(reasons))
    if reasons:
        return MarketRelativeV2DatasetBuild(
            dataset=None,
            market_up_probabilities=(),
            up_best_asks=(),
            down_best_asks=(),
            up_best_ask_sizes=(),
            down_best_ask_sizes=(),
            coverage=MarketRelativeV2Coverage(
                input_market_count=1,
                eligible_market_count=0,
                input_snapshot_count=len(observations),
                eligible_snapshot_count=0,
                excluded_reasons=tuple(reasons),
            ),
            families=normalized,
        )

    opening_schema = opening_feature_schema()
    if any(observation.feature_schema_hash != opening_schema.hash for observation in observations):
        raise ValueError("raw-derived observation schema does not match opening_feature_schema")
    research_schema = market_relative_v2_research_schema(normalized)
    samples: list[ResearchSample] = []
    vectors: list[tuple[float, ...]] = []
    for observation in observations:
        opening_values = dict(zip(opening_schema.names, observation.values, strict=True))
        up, down = snapshots[observation.ts_event]
        v2_values = market_relative_feature_values_v2(
            direction_p_up=float(direction_p_up_by_decision[observation.ts_event]),
            boundary_p_up=observation.p_boundary_up,
            market_p_up=observation.p_market_mid_up,
            elapsed_seconds=opening_values["elapsed_seconds"],
            btc_data_age_seconds=opening_values["data_age_seconds"],
            up=up,
            down=down,
        )
        high_frequency = _high_frequency_values(
            raw_build=raw_build,
            decision_ts_ns=observation.ts_event,
        )
        all_values = {**opening_values, **v2_values, **high_frequency}
        vectors.append(tuple(all_values[name] for name in research_schema.names))
        samples.append(
            ResearchSample(
                sample_id=f"{market_slug}@{observation.ts_event}",
                group_id=market_slug,
                feature_ts=_datetime_ns(observation.ts_event),
                label_available_ts=label_available_ts,
                label=label,
            )
        )
    dataset = DirectionDataset(
        samples=tuple(samples),
        vectors=np.asarray(vectors, dtype=float),
        schema=research_schema,
        sample_weights=np.full(len(samples), 1.0 / len(samples)),
    )
    return MarketRelativeV2DatasetBuild(
        dataset=dataset,
        market_up_probabilities=tuple(observation.p_market_mid_up for observation in observations),
        up_best_asks=tuple(snapshots[item.ts_event][0].ask for item in observations),
        down_best_asks=tuple(snapshots[item.ts_event][1].ask for item in observations),
        up_best_ask_sizes=tuple(snapshots[item.ts_event][0].ask_size for item in observations),
        down_best_ask_sizes=tuple(snapshots[item.ts_event][1].ask_size for item in observations),
        coverage=MarketRelativeV2Coverage(1, 1, len(observations), len(observations), ()),
        families=normalized,
    )


def _families(
    values: Sequence[MarketRelativeV2FactorFamily],
) -> tuple[MarketRelativeV2FactorFamily, ...]:
    converted = tuple(MarketRelativeV2FactorFamily(value) for value in values)
    if not converted or len(set(converted)) != len(converted):
        raise ValueError("factor families must be non-empty and unique")
    return tuple(value for value in MarketRelativeV2FactorFamily if value in converted)


def _missing_source_reasons(
    raw_build: ForwardOpeningFeatureBuild,
    families: tuple[MarketRelativeV2FactorFamily, ...],
) -> list[str]:
    present = {
        summary.raw_source
        for summary in raw_build.source_summaries
        if summary.state_event_count > 0 and summary.gap_event_count == 0
    }
    required = {source for family in families for source in _FAMILY_SOURCES.get(family, ())}
    return [f"missing_source:{source}" for source in sorted(required - present)]


def _dual_books(
    *,
    raw_build: ForwardOpeningFeatureBuild,
    up_token_id: str,
    down_token_id: str,
) -> dict[int, tuple[DualTokenBookSnapshot, DualTokenBookSnapshot]]:
    state: dict[str, BtcBookTop] = {}
    events = tuple(sorted(raw_build.input_events, key=lambda value: value.available_ts_ns))
    index = 0
    result: dict[int, tuple[DualTokenBookSnapshot, DualTokenBookSnapshot]] = {}
    for observation in sorted(raw_build.observations, key=lambda value: value.ts_event):
        while index < len(events) and events[index].available_ts_ns <= observation.ts_event:
            event = events[index]
            index += 1
            if event.state_source != "polymarket_clob":
                continue
            if event.gap_before:
                state.pop(event.state_instrument, None)
            if isinstance(event.value, BtcBookTop):
                state[event.state_instrument] = event.value
        if up_token_id in state and down_token_id in state:
            result[observation.ts_event] = (
                _snapshot(state[up_token_id]),
                _snapshot(state[down_token_id]),
            )
    return result


def _snapshot(book: BtcBookTop) -> DualTokenBookSnapshot:
    return DualTokenBookSnapshot(book.bid, book.ask, book.bid_size, book.ask_size)


def _high_frequency_values(
    *, raw_build: ForwardOpeningFeatureBuild, decision_ts_ns: int
) -> dict[str, float]:
    events = tuple(
        event for event in raw_build.input_events if event.available_ts_ns <= decision_ts_ns
    )
    result: dict[str, float] = {}
    latest_prices: list[float] = []
    for source in ("binance_spot", "binance_perp", "okx_spot", "okx_swap"):
        source_events = tuple(event for event in events if event.state_source == source)
        trades = tuple(event.value for event in source_events if isinstance(event.value, BtcTrade))
        books = tuple(event.value for event in source_events if isinstance(event.value, BtcBookTop))
        latest = trades[-1].price if trades else None
        if latest is not None:
            latest_prices.append(latest)
        for seconds in (1, 5, 15):
            cutoff = decision_ts_ns - seconds * 1_000_000_000
            window = tuple(trade for trade in trades if trade.available_ts_ns >= cutoff)
            result[f"{source}_return_{seconds}s"] = (
                log(latest / window[0].price) if latest is not None and window else 0.0
            )
            notional = sum(trade.price * trade.quantity for trade in window)
            signed = sum(
                trade.price * trade.quantity * (1.0 if trade.aggressor_side == "buy" else -1.0)
                for trade in window
                if trade.aggressor_side in {"buy", "sell"}
            )
            result[f"{source}_flow_{seconds}s"] = signed / notional if notional else 0.0
        book = books[-1] if books else None
        if book is None:
            result[f"{source}_book_imbalance"] = 0.0
            result[f"{source}_microprice_distance"] = 0.0
            result[f"{source}_spread_bps"] = 0.0
        else:
            total = book.bid_size + book.ask_size
            midpoint = (book.bid + book.ask) / 2.0
            microprice = (book.ask * book.bid_size + book.bid * book.ask_size) / total
            result[f"{source}_book_imbalance"] = (book.bid_size - book.ask_size) / total
            result[f"{source}_microprice_distance"] = microprice / midpoint - 1.0
            result[f"{source}_spread_bps"] = (book.ask - book.bid) / midpoint * 10_000.0
    if latest_prices:
        consensus = float(np.median(latest_prices))
        result["consensus_dispersion_bps"] = (
            max(abs(price / consensus - 1.0) for price in latest_prices) * 10_000.0
        )
    else:
        result["consensus_dispersion_bps"] = 0.0
    # This value is already calculated causally from Chainlink and venue state by
    # OpeningFeatureState. It remains zero only when that evidence is unavailable.
    return result


def _datetime_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


__all__ = [
    "MarketRelativeV2Coverage",
    "MarketRelativeV2DatasetBuild",
    "MarketRelativeV2FactorFamily",
    "market_relative_v2_research_schema",
    "materialize_market_relative_v2",
]
