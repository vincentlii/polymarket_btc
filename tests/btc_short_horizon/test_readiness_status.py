import json
from datetime import UTC, datetime
from types import SimpleNamespace

from btc_short_horizon.live.dashboard import _readiness_status
from scripts.btc_training_readiness_worker import (
    _expected_coverage_evidence,
    _raw_capture_source_summaries,
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
    schema = "btc-training-readiness-receipt-v2"
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
        process_id=123,
    )

    assert status.details["expected_status_interval_seconds"] == 60.0
    assert status.details["process_id"] == 123


def test_readiness_status_uses_only_the_latest_protocol_epoch(tmp_path) -> None:
    receipt_root = tmp_path / "readiness" / "receipts"
    receipt_root.mkdir(parents=True)
    schema = "btc-training-readiness-receipt-v2"
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


def test_raw_capture_readiness_uses_verified_manifest_evidence_without_feature_replay() -> None:
    t0 = datetime(2026, 8, 18, tzinfo=UTC)
    market = SimpleNamespace(t0=t0, up_token_id="up", down_token_id="down")

    def part(source: str, instrument: str) -> object:
        return SimpleNamespace(
            manifest=SimpleNamespace(
                data_path=f"raw/{source}/{instrument}/part.parquet",
                sha256="a" * 64,
                source=source,
                instrument=instrument,
                ingest_version="v17",
                min_available_ts_ns=int((t0.timestamp() - 3_600) * 1e9),
                max_available_ts_ns=int((t0.timestamp() + 180) * 1e9),
                row_count=10,
                gap_count=0,
            )
        )

    session = SimpleNamespace(
        parts=(
            part("polymarket_clob", "up"),
            part("polymarket_clob", "down"),
            part("polymarket_rtds_chainlink", "btc/usd"),
            part("binance_spot", "BTCUSDT"),
            part("binance_perp", "BTCUSDT"),
            part("okx_spot", "BTC-USDT"),
            part("okx_swap", "BTC-USDT-SWAP"),
        )
    )
    windows = {
        "polymarket_clob": (-90.0, 180.0),
        "polymarket_rtds_chainlink": (-3_600.0, 180.0),
        "binance_spot": (-3_600.0, 180.0),
        "binance_perp": (-3_600.0, 180.0),
        "okx_spot": (-3_600.0, 180.0),
        "okx_swap": (-3_600.0, 180.0),
    }

    summaries, errors = _raw_capture_source_summaries(
        evidence_sessions=(session,),
        market=market,
        ingest_version="v17",
        source_windows_seconds=windows,
    )

    assert errors == []
    assert len(summaries) == 7
    assert {item["instrument"] for item in summaries if item["source"] == "polymarket_clob"} == {
        "up",
        "down",
    }


def test_raw_capture_readiness_fails_closed_when_one_required_instrument_is_missing() -> None:
    t0 = datetime(2026, 8, 18, tzinfo=UTC)
    market = SimpleNamespace(t0=t0, up_token_id="up", down_token_id="down")
    up = SimpleNamespace(
        manifest=SimpleNamespace(
            data_path="raw/polymarket_clob/up/part.parquet",
            sha256="a" * 64,
            source="polymarket_clob",
            instrument="up",
            ingest_version="v17",
            min_available_ts_ns=int((t0.timestamp() - 90) * 1e9),
            max_available_ts_ns=int((t0.timestamp() + 180) * 1e9),
            row_count=1,
            gap_count=0,
        )
    )

    _summaries, errors = _raw_capture_source_summaries(
        evidence_sessions=(SimpleNamespace(parts=(up,)),),
        market=market,
        ingest_version="v17",
        source_windows_seconds={"polymarket_clob": (-90.0, 180.0)},
    )

    assert errors == ["missing_stream_manifest:polymarket_clob:down"]
