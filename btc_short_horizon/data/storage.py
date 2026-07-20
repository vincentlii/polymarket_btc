from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from pathlib import PurePosixPath
import os
import re
from typing import Mapping, Protocol
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

_SAFE_PATH_PART = re.compile(r"^[A-Za-z0-9._%=-]+$")
_CONTENT_HASH_FILENAME_LENGTH = 32
_MAX_ENCODED_INSTRUMENT_PATH_LENGTH = 48
_INSTRUMENT_HASH_PREFIX = "sha256-"
_INSTRUMENT_IDENTITY_FILENAME = "_instrument.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PARTITION_DATE = re.compile(r"^date=\d{4}-\d{2}-\d{2}$")
_PARTITION_HOUR = re.compile(r"^hour=(?:[01]\d|2[0-3])$")


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

    def __post_init__(self) -> None:
        for name in ("source", "instrument", "schema_version", "ingest_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"manifest {name} must be non-empty text")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError("manifest sha256 must be a lowercase SHA-256")
        path = _safe_relative_posix_path(self.data_path, name="manifest data_path")
        if (
            len(path.parts) != 6
            or path.parts[0] != "raw"
            or path.parts[1] != self.source
            or not _PARTITION_DATE.fullmatch(path.parts[3])
            or not _PARTITION_HOUR.fullmatch(path.parts[4])
            or path.name != f"part-{self.sha256[:_CONTENT_HASH_FILENAME_LENGTH]}.parquet"
        ):
            raise ValueError("manifest data_path does not match the partition identity")
        for name, minimum in (("row_count", 1), ("duplicate_count", 0), ("gap_count", 0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"manifest {name} must be an integer >= {minimum}")
        _validate_optional_bounds(
            self.min_source_ts_ns,
            self.max_source_ts_ns,
            name="source timestamp",
        )
        _validate_optional_bounds(
            self.min_available_ts_ns,
            self.max_available_ts_ns,
            name="available timestamp",
        )
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise ValueError("manifest created_at must be a timezone-aware timestamp")
        try:
            created_at = datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise ValueError("manifest created_at must be ISO-8601") from exc
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("manifest created_at must be timezone-aware")
        if not isinstance(self.attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.attributes.items()
        ):
            raise ValueError("manifest attributes must contain string pairs")


class PartWriteInventory(Protocol):
    """Transactional inventory hook for one immutable part/manifest pair."""

    def prepare_part(
        self,
        *,
        manifest_path: str,
        manifest: DataPartitionManifest,
    ) -> DataPartitionManifest: ...

    def commit_part(
        self,
        *,
        manifest_path: str,
        manifest_sha256: str,
    ) -> None: ...


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
        inventory: PartWriteInventory | None = None,
    ) -> DataPartitionManifest:
        if table.num_rows == 0:
            raise ValueError("cannot persist an empty table")
        for value in (source, partition_date, partition_hour, schema_version, ingest_version):
            _require_safe_path_part(value)
        instrument_path = instrument_directory_name(instrument)
        if duplicate_count < 0 or gap_count < 0:
            raise ValueError("duplicate_count and gap_count must be >= 0")

        instrument_directory = self.root / "raw" / source / instrument_path
        _ensure_instrument_identity(instrument_directory, instrument)
        directory = instrument_directory / f"date={partition_date}" / f"hour={partition_hour}"
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".write-{uuid4().hex}.parquet"
        final_path: Path | None = None
        manifest_path: Path | None = None
        created_final_path = False
        try:
            pq.write_table(table, temporary, compression="zstd")
            _fsync_file(temporary)
            content_hash = sha256_file(temporary)
            content_id = _content_id(content_hash)
            final_path = directory / f"part-{content_id}.parquet"
            manifest_path = directory / f"manifest-{content_id}.json"
            relative_manifest_path = manifest_path.relative_to(self.root).as_posix()
            if manifest_path.exists() and not final_path.is_file():
                raise FileNotFoundError(f"manifest references a missing part at {manifest_path}")
            if final_path.exists():
                if sha256_file(final_path) != content_hash:
                    raise FileExistsError(f"content hash prefix collision at {final_path}")
            candidate = DataPartitionManifest(
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
                existing = self.read_manifest(manifest_path.relative_to(self.root).as_posix())
                if _manifest_storage_identity(existing) != _manifest_storage_identity(candidate):
                    raise FileExistsError(f"content hash prefix collision at {manifest_path}")
                candidate = existing
            manifest = (
                candidate
                if inventory is None
                else inventory.prepare_part(
                    manifest_path=relative_manifest_path,
                    manifest=candidate,
                )
            )
            if _manifest_storage_identity(manifest) != _manifest_storage_identity(candidate):
                raise RuntimeError("part inventory changed immutable manifest fields")
            expected_manifest_sha256 = sha256(canonical_json_bytes(asdict(manifest))).hexdigest()

            if final_path.exists():
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(final_path)
                created_final_path = True
                _fsync_directory(directory)
            if manifest_path.exists():
                existing = self.read_manifest(relative_manifest_path)
                if existing != manifest or sha256_file(manifest_path) != expected_manifest_sha256:
                    raise FileExistsError(f"manifest content mismatch at {manifest_path}")
            else:
                write_atomic_json(manifest_path, asdict(manifest))
            if inventory is not None:
                inventory.commit_part(
                    manifest_path=relative_manifest_path,
                    manifest_sha256=expected_manifest_sha256,
                )
            return manifest
        except Exception:
            if created_final_path and manifest_path is not None and not manifest_path.exists():
                final_path.unlink(missing_ok=True)
                _fsync_directory(final_path.parent)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def read_manifest(self, relative_path: str) -> DataPartitionManifest:
        relative = _safe_relative_posix_path(relative_path, name="manifest path")
        root = self.root.resolve()
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("manifest path resolves outside the storage root") from exc
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("manifest payload must be an object")
        manifest = DataPartitionManifest(**payload)
        if manifest_relative_path(manifest) != relative.as_posix():
            raise ValueError("manifest path does not match its data_path")
        return manifest


def _require_safe_path_part(value: str) -> None:
    if not value or value in {".", ".."} or not _SAFE_PATH_PART.fullmatch(value):
        raise ValueError(f"unsafe partition component: {value!r}")


def _safe_relative_posix_path(value: object, *, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if value != value.strip() or "\\" in value or ":" in value or "\0" in value:
        raise ValueError(f"{name} must be a canonical relative POSIX path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"{name} must be a safe relative POSIX path")
    path = Path(*pure.parts)
    if path.is_absolute() or path.drive:
        raise ValueError(f"{name} must stay relative on this platform")
    return path


def _validate_optional_bounds(
    minimum: int | None,
    maximum: int | None,
    *,
    name: str,
) -> None:
    if (minimum is None) != (maximum is None):
        raise ValueError(f"manifest {name} bounds must both be present or absent")
    if minimum is None:
        return
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum < 0
        or maximum < minimum
    ):
        raise ValueError(f"manifest {name} bounds are invalid")


def instrument_directory_name(value: str) -> str:
    """Return the bounded, deterministic directory component for an instrument."""
    if not value or "\x00" in value:
        raise ValueError(f"unsafe partition component: {value!r}")
    encoded = quote(
        value, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._=-"
    )
    if encoded in {".", ".."}:
        raise ValueError(f"unsafe partition component: {value!r}")
    _require_safe_path_part(encoded)
    if len(encoded) > _MAX_ENCODED_INSTRUMENT_PATH_LENGTH:
        return f"{_INSTRUMENT_HASH_PREFIX}{_instrument_digest(value)[:32]}"
    return encoded


def _ensure_instrument_identity(directory: Path, instrument: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    identity_path = directory / _INSTRUMENT_IDENTITY_FILENAME
    expected = {
        "instrument": instrument,
        "sha256": _instrument_digest(instrument),
    }
    if identity_path.exists():
        _validate_instrument_identity(identity_path, expected)
        return
    temporary = directory / f".instrument-{uuid4().hex}.json"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(expected, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, identity_path)
        _fsync_directory(directory)
    except FileExistsError:
        _validate_instrument_identity(identity_path, expected)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_instrument_identity(path: Path, expected: dict[str, str]) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FileExistsError(f"invalid instrument identity at {path}") from exc
    if existing != expected:
        raise FileExistsError(f"instrument path collision at {path}")


def _instrument_digest(instrument: str) -> str:
    return sha256(instrument.encode("utf-8")).hexdigest()


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


def canonical_json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def write_atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".tmp-{uuid4().hex}")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _content_id(content_hash: str) -> str:
    return content_hash[:_CONTENT_HASH_FILENAME_LENGTH]


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_relative_path(manifest: DataPartitionManifest) -> str:
    path = PurePosixPath(manifest.data_path)
    return str(path.with_name(f"manifest-{_content_id(manifest.sha256)}.json"))


def _manifest_storage_identity(manifest: DataPartitionManifest) -> tuple[object, ...]:
    return (
        manifest.source,
        manifest.instrument,
        manifest.schema_version,
        manifest.ingest_version,
        manifest.data_path,
        manifest.sha256,
        manifest.row_count,
        manifest.min_source_ts_ns,
        manifest.max_source_ts_ns,
        manifest.min_available_ts_ns,
        manifest.max_available_ts_ns,
        manifest.duplicate_count,
        manifest.gap_count,
        tuple(sorted(manifest.attributes.items())),
    )


def _fsync_file(path: Path) -> None:
    # Windows rejects fsync on a read-only descriptor even though POSIX accepts it.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DataPartitionManifest",
    "ImmutableParquetStore",
    "PartWriteInventory",
    "canonical_json_bytes",
    "instrument_directory_name",
    "manifest_relative_path",
    "sha256_file",
    "write_atomic_json",
]
