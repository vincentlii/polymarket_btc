from datetime import UTC, datetime, timedelta
from inspect import signature
from types import SimpleNamespace

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow
from scripts.btc_training_readiness_worker import _audit_raw_exit_evidence


def _market() -> MarketWindow:
    t0 = datetime(2026, 8, 13, tzinfo=UTC)
    return MarketWindow(
        BTC_15M_MARKET_FAMILY,
        BTC_15M_MARKET_FAMILY.slug_for(t0),
        "c",
        "up",
        "down",
        t0,
        t0 + timedelta(minutes=15),
        "chainlink-btc-usd-twap-60s-v1",
        "a" * 64,
    )


def _part(
    token: str,
    *,
    market: MarketWindow,
    minimum: datetime | None = None,
    maximum: datetime | None = None,
    gap_count: int = 0,
) -> object:
    start = minimum or market.t0 - timedelta(seconds=90)
    end = maximum or market.t1
    return SimpleNamespace(
        manifest=SimpleNamespace(
            data_path=f"raw/polymarket_clob/{token}/{start.timestamp()}.parquet",
            sha256=("a" if token == "up" else "b") * 64,
            source="polymarket_clob",
            instrument=token,
            ingest_version="v17",
            min_available_ts_ns=int(start.timestamp() * 1e9),
            max_available_ts_ns=int(end.timestamp() * 1e9),
            row_count=10,
            gap_count=gap_count,
        )
    )


def test_exit_audit_is_manifest_only_and_cannot_replay_raw_payloads() -> None:
    parameters = signature(_audit_raw_exit_evidence).parameters

    assert "evidence_sessions" in parameters
    assert "raw_data_root" not in parameters
    assert "polymarket_evidence" not in parameters


def test_raw_exit_readiness_accepts_complete_dual_token_manifest_lineage() -> None:
    market = _market()
    errors, summaries = _audit_raw_exit_evidence(
        evidence_sessions=(
            SimpleNamespace(parts=(_part("up", market=market), _part("down", market=market))),
        ),
        market=market,
        ingest_version="v17",
        capture_lead_seconds=90,
        collection_policy="extended_t0_plus_900",
        coverage_error=None,
    )

    assert errors == []
    assert {item["instrument"] for item in summaries} == {"up", "down"}
    assert all(len(str(item["part_lineage_sha256"])) == 64 for item in summaries)


def test_raw_exit_readiness_rejects_gap_and_silent_single_token_loss() -> None:
    market = _market()
    errors, _summaries = _audit_raw_exit_evidence(
        evidence_sessions=(
            SimpleNamespace(
                parts=(
                    _part("up", market=market, gap_count=1),
                    _part(
                        "down",
                        market=market,
                        maximum=market.t0 + timedelta(minutes=10),
                    ),
                )
            ),
        ),
        market=market,
        ingest_version="v17",
        capture_lead_seconds=90,
        collection_policy="extended_t0_plus_900",
        coverage_error=None,
    )

    assert "stream_manifest_gap:polymarket_clob:up" in errors
    assert "missing_terminal_clob_evidence:down" in errors


def test_raw_exit_readiness_keeps_collection_policy_fail_closed() -> None:
    errors, summaries = _audit_raw_exit_evidence(
        evidence_sessions=(),
        market=_market(),
        ingest_version="v17",
        capture_lead_seconds=90,
        collection_policy="core_t0_plus_180",
        coverage_error=None,
    )

    assert errors == ["not_collected_by_policy"]
    assert summaries == []
