from __future__ import annotations

import pytest

from btc_short_horizon.research.exit_policy import (
    ExitAction,
    ExitObservation,
    evaluate_exit_policies,
)


def test_exit_policy_holds_when_neither_executable_alternative_dominates() -> None:
    decision = evaluate_exit_policies(
        ExitObservation(
            side="up",
            entry_cost_per_share=0.45,
            side_probability_lower=0.51,
            side_probability_upper=0.57,
            executable_bid=0.54,
            opposite_executable_ask=0.56,
            exit_fee_per_share=0.01,
            hedge_fee_per_share=0.01,
            slippage_buffer_per_share=0.005,
            safety_buffer_per_share=0.01,
            remaining_seconds=300.0,
        )
    )

    assert decision.action is ExitAction.HOLD


def test_exit_policy_sells_only_when_net_bid_dominates_hold_upper_bound() -> None:
    decision = evaluate_exit_policies(
        ExitObservation(
            side="down",
            entry_cost_per_share=0.40,
            side_probability_lower=0.39,
            side_probability_upper=0.44,
            executable_bid=0.49,
            opposite_executable_ask=0.60,
            exit_fee_per_share=0.01,
            hedge_fee_per_share=0.01,
            slippage_buffer_per_share=0.005,
            safety_buffer_per_share=0.01,
            remaining_seconds=240.0,
        )
    )

    assert decision.action is ExitAction.SELL_FAK
    assert decision.conservative_advantage == pytest.approx(0.025)


def test_exit_policy_locks_payout_when_pair_cost_is_below_one() -> None:
    decision = evaluate_exit_policies(
        ExitObservation(
            side="up",
            entry_cost_per_share=0.37,
            side_probability_lower=0.43,
            side_probability_upper=0.50,
            executable_bid=0.45,
            opposite_executable_ask=0.54,
            exit_fee_per_share=0.01,
            hedge_fee_per_share=0.01,
            slippage_buffer_per_share=0.005,
            safety_buffer_per_share=0.01,
            remaining_seconds=200.0,
        )
    )

    assert decision.action is ExitAction.BUY_OPPOSITE_FAK
    assert decision.conservative_advantage == pytest.approx(0.065)


def test_exit_policy_rejects_observations_outside_the_preregistered_horizon() -> None:
    with pytest.raises(ValueError, match="remaining_seconds"):
        evaluate_exit_policies(
            ExitObservation(
                side="up",
                entry_cost_per_share=0.4,
                side_probability_lower=0.4,
                side_probability_upper=0.5,
                executable_bid=0.4,
                opposite_executable_ask=0.5,
                exit_fee_per_share=0.0,
                hedge_fee_per_share=0.0,
                slippage_buffer_per_share=0.0,
                safety_buffer_per_share=0.01,
                remaining_seconds=901.0,
            )
        )
