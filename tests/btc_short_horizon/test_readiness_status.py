import json

from scripts.btc_training_readiness_worker import aggregate_receipts


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
