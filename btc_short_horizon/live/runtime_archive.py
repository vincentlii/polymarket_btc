"""Content-addressed backup and verified restore for critical runtime artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any
from uuid import uuid4

from btc_short_horizon.data.archive import ObjectTransport
from btc_short_horizon.data.storage import canonical_json_bytes, sha256_file, write_atomic_json


RUNTIME_SNAPSHOT_SCHEMA_VERSION = "btc-runtime-backup-snapshot-v1"
RUNTIME_RECEIPT_SCHEMA_VERSION = "btc-runtime-backup-receipt-v1"
RUNTIME_RESTORE_RECEIPT_SCHEMA_VERSION = "btc-runtime-restore-receipt-v1"
REQUIRED_RUNTIME_CATEGORIES = ("ledger", "model", "report", "wal")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_NAMES = {".env", "credentials", "credentials.json", "private_key", "secrets"}
_SECRET_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_TEXT_SUFFIXES = {
    ".csv",
    ".env",
    ".html",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
_SECRET_CONTENT_MARKERS = (
    b"poly_api_secret",
    b"poly_api_passphrase",
    b"poly_private_key",
    b"private_key",
    b"seed_phrase",
    b"mnemonic",
)


class RuntimeArchiveIntegrityError(ValueError):
    """Raised when runtime backup evidence is incomplete, unsafe, or inconsistent."""


@dataclass(frozen=True, slots=True)
class RuntimeBackupFile:
    category: str
    relative_path: str
    sha256: str
    size_bytes: int
    object_key: str

    def __post_init__(self) -> None:
        _identifier(self.category, "runtime category")
        _safe_relative(self.relative_path, "runtime relative_path")
        _digest(self.sha256, "runtime file sha256")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise ValueError("runtime file size_bytes must be an integer")
        if self.size_bytes < 0:
            raise ValueError("runtime file size_bytes must be >= 0")
        _safe_relative(self.object_key, "runtime object_key")
        expected = f"runtime/objects/{self.sha256[:2]}/{self.sha256}"
        if self.object_key != expected:
            raise ValueError("runtime object_key does not match file sha256")

    def to_json(self) -> dict[str, object]:
        return {
            "category": self.category,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "object_key": self.object_key,
        }


@dataclass(frozen=True, slots=True)
class RuntimeBackupSnapshot:
    snapshot_id: str
    created_at: str
    release_revision: str
    rule_epoch: str
    files: tuple[RuntimeBackupFile, ...]
    schema_version: str = RUNTIME_SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _digest(self.snapshot_id, "snapshot_id")
        _parse_utc(self.created_at, "created_at")
        _revision(self.release_revision)
        _identifier(self.rule_epoch, "rule_epoch")
        if self.schema_version != RUNTIME_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported runtime snapshot schema")
        if not self.files or not all(isinstance(item, RuntimeBackupFile) for item in self.files):
            raise ValueError("runtime snapshot files must be a non-empty tuple")
        canonical = tuple(sorted(self.files, key=lambda item: (item.category, item.relative_path)))
        if self.files != canonical:
            raise ValueError("runtime snapshot files must be canonical")
        if len({(item.category, item.relative_path) for item in self.files}) != len(self.files):
            raise ValueError("runtime snapshot contains duplicate paths")

    @property
    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({item.category for item in self.files}))

    def to_json(self) -> dict[str, object]:
        return {"snapshot_id": self.snapshot_id, **_snapshot_payload(self)}


@dataclass(frozen=True, slots=True)
class RuntimeBackupReceipt:
    snapshot_id: str
    snapshot_sha256: str
    remote_id: str
    verified_at: str
    file_count: int
    total_bytes: int
    schema_version: str = RUNTIME_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _digest(self.snapshot_id, "snapshot_id")
        _digest(self.snapshot_sha256, "snapshot_sha256")
        _required_text(self.remote_id, "remote_id")
        _parse_utc(self.verified_at, "verified_at")
        _nonnegative_integer(self.file_count, "file_count")
        _nonnegative_integer(self.total_bytes, "total_bytes")
        if self.schema_version != RUNTIME_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported runtime receipt schema")

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "snapshot_sha256": self.snapshot_sha256,
            "remote_id": self.remote_id,
            "verified_at": self.verified_at,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class RuntimeRestoreReceipt:
    snapshot_id: str
    restored_at: str
    file_count: int
    total_bytes: int
    schema_version: str = RUNTIME_RESTORE_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _digest(self.snapshot_id, "snapshot_id")
        _parse_utc(self.restored_at, "restored_at")
        _nonnegative_integer(self.file_count, "file_count")
        _nonnegative_integer(self.total_bytes, "total_bytes")
        if self.schema_version != RUNTIME_RESTORE_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unsupported runtime restore receipt schema")

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "restored_at": self.restored_at,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class RuntimeRecoveryAudit:
    passed: bool
    reason: str
    snapshot_id: str
    categories: tuple[str, ...]
    verified_at: str | None
    restored_at: str | None


class RuntimeBackupRepository:
    """Durable local index of immutable snapshots and verification receipts."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise RuntimeArchiveIntegrityError("runtime backup repository cannot be a symlink")
        self.root = root.resolve()
        self.snapshot_root = self.root / "runtime-backup" / "snapshots"
        self.receipt_root = self.root / "runtime-backup" / "receipts"
        self.restore_root = self.root / "runtime-backup" / "restore-receipts"

    def write_snapshot(self, snapshot: RuntimeBackupSnapshot) -> Path:
        path = self.snapshot_path(snapshot.snapshot_id)
        _write_immutable_json(path, snapshot.to_json())
        return path

    def read_snapshot(self, snapshot_id: str) -> RuntimeBackupSnapshot:
        return read_runtime_snapshot(self.snapshot_path(snapshot_id))

    def snapshot_path(self, snapshot_id: str) -> Path:
        return self.snapshot_root / f"{_digest(snapshot_id, 'snapshot_id')}.json"

    def write_receipt(self, receipt: RuntimeBackupReceipt) -> Path:
        snapshot = self.read_snapshot(receipt.snapshot_id)
        if (
            receipt.snapshot_sha256 != sha256_file(self.snapshot_path(snapshot.snapshot_id))
            or receipt.file_count != len(snapshot.files)
            or receipt.total_bytes != sum(item.size_bytes for item in snapshot.files)
        ):
            raise RuntimeArchiveIntegrityError("runtime receipt does not match its snapshot")
        payload = receipt.to_json()
        digest = sha256(canonical_json_bytes(payload)).hexdigest()
        immutable = self.receipt_root / "immutable" / f"{digest}.json"
        _write_immutable_json(immutable, payload)
        write_atomic_json(
            self.receipt_root / "by-snapshot" / f"{receipt.snapshot_id}.json",
            {
                "schema_version": "btc-runtime-backup-receipt-pointer-v1",
                "receipt_sha256": digest,
                "receipt_path": immutable.relative_to(self.root).as_posix(),
            },
        )
        return immutable

    def read_receipt(self, snapshot_id: str) -> RuntimeBackupReceipt:
        snapshot_id = _digest(snapshot_id, "snapshot_id")
        pointer_path = self.receipt_root / "by-snapshot" / f"{snapshot_id}.json"
        raw = _read_json(pointer_path, "runtime receipt pointer")
        pointer = _mapping(raw, "runtime receipt pointer")
        if pointer.get("schema_version") != "btc-runtime-backup-receipt-pointer-v1":
            raise RuntimeArchiveIntegrityError("unsupported runtime receipt pointer schema")
        digest = _digest(pointer.get("receipt_sha256"), "receipt_sha256")
        relative = _safe_relative(pointer.get("receipt_path"), "receipt_path")
        candidate = self.root / relative
        if candidate.is_symlink():
            raise RuntimeArchiveIntegrityError("runtime receipt cannot be a symlink")
        path = candidate.resolve()
        if self.root not in path.parents or not path.is_file():
            raise RuntimeArchiveIntegrityError("runtime receipt pointer is invalid")
        if sha256_file(path) != digest:
            raise RuntimeArchiveIntegrityError("runtime receipt hash mismatch")
        receipt = _receipt_from_json(_read_json(path, "runtime receipt"))
        if receipt.snapshot_id != snapshot_id:
            raise RuntimeArchiveIntegrityError("runtime receipt snapshot mismatch")
        return receipt

    def write_restore_receipt(self, receipt: RuntimeRestoreReceipt) -> Path:
        snapshot = self.read_snapshot(receipt.snapshot_id)
        if receipt.file_count != len(snapshot.files) or receipt.total_bytes != sum(
            item.size_bytes for item in snapshot.files
        ):
            raise RuntimeArchiveIntegrityError(
                "runtime restore receipt does not match its snapshot"
            )
        payload = receipt.to_json()
        digest = sha256(canonical_json_bytes(payload)).hexdigest()
        path = self.restore_root / f"{digest}.json"
        _write_immutable_json(path, payload)
        write_atomic_json(
            self.restore_root / "by-snapshot" / f"{receipt.snapshot_id}.json",
            {
                "schema_version": "btc-runtime-restore-receipt-pointer-v1",
                "receipt_sha256": digest,
                "receipt_path": path.relative_to(self.root).as_posix(),
            },
        )
        return path

    def read_restore_receipt(self, snapshot_id: str) -> RuntimeRestoreReceipt:
        snapshot_id = _digest(snapshot_id, "snapshot_id")
        pointer_path = self.restore_root / "by-snapshot" / f"{snapshot_id}.json"
        pointer = _mapping(
            _read_json(pointer_path, "runtime restore pointer"),
            "runtime restore pointer",
        )
        if pointer.get("schema_version") != "btc-runtime-restore-receipt-pointer-v1":
            raise RuntimeArchiveIntegrityError("unsupported runtime restore pointer schema")
        digest = _digest(pointer.get("receipt_sha256"), "receipt_sha256")
        relative = _safe_relative(pointer.get("receipt_path"), "receipt_path")
        candidate = self.root / relative
        if candidate.is_symlink():
            raise RuntimeArchiveIntegrityError("runtime restore receipt cannot be a symlink")
        path = candidate.resolve()
        if self.root not in path.parents or not path.is_file():
            raise RuntimeArchiveIntegrityError("runtime restore pointer is invalid")
        if sha256_file(path) != digest:
            raise RuntimeArchiveIntegrityError("runtime restore receipt hash mismatch")
        receipt = _restore_receipt_from_json(_read_json(path, "runtime restore receipt"))
        if receipt.snapshot_id != snapshot_id:
            raise RuntimeArchiveIntegrityError("runtime restore receipt snapshot mismatch")
        return receipt


