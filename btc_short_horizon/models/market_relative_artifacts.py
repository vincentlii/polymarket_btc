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

from btc_short_horizon.models.artifacts import ModelArtifactMetadata
from btc_short_horizon.models.market_relative import FittedMarketRelativeOffsetModel
from btc_short_horizon.models.market_relative_lightgbm import FittedMarketRelativeLightGBM


_MODEL_TYPES = {
    "market_relative_offset_v1": FittedMarketRelativeOffsetModel,
    "market_relative_lightgbm_v1": FittedMarketRelativeLightGBM,
}
_MarketRelativeModel = FittedMarketRelativeOffsetModel | FittedMarketRelativeLightGBM


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


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


__all__ = ["MarketRelativeArtifactStore"]
