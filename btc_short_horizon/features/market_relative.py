"""Causal dual-token Polymarket features for market-relative model v2 research."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models.market_relative import (
    market_relative_runtime_feature_values,
)


_BOOK_FEATURE_NAMES = (
    "pm_up_spread",
    "pm_down_spread",
    "pm_up_depth_imbalance",
    "pm_down_depth_imbalance",
    "pm_up_microprice_distance",
    "pm_down_microprice_distance",
    "pm_complement_ask_excess",
    "pm_complement_bid_shortfall",
)


@dataclass(frozen=True, slots=True)
class DualTokenBookSnapshot:
    bid: float
    ask: float
    bid_size: float
    ask_size: float

    def __post_init__(self) -> None:
        if not all(isfinite(value) for value in (self.bid, self.ask, self.bid_size, self.ask_size)):
            raise ValueError("book values must be finite")
        if not 0.0 < self.bid <= 1.0 or not 0.0 < self.ask <= 1.0:
            raise ValueError("book prices must be in (0, 1]")
        if self.bid > self.ask:
            raise ValueError("bid must not exceed ask")
        if self.bid_size <= 0.0 or self.ask_size <= 0.0:
            raise ValueError("book sizes must be positive")


def market_relative_feature_schema_v2() -> FeatureSchema:
    return FeatureSchema(
        version="btc-market-relative-dual-book-v2",
        names=(
            "direction_market_logit_gap",
            "boundary_market_logit_gap",
            "direction_gap_x_remaining_fraction",
            "boundary_gap_x_remaining_fraction",
            "btc_data_age_seconds",
            "stage_discovery",
            "stage_mid_early",
            *_BOOK_FEATURE_NAMES,
        ),
    )


def market_relative_feature_values_v2(
    *,
    direction_p_up: float,
    boundary_p_up: float,
    market_p_up: float,
    elapsed_seconds: float,
    btc_data_age_seconds: float,
    up: DualTokenBookSnapshot,
    down: DualTokenBookSnapshot,
) -> dict[str, float]:
    values = market_relative_runtime_feature_values(
        direction_p_up=direction_p_up,
        boundary_p_up=boundary_p_up,
        market_p_up=market_p_up,
        elapsed_seconds=elapsed_seconds,
        btc_data_age_seconds=btc_data_age_seconds,
    )
    values.update(
        {
            "pm_up_spread": up.ask - up.bid,
            "pm_down_spread": down.ask - down.bid,
            "pm_up_depth_imbalance": _imbalance(up),
            "pm_down_depth_imbalance": _imbalance(down),
            "pm_up_microprice_distance": _microprice_distance(up),
            "pm_down_microprice_distance": _microprice_distance(down),
            "pm_complement_ask_excess": up.ask + down.ask - 1.0,
            "pm_complement_bid_shortfall": 1.0 - up.bid - down.bid,
        }
    )
    return values


def _imbalance(book: DualTokenBookSnapshot) -> float:
    return (book.bid_size - book.ask_size) / (book.bid_size + book.ask_size)


def _microprice_distance(book: DualTokenBookSnapshot) -> float:
    midpoint = (book.bid + book.ask) / 2.0
    microprice = (book.ask * book.bid_size + book.bid * book.ask_size) / (
        book.bid_size + book.ask_size
    )
    return microprice - midpoint


__all__ = [
    "DualTokenBookSnapshot",
    "market_relative_feature_schema_v2",
    "market_relative_feature_values_v2",
]