def create_runtime_snapshot(
    *,
    sources: Mapping[str, Path],
    repository: RuntimeBackupRepository,
    release_revision: str,
    rule_epoch: str,
    created_at: datetime | None = None,
) -> tuple[RuntimeBackupSnapshot, Path]:
    revision = _revision(release_revision)
    epoch = _identifier(rule_epoch, "rule_epoch")
    normalized = _normalize_sources(sources, repository=repository)
    files: list[RuntimeBackupFile] = []
    for category, root in normalized.items():
        category_files = _source_files(root)
        if not category_files:
            raise RuntimeArchiveIntegrityError(f"runtime source is empty: {category}")
        for path in category_files:
            relative = path.relative_to(root).as_posix()
            _reject_secret_path(relative)
            _reject_secret_content(path)
            before = path.stat()
            digest = sha256_file(path)
            after = path.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or sha256_file(path) != digest
            ):
                raise RuntimeArchiveIntegrityError(f"runtime source changed while hashing: {path}")
            files.append(
                RuntimeBackupFile(
                    category=category,
                    relative_path=relative,
                    sha256=digest,
                    size_bytes=after.st_size,
                    object_key=f"runtime/objects/{digest[:2]}/{digest}",
                )
            )
    files.sort(key=lambda item: (item.category, item.relative_path))
    timestamp = _utc_text(created_at or datetime.now(UTC), "created_at")
    provisional = RuntimeBackupSnapshot(
        snapshot_id="0" * 64,
        created_at=timestamp,
        release_revision=revision,
        rule_epoch=epoch,
        files=tuple(files),
    )
    snapshot_id = sha256(canonical_json_bytes(_snapshot_payload(provisional))).hexdigest()
    snapshot = RuntimeBackupSnapshot(
        snapshot_id=snapshot_id,
        created_at=timestamp,
        release_revision=revision,
        rule_epoch=epoch,
        files=tuple(files),
    )
    return snapshot, repository.write_snapshot(snapshot)


