from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from numbers import Integral
import os
from pathlib import Path
import shutil
from uuid import uuid4

import joblib

from .direction import FittedDirectionModel


@dataclass(frozen=True, slots=True)
class ModelArtifactMetadata:
    model_id: str
    feature_schema_hash: str
    training_start_ns: int
    training_end_ns: int
    calibration_start_ns: int
    calibration_end_ns: int
    data_hash: str
    code_revision: str
    config: dict[str, object]
    model_sha256: str = ""

    def __post_init__(self) -> None:
        required = (
            self.model_id,
            self.feature_schema_hash,
            self.data_hash,
            self.code_revision,
        )
        if any(
            not isinstance(value, str) or not value or value.strip() != value for value in required
        ):
            raise ValueError("model metadata identifiers must not be empty")
        for name in (
            "training_start_ns",
            "training_end_ns",
            "calibration_start_ns",
            "calibration_end_ns",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.training_start_ns > self.training_end_ns:
            raise ValueError("training_start_ns must be <= training_end_ns")
        if self.calibration_start_ns > self.calibration_end_ns:
            raise ValueError("calibration_start_ns must be <= calibration_end_ns")
        if self.training_end_ns >= self.calibration_start_ns:
            raise ValueError("training data must end before calibration data starts")
        if not isinstance(self.config, dict):
            raise ValueError("model metadata config must be a dictionary")
        try:
            normalized_config = json.loads(json.dumps(self.config, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("model metadata config must be finite JSON data") from exc
        object.__setattr__(self, "config", normalized_config)
        if not _is_sha256(self.feature_schema_hash):
            raise ValueError("feature_schema_hash must be a SHA-256 hex digest")
        if not _is_sha256(self.data_hash):
            raise ValueError("data_hash must be a SHA-256 hex digest")
        if not isinstance(self.model_sha256, str):
            raise ValueError("model_sha256 must be a string")
        if self.model_sha256 and not _is_sha256(self.model_sha256):
            raise ValueError("model_sha256 must be an empty value or a SHA-256 hex digest")


class ModelArtifactStore:
    """Writes immutable model files and matching JSON metadata."""

    @staticmethod
    def save(
        *,
        directory: Path,
        model: FittedDirectionModel,
        metadata: ModelArtifactMetadata,
    ) -> ModelArtifactMetadata:
        _validate_model_metadata_consistency(model=model, metadata=metadata)
        directory.parent.mkdir(parents=True, exist_ok=True)
        if directory.exists():
            raise FileExistsError(f"model artifact directory already exists: {directory}")
        staging = directory.parent / f".{directory.name}.staging-{uuid4().hex}"
        staging.mkdir(exist_ok=False)
        model_path = staging / "model.joblib"
        try:
            joblib.dump(model, model_path)
            _fsync_file(model_path)
            model_hash = _sha256_file(model_path)
            effective_metadata = ModelArtifactMetadata(
                **{**asdict(metadata), "model_sha256": model_hash}
            )
            metadata_path = staging / "metadata.json"
            _write_json_fsynced(
                metadata_path,
                asdict(effective_metadata),
            )
            _fsync_directory(staging)
            staging.rename(directory)
            _fsync_directory(directory.parent)
            return effective_metadata
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def load(
        *, directory: Path, expected_schema_hash: str, expected_rule_epoch: str | None = None
    ) -> tuple[FittedDirectionModel, ModelArtifactMetadata]:
        metadata = ModelArtifactMetadata(
            **json.loads((directory / "metadata.json").read_text("utf-8"))
        )
        if metadata.feature_schema_hash != expected_schema_hash:
            raise ValueError(
                "model artifact schema mismatch: "
                f"expected {expected_schema_hash}, got {metadata.feature_schema_hash}"
            )
        if (
            expected_rule_epoch is not None
            and metadata.config.get("rule_epoch") != expected_rule_epoch
        ):
            raise ValueError(
                "model artifact rule epoch mismatch: "
                f"expected {expected_rule_epoch!r}, got {metadata.config.get('rule_epoch')!r}"
            )
        model_path = directory / "model.joblib"
        if not metadata.model_sha256:
            raise ValueError("model artifact metadata is missing model_sha256")
        if metadata.model_sha256 != _sha256_file(model_path):
            raise ValueError("model artifact SHA-256 mismatch")
        model = joblib.load(model_path)
        if not isinstance(model, FittedDirectionModel):
            raise TypeError(f"unexpected model artifact type: {type(model)!r}")
        if model.schema.hash != expected_schema_hash:
            raise ValueError("deserialized model schema mismatch")
        _validate_model_metadata_consistency(model=model, metadata=metadata)
        return model, metadata


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_model_metadata_consistency(
    *, model: FittedDirectionModel, metadata: ModelArtifactMetadata
) -> None:
    if metadata.feature_schema_hash != model.schema.hash:
        raise ValueError("model metadata feature schema does not match the fitted model")
    model_config = model.config_dict
    missing = set(model_config) - set(metadata.config)
    if missing:
        raise ValueError(f"model metadata config is missing fitted keys: {sorted(missing)!r}")
    for key, value in model_config.items():
        if _canonical_json(metadata.config[key]) != _canonical_json(value):
            raise ValueError(f"model metadata config disagrees with fitted model for {key!r}")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _write_json_fsynced(path: Path, payload: object) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
