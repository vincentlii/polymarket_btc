"""Incremental candidate records for offline training-readiness audits."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path

from btc_short_horizon.data.contracts import MarketWindow
from btc_short_horizon.data.coverage_index import SessionCoverageIndex, coverage_evidence_sha256
from btc_short_horizon.data.rule_contract import rule_contract_sha256
from btc_short_horizon.data.storage import write_atomic_json


@dataclass(frozen=True, slots=True)
class ReadinessCandidate:
    schema_version: str
    market_slug: str
    t0: str
    t1: str
    ingest_version: str
    collector_session_id: str
    evidence_sessions: tuple[dict[str, object], ...]
    coverage_evidence_sha256: str
    coverage_error: str | None
    exit_evidence_sessions: tuple[dict[str, object], ...]
    exit_coverage_evidence_sha256: str
    exit_coverage_error: str | None
    exit_collection_policy: str
    catalog_path: str
    catalog_sha256: str
    rule_epoch: str
    rule_contract_sha256: str
    decision_offsets_seconds: tuple[int, ...]
    protocol_sha256: str


def register_readiness_candidate(
    *,
    root: Path,
    market: MarketWindow,
    ingest_version: str,
    collector_session_id: str,
    evidence_sessions: tuple[dict[str, object], ...],
    coverage_error: str | None,
    exit_evidence_sessions: tuple[dict[str, object], ...],
    exit_coverage_error: str | None,
    exit_collection_policy: str,
    catalog_path: Path,
    decision_offsets_seconds: tuple[int, ...],
    protocol_sha256: str,
) -> Path:
    """Register cheap evidence without reading Parquet or blocking rotation."""
    candidate = ReadinessCandidate(
        schema_version="btc-training-readiness-candidate-v1",
        market_slug=market.slug,
        t0=market.t0.isoformat(),
        t1=market.t1.isoformat(),
        ingest_version=ingest_version,
        collector_session_id=collector_session_id,
        evidence_sessions=evidence_sessions,
        coverage_evidence_sha256=coverage_evidence_sha256(evidence_sessions),
        coverage_error=coverage_error,
        exit_evidence_sessions=exit_evidence_sessions,
        exit_coverage_evidence_sha256=coverage_evidence_sha256(exit_evidence_sessions),
        exit_coverage_error=exit_coverage_error,
        exit_collection_policy=exit_collection_policy,
        catalog_path=str(catalog_path),
        catalog_sha256=sha256(catalog_path.read_bytes()).hexdigest(),
        rule_epoch=market.rule_epoch,
        rule_contract_sha256=rule_contract_sha256(market.rule_epoch),
        decision_offsets_seconds=decision_offsets_seconds,
        protocol_sha256=protocol_sha256,
    )
    path = root / "readiness" / "candidates" / f"{market.slug}.json"
    if path.exists():
        expected = json.loads(json.dumps(asdict(candidate), sort_keys=True))
        if json.loads(path.read_text(encoding="utf-8")) != expected:
            raise ValueError("training-readiness candidate identity conflict")
        return path
    write_atomic_json(path, asdict(candidate))
    return path


def collect_source_window_evidence(
    *,
    index: SessionCoverageIndex,
    source_windows_ns: dict[str, tuple[int, int]],
) -> tuple[dict[str, object], ...]:
    """Combine immutable session evidence for source-specific causal windows."""
    by_session_id: dict[str, dict[str, object]] = {}
    for source, (start_ns, end_ns) in sorted(source_windows_ns.items()):
        for entry in index.coverage_evidence(
            start_ns=start_ns,
            end_ns=end_ns,
            required_sources=(source,),
        ):
            session_id = str(entry["session_id"])
            existing = by_session_id.get(session_id)
            if existing is not None and existing != entry:
                raise ValueError("readiness session evidence identity conflict")
            by_session_id[session_id] = entry
    return tuple(
        sorted(
            by_session_id.values(),
            key=lambda item: (int(item["started_at_ns"]), str(item["session_id"])),
        )
    )


def current_protocol_receipts(
    payloads: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return only receipts belonging to the newest observed research protocol."""
    receipts = [
        item
        for item in payloads
        if item.get("schema_version") == "btc-training-readiness-receipt-v2"
    ]
    if not receipts:
        return []
    current_protocol = receipts[-1].get("protocol_sha256")
    return [item for item in receipts if item.get("protocol_sha256") == current_protocol]


__all__ = [
    "ReadinessCandidate",
    "collect_source_window_evidence",
    "current_protocol_receipts",
    "register_readiness_candidate",
]
