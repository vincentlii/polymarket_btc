from datetime import UTC, datetime, timedelta

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow, TimedMarketEvent
from btc_short_horizon.data.collector import PartitionedRawEventWriter, RawCollectorEvent
from scripts.btc_training_readiness_worker import _audit_raw_exit_evidence


def _event(token: str, at: datetime, event_type: str) -> RawCollectorEvent:
    payload = {"event_type": event_type, "asset_id": token}
    if event_type == "book":
        payload.update(
            {
                "timestamp": int(at.timestamp() * 1000),
                "bids": [{"price": "0.49", "size": "10"}],
                "asks": [{"price": "0.51", "size": "10"}],
            }
        )
    else:
        payload.update({"stream_id": "market", "reason": "synthetic_gap"})
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            at,
            at,
            at,
            f"{token}-{event_type}-{at.timestamp()}",
            "polymarket_clob",
            token,
            "test-v1",
            "v16",
        ),
        event_type=event_type,
        payload=payload,
        collector_session_id="session",
        epoch_id=0,
    )


def test_raw_exit_readiness_reads_real_parquet_and_rejects_lifecycle_gap(tmp_path) -> None:
    t0 = datetime(2026, 8, 13, tzinfo=UTC)
    market = MarketWindow(
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
    writer = PartitionedRawEventWriter(tmp_path)
    writer.write(
        (
            _event("up", t0 - timedelta(seconds=1), "book"),
            _event("down", t0 - timedelta(seconds=1), "book"),
            _event("up", market.t1, "book"),
            _event("down", market.t1, "book"),
            _event("up", t0 + timedelta(minutes=12), "continuity_gap"),
        )
    )
    errors = _audit_raw_exit_evidence(
        raw_data_root=tmp_path,
        market=market,
        ingest_version="v16",
        capture_lead_seconds=90,
        collection_policy="extended_t0_plus_900",
        coverage_error=None,
    )
    assert errors == ["clob_gap:up"]
    assert _audit_raw_exit_evidence(
        raw_data_root=tmp_path,
        market=market,
        ingest_version="v16",
        capture_lead_seconds=90,
        collection_policy="core_t0_plus_180",
        coverage_error=None,
    ) == ["not_collected_by_policy"]


def test_raw_exit_readiness_rejects_silent_single_token_loss(tmp_path) -> None:
    t0 = datetime(2026, 8, 13, tzinfo=UTC)
    market = MarketWindow(
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
    writer = PartitionedRawEventWriter(tmp_path)
    writer.write(
        (
            _event("up", t0 - timedelta(seconds=1), "book"),
            _event("down", t0 - timedelta(seconds=1), "book"),
            _event("up", market.t1, "book"),
        )
    )
    assert _audit_raw_exit_evidence(
        raw_data_root=tmp_path,
        market=market,
        ingest_version="v16",
        capture_lead_seconds=90,
        collection_policy="extended_t0_plus_900",
        coverage_error=None,
    ) == ["missing_terminal_clob_evidence:down"]
