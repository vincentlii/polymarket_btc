from __future__ import annotations

import pytest

from btc_short_horizon.research.exit_policy import ExitAction
from btc_short_horizon.research.exit_replay import (
    BookLevel,
    ExitReplayInput,
    fee_rule_sha256,
    replay_exit_policies,
)


def test_sell_fak_replay_uses_partial_depth_vwap_and_fees() -> None:
    report = replay_exit_policies(
        ExitReplayInput(
            market_slug="btc-updown-15m-1",
            side="up",
            position_size=5.0,
            entry_vwap=0.40,
            side_probability_lower=0.20,
            side_probability_upper=0.30,
            side_bids=(BookLevel(0.36, 2.0), BookLevel(0.34, 2.0)),
            opposite_asks=(BookLevel(0.70, 10.0),),
            fee_rate=0.02,
            fee_exponent=1,
            fee_rule_hash=fee_rule_sha256(0.02, 1),
            safety_buffer_per_share=0.0,
            slippage_buffer_per_share=0.0,
            minimum_order_size=1.0,
            remaining_seconds=300.0,
            outcome=0,
        )
    )

    assert report.action is ExitAction.SELL_FAK
    assert report.filled_size == pytest.approx(4.0)
    assert report.unfilled_size == pytest.approx(1.0)
    assert report.execution_vwap == pytest.approx(0.35)
    assert report.fee_paid == pytest.approx(0.0182)
    assert report.realized_pnl == pytest.approx(4 * 0.35 - 0.0182 - 5 * 0.40)
    assert report.fee_rule_hash == fee_rule_sha256(0.02, 1)


def test_pair_lock_replay_buys_only_executable_opposite_depth() -> None:
    report = replay_exit_policies(
        ExitReplayInput(
            market_slug="btc-updown-15m-2",
            side="down",
            position_size=5.0,
            entry_vwap=0.30,
            side_probability_lower=0.45,
            side_probability_upper=0.55,
            side_bids=(BookLevel(0.40, 5.0),),
            opposite_asks=(BookLevel(0.55, 3.0),),
            fee_rate=0.0,
            fee_exponent=1,
            fee_rule_hash=fee_rule_sha256(0.0, 1),
            safety_buffer_per_share=0.0,
            slippage_buffer_per_share=0.0,
            minimum_order_size=1.0,
            remaining_seconds=300.0,
            outcome=1,
        )
    )

    assert report.action is ExitAction.BUY_OPPOSITE_FAK
    assert report.filled_size == pytest.approx(3.0)
    assert report.unfilled_size == pytest.approx(2.0)
    assert report.locked_pair_size == pytest.approx(3.0)
    assert report.realized_pnl == pytest.approx(3 * (1 - 0.30 - 0.55) - 2 * 0.30)


def test_exit_replay_is_deterministic_and_holds_when_minimum_size_unavailable() -> None:
    payload = ExitReplayInput(
        market_slug="btc-updown-15m-3",
        side="up",
        position_size=5.0,
        entry_vwap=0.40,
        side_probability_lower=0.30,
        side_probability_upper=0.35,
        side_bids=(BookLevel(0.50, 0.5),),
        opposite_asks=(BookLevel(0.70, 5.0),),
        fee_rate=0.0,
        fee_exponent=1,
        fee_rule_hash=fee_rule_sha256(0.0, 1),
        safety_buffer_per_share=0.0,
        slippage_buffer_per_share=0.0,
        minimum_order_size=1.0,
        remaining_seconds=300.0,
        outcome=1,
    )

    first = replay_exit_policies(payload)
    second = replay_exit_policies(payload)
    assert first == second
    assert first.action is ExitAction.HOLD
    assert first.filled_size == 0.0
