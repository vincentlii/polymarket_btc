from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.materialized_dataset import read_materialized_direction_dataset


def _write_dataset(path: Path, *, mutate: dict[str, object] | None = None) -> FeatureSchema:
    schema = FeatureSchema(version="test-v1", names=("x", "elapsed_seconds"))
    rows: list[dict[str, object]] = []
    for market_index in range(3):
        start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * market_index)
        for elapsed in (5, 10):
            feature_ts = start + timedelta(seconds=elapsed)
            rows.append(
                {
                    "sample_id": f"market-{market_index}@{int(feature_ts.timestamp() * 1_000_000_000)}",
                    "feature_ts": feature_ts,
                    "label_available_ts": start + timedelta(minutes=16),
                    "label": market_index % 2,
                    "elapsed_seconds": elapsed,
                    "x": float(market_index + elapsed),
                }
            )
    if mutate:
        rows[0].update(mutate)
    pd.DataFrame(rows).to_parquet(path, index=False)
    return schema


def test_reader_round_trips_and_strides_whole_markets(tmp_path: Path) -> None:
    path = tmp_path / "dataset.parquet"
    schema = _write_dataset(path)

    dataset = read_materialized_direction_dataset(path, expected_schema=schema, market_stride=2)

    assert [sample.group_id for sample in dataset.samples] == ["market-0"] * 2 + ["market-2"] * 2
    assert dataset.vectors[:, 0] == pytest.approx([5.0, 10.0, 7.0, 12.0])
    assert dataset.sample_weights == pytest.approx(np.full(4, 0.5))


def test_reader_rejects_schema_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "dataset.parquet"
    _write_dataset(path)

    with pytest.raises(ValueError, match="incompatible schema"):
        read_materialized_direction_dataset(
            path,
            expected_schema=FeatureSchema(version="other", names=("other",)),
        )


def test_reader_rejects_non_finite_values(tmp_path: Path) -> None:
    path = tmp_path / "dataset.parquet"
    schema = _write_dataset(path, mutate={"x": float("nan")})

    with pytest.raises(ValueError, match="finite"):
        read_materialized_direction_dataset(path, expected_schema=schema)


def test_reader_rejects_out_of_order_samples(tmp_path: Path) -> None:
    path = tmp_path / "dataset.parquet"
    schema = _write_dataset(path)
    frame = pd.read_parquet(path)
    frame.iloc[[0, 1]] = frame.iloc[[1, 0]].to_numpy()
    frame.to_parquet(path, index=False)

    with pytest.raises(ValueError, match="chronological"):
        read_materialized_direction_dataset(path, expected_schema=schema)