def upload_runtime_snapshot(
    *,
    snapshot: RuntimeBackupSnapshot,
    snapshot_path: Path,
    sources: Mapping[str, Path],
    transport: ObjectTransport,
) -> None:
    if read_runtime_snapshot(snapshot_path) != snapshot:
        raise RuntimeArchiveIntegrityError("local runtime snapshot record differs")
    normalized = _normalize_sources(sources)
    if tuple(sorted(normalized)) != snapshot.categories:
        raise RuntimeArchiveIntegrityError("runtime upload source categories differ")
    for item in snapshot.files:
        source = normalized[item.category] / _safe_relative(
            item.relative_path, "runtime relative_path"
        )
        _verify_local_file(source, item)
        transport.put(source, item.object_key)
    transport.put(snapshot_path, f"runtime/snapshots/{snapshot.snapshot_id}.json")


def verify_runtime_snapshot(
    *,
    snapshot_id: str,
    repository: RuntimeBackupRepository,
    transport: ObjectTransport,
    temporary_root: Path,
    verified_at: datetime | None = None,
) -> tuple[RuntimeBackupSnapshot, RuntimeBackupReceipt, Path]:
    snapshot, work = _download_verified_snapshot(
        snapshot_id=snapshot_id,
        transport=transport,
        temporary_root=temporary_root,
    )
    try:
        snapshot_path = repository.write_snapshot(snapshot)
        receipt = RuntimeBackupReceipt(
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=sha256_file(snapshot_path),
            remote_id=_required_text(transport.remote_id, "remote_id"),
            verified_at=_utc_text(verified_at or datetime.now(UTC), "verified_at"),
            file_count=len(snapshot.files),
            total_bytes=sum(item.size_bytes for item in snapshot.files),
        )
        receipt_path = repository.write_receipt(receipt)
        return snapshot, receipt, receipt_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restore_runtime_snapshot(
    *,
    snapshot_id: str,
    destination_root: Path,
    repository: RuntimeBackupRepository,
    transport: ObjectTransport,
    temporary_root: Path,
    restored_at: datetime | None = None,
) -> tuple[RuntimeBackupSnapshot, RuntimeRestoreReceipt, Path]:
    if destination_root.is_symlink():
        raise RuntimeArchiveIntegrityError("runtime restore destination cannot be a symlink")
    destination = destination_root.resolve()
    snapshot, work = _download_verified_snapshot(
        snapshot_id=snapshot_id,
        transport=transport,
        temporary_root=temporary_root,
    )
    try:
        object_root = work / "objects"
        targets: list[tuple[RuntimeBackupFile, Path, Path]] = []
        for item in snapshot.files:
            target = (
                destination
                / item.category
                / _safe_relative(item.relative_path, "runtime relative_path")
            )
            source = object_root / item.sha256
            _validate_restore_target(target=target, destination=destination, expected=item)
            targets.append((item, source, target))
        for item, source, target in targets:
            _install_verified_file(source=source, target=target, expected=item)
        repository.write_snapshot(snapshot)
        receipt = RuntimeRestoreReceipt(
            snapshot_id=snapshot.snapshot_id,
            restored_at=_utc_text(restored_at or datetime.now(UTC), "restored_at"),
            file_count=len(snapshot.files),
            total_bytes=sum(item.size_bytes for item in snapshot.files),
        )
        receipt_path = repository.write_restore_receipt(receipt)
        return snapshot, receipt, receipt_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def audit_runtime_recovery(
    *,
    repository: RuntimeBackupRepository,
    snapshot_id: str,
    required_categories: Sequence[str],
    now: datetime,
    maximum_receipt_age: timedelta,
    require_restore_drill: bool = False,
    maximum_restore_age: timedelta | None = None,
) -> RuntimeRecoveryAudit:
    snapshot = repository.read_snapshot(snapshot_id)
    required = tuple(
        sorted({_identifier(item, "required category") for item in required_categories})
    )
    missing = tuple(item for item in required if item not in snapshot.categories)
    if missing:
        return RuntimeRecoveryAudit(
            False,
            f"missing_categories:{','.join(missing)}",
            snapshot.snapshot_id,
            snapshot.categories,
            None,
            None,
        )
    try:
        receipt = repository.read_receipt(snapshot.snapshot_id)
    except (FileNotFoundError, RuntimeArchiveIntegrityError, ValueError):
        return RuntimeRecoveryAudit(
            False,
            "verification_receipt_missing_or_invalid",
            snapshot.snapshot_id,
            snapshot.categories,
            None,
            None,
        )
    snapshot_path = repository.snapshot_path(snapshot.snapshot_id)
    if (
        receipt.snapshot_sha256 != sha256_file(snapshot_path)
        or receipt.file_count != len(snapshot.files)
        or receipt.total_bytes != sum(item.size_bytes for item in snapshot.files)
    ):
        return RuntimeRecoveryAudit(
            False,
            "verification_receipt_snapshot_mismatch",
            snapshot.snapshot_id,
            snapshot.categories,
            receipt.verified_at,
            None,
        )
    observed = _utc_datetime(now, "now")
    verified = _parse_utc(receipt.verified_at, "verified_at")
    if maximum_receipt_age <= timedelta(0):
        raise ValueError("maximum_receipt_age must be positive")
    age = observed - verified
    if age < timedelta(0):
        reason = "verification_receipt_future_dated"
    elif age > maximum_receipt_age:
        reason = "verification_receipt_stale"
    else:
        reason = "ok"
    restored_at: str | None = None
    if reason == "ok" and require_restore_drill:
        if maximum_restore_age is None or maximum_restore_age <= timedelta(0):
            raise ValueError("maximum_restore_age must be positive when restore drill is required")
        try:
            restore_receipt = repository.read_restore_receipt(snapshot.snapshot_id)
        except (FileNotFoundError, RuntimeArchiveIntegrityError, ValueError):
            reason = "restore_receipt_missing_or_invalid"
        else:
            restored_at = restore_receipt.restored_at
            if restore_receipt.file_count != len(
                snapshot.files
            ) or restore_receipt.total_bytes != sum(item.size_bytes for item in snapshot.files):
                reason = "restore_receipt_snapshot_mismatch"
            else:
                restored = _parse_utc(restore_receipt.restored_at, "restored_at")
                restore_age = observed - restored
                if restore_age < timedelta(0):
                    reason = "restore_receipt_future_dated"
                elif restore_age > maximum_restore_age:
                    reason = "restore_receipt_stale"
    return RuntimeRecoveryAudit(
        reason == "ok",
        reason,
        snapshot.snapshot_id,
        snapshot.categories,
        receipt.verified_at,
        restored_at,
    )


