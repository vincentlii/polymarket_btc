from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np

from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.opening_factor_challenge import (
    OpeningFactorDatasetBuild,
    OpeningFactorFamily,
    opening_factor_feature_schema,
)
from btc_short_horizon.research.opening_factor_research import (
    DEVELOPMENT_COMPARISON_COUNT,
    FACTOR_CANDIDATES,
    adjusted_alpha,
    run_opening_factor_development,
)
from btc_short_horizon.research.pipeline import DirectionDataset, OofPrediction
from btc_short_horizon.research.walk_forward import ResearchSample, WalkForwardConfig


def test_factor_candidate_matrix_is_frozen_and_uses_bonferroni_alpha() -> None:
    assert tuple(item.name for item in FACTOR_CANDIDATES) == (
        "control_logistic",
        "boundary_logistic",
        "state_logistic",
        "flow_logistic",
        "cross_market_logistic",
        "all_logistic",
        "all_lightgbm",
    )
    assert FACTOR_CANDIDATES[-1].families == tuple(OpeningFactorFamily)
    assert DEVELOPMENT_COMPARISON_COUNT == 7
    assert adjusted_alpha(DEVELOPMENT_COMPARISON_COUNT) == 0.05 / 7


def _build() -> OpeningFactorDatasetBuild:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    samples = tuple(
        ResearchSample(
            sample_id=f"m{index}@{int((start + timedelta(days=index)).timestamp() * 1e9)}",
            feature_ts=start + timedelta(days=index),
            label_available_ts=start + timedelta(days=index, minutes=16),
            label=index % 2,
            group_id=f"m{index}",
        )
        for index in range(8)
    )
    base_schema = FeatureSchema(version="control", names=("x",))
    factor_schema = opening_factor_feature_schema(
        base_schema=base_schema,
        families=tuple(OpeningFactorFamily),
    )
    weights = np.ones(len(samples))
    control = DirectionDataset(
        samples=samples,
        vectors=np.zeros((len(samples), 1)),
        schema=base_schema,
        sample_weights=weights,
    )
    factors = DirectionDataset(
        samples=samples,
        vectors=np.zeros((len(samples), len(factor_schema.names))),
        schema=factor_schema,
        sample_weights=weights,
    )
    return OpeningFactorDatasetBuild(control, factors, len(samples), 0, tuple(OpeningFactorFamily))


def test_lightgbm_is_paired_against_selected_logistic_not_control(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    build = _build()
    from btc_short_horizon.research import opening_factor_research as research_module

    original_bootstrap = research_module.paired_daily_block_bootstrap
    observed_alphas: list[float] = []

    def record_bootstrap(**kwargs):  # type: ignore[no-untyped-def]
        observed_alphas.append(kwargs["alpha"])
        return original_bootstrap(**kwargs)

    def fake_run(*, dataset, split_config, model_config):  # type: ignore[no-untyped-def]
        del split_config
        if model_config.kind == "lightgbm":
            confidence = 0.65
        elif len(dataset.schema.names) == len(build.factor_dataset.schema.names):
            confidence = 0.70
        elif len(dataset.schema.names) > 1:
            confidence = 0.60
        else:
            confidence = 0.55
        predictions = tuple(
            OofPrediction(
                fold_index=0,
                sample_index=index,
                sample_id=sample.sample_id,
                feature_ts_ns=int(sample.feature_ts.timestamp() * 1e9),
                p_up=confidence if sample.label else 1.0 - confidence,
                label=sample.label,
            )
            for index, sample in enumerate(dataset.samples)
        )
        return SimpleNamespace(predictions=predictions)

    monkeypatch.setattr(
        "btc_short_horizon.research.opening_factor_research.run_walk_forward_model",
        fake_run,
    )
    monkeypatch.setattr(
        "btc_short_horizon.research.opening_factor_research.paired_daily_block_bootstrap",
        record_bootstrap,
    )
    progress: list[str] = []

    result = run_opening_factor_development(
        build=build,
        split_config=WalkForwardConfig(),
        bootstrap_iterations=100,
        progress_callback=lambda item: progress.append(item.candidate.name),
    )

    lightgbm = next(item for item in result.candidates if item.candidate.name == "all_lightgbm")
    assert result.selected_name == "all_logistic"
    assert lightgbm.replacement_baseline_name == "all_logistic"
    assert lightgbm.replacement_log_loss_ci_upper is not None
    assert lightgbm.replacement_log_loss_ci_upper < 0.0
    assert progress == [item.name for item in FACTOR_CANDIDATES]
    assert observed_alphas == [0.05 / DEVELOPMENT_COMPARISON_COUNT] * 7
