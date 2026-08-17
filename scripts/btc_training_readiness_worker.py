"""Audit one queued closed market and emit a machine-readable readiness receipt."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import time

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data import read_market_catalog  # noqa: E402
from btc_short_horizon.data.coverage_index import (  # noqa: E402
    SessionCoverageIndex,
    coverage_evidence_sha256,
)
from btc_short_horizon.data.rule_contract import rule_contract_sha256  # noqa: E402
from btc_short_horizon.data.readiness import (  # noqa: E402
    collect_source_window_evidence,
    current_protocol_receipts,
)
from btc_short_horizon.data.session_inventory import (  # noqa: E402
    PART_STATUS_COMMITTED,
    SESSION_STATUS_COMPLETE,
    SessionInventoryRepository,
)
from btc_short_horizon.data.storage import write_atomic_json  # noqa: E402
from btc_short_horizon.live.runtime import RuntimeStatus, RuntimeStatusStore  # noqa: E402
from btc_short_horizon.research.opening_features import (  # noqa: E402
    build_forward_opening_readiness_observations,
)
from btc_short_horizon.research.opening_evidence import (  # noqa: E402
    scan_forward_raw_event_metadata,
)
from scripts.btc_forward_collector import _readiness_protocol  # noqa: E402

_EXIT_TERMINAL_MAX_AGE_SECONDS = 15
_WATCH_INTERVAL_SECONDS = 60.0


def _readiness_runtime_status(
    *,
    started_at: datetime,
    updated_at: datetime,
    backlog: int,
    last_candidate: str | None,
    last_receipt: str | None,
    last_error: str | None,
    code_revision: str | None,
    processing_candidate: str | None = None,
) -> RuntimeStatus:
    return RuntimeStatus(
        service="training_readiness",
        mode="offline_incremental",
        state="running",
        healthy=last_error is None,
        started_at=started_at,
        updated_at=updated_at,
        details={
            "backlog": backlog,
            "last_candidate": last_candidate,
            "last_receipt": last_receipt,
            "last_error": last_error,
            "processing_candidate": processing_candidate,
            "warming": last_candidate is None,
            "expected_status_interval_seconds": _WATCH_INTERVAL_SECONDS,
            "identity": {"code_revision": code_revision},
        },
    )


def _expected_coverage_evidence(
    *,
    index: SessionCoverageIndex,
    evidence_payload: tuple[dict[str, object], ...],
    coverage_error: object,
    source_windows_ns: dict[str, tuple[int, int]],
) -> tuple[dict[str, object], ...]:
    if coverage_error:
        if evidence_payload:
            raise ValueError("failed coverage candidate must not include evidence sessions")
        return ()
    return collect_source_window_evidence(
        index=index,
        source_windows_ns=source_windows_ns,
    )


def audit_candidate(
    candidate_path: Path, *, raw_data_root: Path, config_path: Path
) -> dict[str, object]:
    project = load_btc_project_config(config_path)
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    if candidate.get("schema_version") != "btc-training-readiness-candidate-v1":
        raise ValueError("unsupported readiness candidate schema")
    if candidate.get("ingest_version") != project.collection.ingest_version:
        raise ValueError("readiness candidate does not match project ingest version")
    expected_protocol = _readiness_protocol(project)
    if candidate.get("protocol_sha256") != expected_protocol["readiness_protocol_sha256"]:
        raise ValueError("readiness candidate protocol mismatch")
    if tuple(candidate.get("decision_offsets_seconds", ())) != tuple(
        expected_protocol["readiness_decision_offsets_seconds"]
    ):
        raise ValueError("readiness candidate decision offsets mismatch")
    catalog_path = Path(candidate["catalog_path"])
    if sha256(catalog_path.read_bytes()).hexdigest() != candidate["catalog_sha256"]:
        raise ValueError("readiness catalog hash mismatch")
    market = read_market_catalog(catalog_path).require(candidate["market_slug"])
    if market.t0.isoformat() != candidate["t0"] or market.t1.isoformat() != candidate["t1"]:
        raise ValueError("readiness candidate market window mismatch")
    if market.rule_epoch != candidate["rule_epoch"]:
        raise ValueError("readiness candidate rule epoch mismatch")
    if candidate.get("rule_contract_sha256") != rule_contract_sha256(market.rule_epoch):
        raise ValueError("readiness candidate rule contract mismatch")
    required_sources = tuple(expected_protocol["readiness_required_sources"])
    evidence_payload = tuple(candidate.get("evidence_sessions", ()))
    if coverage_evidence_sha256(evidence_payload) != candidate.get("coverage_evidence_sha256"):
        raise ValueError("readiness coverage evidence hash mismatch")
    coverage_index = SessionCoverageIndex(raw_data_root)
    t0_ns = int(market.t0.timestamp() * 1e9)
    expected_evidence = _expected_coverage_evidence(
        index=coverage_index,
        evidence_payload=evidence_payload,
        coverage_error=candidate.get("coverage_error"),
        source_windows_ns={
            source: (
                int(t0_ns + offsets[0] * 1_000_000_000),
                int(t0_ns + offsets[1] * 1_000_000_000),
            )
            for source, offsets in expected_protocol[
                "readiness_source_window_offsets_seconds"
            ].items()
        },
    )
    if expected_evidence != evidence_payload:
        raise ValueError("readiness coverage evidence no longer matches persistent index")
    exit_evidence_payload = tuple(candidate.get("exit_evidence_sessions", ()))
    if coverage_evidence_sha256(exit_evidence_payload) != candidate.get(
        "exit_coverage_evidence_sha256"
    ):
        raise ValueError("exit coverage evidence hash mismatch")
    expected_exit_evidence = _expected_coverage_evidence(
        index=coverage_index,
        evidence_payload=exit_evidence_payload,
        coverage_error=candidate.get("exit_coverage_error"),
        source_windows_ns={"polymarket_clob": (t0_ns, int(market.t1.timestamp() * 1e9))},
    )
    if expected_exit_evidence != exit_evidence_payload:
        raise ValueError("exit coverage evidence no longer matches persistent index")
    if candidate.get("coverage_error"):
        errors = [str(candidate["coverage_error"])]
        exit_errors = (
            [str(candidate["exit_coverage_error"])]
            if candidate.get("exit_coverage_error")
            else ["not_audited_after_model_coverage_failure"]
        )
        payload = {
            "schema_version": "btc-training-readiness-receipt-v1",
            "market_slug": market.slug,
            "collector_session_id": candidate["collector_session_id"],
            "evidence_session_ids": [str(item["session_id"]) for item in evidence_payload],
            "coverage_evidence_sha256": candidate["coverage_evidence_sha256"],
            "ingest_version": candidate["ingest_version"],
            "rule_epoch": candidate["rule_epoch"],
            "rule_contract_sha256": candidate["rule_contract_sha256"],
            "protocol_sha256": candidate["protocol_sha256"],
            "decision_coverage": 0,
            "eligible_decisions": 0,
            "source_summaries": [],
            "quality_flags": {},
            "errors": errors,
            "model_feature_ready": False,
            "raw_exit_evidence_ready": False,
            "raw_exit_evidence_errors": exit_errors,
            "ready": False,
            "readiness_scope": "raw_features_only_labels_and_legacy_probability_not_validated",
        }
        payload["receipt_sha256"] = sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return payload
    repository = SessionInventoryRepository(raw_data_root)
    combined_evidence = {
        str(entry["session_id"]): entry for entry in (*evidence_payload, *exit_evidence_payload)
    }
    evidence_sessions = tuple(
        repository.read_session(session_id) for session_id in combined_evidence
    )
    for session in evidence_sessions:
        evidence = combined_evidence[session.session_id]
        if session.status != SESSION_STATUS_COMPLETE:
            raise ValueError("readiness evidence session is not complete")
        if session.ingest_version != candidate["ingest_version"]:
            raise ValueError("readiness session ingest version mismatch")
        if not session.parts or any(part.status != PART_STATUS_COMMITTED for part in session.parts):
            raise ValueError("readiness session has no fully committed evidence")
        inventory_path = repository.inventory_path(session.session_id)
        if sha256(inventory_path.read_bytes()).hexdigest() != evidence["inventory_sha256"]:
            raise ValueError("readiness session inventory hash mismatch")
        for part in session.parts:
            repository.verify_part(part)
    decisions = tuple(
        int(market.t0.timestamp() * 1e9) + seconds * 1_000_000_000
        for seconds in candidate["decision_offsets_seconds"]
    )
    build = build_forward_opening_readiness_observations(
        raw_data_root=raw_data_root,
        market=market,
        start_time=market.t0
        - timedelta(seconds=project.research_timing.max_feature_lookback_seconds),
        end_time=market.t0 + timedelta(seconds=project.research_timing.entry_end_seconds),
        decision_ts_ns=decisions,
        ingest_version=candidate["ingest_version"],
        required_venue_sources=tuple(
            source
            for source in required_sources
            if source not in {"polymarket_clob", "polymarket_rtds_chainlink"}
        ),
    )
    flags = Counter(flag for item in build.observations for flag in item.quality_flags)
    source_rows = {item.raw_source: item.raw_row_count for item in build.source_summaries}
    errors = [candidate["coverage_error"]] if candidate.get("coverage_error") else []
    if len(build.observations) != 36:
        errors.append("decision_coverage_not_36")
    if any(not item.eligible for item in build.observations):
        errors.append("ineligible_decisions")
    for source in required_sources:
        if source_rows.get(source, 0) <= 0:
            errors.append(f"missing_source:{source}")
    exit_errors = _audit_raw_exit_evidence(
        raw_data_root=raw_data_root,
        market=market,
        ingest_version=str(candidate["ingest_version"]),
        capture_lead_seconds=project.collection.polymarket_capture_lead_seconds,
        collection_policy=str(candidate.get("exit_collection_policy")),
        coverage_error=candidate.get("exit_coverage_error"),
    )
    payload = {
        "schema_version": "btc-training-readiness-receipt-v1",
        "market_slug": market.slug,
        "collector_session_id": candidate["collector_session_id"],
        "evidence_session_ids": [str(item["session_id"]) for item in evidence_payload],
        "coverage_evidence_sha256": candidate["coverage_evidence_sha256"],
        "ingest_version": candidate["ingest_version"],
        "rule_epoch": candidate["rule_epoch"],
        "rule_contract_sha256": candidate["rule_contract_sha256"],
        "protocol_sha256": candidate["protocol_sha256"],
        "decision_coverage": len(build.observations),
        "eligible_decisions": sum(item.eligible for item in build.observations),
        "source_summaries": [asdict(item) for item in build.source_summaries],
        "quality_flags": dict(sorted(flags.items())),
        "errors": errors,
        "model_feature_ready": not errors,
        "raw_exit_evidence_ready": not exit_errors,
        "raw_exit_evidence_errors": exit_errors,
        "ready": not errors,
        "readiness_scope": "raw_features_only_labels_and_legacy_probability_not_validated",
    }
    payload["receipt_sha256"] = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def _audit_raw_exit_evidence(
    *,
    raw_data_root: Path,
    market: object,
    ingest_version: str,
    capture_lead_seconds: float,
    collection_policy: str,
    coverage_error: object,
) -> list[str]:
    if collection_policy != "extended_t0_plus_900":
        return ["not_collected_by_policy"]
    if coverage_error:
        return [str(coverage_error)]
    errors: list[str] = []
    t0_ns = int(market.t0.timestamp() * 1e9)  # type: ignore[attr-defined]
    for token_id in (market.up_token_id, market.down_token_id):  # type: ignore[attr-defined]
        scan = scan_forward_raw_event_metadata(
            raw_data_root=raw_data_root,
            source="polymarket_clob",
            instrument=token_id,
            start_time=market.t0 - timedelta(seconds=capture_lead_seconds),  # type: ignore[attr-defined]
            end_time=market.t1,  # type: ignore[attr-defined]
            ingest_version=ingest_version,
        )
        book = scan.event_type("book")
        if book is None or book.min_available_ts_ns > t0_ns:
            errors.append(f"missing_t0_book:{token_id}")
        gap = scan.event_type("continuity_gap")
        if gap is not None and gap.max_available_ts_ns >= t0_ns:
            errors.append(f"clob_gap:{token_id}")
        terminal_ns = int(market.t1.timestamp() * 1e9)  # type: ignore[attr-defined]
        latest = max(
            (
                item.max_available_ts_ns
                for event_type in ("book", "price_change", "last_trade_price")
                if (item := scan.event_type(event_type)) is not None
            ),
            default=0,
        )
        if latest < terminal_ns - _EXIT_TERMINAL_MAX_AGE_SECONDS * 1_000_000_000:
            errors.append(f"missing_terminal_clob_evidence:{token_id}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument(
        "--config", type=Path, default=Path("configs/btc_short_horizon/baseline.toml")
    )
    parser.add_argument("--raw-data-root", type=Path, required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    args = parser.parse_args()
    if args.watch:
        if args.candidate_root is None:
            parser.error("--watch requires --candidate-root")
        started_at = datetime.now(UTC)
        status_store = RuntimeStatusStore(args.runtime_root) if args.runtime_root else None

        def write_watch_status(
            *,
            last_candidate: str | None,
            last_receipt: str | None,
            last_error: str | None,
            processing_candidate: str | None = None,
        ) -> None:
            if status_store is None:
                return
            backlog = sum(
                not (args.receipt_root / item.name).exists()
                for item in args.candidate_root.glob("*.json")
            )
            status_store.write(
                _readiness_runtime_status(
                    started_at=started_at,
                    updated_at=datetime.now(UTC),
                    backlog=backlog,
                    last_candidate=last_candidate,
                    last_receipt=last_receipt,
                    last_error=last_error,
                    code_revision=os.environ.get("BTC_CODE_REVISION"),
                    processing_candidate=processing_candidate,
                )
            )

        write_watch_status(last_candidate=None, last_receipt=None, last_error=None)
        while True:
            last_candidate = None
            last_receipt = None
            last_error = None
            for candidate in sorted(args.candidate_root.glob("*.json")):
                last_candidate = candidate.name
                receipt_path = args.receipt_root / candidate.name
                if receipt_path.exists():
                    continue
                write_watch_status(
                    last_candidate=last_candidate,
                    last_receipt=last_receipt,
                    last_error=None,
                    processing_candidate=candidate.name,
                )
                try:
                    receipt = audit_candidate(
                        candidate, raw_data_root=args.raw_data_root, config_path=args.config
                    )
                    write_atomic_json(receipt_path, receipt)
                    (args.receipt_root / f"error-{candidate.name}").unlink(missing_ok=True)
                    last_receipt = receipt_path.name
                except Exception as exc:
                    last_error = type(exc).__name__
                    write_atomic_json(
                        args.receipt_root / f"error-{candidate.name}",
                        {
                            "schema_version": "btc-training-readiness-error-v1",
                            "candidate": candidate.name,
                            "error": type(exc).__name__,
                        },
                    )
                    write_watch_status(
                        last_candidate=last_candidate,
                        last_receipt=last_receipt,
                        last_error=last_error,
                        processing_candidate=None,
                    )
                else:
                    write_watch_status(
                        last_candidate=last_candidate,
                        last_receipt=last_receipt,
                        last_error=None,
                        processing_candidate=None,
                    )
            if status_store is not None:
                write_watch_status(
                    last_candidate=last_candidate,
                    last_receipt=last_receipt,
                    last_error=last_error,
                    processing_candidate=None,
                )
            time.sleep(_WATCH_INTERVAL_SECONDS)
    if args.candidate is None:
        parser.error("--candidate is required unless --watch is set")
    receipt = audit_candidate(
        args.candidate, raw_data_root=args.raw_data_root, config_path=args.config
    )
    receipt_path = args.receipt_root / f"{receipt['market_slug']}.json"
    if receipt_path.exists():
        if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt:
            raise ValueError("immutable readiness receipt conflict")
    else:
        write_atomic_json(receipt_path, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["ready"] else 2


def aggregate_receipts(receipt_root: Path, *, limit: int = 96) -> dict[str, object]:
    paths = sorted(receipt_root.glob("*.json"), key=lambda item: item.name)[-limit:]
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    receipts = current_protocol_receipts(payloads)
    errors = [
        item for item in payloads if item.get("schema_version") == "btc-training-readiness-error-v1"
    ]
    invalid = [item.get("market_slug") for item in receipts if not item.get("ready")]
    return {
        "schema_version": "btc-training-readiness-status-v1",
        "receipt_count": len(receipts),
        "error_count": len(errors),
        "ready_count": sum(item.get("ready") is True for item in receipts),
        "invalid_markets": invalid,
        "healthy": bool(receipts) and not invalid and not errors,
    }


if __name__ == "__main__":
    raise SystemExit(main())
