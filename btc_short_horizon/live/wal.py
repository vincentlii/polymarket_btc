"""Durable, hash-chained JSONL state log for crash-safe live recovery."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
from time import monotonic, sleep
from typing import Any


_WAL_SCHEMA_VERSION = 1
_ZERO_HASH = "0" * 64
_SENSITIVE_KEYS = frozenset(
    {
        "apikey",
        "apisecret",
        "secret",
        "passphrase",
        "privatekey",
        "polysignature",
        "owner",
        "orderowner",
        "tradeowner",
        "authorization",
        "credentials",
        "mnemonic",
        "password",
        "seedphrase",
    }
)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_SEGMENT_NAME = re.compile(
    r"^(?P<first>[0-9]{20})-(?P<last>[0-9]{20})-(?P<hash>[0-9a-f]{64})\.jsonl$"
)


@dataclass(frozen=True, slots=True)
class WalRotationReceipt:
    archived_path: Path | None
    archived_through_sequence: int
    archived_record_hash: str
    checkpoint_sequence: int
    checkpoint_record_hash: str


class JsonlWriteAheadLog:
    """Append one fsync-backed event at a time and verify the full chain on recovery."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def segment_directory(self) -> Path:
        return self.path.with_name(f"{self.path.name}.segments")

    def append(self, *, event_type: str, ts_ns: int, payload: object) -> None:
        self.append_many(((event_type, ts_ns, payload),))

    def append_many(self, records: Sequence[tuple[str, int, object]]) -> None:
        """Append one logical batch with a single data-file fsync."""

        items = tuple(records)
        if not items:
            raise ValueError("WAL batch must contain at least one record")
        normalized: list[tuple[str, int, object]] = []
        for item in items:
            if not isinstance(item, tuple) or len(item) != 3:
                raise ValueError("each WAL batch item must be an event_type, ts_ns, payload tuple")
            event_type, ts_ns, payload = item
            if not isinstance(event_type, str) or not event_type.strip():
                raise ValueError("event_type is required")
            if isinstance(ts_ns, bool) or not isinstance(ts_ns, int) or ts_ns < 0:
                raise ValueError("ts_ns must be a non-negative integer")
            try:
                normalized_payload = _json_payload(payload)
                _canonical_json(normalized_payload)
            except (TypeError, ValueError) as exc:
                if "credential" in str(exc):
                    raise
                raise ValueError("payload must be a finite JSON value") from exc
            normalized.append((event_type.strip(), ts_ns, normalized_payload))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        with exclusive_file_lock(self.path.with_suffix(f"{self.path.suffix}.lock")):
            previous_sequence, previous_hash = self._last_global_state()
            encoded_records: list[bytes] = []
            for offset, (event_type, ts_ns, payload) in enumerate(normalized, start=1):
                body = {
                    "schema_version": _WAL_SCHEMA_VERSION,
                    "sequence": previous_sequence + offset,
                    "prev_hash": previous_hash,
                    "event_type": event_type,
                    "ts_ns": ts_ns,
                    "payload": payload,
                }
                record_hash = sha256(_canonical_json(body)).hexdigest()
                encoded_records.append(
                    _canonical_json({**body, "record_hash": record_hash}) + b"\n"
                )
                previous_hash = record_hash
            with self.path.open("ab") as handle:
                handle.write(b"".join(encoded_records))
                handle.flush()
                os.fsync(handle.fileno())
        if created:
            _fsync_directory(self.path.parent)

    def read(self) -> tuple[dict[str, Any], ...]:
        with exclusive_file_lock(self.path.with_suffix(f"{self.path.suffix}.lock")):
            return self._read_all_unlocked()

    def read_recovery_window(self) -> tuple[dict[str, Any], ...]:
        """Read only the active checkpoint and tail, anchored to the last segment."""

        with exclusive_file_lock(self.path.with_suffix(f"{self.path.suffix}.lock")):
            archived_sequence, archived_hash = self._last_archived_state()
            if not self.path.exists() or self.path.stat().st_size == 0:
                return self._read_all_unlocked()
            return _read_wal_file(
                self.path,
                expected_sequence=archived_sequence + 1,
                expected_prev_hash=archived_hash,
            )

    def rotate(
        self,
        *,
        event_type: str,
        ts_ns: int,
        payload: object,
    ) -> WalRotationReceipt:
        """Archive the active segment and atomically install a linked checkpoint."""

        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type is required")
        if isinstance(ts_ns, bool) or not isinstance(ts_ns, int) or ts_ns < 0:
            raise ValueError("ts_ns must be a non-negative integer")
        try:
            normalized_payload = _json_payload(payload)
            _canonical_json(normalized_payload)
        except (TypeError, ValueError) as exc:
            if "credential" in str(exc):
                raise
            raise ValueError("payload must be a finite JSON value") from exc

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.segment_directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        with exclusive_file_lock(lock_path):
            all_records = self._read_all_unlocked()
            archived_sequence = 0 if not all_records else int(all_records[-1]["sequence"])
            archived_hash = _ZERO_HASH if not all_records else str(all_records[-1]["record_hash"])
            body = {
                "schema_version": _WAL_SCHEMA_VERSION,
                "sequence": archived_sequence + 1,
                "prev_hash": archived_hash,
                "event_type": event_type.strip(),
                "ts_ns": ts_ns,
                "payload": normalized_payload,
            }
            checkpoint_hash = sha256(_canonical_json(body)).hexdigest()
            encoded = _canonical_json({**body, "record_hash": checkpoint_hash}) + b"\n"

            temporary = self.path.with_name(f".{self.path.name}.checkpoint.tmp")
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())

            archived_path: Path | None = None
            if self.path.exists() and self.path.stat().st_size > 0:
                active_records = self._read_active_unlocked()
                if not active_records:
                    raise ValueError("non-empty active WAL has no records")
                first_sequence = int(active_records[0]["sequence"])
                last_sequence = int(active_records[-1]["sequence"])
                last_hash = str(active_records[-1]["record_hash"])
                archived_path = self.segment_directory / (
                    f"{first_sequence:020d}-{last_sequence:020d}-{last_hash}.jsonl"
                )
                if archived_path.exists():
                    raise FileExistsError(f"WAL segment already exists: {archived_path}")
                os.replace(self.path, archived_path)
                _fsync_directory(self.segment_directory)
            os.replace(temporary, self.path)
            _fsync_directory(self.path.parent)
            return WalRotationReceipt(
                archived_path=archived_path,
                archived_through_sequence=archived_sequence,
                archived_record_hash=archived_hash,
                checkpoint_sequence=archived_sequence + 1,
                checkpoint_record_hash=checkpoint_hash,
            )

    def _read_all_unlocked(self) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        expected_sequence = 1
        previous_hash = _ZERO_HASH
        for segment in self._segment_paths():
            segment_records = _read_wal_file(
                segment,
                expected_sequence=expected_sequence,
                expected_prev_hash=previous_hash,
            )
            if not segment_records:
                raise ValueError(f"empty WAL segment: {segment}")
            first, last, expected_hash = _segment_identity(segment)
            if (
                int(segment_records[0]["sequence"]) != first
                or int(segment_records[-1]["sequence"]) != last
                or str(segment_records[-1]["record_hash"]) != expected_hash
            ):
                raise ValueError(f"WAL segment identity mismatch: {segment}")
            records.extend(segment_records)
            expected_sequence = last + 1
            previous_hash = expected_hash
        if self.path.exists() and self.path.stat().st_size > 0:
            active_records = _read_wal_file(
                self.path,
                expected_sequence=expected_sequence,
                expected_prev_hash=previous_hash,
            )
            records.extend(active_records)
        return tuple(records)

    def _read_active_unlocked(self) -> tuple[dict[str, Any], ...]:
        archived_sequence, archived_hash = self._last_archived_state()
        if not self.path.exists() or self.path.stat().st_size == 0:
            return ()
        return _read_wal_file(
            self.path,
            expected_sequence=archived_sequence + 1,
            expected_prev_hash=archived_hash,
        )

    def _segment_paths(self) -> tuple[Path, ...]:
        if not self.segment_directory.exists():
            return ()
        paths = tuple(self.segment_directory.glob("*.jsonl"))
        for path in paths:
            _segment_identity(path)
        return tuple(sorted(paths, key=lambda path: _segment_identity(path)[0]))

    def _last_archived_state(self) -> tuple[int, str]:
        segments = self._segment_paths()
        if not segments:
            return 0, _ZERO_HASH
        path = segments[-1]
        _, expected_sequence, expected_hash = _segment_identity(path)
        sequence, record_hash = _last_record_state(path)
        if sequence != expected_sequence or record_hash != expected_hash:
            raise ValueError(f"WAL segment identity mismatch: {path}")
        return sequence, record_hash

    def _last_global_state(self) -> tuple[int, str]:
        if self.path.exists() and self.path.stat().st_size > 0:
            return _last_record_state(self.path)
        return self._last_archived_state()


