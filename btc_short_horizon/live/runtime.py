"""Small persisted control and observability boundary for long-running services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4


_RUNTIME_SCHEMA_VERSION = 1
_RUNTIME_STATES = frozenset({"starting", "running", "stopping", "stopped", "failed"})


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """A credential-free snapshot written by one long-running service."""

    service: str
    mode: str
    state: str
    healthy: bool
    started_at: datetime
    updated_at: datetime
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_identifier(self.service, "service")
        _require_identifier(self.mode, "mode")
        if self.state not in _RUNTIME_STATES:
            raise ValueError(f"unsupported runtime state: {self.state!r}")
        if not isinstance(self.healthy, bool):
            raise ValueError("healthy must be bool")
        started_at = _as_utc(self.started_at, "started_at")
        updated_at = _as_utc(self.updated_at, "updated_at")
        if updated_at < started_at:
            raise ValueError("updated_at must not precede started_at")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "details", _json_mapping(self.details, "details"))

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": _RUNTIME_SCHEMA_VERSION,
            "service": self.service,
            "mode": self.mode,
            "state": self.state,
            "healthy": self.healthy,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "details": dict(self.details),
        }

    @classmethod
    def from_json(cls, raw: object) -> RuntimeStatus:
        if not isinstance(raw, Mapping):
            raise ValueError("runtime status must be a JSON object")
        if raw.get("schema_version") != _RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported runtime status schema")
        return cls(
            service=_text(raw.get("service"), "service"),
            mode=_text(raw.get("mode"), "mode"),
            state=_text(raw.get("state"), "state"),
            healthy=_bool(raw.get("healthy"), "healthy"),
            started_at=_parse_timestamp(raw.get("started_at"), "started_at"),
            updated_at=_parse_timestamp(raw.get("updated_at"), "updated_at"),
            details=_json_mapping(raw.get("details", {}), "details"),
        )


@dataclass(frozen=True, slots=True)
class StopRequest:
    reason: str
    requested_at: datetime

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("stop reason is required")
        object.__setattr__(self, "requested_at", _as_utc(self.requested_at, "requested_at"))

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": _RUNTIME_SCHEMA_VERSION,
            "reason": self.reason,
            "requested_at": self.requested_at.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: object) -> StopRequest:
        if not isinstance(raw, Mapping):
            raise ValueError("stop request must be a JSON object")
        if raw.get("schema_version") != _RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported stop-request schema")
        return cls(
            reason=_text(raw.get("reason"), "reason"),
            requested_at=_parse_timestamp(raw.get("requested_at"), "requested_at"),
        )


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    healthy: bool
    reason: str
    age_seconds: float | None


class RuntimeStatusStore:
    """Atomic, read-only-to-observers status files under one runtime directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, status: RuntimeStatus) -> Path:
        path = self.status_path(status.service)
        _atomic_write_json(path, status.to_json())
        return path

    def read(self, service: str) -> RuntimeStatus | None:
        path = self.status_path(service)
        try:
            return _read_status(path)
        except FileNotFoundError:
            return None

    def all(self, services: Sequence[str] | None = None) -> tuple[RuntimeStatus, ...]:
        directory = self.root / "status"
        if not directory.exists():
            return ()
        selected = None if services is None else frozenset(services)
        if selected is not None:
            for service in selected:
                _require_identifier(service, "service")
        statuses: list[RuntimeStatus] = []
        for path in sorted(directory.glob("*.json")):
            if selected is not None and path.stem not in selected:
                continue
            try:
                statuses.append(_read_status(path))
            except FileNotFoundError:
                continue
        return tuple(statuses)

    def status_path(self, service: str) -> Path:
        _require_identifier(service, "service")
        return self.root / "status" / f"{service}.json"


class RuntimeControl:
    """A filesystem kill switch intentionally separate from the HTTP dashboard."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def stop_path(self) -> Path:
        return self.root / "control" / "stop.json"

    def request_stop(self, *, reason: str, requested_at: datetime) -> StopRequest:
        request = StopRequest(reason=reason, requested_at=requested_at)
        _atomic_write_json(self.stop_path, request.to_json())
        return request

    def stop_request(self) -> StopRequest | None:
        try:
            return _read_stop_request(self.stop_path)
        except FileNotFoundError:
            return None

    def clear_stop(self) -> bool:
        try:
            self.stop_path.unlink()
        except FileNotFoundError:
            return False
        return True


def build_runtime_identity(
    *,
    config_path: Path,
    ingest_version: str,
    model_sha256: str | None = None,
) -> dict[str, object]:
    """Return the immutable identities needed to reproduce one runtime."""

    if not ingest_version.strip():
        raise ValueError("ingest_version is required")
    code_revision = os.environ.get("BTC_CODE_REVISION", "").strip() or _git_revision(config_path)
    return {
        "code_revision": code_revision,
        "config_sha256": sha256(config_path.read_bytes()).hexdigest(),
        "ingest_version": ingest_version.strip(),
        "model_sha256": model_sha256,
    }


def filesystem_usage(path: Path) -> dict[str, object]:
    """Report credential-free disk usage for a persisted runtime path."""

    target = path.resolve()
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(target)
    used = usage.total - usage.free
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": used,
        "free_bytes": usage.free,
        "used_percent": round(used / usage.total * 100.0, 3),
    }


def check_runtime_health(
    status: RuntimeStatus | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> RuntimeHealth:
    """Fail closed when a service is absent, stale, failed, or reports unhealthy."""

    if max_age_seconds <= 0.0:
        raise ValueError("max_age_seconds must be > 0")
    if status is None:
        return RuntimeHealth(False, "missing_status", None)
    current_time = _as_utc(now, "now")
    age_seconds = (current_time - status.updated_at).total_seconds()
    if age_seconds < -5.0:
        return RuntimeHealth(False, "status_timestamp_in_future", age_seconds)
    if age_seconds > max_age_seconds:
        return RuntimeHealth(False, "stale_status", age_seconds)
    if status.state != "running":
        return RuntimeHealth(False, f"state_{status.state}", age_seconds)
    if not status.healthy:
        return RuntimeHealth(False, "service_unhealthy", age_seconds)
    return RuntimeHealth(True, "ok", age_seconds)


def _read_status(path: Path) -> RuntimeStatus:
    return RuntimeStatus.from_json(_read_json(path))


def _git_revision(config_path: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=config_path.resolve().parent,
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _read_stop_request(path: Path) -> StopRequest:
    return StopRequest.from_json(_read_json(path))


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid runtime JSON: {path}") from exc


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _as_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    return _as_utc(parsed, name)


def _require_identifier(value: object, name: str) -> None:
    text = _text(value, name)
    if any(character in text for character in "/\\") or text in {".", ".."}:
        raise ValueError(f"{name} must be a simple identifier")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _json_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return {str(key): _json_value(item, f"{name}.{key}") for key, item in value.items()}


def _json_value(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return _json_mapping(value, name)
    if isinstance(value, list | tuple):
        return [_json_value(item, name) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    raise ValueError(f"{name} is not JSON-compatible")
