"""Write the mandatory BTC research artifact set atomically per run."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from html import escape
import json
from math import isfinite
from pathlib import Path
import shutil
from typing import Mapping, Sequence
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True, slots=True)
class RunManifest:
    code_revision: str
    upstream_revision: str
    data_hashes: Mapping[str, str]
    model_hashes: Mapping[str, str]
    scenario: Mapping[str, object]
    seed: int


@dataclass(frozen=True, slots=True)
class BtcRunArtifacts:
    manifest: RunManifest
    data_quality: Mapping[str, object]
    predictions: Sequence[Mapping[str, object]] = ()
    opening_paths: Sequence[Mapping[str, object]] = ()
    opportunities: Sequence[Mapping[str, object]] = ()
    orders: Sequence[Mapping[str, object]] = ()
    fills: Sequence[Mapping[str, object]] = ()
    closed_trades: Sequence[Mapping[str, object]] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)


class RunArtifactWriter:
    """Creates a self-contained artifact directory without per-market HTML files."""

    _PARQUET_RECORDS = {
        "predictions.parquet": "predictions",
        "opening_paths.parquet": "opening_paths",
        "opportunities.parquet": "opportunities",
        "orders.parquet": "orders",
        "fills.parquet": "fills",
        "closed_trades.parquet": "closed_trades",
    }

    @classmethod
    def write(cls, *, directory: Path, artifacts: BtcRunArtifacts, title: str) -> None:
        if not title or not title.strip():
            raise ValueError("title is required")
        if directory.exists():
            raise FileExistsError(f"run artifact directory already exists: {directory}")
        directory.parent.mkdir(parents=True, exist_ok=True)
        staging = directory.with_name(f".{directory.name}.{uuid4().hex}.tmp")
        try:
            staging.mkdir()
            _atomic_json(staging / "run_manifest.json", artifacts.manifest)
            _atomic_json(staging / "data_quality.json", artifacts.data_quality)
            _atomic_json(staging / "metrics.json", artifacts.metrics)
            for file_name, attribute in cls._PARQUET_RECORDS.items():
                _atomic_parquet(staging / file_name, getattr(artifacts, attribute))
            _atomic_text(
                staging / "report.html",
                _html_report(
                    title=title,
                    metrics=artifacts.metrics,
                    data_quality=artifacts.data_quality,
                ),
            )
            staging.replace(directory)
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _atomic_json(path: Path, value: object) -> None:
    _atomic_text(
        path,
        json.dumps(_jsonable(value), allow_nan=False, indent=2, sort_keys=True) + "\n",
    )


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_parquet(path: Path, records: Sequence[Mapping[str, object]]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        field_names = tuple(sorted({str(key) for record in records for key in record}))
        normalized = [
            {field: _jsonable(record.get(field)) for field in field_names} for record in records
        ]
        table = pa.Table.from_pylist(normalized)
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    return value


def _html_report(
    *, title: str, metrics: Mapping[str, object], data_quality: Mapping[str, object]
) -> str:
    return """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><title>{title}</title></head>
<body><h1>{title}</h1><h2>Metrics</h2><pre>{metrics}</pre>
<h2>Data quality</h2><pre>{data_quality}</pre></body></html>
""".format(
        title=escape(title),
        metrics=escape(json.dumps(_jsonable(metrics), allow_nan=False, indent=2, sort_keys=True)),
        data_quality=escape(
            json.dumps(_jsonable(data_quality), allow_nan=False, indent=2, sort_keys=True)
        ),
    )