def _read_wal_file(
    path: Path,
    *,
    expected_sequence: int,
    expected_prev_hash: str,
) -> tuple[dict[str, Any], ...]:
    raw = path.read_bytes()
    if not raw:
        return ()
    if not raw.endswith(b"\n"):
        raise ValueError(f"truncated WAL tail: {path}")
    records: list[dict[str, Any]] = []
    previous_hash = expected_prev_hash
    for offset, line in enumerate(raw.splitlines()):
        sequence = expected_sequence + offset
        if not line:
            raise ValueError(f"invalid blank WAL record at sequence {sequence}")
        try:
            record = _strict_json_loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"invalid WAL JSON at sequence {sequence}") from exc
        validated = _validate_record(
            record,
            expected_sequence=sequence,
            expected_prev_hash=previous_hash,
        )
        previous_hash = str(validated["record_hash"])
        records.append(validated)
    return tuple(records)


def _segment_identity(path: Path) -> tuple[int, int, str]:
    match = _SEGMENT_NAME.fullmatch(path.name)
    if match is None:
        raise ValueError(f"invalid WAL segment filename: {path}")
    first = int(match.group("first"))
    last = int(match.group("last"))
    record_hash = match.group("hash")
    if first < 1 or last < first:
        raise ValueError(f"invalid WAL segment sequence range: {path}")
    return first, last, record_hash


