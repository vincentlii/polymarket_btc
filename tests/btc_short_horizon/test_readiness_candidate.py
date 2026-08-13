from datetime import UTC, datetime, timedelta
import json

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow
from btc_short_horizon.data.readiness import register_readiness_candidate


def test_candidate_is_stable_and_declares_36_offline_ticks(tmp_path) -> None:
    t0 = datetime(2026, 8, 13, tzinfo=UTC)
    market = MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id="condition",
        up_token_id="up",
        down_token_id="down",
        t0=t0,
        t1=t0 + timedelta(minutes=15),
        rule_epoch="chainlink-btc-usd-twap-60s-v1",
        rule_hash="a" * 64,
    )
    catalog = tmp_path / "catalog.json"
    catalog.write_text("{}", encoding="utf-8")
    kwargs = dict(
        root=tmp_path,
        market=market,
        ingest_version="v16",
        collector_session_id="session",
        evidence_sessions=({"session_id": "session"},),
        coverage_error=None,
        exit_evidence_sessions=({"session_id": "session"},),
        exit_coverage_error=None,
        exit_collection_policy="extended_t0_plus_900",
        catalog_path=catalog,
        decision_offsets_seconds=tuple(range(5, 181, 5)),
        protocol_sha256="b" * 64,
    )
    path = register_readiness_candidate(**kwargs)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["decision_offsets_seconds"] == list(range(5, 181, 5))
    assert len(payload["rule_contract_sha256"]) == 64
    assert register_readiness_candidate(**kwargs) == path
