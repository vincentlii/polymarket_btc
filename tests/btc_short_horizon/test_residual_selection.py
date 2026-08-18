from __future__ import annotations

from btc_short_horizon.research.residual_selection import (
    CandidateWindowScore,
    DevelopmentGateEvidence,
    LeafAudit,
    apply_bonferroni_correction,
    evaluate_development_gate,
    rank_by_median_window_score,
    require_identical_paired_markets,
)
import pytest


def test_leaf_audit_counts_unique_markets_not_snapshot_rows() -> None:
    audit = LeafAudit.from_assignments(
        leaf_by_sample=(0, 0, 0, 1, 1),
        market_by_sample=("m1", "m1", "m2", "m3", "m3"),
        minimum_markets_per_leaf=2,
    )

    assert audit.unique_markets_by_leaf == {0: 2, 1: 1}
    assert audit.invalid_leaves == (1,)


def test_candidate_ranking_uses_median_window_score_with_deterministic_tie_break() -> None:
    ranked = rank_by_median_window_score(
        (
            CandidateWindowScore("b", (1.0, 100.0, 1.0)),
            CandidateWindowScore("a", (2.0, 2.0, 2.0)),
            CandidateWindowScore("c", (2.0, 2.0, 2.0)),
        )
    )

    assert [item.candidate_id for item in ranked] == ["a", "c", "b"]


def test_development_gate_fail_closed_keeps_holdout_sealed() -> None:
    failure = evaluate_development_gate(
        DevelopmentGateEvidence(
            paired_log_loss_ci_lower=-0.001,
            paired_brier_ci_lower=0.001,
            calibration_safe=True,
            robust_edge_ci_lower=0.01,
            opportunity_count=500,
            maximum_direction_share=0.70,
            maximum_price_bucket_share=0.70,
            operationally_valid=True,
        )
    )
    success = evaluate_development_gate(
        DevelopmentGateEvidence(
            paired_log_loss_ci_lower=0.001,
            paired_brier_ci_lower=0.001,
            calibration_safe=True,
            robust_edge_ci_lower=0.01,
            opportunity_count=500,
            maximum_direction_share=0.70,
            maximum_price_bucket_share=0.70,
            operationally_valid=True,
        )
    )

    assert failure.accepted is False
    assert "probability_improvement_not_supported" in failure.failures
    assert success.accepted is True


def test_candidate_family_uses_frozen_bonferroni_threshold() -> None:
    decision = apply_bonferroni_correction({"a": 0.009, "b": 0.011, "c": 0.04, "d": 0.5, "e": 0.8})

    assert decision.adjusted_alpha == pytest.approx(0.01)
    assert decision.accepted_candidate_ids == ("a",)


def test_shared_and_independent_stage_comparison_requires_exact_pairing() -> None:
    assert require_identical_paired_markets(
        shared_stage_markets=("m2", "m1"),
        independent_stage_markets=("m1", "m2"),
    ) == ("m1", "m2")
    with pytest.raises(ValueError, match="identical markets"):
        require_identical_paired_markets(
            shared_stage_markets=("m1", "m2"),
            independent_stage_markets=("m1", "m3"),
        )
