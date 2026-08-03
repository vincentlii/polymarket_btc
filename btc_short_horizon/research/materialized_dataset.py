"""Strict streamed reader for frozen materialized direction datasets."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from numbers import Integral
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample


_BATCH_SIZE = 8_192


def read_materialized_direction_dataset(
    path: Path,
    *,
    expected_schema: FeatureSchema | None = None,
    market_stride: int = 1,
    expected_offsets: Sequence[int] | None = None,
) -> DirectionDataset:
    """Read a frozen Parquet dataset without changing its causal sample lineage.

    ``expected_offsets`` is an internal narrowing contract used by the legacy
    opening-proxy CLI; absent it, every snapshot in each selected market is read.
    """

    if (
        isinstance(market_stride, bool)
        or not isinstance(market_stride, Integral)
        or market_stride < 1
    ):
        raise ValueError("materialized market_stride must be >= 1")
    try:
        parquet = pq.ParquetFile(path)
    except (OSError, ValueError) as exc:
        raise ValueError("materialized proxy dataset has an incompatible schema") from exc
    schema = expected_schema or _schema_from_parquet(parquet)
    required = {
        "sample_id",
        "feature_ts",
        "label_available_ts",
        "label",
        "elapsed_seconds",
        *schema.names,
    }
    if not required.issubset(parquet.schema_arrow.names):
        raise ValueError("materialized proxy dataset has an incompatible schema")
    offsets = _normalize_offsets(expected_offsets)
    columns = tuple(
        dict.fromkeys(
            (
                "sample_id",
                "feature_ts",
                "label_available_ts",
                "label",
                "elapsed_seconds",
                *schema.names,
            )
        )
    )
    rows: list[tuple[ResearchSample, np.ndarray]] = []
    source_group: str | None = None
    selected_group: str | None = None
    source_market_index = -1
    seen_groups: set[str] = set()
    selected_offsets: list[int] = []
    previous_feature_ts: datetime | None = None

    def finish_selected_group() -> None:
        if (
            selected_group is not None
            and offsets is not None
            and tuple(selected_offsets) != offsets
        ):
            raise ValueError(
                f"materialized proxy market {selected_group!r} has incomplete snapshots"
            )

    for batch in parquet.iter_batches(batch_size=_BATCH_SIZE, columns=columns):
        values = {name: batch.column(index) for index, name in enumerate(columns)}
        sample_ids = values["sample_id"].to_pylist()
        feature_timestamps = values["feature_ts"].to_pylist()
        label_available_timestamps = values["label_available_ts"].to_pylist()
        elapsed = _strict_integer_array(
            values["elapsed_seconds"].to_numpy(zero_copy_only=False), name="elapsed_seconds"
        )
        labels = _strict_integer_array(values["label"].to_numpy(zero_copy_only=False), name="label")
        feature_matrix = np.column_stack(
            [
                np.asarray(values[name].to_numpy(zero_copy_only=False), dtype=float)
                for name in schema.names
            ]
        )
        if not np.isfinite(feature_matrix).all():
            raise ValueError("materialized proxy feature values must be finite")
        for index, sample_id_raw in enumerate(sample_ids):
            sample_id = str(sample_id_raw)
            group_id = _sample_group_id(sample_id)
            feature_ts = pd.Timestamp(feature_timestamps[index]).to_pydatetime()
            label_available_ts = pd.Timestamp(label_available_timestamps[index]).to_pydatetime()
            _require_aware(feature_ts, name="feature_ts")
            _require_aware(label_available_ts, name="label_available_ts")
            if previous_feature_ts is not None and feature_ts < previous_feature_ts:
                raise ValueError("materialized proxy dataset must be chronological")
            previous_feature_ts = feature_ts
            label = int(labels[index])
            if label not in {0, 1}:
                raise ValueError("materialized proxy labels must be binary")
            _, _, timestamp = sample_id.rpartition("@")
            if not timestamp.isdigit() or int(timestamp) != _ns(feature_ts):
                raise ValueError("materialized proxy sample_id timestamp must equal feature_ts")
            if source_group != group_id:
                finish_selected_group()
                if group_id in seen_groups:
                    raise ValueError("materialized proxy dataset must keep each market contiguous")
                seen_groups.add(group_id)
                source_group = group_id
                source_market_index += 1
                selected_group = group_id if source_market_index % market_stride == 0 else None
                selected_offsets = []
            if selected_group is None or (
                offsets is not None and int(elapsed[index]) not in offsets
            ):
                continue
            selected_offsets.append(int(elapsed[index]))
            rows.append(
                (
                    ResearchSample(
                        sample_id=sample_id,
                        feature_ts=feature_ts,
                        label_available_ts=label_available_ts,
                        label=label,
                        group_id=group_id,
                    ),
                    feature_matrix[index],
                )
            )
    finish_selected_group()
    if not rows:
        raise ValueError("materialized proxy dataset has no samples in the entry window")
    samples = tuple(row[0] for row in rows)
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample.group_id] = counts.get(sample.group_id, 0) + 1
    return DirectionDataset(
        samples=samples,
        vectors=np.asarray([row[1] for row in rows], dtype=float),
        schema=schema,
        sample_weights=np.asarray([1.0 / counts[sample.group_id] for sample in samples]),
    )


def _schema_from_parquet(parquet: pq.ParquetFile) -> FeatureSchema:
    ignored = {"sample_id", "feature_ts", "label_available_ts", "label", "sample_weight"}
    names = tuple(name for name in parquet.schema_arrow.names if name not in ignored)
    if not names:
        raise ValueError("materialized proxy dataset has an incompatible schema")
    return FeatureSchema(version="materialized-direction-dataset-v1", names=names)


def _normalize_offsets(values: Sequence[int] | None) -> tuple[int, ...] | None:
    if values is None:
        return None
    offsets = tuple(values)
    if not offsets or any(
        isinstance(value, bool) or not isinstance(value, Integral) for value in offsets
    ):
        raise ValueError("materialized expected offsets must be integer values")
    if tuple(sorted(set(offsets))) != offsets:
        raise ValueError("materialized expected offsets must be strictly increasing")
    return offsets


def _sample_group_id(sample_id: str) -> str:
    group_id, separator, timestamp = sample_id.rpartition("@")
    if not separator or not group_id or not timestamp.isdigit():
        raise ValueError(f"invalid materialized proxy sample_id: {sample_id!r}")
    return group_id


def _strict_integer_array(values: object, *, name: str) -> np.ndarray:
    raw = np.asarray(values)
    if (
        raw.ndim != 1
        or np.issubdtype(raw.dtype, np.bool_)
        or not (np.issubdtype(raw.dtype, np.integer) or np.issubdtype(raw.dtype, np.floating))
    ):
        raise ValueError(f"materialized proxy {name} must contain numeric integers")
    numeric = np.asarray(raw, dtype=float)
    if not np.isfinite(numeric).all() or np.any(numeric != np.floor(numeric)):
        raise ValueError(f"materialized proxy {name} must contain finite integers")
    return numeric.astype(np.int64)


def _require_aware(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"materialized proxy {name} must be timezone-aware")


def _ns(value: datetime) -> int:
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


__all__ = ["read_materialized_direction_dataset"]
