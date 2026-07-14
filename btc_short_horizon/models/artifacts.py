from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path

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
        if any(not value for value in required):
            raise ValueError("model metadata identifiers must not be empty")
        if self.training_start_ns > self.training_end_ns:
            raise ValueError("training_start_ns must be <= training_end_ns")
        if self.calibration_start_ns > self.calibration_end_ns:
            raise ValueError("calibration_start_ns must be <= calibration_end_ns")


class ModelArtifactStore:
    """Writes immutable model files and matching JSON metadata."""

    @staticmethod
    def save(
        *,
        directory: Path,
        model: FittedDirectionModel,
        metadata: ModelArtifactMetadata,
    ) -> ModelArtifactMetadata:
        directory.mkdir(parents=True, exist_ok=False)
        model_path = directory / "model.joblib"
        temp_path = directory / "model.joblib.tmp"
        try:
            joblib.dump(model, temp_path)
            temp_path.replace(model_path)
            model_hash = _sha256_file(model_path)
            effective_metadata = ModelArtifactMetadata(
                **{**asdict(metadata), "model_sha256": model_hash}
            )
            (directory / "metadata.json").write_text(
                json.dumps(asdict(effective_metadata), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return effective_metadata
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def load(
        *, directory: Path, expected_schema_hash: str
    ) -> tuple[FittedDirectionModel, ModelArtifactMetadata]:
        metadata = ModelArtifactMetadata(
            **json.loads((directory / "metadata.json").read_text("utf-8"))
        )
        if metadata.feature_schema_hash != expected_schema_hash:
            raise ValueError(
                "model artifact schema mismatch: "
                f"expected {expected_schema_hash}, got {metadata.feature_schema_hash}"
            )
        model_path = directory / "model.joblib"
        if metadata.model_sha256 != _sha256_file(model_path):
            raise ValueError("model artifact SHA-256 mismatch")
        model = joblib.load(model_path)
        if not isinstance(model, FittedDirectionModel):
            raise TypeError(f"unexpected model artifact type: {type(model)!r}")
        if model.schema.hash != expected_schema_hash:
            raise ValueError("deserialized model schema mismatch")
        return model, metadata


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
