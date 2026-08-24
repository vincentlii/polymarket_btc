from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from btc_short_horizon.research.opening_model_gate import (
    CandidateEvaluation,
    CandidatePairedEvidence,
    build_direction_gate_artifact,
    paired_daily_block_bootstrap,
    select_direction_candidate,
    weighted_calibration_error,
)
from btc_short_horizon.research import provenance as provenance_module


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


def test_multiple_lightgbm_candidates_require_family_adjusted_confidence() -> None:
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
    second_lightgbm = _candidate(
        name="lightgbm-base",
        kind="lightgbm",
        log_loss=0.645,
        brier=0.225,
        calibration_error=0.015,
        log_loss_ci_lower=0.001,
        brier_ci_lower=0.0005,
    )

    decision = select_direction_candidate((logistic, lightgbm, second_lightgbm))

    assert decision.selected_name == "logistic-c0.1"
    assert decision.eligible_lightgbm_candidates == ()


def test_direction_gate_artifact_reports_failed_conditions_and_target_bands() -> None:
    band = {
        "lower": 0.55,
        "upper": 0.60,
        "snapshot_count": 500,
        "effective_market_count": 250,
        "calibration_error": 0.02,
    }
    artifact = build_direction_gate_artifact(
        sealed_holdout_markets=1_345,
        log_loss_improvement=0.04,
        log_loss_ci_lower=0.03,
        log_loss_ci_upper=0.05,
        brier_improvement=0.02,
        brier_ci_lower=0.01,
        brier_ci_upper=0.03,
        calibration_slope=0.97,
        target_bands=tuple(
            {**band, "lower": lower, "upper": upper}
            for lower, upper in (
                (0.55, 0.60),
                (0.60, 0.65),
                (0.65, 0.70),
                (0.70, 1.00),
            )
        ),
        protocol_eligible=False,
        protocol_failures=("non_full_walk_forward_profile",),
    )

    assert artifact["evidence"]["sealed_holdout_markets"] == 1_345
    assert artifact["evidence"]["paired_vs_training_prior"]["log_loss_ci_lower"] == 0.03
    assert artifact["evidence"]["target_confidence_bands"][0]["effective_market_count"] == 250
    assert artifact["evidence"]["protocol_failures"] == ["non_full_walk_forward_profile"]
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


def test_direction_gate_rejects_incoherent_protocol_and_band_evidence() -> None:
    band = {
        "lower": 0.55,
        "upper": 0.60,
        "snapshot_count": 300,
        "effective_market_count": 300,
        "calibration_error": 0.02,
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
    arguments = {
        "sealed_holdout_markets": 2_500,
        "log_loss_improvement": 0.002,
        "log_loss_ci_lower": 0.0001,
        "log_loss_ci_upper": 0.004,
        "brier_improvement": 0.001,
        "brier_ci_lower": 0.0001,
        "brier_ci_upper": 0.002,
        "calibration_slope": 1.0,
        "target_bands": bands,
    }

    with pytest.raises(ValueError, match="cannot accompany"):
        build_direction_gate_artifact(
            **arguments,
            protocol_eligible=True,
            protocol_failures=("failure",),
        )
    with pytest.raises(ValueError, match="boundaries"):
        build_direction_gate_artifact(
            **{**arguments, "target_bands": ({**bands[0], "lower": 0.54}, *bands[1:])},
            protocol_eligible=True,
        )


def test_candidate_selection_rejects_duplicate_names() -> None:
    candidate = _candidate(
        name="logistic-c0.1",
        kind="logistic",
        log_loss=0.65,
        brier=0.23,
        calibration_error=0.02,
    )

    with pytest.raises(ValueError, match="unique"):
        select_direction_candidate((candidate, candidate))


def test_probability_evidence_rejects_nan_fractional_labels_and_duplicate_pairs() -> None:
    with pytest.raises(ValueError, match="probabilities"):
        weighted_calibration_error(
            labels=np.asarray([0, 1]),
            probabilities=np.asarray([0.2, np.nan]),
            weights=np.ones(2),
        )
    with pytest.raises(ValueError, match="binary"):
        weighted_calibration_error(
            labels=np.asarray([0.1, 1.0]),
            probabilities=np.asarray([0.2, 0.8]),
            weights=np.ones(2),
        )

    item = SimpleNamespace(
        sample_index=0,
        sample_id="market@1",
        feature_ts_ns=1,
        p_up=0.6,
        label=1,
    )
    with pytest.raises(ValueError, match="duplicate"):
        paired_daily_block_bootstrap(
            candidate_predictions=(item, item),
            baseline_predictions=(item, item),
            weights=np.ones(1),
            baseline_name="baseline",
            iterations=100,
        )


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

    monkeypatch.setattr(provenance_module.subprocess, "check_output", fake_check_output)

    provenance = provenance_module.git_provenance()

    assert provenance["dirty"] is True
    assert provenance["head_revision"] == "abc123"
    assert provenance["revision_label"].startswith("abc123-dirty:")
    assert provenance["tracked_diff_sha256"]
    assert provenance["status_entry_count"] == 2