def read_runtime_snapshot(path: Path) -> RuntimeBackupSnapshot:
    raw = _mapping(_read_json(path, "runtime snapshot"), "runtime snapshot")
    if raw.get("schema_version") != RUNTIME_SNAPSHOT_SCHEMA_VERSION:
        raise RuntimeArchiveIntegrityError("unsupported runtime snapshot schema")
    raw_files = raw.get("files")
    if not isinstance(raw_files, list):
        raise RuntimeArchiveIntegrityError("runtime snapshot files must be a list")
    files = tuple(_file_from_json(item) for item in raw_files)
    if tuple(sorted(files, key=lambda item: (item.category, item.relative_path))) != files:
        raise RuntimeArchiveIntegrityError("runtime snapshot files are not canonical")
    if len({(item.category, item.relative_path) for item in files}) != len(files):
        raise RuntimeArchiveIntegrityError("runtime snapshot contains duplicate paths")
    snapshot = RuntimeBackupSnapshot(
        snapshot_id=_digest(raw.get("snapshot_id"), "snapshot_id"),
        created_at=_utc_text(_parse_utc(raw.get("created_at"), "created_at"), "created_at"),
        release_revision=_revision(raw.get("release_revision")),
        rule_epoch=_identifier(raw.get("rule_epoch"), "rule_epoch"),
        files=files,
    )
    expected = sha256(canonical_json_bytes(_snapshot_payload(snapshot))).hexdigest()
    if snapshot.snapshot_id != expected:
        raise RuntimeArchiveIntegrityError("runtime snapshot ID mismatch")
    if path.read_bytes() != canonical_json_bytes(snapshot.to_json()):
        raise RuntimeArchiveIntegrityError("runtime snapshot is not canonically serialized")
    return snapshot


