"""Portable, hash-bound storage for an anchored market-relative research dataset."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

import numpy as np

from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.market_relative_stage_oof import AnchoredDirectionDataset
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample


_SCHEMA_VERSION = "btc-market-relative-anchored-dataset-v1"


def save_anchored_direction_dataset(
    *,
    path: Path,
    dataset: AnchoredDirectionDataset,
    lineage: dict[str, object],
) -> str:
    """Atomically write one dataset without pickle and return its content hash."""

    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_ids = np.asarray([item.sample_id for item in dataset.dataset.samples], dtype=np.str_)
    group_ids = np.asarray([item.group_id for item in dataset.dataset.samples], dtype=np.str_)
    feature_ts_ns = np.asarray(
        [_datetime_ns(item.feature_ts) for item in dataset.dataset.samples], dtype=np.int64
    )
    label_available_ts_ns = np.asarray(
        [_datetime_ns(item.label_available_ts) for item in dataset.dataset.samples],
        dtype=np.int64,
    )
    labels = np.asarray([item.label for item in dataset.dataset.samples], dtype=np.int8)
    metadata = {
        "schema_version": _SCHEMA_VERSION,
        "feature_schema_version": dataset.dataset.schema.version,
        "feature_names": list(dataset.dataset.schema.names),
        "feature_schema_hash": dataset.dataset.schema.hash,
        "fee_rule_hash": dataset.fee_rule_hash,
        "lineage": lineage,
    }
    staging = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with staging.open("wb") as handle:
            np.savez_compressed(
                handle,
                metadata_json=np.asarray(
                    json.dumps(metadata, sort_keys=True, separators=(",", ":"))
                ),
                sample_ids=sample_ids,
                group_ids=group_ids,
                feature_ts_ns=feature_ts_ns,
                label_available_ts_ns=label_available_ts_ns,
                labels=labels,
                vectors=dataset.dataset.vectors,
                sample_weights=dataset.dataset.sample_weights,
                market_up_probabilities=dataset.market_up_probabilities,
                up_best_asks=dataset.up_best_asks,
                down_best_asks=dataset.down_best_asks,
                up_best_ask_sizes=dataset.up_best_ask_sizes,
                down_best_ask_sizes=dataset.down_best_ask_sizes,
                fee_rates=dataset.fee_rates,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)
    return _file_sha256(path)


def load_anchored_direction_dataset(
    path: Path,
) -> tuple[AnchoredDirectionDataset, dict[str, object]]:
    """Read and fully validate a non-pickle anchored dataset."""

    with np.load(path, allow_pickle=False) as payload:
        metadata = json.loads(str(payload["metadata_json"].item()))
        if metadata.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("unsupported anchored dataset schema")
        names = tuple(str(value) for value in metadata.get("feature_names", ()))
        schema = FeatureSchema(version=str(metadata.get("feature_schema_version", "")), names=names)
        if schema.hash != metadata.get("feature_schema_hash"):
            raise ValueError("anchored dataset feature schema hash mismatch")
        sample_ids = tuple(str(value) for value in payload["sample_ids"])
        group_ids = tuple(str(value) for value in payload["group_ids"])
        feature_ts_ns = np.asarray(payload["feature_ts_ns"], dtype=np.int64)
        label_available_ts_ns = np.asarray(payload["label_available_ts_ns"], dtype=np.int64)
        labels = np.asarray(payload["labels"], dtype=np.int8)
        rows = len(sample_ids)
        if not (
            rows
            == len(group_ids)
            == len(feature_ts_ns)
            == len(label_available_ts_ns)
            == len(labels)
        ):
            raise ValueError("anchored dataset sample identity columns are misaligned")
        samples = tuple(
            ResearchSample(
                sample_id=sample_id,
                group_id=group_id,
                feature_ts=_ns_datetime(int(feature_ns)),
                label_available_ts=_ns_datetime(int(label_ns)),
                label=int(label),
            )
            for sample_id, group_id, feature_ns, label_ns, label in zip(
                sample_ids,
                group_ids,
                feature_ts_ns,
                label_available_ts_ns,
                labels,
                strict=True,
            )
        )
        direction = DirectionDataset(
            samples=samples,
            vectors=np.asarray(payload["vectors"], dtype=float),
            schema=schema,
            sample_weights=np.asarray(payload["sample_weights"], dtype=float),
        )
        anchored = AnchoredDirectionDataset(
            dataset=direction,
            market_up_probabilities=np.asarray(payload["market_up_probabilities"], dtype=float),
            up_best_asks=np.asarray(payload["up_best_asks"], dtype=float),
            down_best_asks=np.asarray(payload["down_best_asks"], dtype=float),
            up_best_ask_sizes=np.asarray(payload["up_best_ask_sizes"], dtype=float),
            down_best_ask_sizes=np.asarray(payload["down_best_ask_sizes"], dtype=float),
            fee_rates=np.asarray(payload["fee_rates"], dtype=float),
            fee_rule_hash=str(metadata.get("fee_rule_hash", "")),
        )
    lineage = metadata.get("lineage")
    if not isinstance(lineage, dict):
        raise ValueError("anchored dataset lineage must be an object")
    return anchored, lineage


def anchored_dataset_sha256(path: Path) -> str:
    return _file_sha256(path)


def _datetime_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dataset timestamps must be timezone-aware")
    return int(value.timestamp() * 1_000_000_000)


def _ns_datetime(value: int) -> datetime:
    if value < 0:
        raise ValueError("dataset timestamps must be non-negative")
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "anchored_dataset_sha256",
    "load_anchored_direction_dataset",
    "save_anchored_direction_dataset",
]
