from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace

import numpy as np
import pytest

from btc_short_horizon.research.opening_market_relative import (
    MarketRelativeFeatureFamily,
    OpeningMarketRelativeDatasetBuild,
)
from btc_short_horizon.research.opening_market_relative_research import (
    MARKET_RELATIVE_LOGISTIC_ABLATIONS,
    MarketRelativeCandidateMetrics,
    MarketRelativeDevelopmentRun,
    MarketRelativeSealedHoldoutRun,
    build_market_relative_artifact_contract,
    evaluate_market_relative_research_gate,
    market_relative_development_report,
    run_market_relative_development,
    run_market_relative_sealed_holdout,
)
from btc_short_horizon.research.pipeline import (
    DirectionDataset,
    HoldoutPrediction,
    OofPrediction,
    SealedHoldoutModelRun,
)
from btc_short_horizon.research.walk_forward import ResearchSample, WalkForwardConfig
from btc_short_horizon.features import FeatureSchema


def _dataset(*, extra_feature: bool = False) -> DirectionDataset:
    schema = FeatureSchema(
        version="challenger" if extra_feature else "control",
        names=("x", "market_logit") if extra_feature else ("x",),
    )
    samples = tuple(
        ResearchSample(
            sample_id=f"m{index}@5",
            feature_ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
            label_available_ts=datetime(2026, 1, 2, tzinfo=UTC) + timedelta(days=index),
            label=index % 2,
            group_id=f"m{index}",
        )
        for index in range(8)
    )
    vectors = np.asarray(
        [[float(index % 2), 0.1] if extra_feature else [float(index % 2)] for index in range(8)]
    )
    return DirectionDataset(
        samples=samples,
        vectors=vectors,
        schema=schema,
        sample_weights=np.ones(8),
    )


def _build(*, eligible: int = 8) -> OpeningMarketRelativeDatasetBuild:
    return OpeningMarketRelativeDatasetBuild(
        paired_control_dataset=_dataset(),
        challenger_dataset=_dataset(extra_feature=True),
        eligible_market_count=eligible,
        excluded_missing_market_observation=2,
        excluded_invalid_market_observation=1,
        snapshots_per_market=1,
        families=tuple(MarketRelativeFeatureFamily),
    )


def _predictions(probability_shift: float = 0.0) -> tuple[OofPrediction, ...]:
    return tuple(
        OofPrediction(
            fold_index=0,
            sample_index=index,
            sample_id=f"m{index}@5",
            feature_ts_ns=int(
                (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)).timestamp()
                * 1_000_000_000
            ),
            p_up=(0.35 - probability_shift if index % 2 == 0 else 0.65 + probability_shift),
            label=index % 2,
        )
        for index in range(8)
    )


def _metrics(name: str, *, improvement: float) -> MarketRelativeCandidateMetrics:
    return MarketRelativeCandidateMetrics(
        name=name,
        families=() if name == "control" else (MarketRelativeFeatureFamily.MARKET_ANCHOR,),
        log_loss=0.60 - improvement,
        brier=0.21 - improvement,
        calibration_error=0.03 - improvement,
        calibration_slope=1.0,
        predictions=_predictions(improvement),
        paired_log_loss_improvement=improvement if name != "control" else None,
        paired_log_loss_ci_lower=improvement / 2 if name != "control" else None,
        paired_log_loss_ci_upper=improvement * 2 if name != "control" else None,
        paired_brier_improvement=improvement if name != "control" else None,
        paired_brier_ci_lower=improvement / 2 if name != "control" else None,
        paired_brier_ci_upper=improvement * 2 if name != "control" else None,
    )


def _sealed_model_run() -> SealedHoldoutModelRun:
    prediction = HoldoutPrediction(
        sample_index=2,
        sample_id="m2@5",
        feature_ts_ns=2,
        p_up=0.70,
        label=1,
    )
    return SealedHoldoutModelRun(
        train_indices=(0,),
        fit_indices=(0,),
        early_stopping_indices=(),
        calibration_indices=(1,),
        holdout_indices=(2,),
        model=object(),
        predictions=(prediction,),
    )


def _sealed_run(
    development: MarketRelativeDevelopmentRun,
    *,
    accepted: bool = True,
):
    run = _sealed_model_run()
    return MarketRelativeSealedHoldoutRun(
        accepted=accepted,
        selected_name=development.selected_name or "market_anchor",
        failed_conditions=() if accepted else ("sealed_log_loss_ci_lower_not_positive",),
        holdout_market_count=2_500,
        control_run=run,
        challenger_run=run,
        log_loss=0.5,
        brier=0.2,
        calibration_error=0.02,
        calibration_slope=1.0,
        paired_log_loss_improvement=0.01,
        paired_log_loss_ci_lower=0.001,
        paired_log_loss_ci_upper=0.02,
        paired_brier_improvement=0.01,
        paired_brier_ci_lower=0.001,
        paired_brier_ci_upper=0.02,
        dataset_lineage_hash=development.dataset_lineage_hash,
        protocol_hash=development.protocol_hash,
    )


