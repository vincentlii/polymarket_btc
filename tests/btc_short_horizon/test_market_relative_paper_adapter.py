from __future__ import annotations

from types import SimpleNamespace

from btc_short_horizon.features import opening_feature_schema
from btc_short_horizon.live.paper_runtime import MarketRelativePaperAdapter, ModelPaperPredictor
from btc_short_horizon.models import OpeningMispricingPrediction
from btc_short_horizon.models.market_relative import ProbabilityInterval
from btc_short_horizon.research.market_relative_v2 import (
    market_relative_v2_profile_families,
    market_relative_v2_research_schema,
)
from btc_short_horizon.research.opening_evidence import OpeningMarketObservation


def test_core_paper_adapter_uses_the_exact_offline_v2_schema(monkeypatch, tmp_path) -> None:
    families = market_relative_v2_profile_families("core")
    schema = market_relative_v2_research_schema(families)
    observed = {}

    class Model:
        def __init__(self) -> None:
            self.schema = schema

        def predict_probability_intervals(self, vector, anchor):  # type: ignore[no-untyped-def]
            observed["vector"] = vector
            observed["anchor"] = anchor
            return (ProbabilityInterval(0.52, 0.56, 0.60),)

    def load(**kwargs):  # type: ignore[no-untyped-def]
        observed.update(kwargs)
        return Model(), SimpleNamespace(
            model_id="relative-v2",
            feature_schema_hash=schema.hash,
        )

    monkeypatch.setattr(
        "btc_short_horizon.live.paper_runtime.MarketRelativeArtifactStore.load", load
    )
    adapter = MarketRelativePaperAdapter(
        directory=tmp_path,
        expected_rule_epoch="twap",
        feature_profile="core",
    )
    opening_schema = opening_feature_schema()
    values = dict.fromkeys(opening_schema.names, 0.0)
    values.update(
        elapsed_seconds=5.0,
        remaining_seconds=895.0,
        p_boundary_up=0.54,
        data_age_seconds=0.1,
        binance_spot_return_1s=0.001,
    )
    observation = OpeningMarketObservation(
        market_slug="market",
        decision_ts_ns=6_000_000_000,
        p_market_mid_up=0.51,
        data_age_seconds=0.1,
        up_available_ts_ns=5_900_000_000,
        down_available_ts_ns=5_900_000_000,
        up_epoch_id=1,
        down_epoch_id=1,
        has_data_gap=False,
        structure_valid=True,
        tick_unchanged=True,
        opening_feature_schema_hash=opening_schema.hash,
        opening_feature_values=opening_schema.vector_from(values),
        up_book=(0.50, 0.52, 10.0, 11.0),
        down_book=(0.48, 0.50, 12.0, 13.0),
    )
    prediction = OpeningMispricingPrediction(
        market_slug="market",
        model_version="legacy",
        feature_schema_hash="a" * 64,
        market_window_start_ts_ns=1_000_000_000,
        trigger_ts_ns=6_000_000_000,
        p_up=0.55,
        p_boundary_up=0.54,
        p_market_mid_up=0.51,
        data_age_seconds=0.1,
    )

    attached = adapter.attach(prediction, observation)

    assert observed["expected_schema_hash"] == schema.hash
    assert observed["expected_rule_epoch"] == "twap"
    assert attached.market_relative_model_version == "relative-v2"
    assert attached.market_relative_p_up == 0.56
    assert observed["vector"].shape == (1, len(schema.names))


def test_flow_paper_adapter_abstains_when_live_feature_quality_is_invalid(
    monkeypatch, tmp_path
) -> None:
    families = market_relative_v2_profile_families("flow")
    schema = market_relative_v2_research_schema(families)
    model = SimpleNamespace(schema=schema)
    metadata = SimpleNamespace(model_id="relative-v2", feature_schema_hash=schema.hash)
    monkeypatch.setattr(
        "btc_short_horizon.live.paper_runtime.MarketRelativeArtifactStore.load",
        lambda **_kwargs: (model, metadata),
    )
    adapter = MarketRelativePaperAdapter(
        directory=tmp_path, expected_rule_epoch="twap", feature_profile="flow"
    )
    prediction = OpeningMispricingPrediction(
        market_slug="market",
        model_version="legacy",
        feature_schema_hash="a" * 64,
        market_window_start_ts_ns=1_000_000_000,
        trigger_ts_ns=6_000_000_000,
        p_up=0.55,
        p_boundary_up=0.54,
        p_market_mid_up=0.51,
        data_age_seconds=0.1,
    )
    opening_schema = opening_feature_schema()
    observation = OpeningMarketObservation(
        market_slug="market",
        decision_ts_ns=6_000_000_000,
        p_market_mid_up=0.51,
        data_age_seconds=0.1,
        up_available_ts_ns=5_900_000_000,
        down_available_ts_ns=5_900_000_000,
        up_epoch_id=1,
        down_epoch_id=1,
        has_data_gap=False,
        structure_valid=True,
        tick_unchanged=True,
        opening_feature_schema_hash=opening_schema.hash,
        opening_feature_values=tuple(0.0 for _ in opening_schema.names),
        opening_feature_quality_flags=frozenset({"binance_spot_trade_stale"}),
        up_book=(0.50, 0.52, 10.0, 11.0),
        down_book=(0.48, 0.50, 12.0, 13.0),
    )

    assert adapter.attach(prediction, observation) is prediction


def test_paper_execution_requires_an_explicit_research_or_promotion_gate() -> None:
    predictor = object.__new__(ModelPaperPredictor)

    for config, expected in (
        ({}, False),
        ({"paper_experiment_gate_passed": True}, False),
        (
            {
                "observation_only": True,
                "paper_experiment_gate_passed": False,
                "paper_experiment_only": True,
                "runtime_promotion_eligible": False,
                "sealed_holdout_evaluated": False,
                "multiple_comparison_gate_passed": False,
                "selection_receipt_sha256": "1" * 64,
            },
            False,
        ),
        (
            {
                "paper_experiment_gate_passed": True,
                "paper_experiment_only": True,
                "runtime_promotion_eligible": False,
                "sealed_holdout_evaluated": False,
                "multiple_comparison_gate_passed": False,
                "selection_receipt_sha256": "1" * 64,
            },
            True,
        ),
        (
            {
                "runtime_promotion_eligible": True,
                "sealed_holdout_evaluated": True,
                "multiple_comparison_gate_passed": True,
            },
            True,
        ),
    ):
        predictor._market_relative = SimpleNamespace(  # noqa: SLF001
            metadata=SimpleNamespace(config=config)
        )
        assert predictor.market_relative_paper_execution_enabled is expected
