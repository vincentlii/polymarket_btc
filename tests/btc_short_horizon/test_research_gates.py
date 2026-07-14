from __future__ import annotations

from btc_short_horizon.research.gates import (
    CalibrationBinEvidence,
    DirectionGateEvidence,
    MakerGateEvidence,
    OpeningMispricingGateEvidence,
    evaluate_direction_gate,
    evaluate_maker_gate,
    evaluate_opening_mispricing_gate,
)


def test_direction_gate_requires_sample_size_metrics_ci_slope_and_target_bins() -> None:
    decision = evaluate_direction_gate(
        DirectionGateEvidence(
            sealed_holdout_markets=2_500,
            log_loss_improvement=0.002,
            log_loss_ci_lower=0.0001,
            brier_improvement=0.001,
            brier_ci_lower=0.0001,
            calibration_slope=1.0,
            target_bins=(CalibrationBinEvidence(sample_count=300, calibration_error=0.03),),
        )
    )

    assert decision.accepted
    assert decision.failed_conditions == ()


def test_opening_mispricing_and_maker_gates_return_actionable_failure_reasons() -> None:
    opening_mispricing = evaluate_opening_mispricing_gate(
        OpeningMispricingGateEvidence(paired_net_edge_ci_lower=0.0)
    )
    maker = evaluate_maker_gate(
        MakerGateEvidence(
            total_fills=499,
            up_fills=149,
            down_fills=149,
            net_ev_per_filled_share=0.004,
            net_ev_ci_lower=0.0,
            pnl_without_top_one_percent=-0.01,
            positive_nonoverlap_week_ratio=0.69,
            largest_month_pnl_share=0.51,
            capacity_net_edge_ci_lower=0.0,
            rebate_free=False,
            pessimistic_queue=False,
            p99_latency=False,
            trade_order_robust=False,
        )
    )

    assert opening_mispricing.accepted is False
    assert opening_mispricing.failed_conditions == ("paired_net_edge_ci_lower_must_be_positive",)
    assert maker.accepted is False
    assert {
        "insufficient_total_fills",
        "insufficient_up_fills",
        "insufficient_down_fills",
        "net_ev_per_share_below_threshold",
        "net_ev_ci_lower_not_positive",
        "top_one_percent_removal_not_positive",
        "insufficient_positive_test_weeks",
        "single_month_pnl_concentration",
        "capacity_ci_lower_not_positive",
        "rebate_free_requirement_failed",
        "pessimistic_queue_requirement_failed",
        "p99_latency_requirement_failed",
        "trade_ordering_not_robust",
    } <= set(maker.failed_conditions)
