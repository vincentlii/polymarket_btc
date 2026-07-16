from __future__ import annotations

from btc_short_horizon.research.opening_model_gate import (
    CandidateEvaluation,
    CandidatePairedEvidence,
    build_direction_gate_artifact,
    select_direction_candidate,
)
from scripts import btc_opening_mispricing_proxy as proxy_script


def _candidate(
    *,
    name: str,
    kind: str,
    log_loss: float,
    brier: float,
    calibration_error: float,
    log_loss_ci_lower: float | None = None,
    brier_ci_lower: float | None = None,
) -> CandidateEvaluation:
    paired = (
        None
        if log_loss_ci_lower is None or brier_ci_lower is None
        else CandidatePairedEvidence(
            baseline_name="logistic-c0.1",
            log_loss_improvement=0.01,
            log_loss_ci_lower=log_loss_ci_lower,
            log_loss_ci_upper=0.02,
            brier_improvement=0.005,
            brier_ci_lower=brier_ci_lower,
            brier_ci_upper=0.01,
        )
    )
    return CandidateEvaluation(
        name=name,
        kind=kind,
        log_loss=log_loss,
        brier=brier,
        calibration_error=calibration_error,
        paired_vs_best_logistic=paired,
    )


def test_lightgbm_falls_back_when_paired_improvement_is_not_significant() -> None:
    logistic = _candidate(
        name="logistic-c0.1",
        kind="logistic",
        log_loss=0.65,
        brier=0.23,
        calibration_error=0.02,
    )
    lightgbm = _candidate(
        name="lightgbm-small",
        kind="lightgbm",
        log_loss=0.64,
        brier=0.22,
        calibration_error=0.01,
        log_loss_ci_lower=-0.001,
        brier_ci_lower=0.001,
    )

    decision = select_direction_candidate((logistic, lightgbm))

    assert decision.selected_name == "logistic-c0.1"
    assert decision.reason == "no_lightgbm_candidate_met_replacement_rule"


def test_lightgbm_wins_only_when_all_replacement_conditions_pass() -> None:
    logistic = _candidate(
        name="logistic-c0.1",
        kind="logistic",
        log_loss=0.65,
        brier=0.23,
        calibration_error=0.02,
    )
    lightgbm = _candidate(
        name="lightgbm-small",
        kind="lightgbm",
        log_loss=0.64,
        brier=0.22,
        calibration_error=0.01,
        log_loss_ci_lower=0.002,
        brier_ci_lower=0.001,
    )

    decision = select_direction_candidate((logistic, lightgbm))

    assert decision.selected_name == "lightgbm-small"
    assert decision.reason == "lightgbm_met_replacement_rule"


def test_lightgbm_falls_back_when_calibration_does_not_improve() -> None:
    logistic = _candidate(
        name="logistic-c0.1",
        kind="logistic",
        log_loss=0.65,
        brier=0.23,
        calibration_error=0.02,
    )
    lightgbm = _candidate(
        name="lightgbm-small",
        kind="lightgbm",
        log_loss=0.64,
        brier=0.22,
        calibration_error=0.021,
        log_loss_ci_lower=0.002,
        brier_ci_lower=0.001,
    )

    decision = select_direction_candidate((logistic, lightgbm))

    assert decision.selected_name == "logistic-c0.1"


def test_direction_gate_artifact_reports_failed_conditions_and_target_bands() -> None:
    artifact = build_direction_gate_artifact(
        sealed_holdout_markets=1_345,
        log_loss_improvement=0.04,
        log_loss_ci_lower=0.03,
        log_loss_ci_upper=0.05,
        brier_improvement=0.02,
        brier_ci_lower=0.01,
        brier_ci_upper=0.03,
        calibration_slope=0.97,
        target_bands=(
            {
                "lower": 0.55,
                "upper": 0.60,
                "snapshot_count": 500,
                "effective_market_count": 250,
                "calibration_error": 0.02,
            },
        ),
        protocol_eligible=False,
        protocol_failures=("non_full_walk_forward_profile",),
    )

    assert artifact["evidence"]["sealed_holdout_markets"] == 1_345
    assert artifact["evidence"]["paired_vs_training_prior"]["log_loss_ci_lower"] == 0.03
    assert artifact["evidence"]["target_confidence_bands"][0]["effective_market_count"] == 250
    assert artifact["decision"]["accepted"] is False
    assert set(artifact["decision"]["failed_conditions"]) >= {
        "insufficient_sealed_holdout_markets",
        "target_bin_insufficient_samples",
        "non_full_walk_forward_profile",
    }


def test_direction_gate_artifact_can_accept_complete_evidence() -> None:
    band = {
        "lower": 0.55,
        "upper": 0.60,
        "snapshot_count": 600,
        "effective_market_count": 300,
        "calibration_error": 0.03,
    }
    bands = tuple(
        {**band, "lower": lower, "upper": upper}
        for lower, upper in (
            (0.55, 0.60),
            (0.60, 0.65),
            (0.65, 0.70),
            (0.70, 1.00),
        )
    )

    artifact = build_direction_gate_artifact(
        sealed_holdout_markets=2_500,
        log_loss_improvement=0.002,
        log_loss_ci_lower=0.0001,
        log_loss_ci_upper=0.004,
        brier_improvement=0.001,
        brier_ci_lower=0.0001,
        brier_ci_upper=0.002,
        calibration_slope=1.0,
        target_bands=bands,
        protocol_eligible=True,
    )

    assert artifact["decision"] == {"accepted": True, "failed_conditions": []}


def test_dirty_git_provenance_never_reports_a_clean_revision(
    monkeypatch,
) -> None:
    def fake_check_output(command, **_kwargs):
        if command[:3] == ("git", "rev-parse", "HEAD"):
            return "abc123\n"
        if command[:3] == ("git", "status", "--porcelain=v1"):
            return b" M scripts/model.py\n?? new.py\n"
        if command[:3] == ("git", "diff", "--binary"):
            return b"diff --git a/scripts/model.py b/scripts/model.py\n"
        raise AssertionError(command)

    monkeypatch.setattr(proxy_script.subprocess, "check_output", fake_check_output)

    provenance = proxy_script._git_provenance()

    assert provenance["dirty"] is True
    assert provenance["head_revision"] == "abc123"
    assert provenance["revision_label"].startswith("abc123-dirty:")
    assert provenance["tracked_diff_sha256"]
    assert provenance["status_entry_count"] == 2
