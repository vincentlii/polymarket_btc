"""Persistent incremental time coverage for completed collector sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from btc_short_horizon.data.session_inventory import CollectorSessionRecord
from btc_short_horizon.data.storage import write_atomic_json


@dataclass(frozen=True, slots=True)
class SessionCoverage:
    session_id: str
    ingest_version: str
    inventory_sha256: str
    started_at_ns: int
    completed_at_ns: int
    sources: dict[str, dict[str, int]]


class SessionCoverageIndex:
    def __init__(self, root: Path) -> None:
        self.path = root / "readiness" / "session-coverage-v1.json"

    def append(self, session: CollectorSessionRecord, *, inventory_path: Path) -> None:
        if session.status != "complete" or session.completed_at is None:
            raise ValueError("only complete sessions can enter the coverage index")
        bounds: dict[str, dict[str, int]] = {}
        for part in session.parts:
            manifest = part.manifest
            if manifest.min_available_ts_ns is None or manifest.max_available_ts_ns is None:
                continue
            values = bounds.setdefault(
                manifest.source,
                {
                    "min_available_ts_ns": manifest.min_available_ts_ns,
                    "max_available_ts_ns": manifest.max_available_ts_ns,
                    "min_source_ts_ns": manifest.min_source_ts_ns or manifest.min_available_ts_ns,
                    "max_source_ts_ns": manifest.max_source_ts_ns or manifest.max_available_ts_ns,
                    "row_count": 0,
                    "gap_count": 0,
                },
            )
            values["min_available_ts_ns"] = min(
                values["min_available_ts_ns"], manifest.min_available_ts_ns
            )
            values["max_available_ts_ns"] = max(
                values["max_available_ts_ns"], manifest.max_available_ts_ns
            )
            if manifest.min_source_ts_ns is not None:
                values["min_source_ts_ns"] = min(
                    values["min_source_ts_ns"], manifest.min_source_ts_ns
                )
            if manifest.max_source_ts_ns is not None:
                values["max_source_ts_ns"] = max(
                    values["max_source_ts_ns"], manifest.max_source_ts_ns
                )
            values["row_count"] += manifest.row_count
            values["gap_count"] += manifest.gap_count
        entry = SessionCoverage(
            session_id=session.session_id,
            ingest_version=session.ingest_version,
            inventory_sha256=sha256(inventory_path.read_bytes()).hexdigest(),
            started_at_ns=_timestamp_ns(session.started_at),
            completed_at_ns=_timestamp_ns(session.completed_at),
            sources=bounds,
        )
        payload = self._read()
        entries = payload["entries"]
        existing = next(
            (item for item in entries if item["session_id"] == session.session_id), None
        )
        encoded = json.loads(json.dumps(asdict(entry), sort_keys=True))
        if existing is not None:
            if existing != encoded:
                raise ValueError("session coverage identity conflict")
            return
        entries.append(encoded)
        entries.sort(key=lambda item: (item["started_at_ns"], item["session_id"]))
        write_atomic_json(self.path, payload)

    def coverage_evidence(
        self,
        *,
        start_ns: int,
        end_ns: int,
        required_sources: tuple[str, ...],
        maximum_session_gap_ns: int = 5_000_000_000,
    ) -> tuple[dict[str, object], ...]:
        """Return evidence when durable sessions cover the interval without declared gaps."""
        if start_ns >= end_ns or maximum_session_gap_ns < 0:
            raise ValueError("invalid coverage interval")
        entries = tuple(
            entry
            for entry in self._read()["entries"]
            if entry["started_at_ns"] <= end_ns and entry["completed_at_ns"] >= start_ns
        )
        if not entries:
            raise ValueError("session coverage gap")
        cursor = start_ns
        selected: list[dict[str, object]] = []
        for entry in entries:
            if int(entry["started_at_ns"]) > cursor + maximum_session_gap_ns:
                raise ValueError("session coverage gap")
            if any(source not in entry["sources"] for source in required_sources):
                raise ValueError("session coverage missing required source")
            for source in required_sources:
                source_evidence = entry["sources"][source]
                if int(source_evidence["gap_count"]) > 0:
                    raise ValueError(f"session coverage recorded source gap: {source}")
                if int(source_evidence["row_count"]) < 1:
                    raise ValueError(f"session coverage empty required source: {source}")
            selected.append(entry)
            cursor = max(cursor, int(entry["completed_at_ns"]))
            if cursor + maximum_session_gap_ns >= end_ns:
                break
        if cursor + maximum_session_gap_ns < end_ns:
            raise ValueError("session coverage gap")
        return tuple(selected)

    def overlapping_session_ids(
        self, *, start_ns: int, end_ns: int, required_sources: tuple[str, ...]
    ) -> tuple[str, ...]:
        return tuple(
            entry["session_id"]
            for entry in self.coverage_evidence(
                start_ns=start_ns,
                end_ns=end_ns,
                required_sources=required_sources,
            )
        )

    def _read(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema_version": "btc-session-coverage-index-v1", "entries": []}
        if payload.get("schema_version") != "btc-session-coverage-index-v1" or not isinstance(
            payload.get("entries"), list
        ):
            raise ValueError("invalid session coverage index")
        return payload


def coverage_evidence_sha256(entries: tuple[dict[str, object], ...]) -> str:
    return sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _timestamp_ns(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000)


__all__ = ["SessionCoverage", "SessionCoverageIndex", "coverage_evidence_sha256"]
