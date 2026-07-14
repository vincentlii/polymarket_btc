from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    version: str
    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("version is required")
        if not self.names:
            raise ValueError("names must not be empty")
        if len(set(self.names)) != len(self.names):
            raise ValueError("feature names must be unique")
        if any(not name or name.strip() != name for name in self.names):
            raise ValueError("feature names must be non-empty and trimmed")

    @property
    def hash(self) -> str:
        payload = json.dumps(
            {"version": self.version, "names": self.names},
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    def vector_from(self, values: Mapping[str, float]) -> tuple[float, ...]:
        unknown = set(values).difference(self.names)
        if unknown:
            raise ValueError(f"unknown features: {sorted(unknown)}")
        missing = [name for name in self.names if name not in values]
        if missing:
            raise ValueError(f"missing features: {missing}")
        vector = tuple(float(values[name]) for name in self.names)
        if any(not isfinite(value) for value in vector):
            raise ValueError("feature vector must be finite")
        return vector

    def mapping_from(self, vector: Sequence[float]) -> dict[str, float]:
        if len(vector) != len(self.names):
            raise ValueError(f"expected {len(self.names)} values, got {len(vector)}")
        mapped = {name: float(value) for name, value in zip(self.names, vector, strict=True)}
        if any(not isfinite(value) for value in mapped.values()):
            raise ValueError("feature vector must be finite")
        return mapped
