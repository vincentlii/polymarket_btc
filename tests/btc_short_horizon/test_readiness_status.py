import json

from scripts.btc_training_readiness_worker import (
    _expected_coverage_evidence,
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
            start_ns=1,
            end_ns=2,
            required_sources=("polymarket_clob",),
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
