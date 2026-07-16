from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Mapping
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9._%=-]+$")
_CONTENT_HASH_FILENAME_LENGTH = 32


@dataclass(frozen=True, slots=True)
class DataPartitionManifest:
    source: str
    instrument: str
    schema_version: str
    ingest_version: str
    data_path: str
    sha256: str
    row_count: int
    min_source_ts_ns: int | None
    max_source_ts_ns: int | None
    min_available_ts_ns: int | None
    max_available_ts_ns: int | None
    duplicate_count: int
    gap_count: int
    created_at: str
    attributes: dict[str, str]


class ImmutableParquetStore:
    """Writes atomic, content-addressed Parquet parts and adjacent manifests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(
        self,
        *,
        table: pa.Table,
        source: str,
        instrument: str,
        partition_date: str,
        partition_hour: str,
        schema_version: str,
        ingest_version: str,
        duplicate_count: int = 0,
        gap_count: int = 0,
        attributes: Mapping[str, str] | None = None,
    ) -> DataPartitionManifest:
        if table.num_rows == 0:
            raise ValueError("cannot persist an empty table")
        for value in (source, partition_date, partition_hour, schema_version, ingest_version):
            _require_safe_path_part(value)
        instrument_path = _encoded_path_part(instrument)
        if duplicate_count < 0 or gap_count < 0:
            raise ValueError("duplicate_count and gap_count must be >= 0")

        directory = (
            self.root
            / "raw"
            / source
            / instrument_path
            / f"date={partition_date}"
            / f"hour={partition_hour}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".write-{uuid4().hex}.parquet"
        final_path: Path | None = None
        manifest_path: Path | None = None
        created_final_path = False
        try:
            pq.write_table(table, temporary, compression="zstd")
            content_hash = _sha256_file(temporary)
            content_id = _content_id(content_hash)
            final_path = directory / f"part-{content_id}.parquet"
            manifest_path = directory / f"manifest-{content_id}.json"
            if final_path.exists():
                if _sha256_file(final_path) != content_hash:
                    raise FileExistsError(f"content hash prefix collision at {final_path}")
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(final_path)
                created_final_path = True
            manifest = DataPartitionManifest(
                source=source,
                instrument=instrument,
                schema_version=schema_version,
                ingest_version=ingest_version,
                data_path=str(final_path.relative_to(self.root).as_posix()),
                sha256=content_hash,
                row_count=table.num_rows,
                min_source_ts_ns=_column_min(table, "source_ts_ns"),
                max_source_ts_ns=_column_max(table, "source_ts_ns"),
                min_available_ts_ns=_column_min(table, "available_ts_ns"),
                max_available_ts_ns=_column_max(table, "available_ts_ns"),
                duplicate_count=duplicate_count,
                gap_count=gap_count,
                created_at=datetime.now(UTC).isoformat(),
                attributes=dict(attributes or {}),
            )
            if manifest_path.exists():
                existing = self.read_manifest(str(manifest_path.relative_to(self.root)))
                if existing.sha256 != content_hash:
                    raise FileExistsError(f"content hash prefix collision at {manifest_path}")
                return existing
            _atomic_write_json(manifest_path, asdict(manifest))
            return manifest
        except Exception:
            if created_final_path and manifest_path is not None and not manifest_path.exists():
                final_path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def read_manifest(self, relative_path: str) -> DataPartitionManifest:
        path = self.root / relative_path
        payload = json.loads(path.read_text(encoding="utf-8"))
        return DataPartitionManifest(**payload)


def _require_safe_path_part(value: str) -> None:
    if not value or not _SAFE_PATH_PART.fullmatch(value):
        raise ValueError(f"unsafe partition component: {value!r}")


def _encoded_path_part(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError(f"unsafe partition component: {value!r}")
    encoded = quote(
        value, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._=-"
    )
    if encoded in {".", ".."}:
        raise ValueError(f"unsafe partition component: {value!r}")
    _require_safe_path_part(encoded)
    return encoded


def _column_min(table: pa.Table, name: str) -> int | None:
    if name not in table.column_names:
        return None
    values = [value.as_py() for value in table[name] if value.is_valid]
    return int(min(values)) if values else None


def _column_max(table: pa.Table, name: str) -> int | None:
    if name not in table.column_names:
        return None
    values = [value.as_py() for value in table[name] if value.is_valid]
    return int(max(values)) if values else None


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".tmp-{uuid4().hex}")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _content_id(content_hash: str) -> str:
    return content_hash[:_CONTENT_HASH_FILENAME_LENGTH]


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