def _download_verified_snapshot(
    *, snapshot_id: str, transport: ObjectTransport, temporary_root: Path
) -> tuple[RuntimeBackupSnapshot, Path]:
    identifier = _digest(snapshot_id, "snapshot_id")
    root = temporary_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    work = root / f"runtime-archive-{uuid4().hex}"
    work.mkdir()
    try:
        snapshot_path = work / "snapshot.json"
        transport.get(f"runtime/snapshots/{identifier}.json", snapshot_path)
        snapshot = read_runtime_snapshot(snapshot_path)
        if snapshot.snapshot_id != identifier:
            raise RuntimeArchiveIntegrityError("remote runtime snapshot ID mismatch")
        object_root = work / "objects"
        object_root.mkdir()
        downloaded: set[str] = set()
        for item in snapshot.files:
            if item.sha256 in downloaded:
                continue
            path = object_root / item.sha256
            transport.get(item.object_key, path)
            if path.stat().st_size != item.size_bytes or sha256_file(path) != item.sha256:
                raise RuntimeArchiveIntegrityError(
                    f"runtime object hash mismatch: {item.object_key}"
                )
            downloaded.add(item.sha256)
        return snapshot, work
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _normalize_sources(
    sources: Mapping[str, Path], *, repository: RuntimeBackupRepository | None = None
) -> dict[str, Path]:
    if not sources:
        raise ValueError("at least one runtime source is required")
    normalized: dict[str, Path] = {}
    for raw_category, raw_root in sources.items():
        category = _identifier(raw_category, "runtime category")
        if category in normalized:
            raise ValueError(f"duplicate runtime category: {category}")
        if not isinstance(raw_root, Path):
            raise ValueError("runtime source roots must be Path values")
        if raw_root.is_symlink():
            raise RuntimeArchiveIntegrityError(f"runtime source cannot be a symlink: {category}")
        root = raw_root.resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"runtime source is not a directory: {root}")
        normalized[category] = root
    roots = tuple(normalized.items())
    for index, (category, root) in enumerate(roots):
        for other_category, other in roots[index + 1 :]:
            if root == other or root in other.parents or other in root.parents:
                raise RuntimeArchiveIntegrityError(
                    f"runtime source roots overlap: {category}, {other_category}"
                )
        if repository is not None and (
            repository.root == root
            or repository.root in root.parents
            or root in repository.root.parents
        ):
            raise RuntimeArchiveIntegrityError("runtime repository and source roots overlap")
    return dict(sorted(normalized.items()))


