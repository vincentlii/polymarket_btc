from __future__ import annotations

from dataclasses import replace

import pytest

from btc_short_horizon.data.rule_contract import (
    CHAINLINK_BTC_USD_POINT_V1,
    rule_contract_sha256,
)
from btc_short_horizon.models.artifacts import ModelArtifactMetadata
from btc_short_horizon.models.market_relative_artifacts import (
    validate_market_relative_promotion_contract,
)


def _metadata() -> ModelArtifactMetadata:
    epoch = CHAINLINK_BTC_USD_POINT_V1
    return ModelArtifactMetadata(
        model_id="v2",
        feature_schema_hash="a" * 64,
        training_start_ns=1,
        training_end_ns=2,
        calibration_start_ns=3,
        calibration_end_ns=4,
        data_hash="b" * 64,
        code_revision="revision",
        config={
            "rule_epoch": epoch,
            "rule_contract_sha256": rule_contract_sha256(epoch),
            "runtime_promotion_eligible": True,
            "sealed_holdout_evaluated": True,
            "multiple_comparison_gate_passed": True,
            "selection_receipt_sha256": "c" * 64,
            "selected_stage_config_sha256": {
                "early_3s_to_30s": "d" * 64,
                "price_discovery_35s_to_90s": "e" * 64,
                "mid_early_95s_to_180s": "f" * 64,
            },
            "stage_calibrators": {
                "early_3s_to_30s": "beta",
                "price_discovery_35s_to_90s": "beta",
                "mid_early_95s_to_180s": "sigmoid",
            },
            "p_lower": {
                objective: {
                    unit: {
                        "value": 0.001,
                        "independent_market_count": 300,
                        "aggregation": (
                            "snapshot_mean_within_market_then_market_weighted_block_resample"
                        ),
                    }
                    for unit in ("market", "day", "week")
                }
                for objective in ("log_loss", "brier", "net_ev")
            },
            "factor_family_ablations": ["anchor", "pm_dual_token"],
            "minimum_leaf_unique_market_count": 120,
            "minimum_markets_per_leaf_required": 100,
            "minimum_independent_markets_required": 300,
        },
    )


def test_promotion_contract_fails_closed_until_raw_derived_exit_producer_exists() -> None:
    with pytest.raises(ValueError, match="raw-derived full-depth"):
        validate_market_relative_promotion_contract(_metadata())


def test_promotion_contract_fails_closed_when_week_evidence_is_missing() -> None:
    metadata = _metadata()
    config = dict(metadata.config)
    config["p_lower"] = {
        objective: {"market": metadata.config["p_lower"][objective]["market"]}
        for objective in ("log_loss", "brier", "net_ev")
    }
    with pytest.raises(ValueError, match="market/day/week"):
        validate_market_relative_promotion_contract(replace(metadata, config=config))


@pytest.mark.parametrize("missing", ["selection_receipt_sha256", "selected_stage_config_sha256"])
def test_promotion_contract_requires_selection_and_full_depth_exit_lineage(missing: str) -> None:
    metadata = _metadata()
    config = dict(metadata.config)
    config.pop(missing)
    with pytest.raises(ValueError):
        validate_market_relative_promotion_contract(replace(metadata, config=config))
