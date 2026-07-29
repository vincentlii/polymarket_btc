"""Crash-auditable session inventory for immutable forward raw evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from io import BufferedRandom
import json
import os
from pathlib import Path, PurePosixPath
import re
from threading import RLock
from typing import Mapping

from btc_short_horizon.data.storage import (
    DataPartitionManifest,
    canonical_json_bytes,
    manifest_relative_path,
    sha256_file,
    write_atomic_json,
)


SESSION_INVENTORY_SCHEMA_VERSION = "btc-session-inventory-v1"
SESSION_REGISTRY_SCHEMA_VERSION = "btc-session-registry-v1"
SESSION_ARCHIVE_MARKER_SCHEMA_VERSION = "btc-session-archive-marker-v1"
SESSION_INVENTORY_MANIFEST_ATTRIBUTE = "session_inventory_schema_version"
SESSION_STATUS_OPEN = "open"
SESSION_STATUS_COMPLETE = "complete"
SESSION_STATUS_FAILED = "failed"
PART_STATUS_PREPARED = "prepared"
PART_STATUS_COMMITTED = "committed"

_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY_KEYS = {
    "schema_version",
    "session_id",
    "inventory_path",
    "ingest_version",
    "started_at",
}
_INVENTORY_KEYS = {
    "schema_version",
    "session_id",
    "ingest_version",
    "started_at",
    "updated_at",
    "status",
    "completed_at",
    "failure_reason",
    "attributes",
    "parts",
}
_PART_KEYS = {
    "manifest_path",
    "manifest_sha256",
    "manifest",
    "status",
    "prepared_at",
    "committed_at",
}


class SessionInventoryError(ValueError):
    """Raised when raw evidence is not provably covered by its session inventory."""


class SessionArchivedError(SessionInventoryError):
    """Raised when requested evidence was safely archived and must be restored first."""


@dataclass(frozen=True, slots=True)
class SessionPartRecord:
    manifest_path: str
    manifest_sha256: str
    manifest: DataPartitionManifest
    status: str
    prepared_at: str
    committed_at: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorSessionRecord:
    session_id: str
    ingest_version: str
    started_at: str
    updated_at: str
    status: str
    completed_at: str | None
    failure_reason: str | None
    attributes: dict[str, str]
    parts: tuple[SessionPartRecord, ...]
    schema_version: str = SESSION_INVENTORY_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class SessionInventoryAudit:
    session_count: int
    open_session_count: int
    complete_session_count: int
    failed_session_count: int
    archived_session_count: int
    prepared_part_count: int
    archived_part_count: int
    committed_part_count: int
    row_count: int
    byte_count: int


class CollectorStorageLease:
    """Cross-process exclusive lease for the single forward raw-data writer."""

    def __init__(self, root: Path, *, owner: Mapping[str, str] | None = None) -> None:
        self.root = root.resolve()
        self.lock_path = self.root / "inventory" / "collector.lock"
        self.info_path = self.root / "inventory" / "collector-lease.json"
        self.owner = _string_mapping(owner or {}, "owner")
        self._handle: BufferedRandom | None = None

    def acquire(self) -> CollectorStorageLease:
        if self._handle is not None:
            raise RuntimeError("collector storage lease is already acquired")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            _lock_file(handle)
            write_atomic_json(
                self.info_path,
                {
                    "schema_version": "btc-collector-lease-v1",
                    "acquired_at": _utc_now_text(),
                    "pid": os.getpid(),
                    "owner": self.owner,
                },
            )
        except Exception:
            try:
                _unlock_file(handle)
            except OSError:
                pass
            handle.close()
            raise
        self._handle = handle
        return self

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self.info_path.unlink(missing_ok=True)
        try:
            _unlock_file(handle)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> CollectorStorageLease:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


class SessionInventoryRepository:
    """Own session registration, strict reads, and whole-root integrity audits."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.registry_root = self.root / "inventory" / "session-registry"
        self.sessions_root = self.root / "inventory" / "sessions"

    def start_session(
        self,
        *,
        session_id: str,
        ingest_version: str,
        attributes: Mapping[str, str] | None = None,
        started_at: datetime | None = None,
    ) -> CollectorSessionInventory:
        session_id = _normalize_session_id(session_id)
        ingest_version = _required_text(ingest_version, "ingest_version")
        normalized_attributes = _string_mapping(attributes or {}, "attributes")
        timestamp = _utc_now_text() if started_at is None else _utc_text(started_at, "started_at")
        registry_path = self.registry_path(session_id)
        inventory_path = self.inventory_path(session_id)
        if registry_path.exists() or inventory_path.exists():
            raise FileExistsError(f"collector session already exists: {session_id}")
        relative_inventory = inventory_path.relative_to(self.root).as_posix()
        registry = {
            "schema_version": SESSION_REGISTRY_SCHEMA_VERSION,
            "session_id": session_id,
            "inventory_path": relative_inventory,
            "ingest_version": ingest_version,
            "started_at": timestamp,
        }
        record = CollectorSessionRecord(
            session_id=session_id,
            ingest_version=ingest_version,
            started_at=timestamp,
            updated_at=timestamp,
            status=SESSION_STATUS_OPEN,
            completed_at=None,
            failure_reason=None,
            attributes=normalized_attributes,
            parts=(),
        )
        write_atomic_json(registry_path, registry)
        try:
            write_atomic_json(inventory_path, _inventory_payload(record))
        except Exception:
            registry_path.unlink(missing_ok=True)
            raise
        return CollectorSessionInventory(repository=self, session_id=session_id)

    def open_session(self, session_id: str) -> CollectorSessionInventory:
        session_id = _normalize_session_id(session_id)
        self.read_session(session_id)
        return CollectorSessionInventory(repository=self, session_id=session_id)

    def read_session(self, session_id: str) -> CollectorSessionRecord:
        session_id = _normalize_session_id(session_id)
        registry = self._read_registry(self.registry_path(session_id))
        if registry["session_id"] != session_id:
            raise SessionInventoryError("session registry filename does not match session_id")
        inventory_path = self.root / _safe_relative_path(
            str(registry["inventory_path"]),
            name="inventory_path",
        )
        expected_inventory_path = self.inventory_path(session_id)
        if inventory_path != expected_inventory_path:
            raise SessionInventoryError("session registry points outside its canonical inventory")
        record = _read_inventory(inventory_path)
        if (
            record.session_id != session_id
            or record.ingest_version != registry["ingest_version"]
            or record.started_at != registry["started_at"]
        ):
            raise SessionInventoryError("session registry and inventory identities differ")
        return record

    def read_all(self) -> tuple[CollectorSessionRecord, ...]:
        registry_paths = tuple(sorted(self.registry_root.glob("*.json")))
        inventory_paths = tuple(sorted(self.sessions_root.glob("*.json")))
        registry_ids = {path.stem for path in registry_paths}
        inventory_ids = {path.stem for path in inventory_paths}
        if registry_ids != inventory_ids:
            missing_inventory = sorted(registry_ids - inventory_ids)
            missing_registry = sorted(inventory_ids - registry_ids)
            raise SessionInventoryError(
                "session registry/inventory mismatch: "
                f"missing_inventory={missing_inventory}, missing_registry={missing_registry}"
            )
        return tuple(self.read_session(session_id) for session_id in sorted(registry_ids))

    def expected_parts(
        self,
        *,
        source: str,
        instrument: str,
        start_available_ts_ns: int,
        end_available_ts_ns: int,
        ingest_version: str | None,
    ) -> tuple[SessionPartRecord, ...]:
        if start_available_ts_ns < 0 or end_available_ts_ns < start_available_ts_ns:
            raise ValueError("invalid inventory time window")
        expected: list[SessionPartRecord] = []
        for session in self.read_all():
            matching = tuple(
                part
                for part in session.parts
                if _part_overlaps(
                    part,
                    source=source,
                    instrument=instrument,
                    start_available_ts_ns=start_available_ts_ns,
                    end_available_ts_ns=end_available_ts_ns,
                    ingest_version=ingest_version,
                )
            )
            if not matching:
                continue
            if session.status == SESSION_STATUS_FAILED:
                raise SessionInventoryError(
                    f"raw window intersects failed collector session {session.session_id}"
                )
            for part in matching:
                if part.status != PART_STATUS_COMMITTED:
                    raise SessionInventoryError(
                        f"raw window intersects uncommitted part {part.manifest_path}"
                    )
                archived_snapshot = self.archived_snapshot_for_part(
                    session_id=session.session_id,
                    part=part,
                )
                if archived_snapshot is not None:
                    raise SessionArchivedError(
                        "raw evidence is remote-only and must be restored from snapshot "
                        f"{archived_snapshot}: {part.manifest_path}"
                    )
                expected.append(part)
        return tuple(sorted(expected, key=lambda item: item.manifest_path))

    def audit(
        self,
        *,
        allow_open_sessions: bool = True,
        allow_failed_sessions: bool = False,
    ) -> SessionInventoryAudit:
        sessions = self.read_all()
        expected_manifests: dict[str, SessionPartRecord] = {}
        claimed_manifest_paths: set[str] = set()
        prepared_part_count = 0
        archived_part_count = 0
        archived_session_ids: set[str] = set()
        row_count = 0
        byte_count = 0
        for session in sessions:
            if session.status == SESSION_STATUS_OPEN and not allow_open_sessions:
                raise SessionInventoryError(
                    f"collector session is still open: {session.session_id}"
                )
            if session.status == SESSION_STATUS_FAILED and not allow_failed_sessions:
                raise SessionInventoryError(f"collector session failed: {session.session_id}")
            for part in session.parts:
                if part.manifest_path in claimed_manifest_paths:
                    raise SessionInventoryError(
                        f"manifest is claimed by multiple sessions: {part.manifest_path}"
                    )
                claimed_manifest_paths.add(part.manifest_path)
                if part.status != PART_STATUS_COMMITTED:
                    if session.status == SESSION_STATUS_COMPLETE:
                        raise SessionInventoryError(
                            f"complete session has an uncommitted part: {part.manifest_path}"
                        )
                    prepared_part_count += 1
                    continue
                try:
                    self.verify_part(part)
                except SessionInventoryError:
                    archived_snapshot = self.archived_snapshot_for_part(
                        session_id=session.session_id,
                        part=part,
                    )
                    if archived_snapshot is None:
                        raise
                    archived_part_count += 1
                    archived_session_ids.add(session.session_id)
                    continue
                expected_manifests[part.manifest_path] = part
                row_count += part.manifest.row_count
                byte_count += (
                    (self.root / _safe_relative_path(part.manifest.data_path)).stat().st_size
                )

        for path in self.root.glob("raw/**/manifest-*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SessionInventoryError(
                    f"cannot read raw manifest during audit: {path}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise SessionInventoryError(f"raw manifest is not an object: {path}")
            attributes = payload.get("attributes")
            if not isinstance(attributes, Mapping) or (
                attributes.get(SESSION_INVENTORY_MANIFEST_ATTRIBUTE)
                != SESSION_INVENTORY_SCHEMA_VERSION
            ):
                continue
            relative = path.relative_to(self.root).as_posix()
            if relative not in claimed_manifest_paths:
                raise SessionInventoryError(f"managed raw manifest is not inventoried: {relative}")

        return SessionInventoryAudit(
            session_count=len(sessions),
            open_session_count=sum(item.status == SESSION_STATUS_OPEN for item in sessions),
            complete_session_count=sum(item.status == SESSION_STATUS_COMPLETE for item in sessions),
            failed_session_count=sum(item.status == SESSION_STATUS_FAILED for item in sessions),
            archived_session_count=len(archived_session_ids),
            prepared_part_count=prepared_part_count,
            archived_part_count=archived_part_count,
            committed_part_count=len(expected_manifests),
            row_count=row_count,
            byte_count=byte_count,
        )

    def recover_interrupted_sessions(
        self,
        *,
        reason: str = "collector process ended before session completion",
    ) -> tuple[str, ...]:
        """Reconcile durable prepared pairs, then fail every orphaned open session."""

        recovered: list[str] = []
        for session in self.read_all():
            if session.status != SESSION_STATUS_OPEN:
                continue
            handle = self.open_session(session.session_id)
            for part in session.parts:
                if part.status != PART_STATUS_PREPARED:
                    continue
                try:
                    self.verify_part(part)
                except SessionInventoryError:
                    continue
                handle.commit_part(
                    manifest_path=part.manifest_path,
                    manifest_sha256=part.manifest_sha256,
                )
            handle.fail(reason)
            recovered.append(session.session_id)
        return tuple(recovered)

    def archive_marker_path(self, session_id: str) -> Path:
        return (
            self.root
            / "inventory"
            / "archived-sessions"
            / f"{_normalize_session_id(session_id)}.json"
        )

    def registry_path(self, session_id: str) -> Path:
        return self.registry_root / f"{_normalize_session_id(session_id)}.json"

    def inventory_path(self, session_id: str) -> Path:
        return self.sessions_root / f"{_normalize_session_id(session_id)}.json"

    def read_archive_marker(self, session_id: str) -> dict[str, object]:
        normalized = _normalize_session_id(session_id)
        return _read_archive_marker(
            self.archive_marker_path(normalized),
            expected_session_id=normalized,
        )

    def archived_snapshot_for_part(
        self,
        *,
        session_id: str,
        part: SessionPartRecord,
    ) -> str | None:
        manifest_path = self.root / _safe_relative_path(part.manifest_path)
        data_path = self.root / _safe_relative_path(part.manifest.data_path)
        if manifest_path.is_file() and data_path.is_file():
            return None
        marker_path = self.archive_marker_path(session_id)
        if not marker_path.is_file():
            return None
        marker = _read_archive_marker(marker_path, expected_session_id=session_id)
        expected = {
            part.manifest_path: part.manifest_sha256,
            part.manifest.data_path: part.manifest.sha256,
        }
        archived_files = {item["relative_path"]: item["sha256"] for item in marker["files"]}
        if any(archived_files.get(path) != digest for path, digest in expected.items()):
            return None
        for path, digest in expected.items():
            local = self.root / _safe_relative_path(path)
            if local.exists() and (not local.is_file() or sha256_file(local) != digest):
                return None
        return str(marker["snapshot_id"])

    def verify_part(self, part: SessionPartRecord) -> None:
        manifest_path = self.root / _safe_relative_path(part.manifest_path)
        data_path = self.root / _safe_relative_path(part.manifest.data_path)
        if not manifest_path.is_file():
            raise SessionInventoryError(
                f"inventory references missing manifest: {part.manifest_path}"
            )
        if not data_path.is_file():
            raise SessionInventoryError(
                f"inventory references missing raw part: {part.manifest.data_path}"
            )
        if sha256_file(manifest_path) != part.manifest_sha256:
            raise SessionInventoryError(f"inventory manifest sha256 mismatch: {part.manifest_path}")
        if sha256_file(data_path) != part.manifest.sha256:
            raise SessionInventoryError(
                f"inventory raw part sha256 mismatch: {part.manifest.data_path}"
            )
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionInventoryError(
                f"cannot decode inventoried manifest: {part.manifest_path}"
            ) from exc
        if payload != asdict(part.manifest):
            raise SessionInventoryError(
                f"inventory manifest payload mismatch: {part.manifest_path}"
            )

    def _read_registry(self, path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionInventoryError(f"cannot read session registry: {path}") from exc
        if not isinstance(payload, dict) or set(payload) != _REGISTRY_KEYS:
            raise SessionInventoryError(f"invalid session registry schema: {path}")
        if payload["schema_version"] != SESSION_REGISTRY_SCHEMA_VERSION:
            raise SessionInventoryError(f"unsupported session registry version: {path}")
        _normalize_session_id(payload["session_id"])
        _required_text(payload["ingest_version"], "ingest_version")
        _parse_utc_text(payload["started_at"], "started_at")
        _safe_relative_path(payload["inventory_path"], name="inventory_path")
        return payload


class CollectorSessionInventory:
    """One collector's serialized write-ahead inventory and lifecycle handle."""

    def __init__(self, *, repository: SessionInventoryRepository, session_id: str) -> None:
        self.repository = repository
        self.session_id = _normalize_session_id(session_id)
        self._lock = RLock()

    @property
    def path(self) -> Path:
        return self.repository.inventory_path(self.session_id)

    def snapshot(self) -> CollectorSessionRecord:
        with self._lock:
            return self.repository.read_session(self.session_id)

    def update_attributes(self, attributes: Mapping[str, str]) -> None:
        """Replace descriptive attributes while the durable session is open."""

        normalized = _string_mapping(attributes, "attributes")
        with self._lock:
            session = self.repository.read_session(self.session_id)
            if session.status != SESSION_STATUS_OPEN:
                raise SessionInventoryError("only an open collector session can update attributes")
            if session.attributes == normalized:
                return
            updated_at = _utc_now_text()
            self._write(
                replace(
                    session,
                    updated_at=updated_at,
                    attributes=normalized,
                )
            )

    def prepare_part(
        self,
        *,
        manifest_path: str,
        manifest: DataPartitionManifest,
    ) -> DataPartitionManifest:
        manifest_path = _safe_relative_text(manifest_path, name="manifest_path")
        if manifest_relative_path(manifest) != manifest_path:
            raise SessionInventoryError("manifest path does not match the immutable data path")
        if manifest.ingest_version != self.snapshot().ingest_version:
            raise SessionInventoryError("manifest ingest_version does not match collector session")
        if manifest.attributes.get("collector_session_id") != self.session_id:
            raise SessionInventoryError("manifest collector_session_id does not match inventory")
        if (
            manifest.attributes.get(SESSION_INVENTORY_MANIFEST_ATTRIBUTE)
            != SESSION_INVENTORY_SCHEMA_VERSION
        ):
            raise SessionInventoryError("manifest does not declare its session inventory contract")
        with self._lock:
            session = self.repository.read_session(self.session_id)
            if session.status == SESSION_STATUS_COMPLETE:
                raise SessionInventoryError(
                    "cannot prepare a part for a complete collector session"
                )
            existing = next(
                (part for part in session.parts if part.manifest_path == manifest_path),
                None,
            )
            if existing is not None:
                if _manifest_immutable_payload(existing.manifest) != _manifest_immutable_payload(
                    manifest
                ):
                    raise SessionInventoryError(
                        "prepared manifest conflicts with existing inventory"
                    )
                return existing.manifest
            prepared_at = _utc_now_text()
            manifest_sha256 = _manifest_sha256(manifest)
            part = SessionPartRecord(
                manifest_path=manifest_path,
                manifest_sha256=manifest_sha256,
                manifest=manifest,
                status=PART_STATUS_PREPARED,
                prepared_at=prepared_at,
            )
            updated = replace(
                session,
                updated_at=prepared_at,
                parts=tuple(sorted((*session.parts, part), key=lambda item: item.manifest_path)),
            )
            self._write(updated)
            return manifest

    def commit_part(self, *, manifest_path: str, manifest_sha256: str) -> None:
        manifest_path = _safe_relative_text(manifest_path, name="manifest_path")
        _require_sha256(manifest_sha256, "manifest_sha256")
        with self._lock:
            session = self.repository.read_session(self.session_id)
            if session.status == SESSION_STATUS_COMPLETE:
                raise SessionInventoryError("cannot commit a part for a complete collector session")
            index = next(
                (
                    index
                    for index, part in enumerate(session.parts)
                    if part.manifest_path == manifest_path
                ),
                None,
            )
            if index is None:
                raise SessionInventoryError("cannot commit an unprepared manifest")
            part = session.parts[index]
            if part.manifest_sha256 != manifest_sha256:
                raise SessionInventoryError(
                    "committed manifest hash differs from prepared inventory"
                )
            self.repository.verify_part(part)
            if part.status == PART_STATUS_COMMITTED:
                return
            committed_at = _utc_now_text()
            parts = list(session.parts)
            parts[index] = replace(
                part,
                status=PART_STATUS_COMMITTED,
                committed_at=committed_at,
            )
            self._write(
                replace(
                    session,
                    updated_at=committed_at,
                    parts=tuple(parts),
                )
            )

    def complete(self) -> None:
        with self._lock:
            session = self.repository.read_session(self.session_id)
            if session.status == SESSION_STATUS_COMPLETE:
                return
            if session.status != SESSION_STATUS_OPEN:
                raise SessionInventoryError("failed collector session cannot become complete")
            if any(part.status != PART_STATUS_COMMITTED for part in session.parts):
                raise SessionInventoryError("collector session cannot close with prepared parts")
            for part in session.parts:
                self.repository.verify_part(part)
            completed_at = _utc_now_text()
            self._write(
                replace(
                    session,
                    updated_at=completed_at,
                    status=SESSION_STATUS_COMPLETE,
                    completed_at=completed_at,
                )
            )

    def fail(self, reason: str) -> None:
        reason = _required_text(reason, "failure_reason")
        if len(reason) > 512:
            reason = reason[:512]
        with self._lock:
            session = self.repository.read_session(self.session_id)
            if session.status == SESSION_STATUS_COMPLETE:
                raise SessionInventoryError("complete collector session cannot become failed")
            if session.status == SESSION_STATUS_FAILED:
                return
            failed_at = _utc_now_text()
            self._write(
                replace(
                    session,
                    updated_at=failed_at,
                    status=SESSION_STATUS_FAILED,
                    completed_at=failed_at,
                    failure_reason=reason,
                )
            )

    def _write(self, record: CollectorSessionRecord) -> None:
        write_atomic_json(self.path, _inventory_payload(record))


def _inventory_payload(record: CollectorSessionRecord) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "session_id": record.session_id,
        "ingest_version": record.ingest_version,
        "started_at": record.started_at,
        "updated_at": record.updated_at,
        "status": record.status,
        "completed_at": record.completed_at,
        "failure_reason": record.failure_reason,
        "attributes": dict(record.attributes),
        "parts": [
            {
                "manifest_path": part.manifest_path,
                "manifest_sha256": part.manifest_sha256,
                "manifest": asdict(part.manifest),
                "status": part.status,
                "prepared_at": part.prepared_at,
                "committed_at": part.committed_at,
            }
            for part in record.parts
        ],
    }


def _read_inventory(path: Path) -> CollectorSessionRecord:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionInventoryError(f"cannot read session inventory: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != _INVENTORY_KEYS:
        raise SessionInventoryError(f"invalid session inventory schema: {path}")
    if payload["schema_version"] != SESSION_INVENTORY_SCHEMA_VERSION:
        raise SessionInventoryError(f"unsupported session inventory version: {path}")
    session_id = _normalize_session_id(payload["session_id"])
    ingest_version = _required_text(payload["ingest_version"], "ingest_version")
    started_at = _parse_utc_text(payload["started_at"], "started_at")
    updated_at = _parse_utc_text(payload["updated_at"], "updated_at")
    if updated_at < started_at:
        raise SessionInventoryError("session updated_at precedes started_at")
    status = payload["status"]
    if status not in {SESSION_STATUS_OPEN, SESSION_STATUS_COMPLETE, SESSION_STATUS_FAILED}:
        raise SessionInventoryError("invalid collector session status")
    completed_at_value = payload["completed_at"]
    completed_at = (
        None if completed_at_value is None else _parse_utc_text(completed_at_value, "completed_at")
    )
    failure_reason_value = payload["failure_reason"]
    if failure_reason_value is not None and not isinstance(failure_reason_value, str):
        raise SessionInventoryError("failure_reason must be text or null")
    if isinstance(failure_reason_value, str) and len(failure_reason_value) > 512:
        raise SessionInventoryError("failure_reason exceeds the storage bound")
    if completed_at is not None and (completed_at < started_at or completed_at != updated_at):
        raise SessionInventoryError("terminal session timestamp disagrees with updated_at")
    if status == SESSION_STATUS_OPEN and (
        completed_at is not None or failure_reason_value is not None
    ):
        raise SessionInventoryError("open session has terminal fields")
    if status == SESSION_STATUS_COMPLETE and (
        completed_at is None or failure_reason_value is not None
    ):
        raise SessionInventoryError("complete session has invalid terminal fields")
    if status == SESSION_STATUS_FAILED and (completed_at is None or not failure_reason_value):
        raise SessionInventoryError("failed session has invalid terminal fields")
    attributes = _string_mapping(payload["attributes"], "attributes")
    parts_value = payload["parts"]
    if not isinstance(parts_value, list):
        raise SessionInventoryError("session parts must be a list")
    parts = tuple(
        _read_part(
            item,
            session_id=session_id,
            session_started_at=started_at,
        )
        for item in parts_value
    )
    paths = [part.manifest_path for part in parts]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise SessionInventoryError("session parts must be uniquely sorted by manifest_path")
    if status == SESSION_STATUS_COMPLETE and any(
        part.status != PART_STATUS_COMMITTED for part in parts
    ):
        raise SessionInventoryError("complete session contains uncommitted parts")
    if any(part.manifest.ingest_version != ingest_version for part in parts):
        raise SessionInventoryError("session contains a part from another ingest version")
    return CollectorSessionRecord(
        session_id=session_id,
        ingest_version=ingest_version,
        started_at=started_at.isoformat(),
        updated_at=updated_at.isoformat(),
        status=str(status),
        completed_at=None if completed_at is None else completed_at.isoformat(),
        failure_reason=None if failure_reason_value is None else failure_reason_value,
        attributes=attributes,
        parts=parts,
    )


def _read_part(
    value: object,
    *,
    session_id: str,
    session_started_at: datetime,
) -> SessionPartRecord:
    if not isinstance(value, dict) or set(value) != _PART_KEYS:
        raise SessionInventoryError("invalid session part schema")
    manifest_path = _safe_relative_text(value["manifest_path"], name="manifest_path")
    manifest_sha256 = _require_sha256(value["manifest_sha256"], "manifest_sha256")
    manifest_value = value["manifest"]
    if not isinstance(manifest_value, dict):
        raise SessionInventoryError("session part manifest must be an object")
    try:
        manifest = DataPartitionManifest(**manifest_value)
    except (TypeError, ValueError) as exc:
        raise SessionInventoryError("invalid data partition manifest in session inventory") from exc
    if manifest_relative_path(manifest) != manifest_path:
        raise SessionInventoryError("inventoried manifest path does not match data_path")
    if manifest.attributes.get("collector_session_id") != session_id:
        raise SessionInventoryError("inventoried manifest belongs to another collector session")
    if (
        manifest.attributes.get(SESSION_INVENTORY_MANIFEST_ATTRIBUTE)
        != SESSION_INVENTORY_SCHEMA_VERSION
    ):
        raise SessionInventoryError("inventoried manifest omits its session inventory contract")
    if _manifest_sha256(manifest) != manifest_sha256:
        raise SessionInventoryError("inventoried manifest hash does not match payload")
    status = value["status"]
    if status not in {PART_STATUS_PREPARED, PART_STATUS_COMMITTED}:
        raise SessionInventoryError("invalid session part status")
    prepared = _parse_utc_text(value["prepared_at"], "prepared_at")
    if prepared < session_started_at:
        raise SessionInventoryError("session part was prepared before its session started")
    prepared_at = prepared.isoformat()
    committed_value = value["committed_at"]
    committed = (
        None if committed_value is None else _parse_utc_text(committed_value, "committed_at")
    )
    if (status == PART_STATUS_PREPARED) != (committed is None):
        raise SessionInventoryError("session part status and committed_at disagree")
    if committed is not None and committed < prepared:
        raise SessionInventoryError("session part committed before it was prepared")
    return SessionPartRecord(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
        status=str(status),
        prepared_at=prepared_at,
        committed_at=None if committed is None else committed.isoformat(),
    )


def _read_archive_marker(path: Path, *, expected_session_id: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionInventoryError(f"cannot read session archive marker: {path}") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "session_id",
        "snapshot_id",
        "remote_id",
        "archived_at",
        "files",
    }:
        raise SessionInventoryError(f"invalid session archive marker schema: {path}")
    if payload["schema_version"] != SESSION_ARCHIVE_MARKER_SCHEMA_VERSION:
        raise SessionInventoryError(f"unsupported session archive marker version: {path}")
    if _normalize_session_id(payload["session_id"]) != _normalize_session_id(expected_session_id):
        raise SessionInventoryError("session archive marker identity mismatch")
    _require_sha256(payload["snapshot_id"], "snapshot_id")
    _required_text(payload["remote_id"], "remote_id")
    _parse_utc_text(payload["archived_at"], "archived_at")
    files = payload["files"]
    if not isinstance(files, list):
        raise SessionInventoryError("session archive marker files must be a list")
    normalized_files: list[dict[str, object]] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "relative_path",
            "sha256",
            "size_bytes",
        }:
            raise SessionInventoryError("invalid archived session file schema")
        relative_path = _safe_relative_text(item["relative_path"], name="relative_path")
        digest = _require_sha256(item["sha256"], "sha256")
        size = item["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SessionInventoryError("archived session size_bytes must be non-negative")
        normalized_files.append(
            {
                "relative_path": relative_path,
                "sha256": digest,
                "size_bytes": size,
            }
        )
    paths = [str(item["relative_path"]) for item in normalized_files]
    if paths != sorted(set(paths)):
        raise SessionInventoryError("archive marker files must be uniquely sorted")
    payload["files"] = normalized_files
    return payload


def _part_overlaps(
    part: SessionPartRecord,
    *,
    source: str,
    instrument: str,
    start_available_ts_ns: int,
    end_available_ts_ns: int,
    ingest_version: str | None,
) -> bool:
    manifest = part.manifest
    minimum = manifest.min_available_ts_ns
    maximum = manifest.max_available_ts_ns
    return (
        manifest.source == source
        and manifest.instrument == instrument
        and (ingest_version is None or manifest.ingest_version == ingest_version)
        and isinstance(minimum, int)
        and isinstance(maximum, int)
        and minimum <= end_available_ts_ns
        and maximum >= start_available_ts_ns
    )


def _manifest_immutable_payload(manifest: DataPartitionManifest) -> dict[str, object]:
    payload = asdict(manifest)
    payload.pop("created_at")
    return payload


def _manifest_sha256(manifest: DataPartitionManifest) -> str:
    from hashlib import sha256

    return sha256(canonical_json_bytes(asdict(manifest))).hexdigest()


def _normalize_session_id(value: object) -> str:
    if not isinstance(value, str):
        raise SessionInventoryError("collector session id must be text")
    normalized = value.strip()
    if not _SESSION_ID.fullmatch(normalized):
        raise SessionInventoryError("collector session id has an invalid format")
    return normalized


def _safe_relative_text(value: object, *, name: str) -> str:
    return _safe_relative_path(value, name=name).as_posix()


def _safe_relative_path(value: object, *, name: str = "path") -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SessionInventoryError(f"{name} must be non-empty text")
    if value != value.strip() or "\\" in value or ":" in value or "\0" in value:
        raise SessionInventoryError(f"{name} must be a canonical relative POSIX path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise SessionInventoryError(f"{name} must be a safe relative POSIX path")
    path = Path(*pure.parts)
    if path.is_absolute() or path.drive:
        raise SessionInventoryError(f"{name} must stay relative on this platform")
    return path


def normalize_session_id(value: object) -> str:
    """Validate and canonicalize an externally supplied collector session id."""

    return _normalize_session_id(value)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SessionInventoryError(f"{name} must be non-empty text")
    return value.strip()


def _string_mapping(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not key or not isinstance(item, str) or not item
        for key, item in value.items()
    ):
        raise SessionInventoryError(f"{name} must contain non-empty string pairs")
    return dict(value)


def _require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SessionInventoryError(f"{name} must be a lowercase SHA-256")
    return value


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat()


def _utc_text(value: datetime, name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionInventoryError(f"{name} must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _parse_utc_text(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise SessionInventoryError(f"{name} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SessionInventoryError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SessionInventoryError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _lock_file(handle: BufferedRandom) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise SessionInventoryError(
            "another forward collector already owns the raw-data storage lease"
        ) from exc


def _unlock_file(handle: BufferedRandom) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "CollectorSessionInventory",
    "CollectorSessionRecord",
    "CollectorStorageLease",
    "PART_STATUS_COMMITTED",
    "PART_STATUS_PREPARED",
    "SESSION_INVENTORY_MANIFEST_ATTRIBUTE",
    "SESSION_INVENTORY_SCHEMA_VERSION",
    "SESSION_ARCHIVE_MARKER_SCHEMA_VERSION",
    "SESSION_STATUS_COMPLETE",
    "SESSION_STATUS_FAILED",
    "SESSION_STATUS_OPEN",
    "SessionInventoryAudit",
    "SessionArchivedError",
    "SessionInventoryError",
    "SessionInventoryRepository",
    "SessionPartRecord",
    "normalize_session_id",
]
