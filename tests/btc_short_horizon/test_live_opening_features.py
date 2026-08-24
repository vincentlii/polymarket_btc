from __future__ import annotations

from datetime import UTC, datetime, timedelta

from btc_short_horizon.data import TimedMarketEvent
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.live.opening_features import LiveOpeningFeatureAdapter


T0 = datetime(2026, 8, 19, tzinfo=UTC)


def _event(
    *, source: str, instrument: str, event_type: str, at: datetime, payload: dict, sequence: int
) -> RawCollectorEvent:
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=at,
            collector_receive_ts=at,
            available_ts=at,
            sequence_or_hash=f"{source}:{instrument}:{event_type}:{sequence}",
            source=source,
            instrument=instrument,
            schema_version="test-v1",
            ingest_version="test-v1",
        ),
        event_type=event_type,
        payload=payload,
        collector_session_id="session",
        epoch_id=0,
        admission_sequence=sequence,
    )


def test_live_adapter_prewarms_causal_core_features_and_ignores_duplicate_replay() -> None:
    adapter = LiveOpeningFeatureAdapter(
        up_token_id="up", down_token_id="down", required_venue_sources=("binance_spot",)
    )
    events = (
        _chainlink(T0 - timedelta(seconds=1), 100.0, 1),
        _trade(T0 - timedelta(seconds=1), 100.0, 2),
        _trade(T0 + timedelta(seconds=4), 101.0, 3),
        _depth(T0 + timedelta(seconds=4), 4),
        _book("up", T0 + timedelta(seconds=4), "0.54", "0.56", 5),
        _book("down", T0 + timedelta(seconds=4), "0.44", "0.46", 6),
        _chainlink(T0 + timedelta(seconds=4), 100.5, 7),
    )
    for event in events:
        adapter.on_event(event)
    for event in events:
        adapter.on_event(event)

    observation = adapter.snapshot(
        decision_ts_ns=int((T0 + timedelta(seconds=5)).timestamp() * 1_000_000_000),
        market_window_start_ns=int(T0.timestamp() * 1_000_000_000),
    )

    assert observation.eligible
    assert observation.p_market_mid_up == 0.55
    assert 0.5 < observation.p_boundary_up < 1.0


def _trade(at: datetime, price: float, sequence: int) -> RawCollectorEvent:
    return _event(
        source="binance_spot",
        instrument="BTCUSDT",
        event_type="aggtrade",
        at=at,
        sequence=sequence,
        payload={
            "stream": "btcusdt@aggTrade",
            "data": {
                "e": "aggTrade",
                "s": "BTCUSDT",
                "T": int(at.timestamp() * 1_000),
                "a": sequence,
                "p": str(price),
                "q": "1",
                "m": False,
            },
        },
    )


def _depth(at: datetime, sequence: int) -> RawCollectorEvent:
    return _event(
        source="binance_spot",
        instrument="BTCUSDT",
        event_type="partial_depth_snapshot",
        at=at,
        sequence=sequence,
        payload={
            "stream": "btcusdt@depth20@100ms",
            "data": {
                "lastUpdateId": sequence,
                "bids": [["100.9", "2"]],
                "asks": [["101.1", "3"]],
            },
        },
    )


def _chainlink(at: datetime, price: float, sequence: int) -> RawCollectorEvent:
    return _event(
        source="polymarket_rtds_chainlink",
        instrument="btc/usd",
        event_type="crypto_prices_chainlink",
        at=at,
        sequence=sequence,
        payload={
            "topic": "crypto_prices_chainlink",
            "payload": {
                "symbol": "btc/usd",
                "timestamp": int(at.timestamp() * 1_000),
                "value": price,
            },
        },
    )


def _book(token: str, at: datetime, bid: str, ask: str, sequence: int) -> RawCollectorEvent:
    return _event(
        source="polymarket_clob",
        instrument=token,
        event_type="book",
        at=at,
        sequence=sequence,
        payload={
            "event_type": "book",
            "asset_id": token,
            "timestamp": int(at.timestamp() * 1_000),
            "bids": [{"price": bid, "size": "10"}],
            "asks": [{"price": ask, "size": "11"}],
        },
    )
