"""Immutable artifacts for the Paper-only market-relative probability model."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import joblib

from btc_short_horizon.data.rule_contract import rule_contract_sha256
from btc_short_horizon.models.artifacts import ModelArtifactMetadata
from btc_short_horizon.models.market_relative import FittedMarketRelativeOffsetModel
from btc_short_horizon.models.market_relative_lightgbm import FittedMarketRelativeLightGBM


_MODEL_TYPES = {
    "market_relative_offset_v1": FittedMarketRelativeOffsetModel,
    "market_relative_lightgbm_v1": FittedMarketRelativeLightGBM,
}
_MarketRelativeModel = FittedMarketRelativeOffsetModel | FittedMarketRelativeLightGBM
_BLOCK_AGGREGATION = "snapshot_mean_within_market_then_market_weighted_block_resample"


class MarketRelativeArtifactStore:
    """Publish and load one rule-bound residual model without touching Legacy."""

    @staticmethod
    def save(
        *,
        directory: Path,
        model: _MarketRelativeModel,
        metadata: ModelArtifactMetadata,
    ) -> ModelArtifactMetadata:
        _validate(model=model, metadata=metadata)
        directory.parent.mkdir(parents=True, exist_ok=True)
        if directory.exists():
            raise FileExistsError(f"model artifact directory already exists: {directory}")
        staging = directory.parent / f".{directory.name}.staging-{uuid4().hex}"
        staging.mkdir(exist_ok=False)
        try:
            model_path = staging / "model.joblib"
            joblib.dump(model, model_path)
            _fsync_file(model_path)
            effective = ModelArtifactMetadata(
                **{**asdict(metadata), "model_sha256": _sha256_file(model_path)}
            )
            metadata_path = staging / "metadata.json"
            with metadata_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(asdict(effective), handle, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            staging.rename(directory)
            return effective
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def load(
        *,
        directory: Path,
        expected_schema_hash: str,
        expected_rule_epoch: str,
    ) -> tuple[_MarketRelativeModel, ModelArtifactMetadata]:
        metadata = ModelArtifactMetadata(
            **json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        )
        if metadata.feature_schema_hash != expected_schema_hash:
            raise ValueError(
                "market-relative artifact schema mismatch: "
                f"expected {expected_schema_hash}, got {metadata.feature_schema_hash}"
            )
        if metadata.config.get("rule_epoch") != expected_rule_epoch:
            raise ValueError(
                "market-relative artifact rule epoch mismatch: "
                f"expected {expected_rule_epoch!r}, got {metadata.config.get('rule_epoch')!r}"
            )
        model_path = directory / "model.joblib"
        if not metadata.model_sha256 or metadata.model_sha256 != _sha256_file(model_path):
            raise ValueError("market-relative artifact SHA-256 mismatch")
        model = joblib.load(model_path)
        expected_type = _MODEL_TYPES.get(str(metadata.config.get("opening_model_family")))
        if expected_type is None or not isinstance(model, expected_type):
            raise TypeError(f"unexpected market-relative artifact type: {type(model)!r}")
        _validate(model=model, metadata=metadata)
        return model, metadata


def _validate(
    *,
    model: _MarketRelativeModel,
    metadata: ModelArtifactMetadata,
) -> None:
    if model.schema.hash != metadata.feature_schema_hash:
        raise ValueError("market-relative metadata schema does not match the fitted model")
    family = metadata.config.get("opening_model_family")
    expected_type = _MODEL_TYPES.get(str(family))
    if expected_type is None or not isinstance(model, expected_type):
        raise ValueError(f"unsupported opening_model_family: {family!r}")
    if isinstance(model, FittedMarketRelativeLightGBM):
        if metadata.config.get("paper_experiment_only") is not True:
            raise ValueError("LightGBM market-relative artifacts must be Paper-only")
        if metadata.config.get("runtime_promotion_eligible") is not False:
            raise ValueError("development LightGBM artifacts cannot be promotion eligible")
        if metadata.config.get("probability_uncertainty_radius") != (
            model.probability_uncertainty_radius
        ):
            raise ValueError("LightGBM uncertainty radius metadata mismatch")
    rule_epoch = metadata.config.get("rule_epoch")
    if not isinstance(rule_epoch, str) or not rule_epoch.strip():
        raise ValueError("market-relative artifact requires a rule_epoch")
    contract_hash = metadata.config.get("rule_contract_sha256")
    if contract_hash is not None and contract_hash != rule_contract_sha256(rule_epoch):
        raise ValueError("market-relative artifact rule_contract_sha256 mismatch")


def validate_market_relative_promotion_contract(
    metadata: ModelArtifactMetadata,
) -> ModelArtifactMetadata:
    """Fail closed unless every preregistered promotion gate is embedded in metadata."""

    config = metadata.config
    epoch = config.get("rule_epoch")
    if not isinstance(epoch, str) or config.get("rule_contract_sha256") != rule_contract_sha256(
        epoch
    ):
        raise ValueError("promotion requires a matching rule_contract_sha256")
    if config.get("runtime_promotion_eligible") is not True:
        raise ValueError("artifact is not runtime promotion eligible")
    if config.get("sealed_holdout_evaluated") is not True:
        raise ValueError("promotion requires sealed holdout evidence")
    if config.get("multiple_comparison_gate_passed") is not True:
        raise ValueError("promotion requires the multiple-comparison gate")
    _require_sha256(config.get("selection_receipt_sha256"), "selection receipt")
    calibrators = config.get("stage_calibrators")
    required_stages = {
        "early_3s_to_30s",
        "price_discovery_35s_to_90s",
        "mid_early_95s_to_180s",
    }
    if not isinstance(calibrators, dict) or set(calibrators) != required_stages:
        raise ValueError("promotion requires exactly three stage calibrators")
    if any(not isinstance(value, str) or not value for value in calibrators.values()):
        raise ValueError("stage calibrator identifiers must be non-empty")
    selected_configs = config.get("selected_stage_config_sha256")
    if not isinstance(selected_configs, dict) or set(selected_configs) != required_stages:
        raise ValueError("promotion requires exactly three selected stage configurations")
    for value in selected_configs.values():
        _require_sha256(value, "selected stage configuration")
    minimum_markets = config.get("minimum_independent_markets_required")
    if (
        isinstance(minimum_markets, bool)
        or not isinstance(minimum_markets, int)
        or minimum_markets < 1
    ):
        raise ValueError("promotion requires minimum_independent_markets_required")
    lower_bounds = config.get("p_lower")
    objectives = {"log_loss", "brier", "net_ev"}
    units = {"market", "day", "week"}
    if not isinstance(lower_bounds, dict) or set(lower_bounds) != objectives:
        raise ValueError("promotion requires log_loss/brier/net_ev p_lower evidence")
    for objective in sorted(objectives):
        objective_evidence = lower_bounds[objective]
        if not isinstance(objective_evidence, dict) or set(objective_evidence) != units:
            raise ValueError("promotion requires market/day/week p_lower evidence")
        for unit in sorted(units):
            evidence = objective_evidence[unit]
            if not isinstance(evidence, dict):
                raise ValueError(f"{objective}/{unit} p_lower evidence must be an object")
            value = evidence.get("value")
            markets = evidence.get("independent_market_count")
            if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0.0:
                raise ValueError(f"{objective}/{unit} p_lower must be positive")
            if (
                isinstance(markets, bool)
                or not isinstance(markets, int)
                or markets < minimum_markets
            ):
                raise ValueError(
                    f"{objective}/{unit} p_lower requires at least "
                    f"{minimum_markets} independent markets"
                )
            if evidence.get("aggregation") != _BLOCK_AGGREGATION:
                raise ValueError(f"{objective}/{unit} p_lower uses unsupported aggregation")
    ablations = config.get("factor_family_ablations")
    if (
        not isinstance(ablations, list)
        or not ablations
        or any(not isinstance(value, str) or not value for value in ablations)
        or len(set(ablations)) != len(ablations)
    ):
        raise ValueError("promotion requires unique factor-family ablation evidence")
    observed = config.get("minimum_leaf_unique_market_count")
    required = config.get("minimum_markets_per_leaf_required")
    if (
        isinstance(observed, bool)
        or not isinstance(observed, int)
        or isinstance(required, bool)
        or not isinstance(required, int)
        or required <= 0
        or observed < required
    ):
        raise ValueError("promotion requires the independent-market leaf gate")
    raise ValueError(
        "promotion remains blocked until a raw-derived full-depth exit replay producer exists"
    )


def _require_sha256(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"promotion requires a lowercase SHA-256 for {label}")


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


__all__ = ["MarketRelativeArtifactStore", "validate_market_relative_promotion_contract"]