def test_logistic_ablation_protocol_is_frozen() -> None:
    assert tuple((item.name, item.families) for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS) == (
        ("control", ()),
        ("market_anchor", (MarketRelativeFeatureFamily.MARKET_ANCHOR,)),
        (
            "market_anchor_boundary",
            (
                MarketRelativeFeatureFamily.MARKET_ANCHOR,
                MarketRelativeFeatureFamily.BOUNDARY_RESIDUAL,
            ),
        ),
        ("market_relative_all", tuple(MarketRelativeFeatureFamily)),
    )


def test_coverage_shortfall_returns_structured_no_go_without_fitting() -> None:
    result = run_market_relative_development(
        datasets_by_ablation={
            item.name: _build(eligible=8) for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS[1:]
        },
        minimum_eligible_markets=10,
    )

    assert result.accepted is False
    assert result.selected_name is None
    assert result.failed_conditions == ("eligible_market_coverage_below_minimum",)
    assert result.coverage["eligible_market_count"] == 8
    assert result.candidates == ()


def test_development_rejects_unpaired_candidate_indices_before_fitting() -> None:
    builds = {item.name: _build() for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS[1:]}
    broken = builds["market_anchor"]
    assert broken.challenger_dataset is not None
    samples = list(broken.challenger_dataset.samples)
    samples[0] = replace(samples[0], sample_id="different@5", group_id="different")
    builds["market_anchor"] = replace(
        broken,
        challenger_dataset=DirectionDataset(
            samples=tuple(samples),
            vectors=broken.challenger_dataset.vectors,
            schema=broken.challenger_dataset.schema,
            sample_weights=broken.challenger_dataset.sample_weights,
        ),
    )

    with pytest.raises(ValueError, match="identical paired samples"):
        run_market_relative_development(
            datasets_by_ablation=builds,
            minimum_eligible_markets=1,
            bootstrap_iterations=100,
        )


def test_research_gate_requires_paired_improvement_and_keeps_holdout_sealed() -> None:
    development = MarketRelativeDevelopmentRun(
        accepted=False,
        selected_name=None,
        failed_conditions=(),
        coverage={"eligible_market_count": 8},
        candidates=(
            _metrics("control", improvement=0.0),
            _metrics("market_anchor", improvement=0.01),
        ),
        sealed_holdout_evaluated=False,
    )

    gated = evaluate_market_relative_research_gate(development)

    assert gated.accepted is True
    assert gated.selected_name == "market_anchor"
    assert gated.sealed_holdout_evaluated is False


def test_sealed_holdout_cannot_run_before_development_gate_passes() -> None:
    development = MarketRelativeDevelopmentRun(
        accepted=False,
        selected_name=None,
        failed_conditions=("no_challenger_passed_paired_development_gate",),
        coverage={"eligible_market_count": 8},
        candidates=(),
        sealed_holdout_evaluated=False,
    )

    with pytest.raises(ValueError, match="development gate"):
        run_market_relative_sealed_holdout(
            development=development,
            datasets_by_ablation={
                item.name: _build() for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS[1:]
            },
            minimum_holdout_markets=1,
        )


def test_artifact_contract_binds_family_protocol_and_source_hash() -> None:
    development = evaluate_market_relative_research_gate(
        MarketRelativeDevelopmentRun(
            accepted=False,
            selected_name=None,
            failed_conditions=(),
            coverage={"eligible_market_count": 8},
            candidates=(
                _metrics("control", improvement=0.0),
                _metrics("market_anchor", improvement=0.01),
            ),
            sealed_holdout_evaluated=False,
        )
    )

    development = replace(
        development,
        dataset_lineage_hash="b" * 64,
        control_schema_hash="c" * 64,
        challenger_schema_hashes={"market_anchor": "d" * 64},
        protocol_hash="e" * 64,
        split_config={"train_duration_seconds": 90 * 86_400},
        model_config={"kind": "logistic", "logistic_c": 0.1},
    )
    sealed = _sealed_run(development)

    contract = build_market_relative_artifact_contract(
        development=development,
        sealed_holdout=sealed,
        source_hash="a" * 64,
        rule_epoch="btc-chainlink-v1",
        ingest_version="btc-short-horizon-v14",
        join_cadence_seconds=5,
        opening_protocol={"version": 2, "entry_end_seconds": 180},
    )

    assert contract["opening_model_family"] == "market_relative_v1"
    assert contract["market_relative_feature_families"] == ["market_anchor"]
    assert contract["market_relative_protocol"]["probability_clip_epsilon"] == 1e-6
    assert contract["market_relative_protocol"]["join_cadence_seconds"] == 5
    assert contract["market_relative_protocol"]["opening_protocol"]["version"] == 2
    assert contract["source_hash"] == "a" * 64
    assert contract["dataset_lineage_hash"] == "b" * 64
    assert contract["control_feature_schema_hash"] == "c" * 64
    assert contract["selected_feature_schema_hash"] == "d" * 64
    assert contract["protocol_hash"] == "e" * 64
    assert contract["rule_epoch"] == "btc-chainlink-v1"
    assert contract["ingest_version"] == "btc-short-horizon-v14"
    assert contract["split_config"]["train_duration_seconds"] == 90 * 86_400
    assert contract["model_config"]["kind"] == "logistic"
    assert contract["runtime_promotion_eligible"] is False
    assert contract["promotion_blockers"] == ["pair_session_identity_not_proven"]


