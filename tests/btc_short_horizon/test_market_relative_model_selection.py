from __future__ import annotations

from btc_short_horizon.research.market_relative_model_selection import (
    CandidateDevelopmentEvidence,
    select_market_relative_models,
    validate_selection_receipt,
)
from btc_short_horizon.strategy import OpeningStage


def _evidence(*, leaf: int | None = None, sealed: int = 0):
    return CandidateDevelopmentEvidence(
        fold_metrics=({"execution_score": 0.01},),
        objective_improvements={"log_loss": 0.01, "brier": 0.01, "net_ev": 0.01},
        objective_lower_bounds={"log_loss": 0.001, "brier": 0.001, "net_ev": 0.001},
        objective_p_values={"log_loss": 1e-6, "brier": 1e-6, "net_ev": 1e-6},
        oof_market_count=300,
        minimum_leaf_unique_market_count=leaf,
        sealed_sample_count=sealed,
    )


def test_bounded_selection_visits_64_lgb_and_5_logistic_per_stage_without_sealed_rows():
    calls = []
    stage_inputs = {stage: object() for stage in OpeningStage}

    def evaluate(stage, candidate, supplied):
        assert supplied is stage_inputs[stage]
        calls.append((stage, candidate.config.kind, candidate.manifest_hash))
        return _evidence(leaf=120 if candidate.config.kind == "lightgbm" else None)

    receipt = select_market_relative_models(
        stage_inputs=stage_inputs,
        evaluate_candidate=evaluate,
        mode="bounded",
        minimum_markets_per_leaf=100,
    )

    for stage in OpeningStage:
        stage_calls = [value for value in calls if value[0] is stage]
        assert sum(value[1] == "logistic" for value in stage_calls) == 5
        assert sum(value[1] == "lightgbm" for value in stage_calls) == 64
        assert len({value[2] for value in stage_calls}) == 69
        assert receipt["stages"][stage.value]["candidate_count"] == 69
    assert receipt["sealed_holdout_evaluated"] is False
    validate_selection_receipt(receipt)


def test_leaf_gate_prevents_under_supported_lgb_from_replacing_logistic():
    def evaluate(_stage, candidate, _supplied):
        evidence = _evidence(leaf=10 if candidate.config.kind == "lightgbm" else None)
        if candidate.config.kind == "logistic":
            return CandidateDevelopmentEvidence(
                fold_metrics=evidence.fold_metrics,
                objective_improvements={"log_loss": 0.0, "brier": 0.0, "net_ev": 0.0},
                objective_lower_bounds={"log_loss": 0.0, "brier": 0.0, "net_ev": 0.0},
                objective_p_values={"log_loss": 1.0, "brier": 1.0, "net_ev": 1.0},
                oof_market_count=evidence.oof_market_count,
            )
        return evidence

    receipt = select_market_relative_models(
        stage_inputs={stage: stage.value for stage in OpeningStage},
        evaluate_candidate=evaluate,
        mode="bounded",
        minimum_markets_per_leaf=100,
    )
    assert all(
        value["champion_config"]["kind"] == "logistic" for value in receipt["stages"].values()
    )


def test_sealed_evidence_is_rejected():
    def evaluate(_stage, _candidate, _supplied):
        return _evidence(sealed=1)

    try:
        select_market_relative_models(
            stage_inputs={stage: object() for stage in OpeningStage},
            evaluate_candidate=evaluate,
        )
    except ValueError as exc:
        assert "sealed holdout" in str(exc)
    else:
        raise AssertionError("sealed rows must fail closed")


def test_selection_ranks_candidates_by_median_fold_execution_score_not_global_mean():
    def evaluate(_stage, candidate, _supplied):
        if candidate.config.logistic_c == 0.01:
            folds = (
                {"execution_score": -0.10},
                {"execution_score": -0.10},
                {"execution_score": 1.55},
            )
            improvement = 0.45
        else:
            folds = ({"execution_score": 0.02}, {"execution_score": 0.03})
            improvement = 0.025
        return CandidateDevelopmentEvidence(
            fold_metrics=folds,
            objective_improvements={"log_loss": 0.01, "brier": 0.01, "net_ev": improvement},
            objective_lower_bounds={"log_loss": 0.001, "brier": 0.001, "net_ev": 0.001},
            objective_p_values={"log_loss": 1e-6, "brier": 1e-6, "net_ev": 1e-6},
            oof_market_count=300,
        )

    receipt = select_market_relative_models(
        stage_inputs={stage: object() for stage in OpeningStage},
        evaluate_candidate=evaluate,
    )
    for stage in OpeningStage:
        assert not receipt["stages"][stage.value]["champion_name"].endswith("c0.01")


def test_lightgbm_must_pass_actual_bonferroni_adjusted_p_value_gate():
    def evaluate(_stage, candidate, _supplied):
        evidence = _evidence(leaf=120 if candidate.config.kind == "lightgbm" else None)
        if candidate.config.kind == "logistic":
            return evidence
        return CandidateDevelopmentEvidence(
            fold_metrics=({"execution_score": 1.0},),
            objective_improvements={"log_loss": 1.0, "brier": 1.0, "net_ev": 1.0},
            objective_lower_bounds={"log_loss": 0.5, "brier": 0.5, "net_ev": 0.5},
            objective_p_values={"log_loss": 0.001, "brier": 0.001, "net_ev": 0.001},
            oof_market_count=300,
            minimum_leaf_unique_market_count=120,
        )

    receipt = select_market_relative_models(
        stage_inputs={stage: object() for stage in OpeningStage},
        evaluate_candidate=evaluate,
        mode="bounded",
        familywise_alpha=0.05,
    )
    for stage in OpeningStage:
        stage_receipt = receipt["stages"][stage.value]
        assert stage_receipt["bonferroni_test_count"] == 69 * 3
        assert stage_receipt["champion_config"]["kind"] == "logistic"
