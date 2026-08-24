"""Pre-registered, execution-aware exit decisions for offline paired research."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class ExitAction(StrEnum):
    HOLD = "hold"
    SELL_FAK = "sell_fak"
    BUY_OPPOSITE_FAK = "buy_opposite_fak"


@dataclass(frozen=True, slots=True)
class ExitObservation:
    side: str
    entry_cost_per_share: float
    side_probability_lower: float
    side_probability_upper: float
    executable_bid: float
    opposite_executable_ask: float
    exit_fee_per_share: float
    hedge_fee_per_share: float
    slippage_buffer_per_share: float
    safety_buffer_per_share: float
    remaining_seconds: float

    def __post_init__(self) -> None:
        if self.side not in {"up", "down"}:
            raise ValueError("side must be 'up' or 'down'")
        probabilities = (self.side_probability_lower, self.side_probability_upper)
        if not all(isfinite(value) and 0.0 < value < 1.0 for value in probabilities):
            raise ValueError("probability bounds must be finite and in (0, 1)")
        if self.side_probability_lower > self.side_probability_upper:
            raise ValueError("probability lower bound cannot exceed upper bound")
        prices = (
            self.entry_cost_per_share,
            self.executable_bid,
            self.opposite_executable_ask,
        )
        if not all(isfinite(value) and 0.0 <= value <= 1.0 for value in prices):
            raise ValueError("prices must be finite and in [0, 1]")
        costs = (
            self.exit_fee_per_share,
            self.hedge_fee_per_share,
            self.slippage_buffer_per_share,
            self.safety_buffer_per_share,
        )
        if not all(isfinite(value) and value >= 0.0 for value in costs):
            raise ValueError("costs and buffers must be finite and non-negative")
        if not isfinite(self.remaining_seconds) or not 0.0 <= self.remaining_seconds <= 900.0:
            raise ValueError("remaining_seconds must be finite and in [0, 900]")


@dataclass(frozen=True, slots=True)
class ExitDecision:
    action: ExitAction
    conservative_advantage: float
    reason: str


def evaluate_exit_policies(observation: ExitObservation) -> ExitDecision:
    """Choose the strongest conservative alternative to hold-to-settlement."""

    sell_advantage = (
        observation.executable_bid
        - observation.exit_fee_per_share
        - observation.slippage_buffer_per_share
        - observation.side_probability_upper
        - observation.safety_buffer_per_share
    )
    pair_advantage = (
        1.0
        - observation.entry_cost_per_share
        - observation.opposite_executable_ask
        - observation.hedge_fee_per_share
        - observation.slippage_buffer_per_share
        - observation.safety_buffer_per_share
    )
    if pair_advantage > 0.0 and pair_advantage >= sell_advantage:
        return ExitDecision(
            action=ExitAction.BUY_OPPOSITE_FAK,
            conservative_advantage=pair_advantage,
            reason="paired_payout_lock_dominates_hold",
        )
    if sell_advantage > 0.0:
        return ExitDecision(
            action=ExitAction.SELL_FAK,
            conservative_advantage=sell_advantage,
            reason="net_executable_bid_dominates_hold_upper_bound",
        )
    return ExitDecision(
        action=ExitAction.HOLD,
        conservative_advantage=0.0,
        reason="no_executable_exit_dominates_hold",
    )


__all__ = ["ExitAction", "ExitDecision", "ExitObservation", "evaluate_exit_policies"]