def _validate_record(
    value: object,
    *,
    expected_sequence: int | None = None,
    expected_prev_hash: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("WAL record must be an object")
    expected_keys = {
        "schema_version",
        "sequence",
        "prev_hash",
        "event_type",
        "ts_ns",
        "payload",
        "record_hash",
    }
    if set(value) != expected_keys or value.get("schema_version") != _WAL_SCHEMA_VERSION:
        raise ValueError("unsupported WAL record schema")
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("invalid WAL sequence")
    if expected_sequence is not None and sequence != expected_sequence:
        raise ValueError("non-contiguous WAL sequence")
    prev_hash = value.get("prev_hash")
    if not _is_sha256(prev_hash):
        raise ValueError("invalid WAL previous hash")
    if expected_prev_hash is not None and prev_hash != expected_prev_hash:
        raise ValueError("broken WAL hash chain")
    event_type = value.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("invalid WAL event_type")
    ts_ns = value.get("ts_ns")
    if isinstance(ts_ns, bool) or not isinstance(ts_ns, int) or ts_ns < 0:
        raise ValueError("invalid WAL ts_ns")
    payload = _json_payload(value.get("payload"))
    record_hash = value.get("record_hash")
    if not _is_sha256(record_hash):
        raise ValueError("invalid WAL record hash")
    body = {key: value[key] for key in value if key != "record_hash"}
    if sha256(_canonical_json(body)).hexdigest() != record_hash:
        raise ValueError("WAL record hash mismatch")
    return {
        "schema_version": _WAL_SCHEMA_VERSION,
        "sequence": sequence,
        "prev_hash": prev_hash,
        "event_type": event_type,
        "ts_ns": ts_ns,
        "payload": payload,
        "record_hash": record_hash,
    }


def _last_record_state(path: Path) -> tuple[int, str]:
    if not path.exists() or path.stat().st_size == 0:
        return 0, _ZERO_HASH
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(size - 1)
        if handle.read(1) != b"\n":
            raise ValueError("truncated WAL tail")
        cursor = size - 1
        line_start = 0
        while cursor > 0:
            start = max(0, cursor - 4096)
            handle.seek(start)
            chunk = handle.read(cursor - start)
            delimiter = chunk.rfind(b"\n")
            if delimiter >= 0:
                line_start = start + delimiter + 1
                break
            cursor = start
        handle.seek(line_start)
        line = handle.read(size - 1 - line_start)
    try:
        raw = _strict_json_loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("invalid final WAL record") from exc
    record = _validate_record(raw)
    return record["sequence"], record["record_hash"]


def _json_payload(payload: object) -> object:
    if is_dataclass(payload) and not isinstance(payload, type):
        return _json_payload(asdict(payload))
    if isinstance(payload, Enum):
        return _json_payload(payload.value)
    if isinstance(payload, Mapping):
        result: dict[str, object] = {}
        for key, value in payload.items():
            text_key = str(key)
            normalized_key = "".join(
                character for character in text_key.casefold() if character.isalnum()
            )
            if normalized_key in _SENSITIVE_KEYS:
                raise ValueError(f"credential-shaped WAL field is forbidden: {text_key}")
            result[text_key] = _json_payload(value)
        return result
    if isinstance(payload, list | tuple):
        return [_json_payload(value) for value in payload]
    if isinstance(payload, str | int | float | bool) or payload is None:
        return payload
    raise TypeError(f"payload is not JSON-compatible: {type(payload)!r}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _strict_json_loads(value: bytes) -> object:
    def reject_constant(constant: str) -> object:
        raise ValueError(f"non-finite JSON constant: {constant}")

    return json.loads(value.decode("utf-8"), parse_constant=reject_constant)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@contextmanager
def exclusive_file_lock(path: Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    resolved = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(resolved, threading.Lock())
    with thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = monotonic() + timeout_seconds
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if monotonic() >= deadline:
                            raise TimeoutError(f"timed out acquiring WAL lock: {path}")
                        sleep(0.01)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on the Linux VPS/CI path
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["JsonlWriteAheadLog", "WalRotationReceipt", "exclusive_file_lock"]
