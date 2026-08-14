import json
from datetime import UTC, datetime

from btc_short_horizon.live.dashboard import _readiness_status
from scripts.btc_training_readiness_worker import (
    _expected_coverage_evidence,
    _readiness_runtime_status,
    aggregate_receipts,
)


def test_recorded_coverage_failure_is_a_terminal_no_go_without_reaudit() -> None:
    class Index:
        def coverage_evidence(self, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("known coverage failure must not be retried")

    assert (
        _expected_coverage_evidence(
            index=Index(),
            evidence_payload=(),
            coverage_error="session coverage gap",
            source_windows_ns={"polymarket_clob": (1, 2)},
        )
        == ()
    )


def test_recent_status_is_lightweight_and_fails_closed(tmp_path) -> None:
    schema = "btc-training-readiness-receipt-v1"
    (tmp_path / "a.json").write_text(
        json.dumps({"schema_version": schema, "market_slug": "a", "ready": True})
    )
    (tmp_path / "b.json").write_text(
        json.dumps({"schema_version": schema, "market_slug": "b", "ready": False})
    )
    (tmp_path / "error-c.json").write_text(
        json.dumps({"schema_version": "btc-training-readiness-error-v1", "candidate": "c"})
    )
    status = aggregate_receipts(tmp_path)
    assert status["receipt_count"] == 2
    assert status["error_count"] == 1
    assert status["invalid_markets"] == ["b"]
    assert not status["healthy"]


def test_readiness_runtime_status_declares_its_watch_interval() -> None:
    now = datetime(2026, 8, 13, 16, 0, tzinfo=UTC)

    status = _readiness_runtime_status(
        started_at=now,
        updated_at=now,
        backlog=0,
        last_candidate=None,
        last_receipt=None,
        last_error=None,
        code_revision="a" * 40,
    )

    assert status.details["expected_status_interval_seconds"] == 60.0


def test_readiness_status_uses_only_the_latest_protocol_epoch(tmp_path) -> None:
    receipt_root = tmp_path / "readiness" / "receipts"
    receipt_root.mkdir(parents=True)
    schema = "btc-training-readiness-receipt-v1"
    (receipt_root / "btc-updown-15m-100.json").write_text(
        json.dumps(
            {
                "schema_version": schema,
                "market_slug": "old",
                "protocol_sha256": "a" * 64,
                "ready": False,
            }
        )
    )
    (receipt_root / "btc-updown-15m-200.json").write_text(
        json.dumps(
            {
                "schema_version": schema,
                "market_slug": "current",
                "protocol_sha256": "b" * 64,
                "ready": True,
            }
        )
    )

    aggregate = aggregate_receipts(receipt_root)
    dashboard = _readiness_status(tmp_path)

    for status in (aggregate, dashboard):
        assert status["healthy"]
        assert status["receipt_count"] == 1
        assert status["ready_count"] == 1
        assert status["invalid_markets"] == []