def test_artifact_contract_rejects_failed_or_mismatched_real_sealed_run() -> None:
    development = replace(
        evaluate_market_relative_research_gate(
            MarketRelativeDevelopmentRun(
                accepted=False,
                selected_name=None,
                failed_conditions=(),
                coverage={"eligible_market_count": 8},
                candidates=(
                    _metrics("control", improvement=0.0),
                    _metrics("market_anchor", improvement=0.01),
                ),
            )
        ),
        dataset_lineage_hash="b" * 64,
        control_schema_hash="c" * 64,
        challenger_schema_hashes={"market_anchor": "d" * 64},
        protocol_hash="e" * 64,
        split_config={"train_duration_seconds": 1},
        model_config={"kind": "logistic"},
    )
    arguments = {
        "development": development,
        "source_hash": "a" * 64,
        "rule_epoch": "btc-chainlink-v1",
        "ingest_version": "btc-short-horizon-v14",
        "join_cadence_seconds": 5,
        "opening_protocol": {"version": 2},
    }

    with pytest.raises(ValueError, match="sealed holdout gate"):
        build_market_relative_artifact_contract(
            **arguments,
            sealed_holdout=_sealed_run(development, accepted=False),
        )
    with pytest.raises(ValueError, match="lineage"):
        build_market_relative_artifact_contract(
            **arguments,
            sealed_holdout=replace(
                _sealed_run(development),
                dataset_lineage_hash="f" * 64,
            ),
        )


def test_sealed_holdout_rejects_dataset_replacement_before_fitting() -> None:
    builds = {item.name: _build() for item in MARKET_RELATIVE_LOGISTIC_ABLATIONS[1:]}
    development = run_market_relative_development(
        datasets_by_ablation=builds,
        minimum_eligible_markets=100,
    )
    development = replace(
        development,
        accepted=True,
        selected_name="market_anchor",
        failed_conditions=(),
        candidates=(
            _metrics("control", improvement=0.0),
            _metrics("market_anchor", improvement=0.01),
        ),
    )
    selected = builds["market_anchor"]
    assert selected.challenger_dataset is not None
    changed_vectors = selected.challenger_dataset.vectors.copy()
    changed_vectors[0, 0] += 0.25
    builds["market_anchor"] = replace(
        selected,
        challenger_dataset=DirectionDataset(
            samples=selected.challenger_dataset.samples,
            vectors=changed_vectors,
            schema=selected.challenger_dataset.schema,
            sample_weights=selected.challenger_dataset.sample_weights,
        ),
    )

    with pytest.raises(ValueError, match="development dataset lineage"):
        run_market_relative_sealed_holdout(
            development=development,
            datasets_by_ablation=builds,
            minimum_holdout_markets=1,
            split_config=WalkForwardConfig(),
        )


def test_development_report_contains_metrics_and_coverage_without_predictions() -> None:
    development = evaluate_market_relative_research_gate(
        MarketRelativeDevelopmentRun(
            accepted=False,
            selected_name=None,
            failed_conditions=(),
            coverage={"eligible_market_count": 8, "excluded_missing_market_observation": 2},
            candidates=(
                _metrics("control", improvement=0.0),
                _metrics("market_anchor", improvement=0.01),
            ),
            sealed_holdout_evaluated=False,
        )
    )

    report = market_relative_development_report(development)

    assert report["decision"] == {
        "accepted": True,
        "selected_name": "market_anchor",
        "failed_conditions": [],
        "sealed_holdout_evaluated": False,
    }
    assert report["coverage"]["eligible_market_count"] == 8
    assert report["candidates"][1]["paired_vs_control"]["log_loss_ci_lower"] == 0.005
    assert "predictions" not in report["candidates"][1]
