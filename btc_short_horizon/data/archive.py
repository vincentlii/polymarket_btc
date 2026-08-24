"""Content-addressed backup and verified restore for completed collector sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from typing import Callable, Protocol, Sequence
from uuid import uuid4

from btc_short_horizon.data.session_inventory import (
    SESSION_ARCHIVE_MARKER_SCHEMA_VERSION,
    SESSION_STATUS_COMPLETE,
    CollectorSessionRecord,
    CollectorStorageLease,
    SessionInventoryError,
    SessionInventoryRepository,
    normalize_session_id,
)
from btc_short_horizon.data.storage import canonical_json_bytes, sha256_file, write_atomic_json


BACKUP_SNAPSHOT_SCHEMA_VERSION = "btc-backup-snapshot-v1"
BACKUP_RECEIPT_SCHEMA_VERSION = "btc-backup-receipt-v1"
_RAW_PART_FILE = re.compile(r"^(?:part-[0-9a-f]{32}\.parquet|manifest-[0-9a-f]{32}\.json)$")
_PARTITION_DATE = re.compile(r"^date=\d{4}-\d{2}-\d{2}$")
_PARTITION_HOUR = re.compile(r"^hour=(?:[01]\d|2[0-3])$")


class BackupIntegrityError(ValueError):
    """Raised when a local or remote backup cannot prove exact content integrity."""


@dataclass(frozen=True, slots=True)
class BackupFile:
    relative_path: str
    sha256: str
    size_bytes: int
    object_key: str


@dataclass(frozen=True, slots=True)
class BackupSnapshot:
    snapshot_id: str
    created_at: str
    session_ids: tuple[str, ...]
    files: tuple[BackupFile, ...]
    schema_version: str = BACKUP_SNAPSHOT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    snapshot_id: str
    snapshot_sha256: str
    remote_id: str
    verified_at: str
    file_count: int
    total_bytes: int
    schema_version: str = BACKUP_RECEIPT_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class LocalArchivePlan:
    snapshot_id: str
    session_ids: tuple[str, ...]
    file_count: int
    byte_count: int
    applied: bool


class ObjectTransport(Protocol):
    @property
    def remote_id(self) -> str: ...

    def put(self, local_path: Path, object_key: str) -> None: ...

    def get(self, object_key: str, local_path: Path) -> None: ...


class LocalObjectTransport:
    """Filesystem transport used for mounted cloud disks and deterministic tests."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @property
    def remote_id(self) -> str:
        return f"local:{self.root.as_posix()}"

    def put(self, local_path: Path, object_key: str) -> None:
        destination = self.root / _safe_relative(object_key, "object_key")
        if destination.exists():
            if not destination.is_file() or sha256_file(destination) != sha256_file(local_path):
                raise BackupIntegrityError(f"immutable backup object differs: {object_key}")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".partial-{uuid4().hex}")
        try:
            shutil.copyfile(local_path, temporary)
            _fsync_file(temporary)
            if sha256_file(temporary) != sha256_file(local_path):
                raise BackupIntegrityError(f"backup copy hash mismatch: {object_key}")
            temporary.replace(destination)
            _fsync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, object_key: str, local_path: Path) -> None:
        source = self.root / _safe_relative(object_key, "object_key")
        if not source.is_file():
            raise FileNotFoundError(f"backup object does not exist: {object_key}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = local_path.with_name(f".partial-{uuid4().hex}")
        try:
            shutil.copyfile(source, temporary)
            _fsync_file(temporary)
            temporary.replace(local_path)
            _fsync_directory(local_path.parent)
        finally:
            temporary.unlink(missing_ok=True)


class RcloneObjectTransport:
    """Vendor-neutral object transport using rclone without a shell."""

    def __init__(
        self,
        remote_root: str,
        *,
        executable: str = "rclone",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.remote_root = remote_root.strip().rstrip("/")
        if not self.remote_root or ":" not in self.remote_root:
            raise ValueError("rclone remote_root must use the configured remote:path form")
        if not executable.strip():
            raise ValueError("rclone executable is required")
        self.executable = executable.strip()
        self._runner = runner

    @property
    def remote_id(self) -> str:
        return f"rclone:{self.remote_root}"

    def put(self, local_path: Path, object_key: str) -> None:
        self._run(
            "copyto",
            str(local_path),
            self._remote_path(object_key),
            "--immutable",
            "--checksum",
            "--retries",
            "3",
        )

    def get(self, object_key: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            "copyto",
            self._remote_path(object_key),
            str(local_path),
            "--checksum",
            "--retries",
            "3",
        )

    def _remote_path(self, object_key: str) -> str:
        normalized = _safe_relative(object_key, "object_key").as_posix()
        return f"{self.remote_root}/{normalized}"

    def _run(self, *arguments: str) -> None:
        try:
            self._runner(
                [self.executable, *arguments],
                check=True,
                capture_output=True,
                text=True,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"rclone executable is unavailable: {self.executable}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "rclone failed").strip()
            raise RuntimeError(detail) from exc


def create_backup_snapshot(
    *,
    raw_data_root: Path,
    session_ids: Sequence[str] | None = None,
    created_at: datetime | None = None,
) -> tuple[BackupSnapshot, Path]:
    root = raw_data_root.resolve()
    repository = SessionInventoryRepository(root)
    sessions = repository.read_all()
    selected = _select_complete_sessions(sessions, session_ids=session_ids)
    paths: set[Path] = set()
    for session in selected:
        for path in (
            repository.registry_path(session.session_id),
            repository.inventory_path(session.session_id),
        ):
            if not path.is_file():
                raise BackupIntegrityError(f"backup source is missing: {path}")
            paths.add(path)
        for part in session.parts:
            repository.verify_part(part)
            data_path = root / _safe_relative(part.manifest.data_path, "data_path")
            manifest_path = root / _safe_relative(part.manifest_path, "manifest_path")
            paths.update((data_path, manifest_path))
            identity_path = data_path.parents[2] / "_instrument.json"
            if not identity_path.is_file():
                raise BackupIntegrityError(f"instrument identity is missing: {identity_path}")
            paths.add(identity_path)
    files = tuple(
        sorted(
            (_backup_file(root=root, path=path) for path in paths),
            key=lambda item: item.relative_path,
        )
    )
    timestamp = _utc_text(created_at or datetime.now(UTC))
    core = {
        "schema_version": BACKUP_SNAPSHOT_SCHEMA_VERSION,
        "created_at": timestamp,
        "session_ids": [session.session_id for session in selected],
        "files": [asdict(item) for item in files],
    }
    snapshot_id = sha256(canonical_json_bytes(core)).hexdigest()
    snapshot = BackupSnapshot(
        snapshot_id=snapshot_id,
        created_at=timestamp,
        session_ids=tuple(session.session_id for session in selected),
        files=files,
    )
    snapshot_path = root / "inventory" / "backup-snapshots" / f"{snapshot_id}.json"
    if snapshot_path.exists():
        existing = read_backup_snapshot(snapshot_path)
        if existing != snapshot:
            raise BackupIntegrityError(f"backup snapshot id collision: {snapshot_path}")
    else:
        write_atomic_json(snapshot_path, _snapshot_payload(snapshot))
    return snapshot, snapshot_path


def upload_backup_snapshot(
    *,
    raw_data_root: Path,
    snapshot: BackupSnapshot,
    snapshot_path: Path,
    transport: ObjectTransport,
) -> None:
    root = raw_data_root.resolve()
    if read_backup_snapshot(snapshot_path) != snapshot:
        raise BackupIntegrityError("local backup snapshot does not match the requested upload")
    for item in snapshot.files:
        source = root / _safe_relative(item.relative_path, "relative_path")
        _verify_local_backup_file(source, item)
        transport.put(source, item.object_key)
    transport.put(snapshot_path, _snapshot_object_key(snapshot.snapshot_id))


def verify_remote_snapshot(
    *,
    raw_data_root: Path,
    snapshot_id: str,
    transport: ObjectTransport,
    temporary_root: Path,
) -> tuple[BackupSnapshot, BackupReceipt, Path]:
    _require_sha256(snapshot_id, "snapshot_id")
    work = temporary_root.resolve() / f"btc-backup-verify-{uuid4().hex}"
    work.mkdir(parents=True, exist_ok=False)
    try:
        snapshot_path = work / "snapshot.json"
        transport.get(_snapshot_object_key(snapshot_id), snapshot_path)
        snapshot = read_backup_snapshot(snapshot_path)
        if snapshot.snapshot_id != snapshot_id:
            raise BackupIntegrityError("remote snapshot id does not match its object key")
        for index, item in enumerate(snapshot.files):
            downloaded = work / "objects" / str(index)
            transport.get(item.object_key, downloaded)
            _verify_local_backup_file(downloaded, item)
        persisted_snapshot_path = _persist_verified_snapshot(
            raw_data_root.resolve(),
            downloaded_path=snapshot_path,
            snapshot=snapshot,
        )
        receipt = BackupReceipt(
            snapshot_id=snapshot_id,
            snapshot_sha256=sha256_file(persisted_snapshot_path),
            remote_id=transport.remote_id,
            verified_at=_utc_text(datetime.now(UTC)),
            file_count=len(snapshot.files),
            total_bytes=sum(item.size_bytes for item in snapshot.files),
        )
        receipt_id = sha256(
            canonical_json_bytes(
                {
                    "snapshot_id": receipt.snapshot_id,
                    "remote_id": receipt.remote_id,
                }
            )
        ).hexdigest()[:32]
        receipt_path = (
            raw_data_root.resolve()
            / "inventory"
            / "backup-receipts"
            / f"{snapshot_id}-{receipt_id}.json"
        )
        write_atomic_json(receipt_path, asdict(receipt))
        return snapshot, receipt, receipt_path
    finally:
        shutil.rmtree(work, ignore_errors=True)


def restore_remote_snapshot(
    *,
    destination_root: Path,
    snapshot_id: str,
    transport: ObjectTransport,
    temporary_root: Path,
) -> BackupSnapshot:
    """Restore while holding the same exclusive lease used by the collector."""

    destination = destination_root.resolve()
    with CollectorStorageLease(
        destination,
        owner={"command": "restore_remote_snapshot"},
    ):
        return _restore_remote_snapshot_unlocked(
            destination_root=destination,
            snapshot_id=snapshot_id,
            transport=transport,
            temporary_root=temporary_root,
        )


def _restore_remote_snapshot_unlocked(
    *,
    destination_root: Path,
    snapshot_id: str,
    transport: ObjectTransport,
    temporary_root: Path,
) -> BackupSnapshot:
    _require_sha256(snapshot_id, "snapshot_id")
    destination = destination_root.resolve()
    work = temporary_root.resolve() / f"btc-backup-restore-{uuid4().hex}"
    work.mkdir(parents=True, exist_ok=False)
    try:
        snapshot_path = work / "snapshot.json"
        transport.get(_snapshot_object_key(snapshot_id), snapshot_path)
        snapshot = read_backup_snapshot(snapshot_path)
        if snapshot.snapshot_id != snapshot_id:
            raise BackupIntegrityError("remote snapshot id does not match its object key")
        downloaded: dict[str, Path] = {}
        for index, item in enumerate(snapshot.files):
            path = work / "objects" / str(index)
            transport.get(item.object_key, path)
            _verify_local_backup_file(path, item)
            downloaded[item.relative_path] = path
        for item in sorted(snapshot.files, key=_restore_order):
            target = destination / _safe_relative(item.relative_path, "relative_path")
            if target.exists():
                _verify_local_backup_file(target, item)
                continue
            _install_verified_file(
                downloaded[item.relative_path],
                target=target,
                expected=item,
            )
        repository = SessionInventoryRepository(destination)
        for session_id in snapshot.session_ids:
            repository.archive_marker_path(session_id).unlink(missing_ok=True)
        repository.audit(
            allow_open_sessions=True,
            allow_failed_sessions=True,
        )
        _persist_verified_snapshot(
            destination,
            downloaded_path=snapshot_path,
            snapshot=snapshot,
        )
        return snapshot
    finally:
        shutil.rmtree(work, ignore_errors=True)


def archive_verified_sessions_locally(
    *,
    raw_data_root: Path,
    receipt_path: Path,
    session_ids: Sequence[str] | None = None,
    completed_before: datetime | None = None,
    apply: bool = False,
) -> LocalArchivePlan:
    """Preview safely, or archive while excluding the live collector/restore path."""

    if not apply:
        return _archive_verified_sessions_locally_unlocked(
            raw_data_root=raw_data_root,
            receipt_path=receipt_path,
            session_ids=session_ids,
            completed_before=completed_before,
            apply=False,
        )
    root = raw_data_root.resolve()
    with CollectorStorageLease(
        root,
        owner={"command": "archive_verified_sessions_locally"},
    ):
        return _archive_verified_sessions_locally_unlocked(
            raw_data_root=root,
            receipt_path=receipt_path,
            session_ids=session_ids,
            completed_before=completed_before,
            apply=True,
        )


def _archive_verified_sessions_locally_unlocked(
    *,
    raw_data_root: Path,
    receipt_path: Path,
    session_ids: Sequence[str] | None = None,
    completed_before: datetime | None = None,
    apply: bool = False,
) -> LocalArchivePlan:
    """Delete only local immutable pairs already covered by a full-download receipt."""

    root = raw_data_root.resolve()
    receipt = read_backup_receipt(receipt_path)
    snapshot_path = root / "inventory" / "backup-snapshots" / f"{receipt.snapshot_id}.json"
    if not snapshot_path.is_file() or sha256_file(snapshot_path) != receipt.snapshot_sha256:
        raise BackupIntegrityError("backup receipt does not match the local snapshot")
    snapshot = read_backup_snapshot(snapshot_path)
    if receipt.file_count != len(snapshot.files) or receipt.total_bytes != sum(
        item.size_bytes for item in snapshot.files
    ):
        raise BackupIntegrityError("backup receipt counts do not match the local snapshot")
    repository = SessionInventoryRepository(root)
    repository.audit(allow_open_sessions=True, allow_failed_sessions=True)
    by_id = {session.session_id: session for session in repository.read_all()}
    selected_ids = (
        tuple(snapshot.session_ids)
        if session_ids is None
        else tuple(sorted({normalize_session_id(value) for value in session_ids}))
    )
    if not selected_ids or any(
        session_id not in snapshot.session_ids for session_id in selected_ids
    ):
        raise BackupIntegrityError("archive sessions must be covered by the verified snapshot")
    cutoff = None if completed_before is None else _utc_datetime(completed_before)
    snapshot_files = {item.relative_path: item for item in snapshot.files}
    paths_by_session: dict[str, tuple[BackupFile, ...]] = {}
    for session_id in selected_ids:
        session = by_id.get(session_id)
        if session is None or session.status != SESSION_STATUS_COMPLETE:
            raise SessionInventoryError(f"archive session is not complete: {session_id}")
        if cutoff is not None and (
            session.completed_at is None
            or _parse_timestamp(session.completed_at).astimezone(UTC) >= cutoff
        ):
            raise BackupIntegrityError(f"archive session is newer than the cutoff: {session_id}")
        files: list[BackupFile] = []
        for part in session.parts:
            expected = {
                part.manifest_path: part.manifest_sha256,
                part.manifest.data_path: part.manifest.sha256,
            }
            for relative_path, digest in expected.items():
                item = snapshot_files.get(relative_path)
                if item is None or item.sha256 != digest:
                    raise BackupIntegrityError(
                        f"verified snapshot does not cover session file: {relative_path}"
                    )
                files.append(item)
        paths_by_session[session_id] = tuple(
            sorted(
                {item.relative_path: item for item in files}.values(), key=lambda x: x.relative_path
            )
        )
    plan_files = {item.relative_path: item for files in paths_by_session.values() for item in files}
    if not apply:
        return LocalArchivePlan(
            snapshot_id=snapshot.snapshot_id,
            session_ids=selected_ids,
            file_count=len(plan_files),
            byte_count=sum(item.size_bytes for item in plan_files.values()),
            applied=False,
        )
    for session_id in selected_ids:
        marker_path = repository.archive_marker_path(session_id)
        marker = {
            "schema_version": SESSION_ARCHIVE_MARKER_SCHEMA_VERSION,
            "session_id": session_id,
            "snapshot_id": snapshot.snapshot_id,
            "remote_id": receipt.remote_id,
            "archived_at": _utc_text(datetime.now(UTC)),
            "files": [
                {
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
                for item in paths_by_session[session_id]
            ],
        }
        if marker_path.exists():
            existing = repository.read_archive_marker(session_id)
            comparable_keys = {
                "schema_version",
                "session_id",
                "snapshot_id",
                "remote_id",
                "files",
            }
            if {key: existing[key] for key in comparable_keys} != {
                key: marker[key] for key in comparable_keys
            }:
                raise BackupIntegrityError(f"archive marker conflicts: {marker_path}")
        else:
            for item in paths_by_session[session_id]:
                _verify_local_backup_file(
                    root / _safe_relative(item.relative_path, "relative_path"),
                    item,
                )
            write_atomic_json(marker_path, marker)
        for item in paths_by_session[session_id]:
            path = root / _safe_relative(item.relative_path, "relative_path")
            if path.exists():
                _verify_local_backup_file(path, item)
                path.unlink()
    repository.audit(allow_open_sessions=True, allow_failed_sessions=True)
    return LocalArchivePlan(
        snapshot_id=snapshot.snapshot_id,
        session_ids=selected_ids,
        file_count=len(plan_files),
        byte_count=sum(item.size_bytes for item in plan_files.values()),
        applied=True,
    )


def read_backup_snapshot(path: Path) -> BackupSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError(f"cannot read backup snapshot: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "snapshot_id",
        "created_at",
        "session_ids",
        "files",
    }:
        raise BackupIntegrityError("invalid backup snapshot schema")
    if payload["schema_version"] != BACKUP_SNAPSHOT_SCHEMA_VERSION:
        raise BackupIntegrityError("unsupported backup snapshot version")
    snapshot_id = _require_sha256(payload["snapshot_id"], "snapshot_id")
    created_at = _utc_text(_parse_timestamp(payload["created_at"]))
    session_ids_value = payload["session_ids"]
    if not isinstance(session_ids_value, list) or any(
        not isinstance(value, str) or not value for value in session_ids_value
    ):
        raise BackupIntegrityError("backup session_ids must be a string list")
    try:
        session_ids = tuple(normalize_session_id(value) for value in session_ids_value)
    except SessionInventoryError as exc:
        raise BackupIntegrityError("backup session_ids contain an invalid id") from exc
    if session_ids != tuple(session_ids_value):
        raise BackupIntegrityError("backup session_ids must use canonical ids")
    if not session_ids:
        raise BackupIntegrityError("backup snapshot must contain at least one session")
    if session_ids != tuple(sorted(set(session_ids))):
        raise BackupIntegrityError("backup session_ids must be uniquely sorted")
    files_value = payload["files"]
    if not isinstance(files_value, list):
        raise BackupIntegrityError("backup files must be a list")
    files = tuple(_read_backup_file(value) for value in files_value)
    paths = tuple(item.relative_path for item in files)
    if paths != tuple(sorted(set(paths))):
        raise BackupIntegrityError("backup files must be uniquely sorted")
    _validate_snapshot_scope(session_ids=session_ids, files=files)
    snapshot = BackupSnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at,
        session_ids=session_ids,
        files=files,
    )
    core = {
        "schema_version": snapshot.schema_version,
        "created_at": snapshot.created_at,
        "session_ids": list(snapshot.session_ids),
        "files": [asdict(item) for item in snapshot.files],
    }
    if sha256(canonical_json_bytes(core)).hexdigest() != snapshot.snapshot_id:
        raise BackupIntegrityError("backup snapshot content hash does not match snapshot_id")
    return snapshot


def read_backup_receipt(path: Path) -> BackupReceipt:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError(f"cannot read backup receipt: {path}") from exc
    expected_keys = {
        "schema_version",
        "snapshot_id",
        "snapshot_sha256",
        "remote_id",
        "verified_at",
        "file_count",
        "total_bytes",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise BackupIntegrityError("invalid backup receipt schema")
    if payload["schema_version"] != BACKUP_RECEIPT_SCHEMA_VERSION:
        raise BackupIntegrityError("unsupported backup receipt version")
    snapshot_id = _require_sha256(payload["snapshot_id"], "snapshot_id")
    snapshot_sha256 = _require_sha256(payload["snapshot_sha256"], "snapshot_sha256")
    remote_id = payload["remote_id"]
    if not isinstance(remote_id, str) or not remote_id.strip():
        raise BackupIntegrityError("backup receipt remote_id is required")
    verified_at = _utc_text(_parse_timestamp(payload["verified_at"]))
    file_count = _nonnegative_int(payload["file_count"], "file_count")
    total_bytes = _nonnegative_int(payload["total_bytes"], "total_bytes")
    return BackupReceipt(
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha256,
        remote_id=remote_id,
        verified_at=verified_at,
        file_count=file_count,
        total_bytes=total_bytes,
    )


def _select_complete_sessions(
    sessions: Sequence[CollectorSessionRecord],
    *,
    session_ids: Sequence[str] | None,
) -> tuple[CollectorSessionRecord, ...]:
    by_id = {session.session_id: session for session in sessions}
    if session_ids is None:
        selected = tuple(
            session for session in sessions if session.status == SESSION_STATUS_COMPLETE
        )
    else:
        requested = tuple(sorted(set(session_ids)))
        missing = tuple(session_id for session_id in requested if session_id not in by_id)
        if missing:
            raise KeyError(f"unknown collector sessions: {missing}")
        selected = tuple(by_id[session_id] for session_id in requested)
    if not selected:
        raise BackupIntegrityError("backup requires at least one complete collector session")
    incomplete = tuple(
        session.session_id for session in selected if session.status != SESSION_STATUS_COMPLETE
    )
    if incomplete:
        raise SessionInventoryError(f"backup sessions are not complete: {incomplete}")
    return tuple(sorted(selected, key=lambda item: item.session_id))


def _backup_file(*, root: Path, path: Path) -> BackupFile:
    relative = path.resolve().relative_to(root).as_posix()
    digest = sha256_file(path)
    return BackupFile(
        relative_path=relative,
        sha256=digest,
        size_bytes=path.stat().st_size,
        object_key=f"objects/{digest[:2]}/{digest}",
    )


def _read_backup_file(value: object) -> BackupFile:
    if not isinstance(value, dict) or set(value) != {
        "relative_path",
        "sha256",
        "size_bytes",
        "object_key",
    }:
        raise BackupIntegrityError("invalid backup file schema")
    relative_path = _safe_relative(value["relative_path"], "relative_path").as_posix()
    digest = _require_sha256(value["sha256"], "sha256")
    size = value["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise BackupIntegrityError("backup file size_bytes must be non-negative")
    object_key = _safe_relative(value["object_key"], "object_key").as_posix()
    if object_key != f"objects/{digest[:2]}/{digest}":
        raise BackupIntegrityError("backup object key is not content-addressed")
    return BackupFile(
        relative_path=relative_path,
        sha256=digest,
        size_bytes=size,
        object_key=object_key,
    )


def _validate_snapshot_scope(
    *,
    session_ids: tuple[str, ...],
    files: tuple[BackupFile, ...],
) -> None:
    expected_inventory_paths = {
        *(f"inventory/session-registry/{session_id}.json" for session_id in session_ids),
        *(f"inventory/sessions/{session_id}.json" for session_id in session_ids),
    }
    observed_inventory_paths: set[str] = set()
    for item in files:
        pure = PurePosixPath(item.relative_path)
        parts = pure.parts
        if item.relative_path in expected_inventory_paths:
            observed_inventory_paths.add(item.relative_path)
            continue
        if len(parts) == 4 and parts[0] == "raw" and parts[-1] == "_instrument.json":
            continue
        if (
            len(parts) == 6
            and parts[0] == "raw"
            and _PARTITION_DATE.fullmatch(parts[3])
            and _PARTITION_HOUR.fullmatch(parts[4])
            and _RAW_PART_FILE.fullmatch(parts[5])
        ):
            continue
        raise BackupIntegrityError(
            f"backup snapshot contains an out-of-scope path: {item.relative_path}"
        )
    missing_inventory = expected_inventory_paths - observed_inventory_paths
    if missing_inventory:
        raise BackupIntegrityError(
            "backup snapshot omits session inventory files: " + ", ".join(sorted(missing_inventory))
        )


def _snapshot_payload(snapshot: BackupSnapshot) -> dict[str, object]:
    return {
        "schema_version": snapshot.schema_version,
        "snapshot_id": snapshot.snapshot_id,
        "created_at": snapshot.created_at,
        "session_ids": list(snapshot.session_ids),
        "files": [asdict(item) for item in snapshot.files],
    }


def _persist_verified_snapshot(
    root: Path,
    *,
    downloaded_path: Path,
    snapshot: BackupSnapshot,
) -> Path:
    target = root / "inventory" / "backup-snapshots" / f"{snapshot.snapshot_id}.json"
    if target.exists():
        if read_backup_snapshot(target) != snapshot:
            raise BackupIntegrityError(f"local backup snapshot conflicts: {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".partial-{uuid4().hex}")
    try:
        shutil.copyfile(downloaded_path, temporary)
        _fsync_file(temporary)
        if read_backup_snapshot(temporary) != snapshot:
            raise BackupIntegrityError("downloaded backup snapshot changed before persistence")
        temporary.replace(target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _install_verified_file(
    source: Path,
    *,
    target: Path,
    expected: BackupFile,
) -> None:
    """Copy through the destination filesystem so cross-volume restore stays atomic."""

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".partial-{uuid4().hex}")
    try:
        shutil.copyfile(source, temporary)
        _fsync_file(temporary)
        _verify_local_backup_file(temporary, expected)
        if target.exists():
            _verify_local_backup_file(target, expected)
            return
        temporary.replace(target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot_object_key(snapshot_id: str) -> str:
    return f"snapshots/{snapshot_id}.json"


def _verify_local_backup_file(path: Path, expected: BackupFile) -> None:
    if not path.is_file():
        raise BackupIntegrityError(f"backup file is missing: {expected.relative_path}")
    if path.stat().st_size != expected.size_bytes or sha256_file(path) != expected.sha256:
        raise BackupIntegrityError(f"backup file integrity mismatch: {expected.relative_path}")


def _restore_order(item: BackupFile) -> tuple[int, str]:
    if item.relative_path.startswith("inventory/session-registry/"):
        return 2, item.relative_path
    if item.relative_path.startswith("inventory/sessions/"):
        return 1, item.relative_path
    return 0, item.relative_path


def _safe_relative(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise BackupIntegrityError(f"{name} must be non-empty text")
    if value != value.strip() or "\\" in value or ":" in value or "\0" in value:
        raise BackupIntegrityError(f"{name} must be a canonical relative POSIX path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise BackupIntegrityError(f"{name} must be a safe relative POSIX path")
    path = Path(*pure.parts)
    if path.is_absolute() or path.drive:
        raise BackupIntegrityError(f"{name} must stay relative on this platform")
    return path


def _require_sha256(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BackupIntegrityError(f"{name} must be a lowercase SHA-256")
    return value


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise BackupIntegrityError("backup timestamp must be text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BackupIntegrityError("backup timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackupIntegrityError("backup timestamp must be timezone-aware")
    return parsed


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BackupIntegrityError("backup timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BackupIntegrityError("archive cutoff must be timezone-aware")
    return value.astimezone(UTC)


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackupIntegrityError(f"{name} must be a non-negative integer")
    return value


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as handle:
        handle.flush()
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
    "BACKUP_RECEIPT_SCHEMA_VERSION",
    "BACKUP_SNAPSHOT_SCHEMA_VERSION",
    "BackupFile",
    "BackupIntegrityError",
    "BackupReceipt",
    "BackupSnapshot",
    "LocalArchivePlan",
    "LocalObjectTransport",
    "ObjectTransport",
    "RcloneObjectTransport",
    "archive_verified_sessions_locally",
    "create_backup_snapshot",
    "read_backup_snapshot",
    "read_backup_receipt",
    "restore_remote_snapshot",
    "upload_backup_snapshot",
    "verify_remote_snapshot",
]
