from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any


class JsonlWriteAheadLog:
    """Append-only, fsync-backed local event log for live recovery."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, *, event_type: str, ts_ns: int, payload: object) -> None:
        if not event_type:
            raise ValueError("event_type is required")
        if ts_ns < 0:
            raise ValueError("ts_ns must be >= 0")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event_type": event_type,
            "ts_ns": int(ts_ns),
            "payload": _json_payload(payload),
        }
        encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            # Python exposes the file descriptor directly, avoiding platform-specific shell tools.
            import os

            os.fsync(handle.fileno())

    def read(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid WAL JSON at line {line_number}") from exc
                if not isinstance(record, dict):
                    raise ValueError(f"invalid WAL record at line {line_number}")
                records.append(record)
        return tuple(records)


def _json_payload(payload: object) -> object:
    if is_dataclass(payload) and not isinstance(payload, type):
        return _json_payload(asdict(payload))
    if isinstance(payload, Enum):
        return _json_payload(payload.value)
    if isinstance(payload, Mapping):
        return {str(key): _json_payload(value) for key, value in payload.items()}
    if isinstance(payload, list | tuple):
        return [_json_payload(value) for value in payload]
    if isinstance(payload, str | int | float | bool) or payload is None:
        return payload
    raise TypeError(f"payload is not JSON-compatible: {type(payload)!r}")