def _source_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeArchiveIntegrityError(f"runtime source contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeArchiveIntegrityError(f"runtime source contains a special file: {path}")
        if path.name.startswith(".partial-") or path.suffix.casefold() in {".tmp", ".partial"}:
            raise RuntimeArchiveIntegrityError(f"runtime source contains a partial file: {path}")
        files.append(path)
    return tuple(files)


def _reject_secret_path(relative_path: str) -> None:
    path = PurePosixPath(relative_path)
    names = {part.casefold() for part in path.parts}
    if names & _SECRET_NAMES or path.suffix.casefold() in _SECRET_SUFFIXES:
        raise RuntimeArchiveIntegrityError(
            f"secret-like runtime path is forbidden: {relative_path}"
        )


def _reject_secret_content(path: Path) -> None:
    if path.suffix.casefold() not in _TEXT_SUFFIXES:
        return
    carry = b""
    overlap = max(len(marker) for marker in _SECRET_CONTENT_MARKERS) - 1
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            observed = (carry + chunk).lower()
            if any(marker in observed for marker in _SECRET_CONTENT_MARKERS):
                raise RuntimeArchiveIntegrityError(
                    f"secret-like content is forbidden in runtime backup: {path}"
                )
            carry = observed[-overlap:]


def _verify_local_file(path: Path, expected: RuntimeBackupFile) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimeArchiveIntegrityError(f"runtime upload source is invalid: {path}")
    if path.stat().st_size != expected.size_bytes or sha256_file(path) != expected.sha256:
        raise RuntimeArchiveIntegrityError(f"runtime upload source hash mismatch: {path}")


def _install_verified_file(*, source: Path, target: Path, expected: RuntimeBackupFile) -> None:
    if target.is_symlink():
        raise RuntimeArchiveIntegrityError(f"runtime restore target is a symlink: {target}")
    if target.exists():
        if not target.is_file() or target.stat().st_size != expected.size_bytes:
            raise RuntimeArchiveIntegrityError(f"different existing runtime file: {target}")
        if sha256_file(target) != expected.sha256:
            raise RuntimeArchiveIntegrityError(f"different existing runtime file: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".partial-{uuid4().hex}")
    try:
        shutil.copyfile(source, temporary)
        _fsync_file(temporary)
        if (
            temporary.stat().st_size != expected.size_bytes
            or sha256_file(temporary) != expected.sha256
        ):
            raise RuntimeArchiveIntegrityError(f"runtime restore copy hash mismatch: {target}")
        temporary.replace(target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_restore_target(
    *, target: Path, destination: Path, expected: RuntimeBackupFile
) -> None:
    try:
        target.relative_to(destination)
    except ValueError as exc:
        raise RuntimeArchiveIntegrityError("runtime restore target escapes destination") from exc
    current = target.parent
    while current != destination:
        if current.is_symlink():
            raise RuntimeArchiveIntegrityError(f"runtime restore parent is a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise RuntimeArchiveIntegrityError(
                f"runtime restore parent is not a directory: {current}"
            )
        current = current.parent
    if target.is_symlink():
        raise RuntimeArchiveIntegrityError(f"runtime restore target is a symlink: {target}")
    if target.exists():
        if not target.is_file() or target.stat().st_size != expected.size_bytes:
            raise RuntimeArchiveIntegrityError(f"different existing runtime file: {target}")
        if sha256_file(target) != expected.sha256:
            raise RuntimeArchiveIntegrityError(f"different existing runtime file: {target}")


def _snapshot_payload(snapshot: RuntimeBackupSnapshot) -> dict[str, object]:
    return {
        "schema_version": snapshot.schema_version,
        "created_at": snapshot.created_at,
        "release_revision": snapshot.release_revision,
        "rule_epoch": snapshot.rule_epoch,
        "files": [item.to_json() for item in snapshot.files],
    }


def _file_from_json(value: object) -> RuntimeBackupFile:
    raw = _mapping(value, "runtime file")
    relative_path = _safe_relative(raw.get("relative_path"), "runtime relative_path")
    object_key = _safe_relative(raw.get("object_key"), "runtime object_key")
    return RuntimeBackupFile(
        category=_identifier(raw.get("category"), "runtime category"),
        relative_path=relative_path.as_posix(),
        sha256=_digest(raw.get("sha256"), "runtime file sha256"),
        size_bytes=_integer(raw.get("size_bytes"), "runtime file size_bytes"),
        object_key=object_key.as_posix(),
    )


def _receipt_from_json(value: object) -> RuntimeBackupReceipt:
    raw = _mapping(value, "runtime receipt")
    if raw.get("schema_version") != RUNTIME_RECEIPT_SCHEMA_VERSION:
        raise RuntimeArchiveIntegrityError("unsupported runtime receipt schema")
    return RuntimeBackupReceipt(
        snapshot_id=_digest(raw.get("snapshot_id"), "snapshot_id"),
        snapshot_sha256=_digest(raw.get("snapshot_sha256"), "snapshot_sha256"),
        remote_id=_required_text(raw.get("remote_id"), "remote_id"),
        verified_at=_utc_text(_parse_utc(raw.get("verified_at"), "verified_at"), "verified_at"),
        file_count=_integer(raw.get("file_count"), "file_count"),
        total_bytes=_integer(raw.get("total_bytes"), "total_bytes"),
    )


def _restore_receipt_from_json(value: object) -> RuntimeRestoreReceipt:
    raw = _mapping(value, "runtime restore receipt")
    if raw.get("schema_version") != RUNTIME_RESTORE_RECEIPT_SCHEMA_VERSION:
        raise RuntimeArchiveIntegrityError("unsupported runtime restore receipt schema")
    return RuntimeRestoreReceipt(
        snapshot_id=_digest(raw.get("snapshot_id"), "snapshot_id"),
        restored_at=_utc_text(_parse_utc(raw.get("restored_at"), "restored_at"), "restored_at"),
        file_count=_integer(raw.get("file_count"), "file_count"),
        total_bytes=_integer(raw.get("total_bytes"), "total_bytes"),
    )


def _write_immutable_json(path: Path, payload: Mapping[str, object]) -> None:
    expected = canonical_json_bytes(dict(payload))
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise RuntimeArchiveIntegrityError(f"immutable runtime record differs: {path}")
        return
    write_atomic_json(path, dict(payload))
    if path.read_bytes() != expected:
        raise RuntimeArchiveIntegrityError(f"runtime record serialization mismatch: {path}")


def _read_json(path: Path, name: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeArchiveIntegrityError(f"invalid {name}: {path}") from exc


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise RuntimeArchiveIntegrityError(f"{name} must be a string-keyed object")
    return value


def _safe_relative(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeArchiveIntegrityError(f"{name} must be a normalized relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise RuntimeArchiveIntegrityError(f"{name} must be a safe relative path")
    return Path(*pure.parts)


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a safe non-empty identifier")
    return value


def _revision(value: object) -> str:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise ValueError("release_revision must be a lowercase 40-character Git revision")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1_024:
        raise ValueError(f"{name} must be bounded non-empty text")
    return value.strip()


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeArchiveIntegrityError(f"{name} must be an integer >= 0")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be an integer >= 0")
    return value


def _utc_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_text(value: datetime, name: str) -> str:
    return _utc_datetime(value, name).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeArchiveIntegrityError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeArchiveIntegrityError(f"invalid {name}") from exc
    if parsed.tzinfo is None:
        raise RuntimeArchiveIntegrityError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _fsync_file(path: Path) -> None:
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
    "REQUIRED_RUNTIME_CATEGORIES",
    "RUNTIME_RECEIPT_SCHEMA_VERSION",
    "RUNTIME_RESTORE_RECEIPT_SCHEMA_VERSION",
    "RUNTIME_SNAPSHOT_SCHEMA_VERSION",
    "RuntimeArchiveIntegrityError",
    "RuntimeBackupFile",
    "RuntimeBackupReceipt",
    "RuntimeBackupRepository",
    "RuntimeBackupSnapshot",
    "RuntimeRecoveryAudit",
    "RuntimeRestoreReceipt",
    "audit_runtime_recovery",
    "create_runtime_snapshot",
    "read_runtime_snapshot",
    "restore_runtime_snapshot",
    "upload_runtime_snapshot",
    "verify_runtime_snapshot",
]
