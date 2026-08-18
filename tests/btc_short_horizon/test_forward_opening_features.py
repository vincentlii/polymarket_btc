from __future__ import annotations

from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path

import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow, TimedMarketEvent
from btc_short_horizon.data.collector import PartitionedRawEventWriter, RawCollectorEvent
from btc_short_horizon.features import opening_feature_schema
from btc_short_horizon.research.opening_features import (
    ForwardFeatureStateEvent,
    ForwardOpeningReadinessBuild,
    _feature_event_sort_key,
    build_forward_opening_feature_observations,
    build_forward_opening_readiness_observations,
    _okx_state_events,
)
from btc_short_horizon.research.opening_evidence import ForwardRawEvent, RawPayloadError


T0 = datetime(2026, 4, 13, tzinfo=UTC)
UP_TOKEN = "up-token"
DOWN_TOKEN = "down-token"
INGEST_VERSION = "btc-short-horizon-v2"


def test_okx_raw_trades_and_books_rebuild_causal_feature_state() -> None:
    base = int(T0.timestamp() * 1_000_000_000)

    def event(event_type: str, payload: dict[str, object], offset: int) -> ForwardRawEvent:
        return ForwardRawEvent(
            path=Path("okx.parquet"),
            row_index=offset,
            source_ts_ns=base + offset,
            collector_receive_ts_ns=base + offset,
            available_ts_ns=base + offset,
            sequence_or_hash=str(offset),
            source="okx_spot",
            instrument="BTC-USDT",
            schema_version="test",
            ingest_version=INGEST_VERSION,
            event_type=event_type,
            collector_session_id="session",
            epoch_id=0,
            admission_sequence=offset,
            payload=payload,
        )

    events, gaps = _okx_state_events(
        (
            event(
                "books_snapshot",
                {
                    "data": [
                        {
                            "ts": str(int(T0.timestamp() * 1000)),
                            "seqId": 1,
                            "prevSeqId": -1,
                            "bids": [["100", "2", "0", "1"]],
                            "asks": [["101", "1", "0", "1"]],
                        }
                    ]
                },
                1,
            ),
            event(
                "trade",
                {
                    "data": [
                        {
                            "instId": "BTC-USDT",
                            "ts": str(int(T0.timestamp() * 1000)),
                            "tradeId": "7",
                            "side": "buy",
                            "px": "100.5",
                            "sz": "0.1",
                        }
                    ]
                },
                2,
            ),
        ),
        source="okx_spot",
        instrument="BTC-USDT",
    )

    assert gaps == 0
    assert len(events) == 2
    assert events[0].value.bid == 100.0  # type: ignore[union-attr]
    assert events[1].value.price == 100.5  # type: ignore[union-attr]


def _market() -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(T0),
        condition_id="condition",
        up_token_id=UP_TOKEN,
        down_token_id=DOWN_TOKEN,
        t0=T0,
        t1=T0 + timedelta(minutes=15),
        rule_epoch="chainlink-v1",
        rule_hash="a" * 64,
    )


def _raw_event(
    *,
    at: datetime,
    source: str,
    instrument: str,
    event_type: str,
    payload: dict[str, object],
    epoch_id: int = 0,
    collector_session_id: str = "test-session",
    ingest_version: str = INGEST_VERSION,
    admission_sequence: int = 0,
) -> RawCollectorEvent:
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=at,
            collector_receive_ts=at,
            available_ts=at,
            sequence_or_hash=f"{source}-{instrument}-{event_type}-{epoch_id}-{at.timestamp()}",
            source=source,
            instrument=instrument,
            schema_version="test-v1",
            ingest_version=ingest_version,
        ),
        event_type=event_type,
        payload=payload,
        collector_session_id=collector_session_id,
        epoch_id=epoch_id,
        admission_sequence=admission_sequence,
    )


def _clob_book(
    *,
    token_id: str,
    at: datetime,
    bid: str,
    ask: str,
    epoch_id: int = 0,
    ingest_version: str = INGEST_VERSION,
) -> RawCollectorEvent:
    return _raw_event(
        at=at,
        source="polymarket_clob",
        instrument=token_id,
        event_type="book",
        epoch_id=epoch_id,
        ingest_version=ingest_version,
        payload={
            "event_type": "book",
            "asset_id": token_id,
            "timestamp": int(at.timestamp() * 1_000),
            "bids": [{"price": bid, "size": "10"}],
            "asks": [{"price": ask, "size": "11"}],
        },
    )


def test_forward_feature_sort_preserves_gap_before_same_time_admission() -> None:
    timestamp = int(T0.timestamp() * 1_000_000_000)
    gap = ForwardFeatureStateEvent(
        raw_source="polymarket_clob",
        state_source="polymarket_clob",
        state_instrument=UP_TOKEN,
        source_ts_ns=timestamp,
        collector_receive_ts_ns=timestamp,
        available_ts_ns=timestamp,
        sequence_or_hash="gap",
        collector_session_id="session",
        epoch_id=1,
        admission_sequence=10,
        gap_before=True,
    )
    snapshot = ForwardFeatureStateEvent(
        raw_source="polymarket_clob",
        state_source="polymarket_clob",
        state_instrument=UP_TOKEN,
        source_ts_ns=timestamp - 1_000_000_000,
        collector_receive_ts_ns=timestamp,
        available_ts_ns=timestamp,
        sequence_or_hash="snapshot",
        collector_session_id="session",
        epoch_id=1,
        admission_sequence=11,
        gap_before=True,
    )

    assert sorted((snapshot, gap), key=_feature_event_sort_key) == [gap, snapshot]


def test_forward_features_reject_different_up_down_polymarket_tolerances(
    tmp_path: Path,
) -> None:
    v8 = "btc-short-horizon-v8"
    PartitionedRawEventWriter(
        tmp_path,
        manifest_attributes={"polymarket_source_timestamp_regression_tolerance_seconds": "0.25"},
    ).write(
        (
            _clob_book(
                token_id=UP_TOKEN,
                at=T0,
                bid="0.60",
                ask="0.61",
                ingest_version=v8,
            ),
        )
    )
    PartitionedRawEventWriter(
        tmp_path,
        manifest_attributes={"polymarket_source_timestamp_regression_tolerance_seconds": "0.5"},
    ).write(
        (
            _clob_book(
                token_id=DOWN_TOKEN,
                at=T0,
                bid="0.39",
                ask="0.40",
                ingest_version=v8,
            ),
        )
    )

    with pytest.raises(RawPayloadError, match="different Polymarket timestamp tolerances"):
        build_forward_opening_feature_observations(
            raw_data_root=tmp_path,
            market=_market(),
            start_time=T0,
            end_time=T0 + timedelta(seconds=1),
            decision_ts_ns=(int(T0.timestamp() * 1_000_000_000),),
            ingest_version=v8,
            required_venue_sources=("binance_spot",),
        )


def _binance_trade(
    *, source: str, at: datetime, price: str, sequence: int, epoch_id: int = 0
) -> RawCollectorEvent:
    is_perp = source == "binance_perp"
    message: dict[str, object] = {
        "e": "aggTrade" if is_perp else "trade",
        "s": "BTCUSDT",
        "T": int(at.timestamp() * 1_000),
        "p": price,
        "q": "1",
        "m": False,
    }
    message["a" if is_perp else "t"] = sequence
    return _raw_event(
        at=at,
        source=source,
        instrument="BTCUSDT",
        event_type="aggtrade" if is_perp else "trade",
        epoch_id=epoch_id,
        payload={"stream": "btcusdt@aggTrade" if is_perp else "btcusdt@trade", "data": message},
    )


def _binance_book(
    *, source: str, at: datetime, bid: str, ask: str, epoch_id: int = 0
) -> RawCollectorEvent:
    return _raw_event(
        at=at,
        source=source,
        instrument="BTCUSDT",
        event_type="book_ticker",
        epoch_id=epoch_id,
        payload={
            "stream": "btcusdt@bookTicker",
            "data": {
                "s": "BTCUSDT",
                "u": int(at.timestamp() * 1_000),
                "b": bid,
                "B": "2",
                "a": ask,
                "A": "3",
            },
        },
    )


def _okx_book(
    *, source: str, at: datetime, bid: str, ask: str, sequence: int = 1
) -> RawCollectorEvent:
    instrument = "BTC-USDT" if source == "okx_spot" else "BTC-USDT-SWAP"
    return _raw_event(
        at=at,
        source=source,
        instrument=instrument,
        event_type="books_snapshot",
        payload={
            "data": [
                {
                    "ts": str(int(at.timestamp() * 1_000)),
                    "seqId": sequence,
                    "prevSeqId": -1,
                    "bids": [[bid, "2", "0", "1"]],
                    "asks": [[ask, "3", "0", "1"]],
                }
            ]
        },
    )


def _chainlink(*, at: datetime, price: float, epoch_id: int = 0) -> RawCollectorEvent:
    return _raw_event(
        at=at,
        source="polymarket_rtds_chainlink",
        instrument="btc/usd",
        event_type="crypto_prices_chainlink",
        epoch_id=epoch_id,
        payload={
            "topic": "crypto_prices_chainlink",
            "payload": {
                "symbol": "btc/usd",
                "timestamp": int(at.timestamp() * 1_000),
                "value": price,
            },
        },
    )


def _events(*, spot_epoch_at_decision: int = 0) -> tuple[RawCollectorEvent, ...]:
    return (
        _chainlink(at=T0 - timedelta(seconds=1), price=100.0),
        _binance_trade(source="binance_spot", at=T0, price="100", sequence=1),
        _binance_trade(
            source="binance_spot", at=T0 + timedelta(seconds=1), price="101", sequence=2
        ),
        _binance_trade(
            source="binance_spot",
            at=T0 + timedelta(seconds=2),
            price="102",
            sequence=3,
            epoch_id=spot_epoch_at_decision,
        ),
        _binance_trade(source="binance_perp", at=T0, price="100", sequence=1),
        _binance_trade(
            source="binance_perp", at=T0 + timedelta(seconds=1), price="101", sequence=2
        ),
        _binance_trade(
            source="binance_perp", at=T0 + timedelta(seconds=2), price="102", sequence=3
        ),
        _binance_book(
            source="binance_spot",
            at=T0 + timedelta(seconds=2),
            bid="101",
            ask="102",
            epoch_id=spot_epoch_at_decision,
        ),
        _binance_book(source="binance_perp", at=T0 + timedelta(seconds=2), bid="101", ask="102"),
        _clob_book(token_id=UP_TOKEN, at=T0 + timedelta(seconds=2), bid="0.60", ask="0.61"),
        _clob_book(token_id=DOWN_TOKEN, at=T0 + timedelta(seconds=2), bid="0.39", ask="0.40"),
    )


def test_forward_raw_builds_an_eligible_causal_opening_feature_observation(tmp_path: Path) -> None:
    PartitionedRawEventWriter(tmp_path).write(_events())
    decision = int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000)

    result = build_forward_opening_feature_observations(
        raw_data_root=tmp_path,
        market=_market(),
        start_time=T0 - timedelta(minutes=5),
        end_time=T0 + timedelta(seconds=3),
        decision_ts_ns=(decision,),
        ingest_version=INGEST_VERSION,
        required_venue_sources=("binance_spot", "binance_perp"),
    )

    observation = result.observations[0]
    values = opening_feature_schema().mapping_from(observation.values)
    assert observation.eligible
    assert observation.p_market_mid_up == pytest.approx(0.605)
    assert values["binance_spot_return_5s"] > 0.0
    assert result.source_event_count("polymarket_clob") == 2


def test_bounded_forward_build_matches_retained_event_build(tmp_path: Path) -> None:
    PartitionedRawEventWriter(tmp_path).write(_events())
    decisions = tuple(
        int((T0 + timedelta(seconds=offset)).timestamp() * 1_000_000_000) for offset in (1, 2, 3)
    )
    arguments = {
        "raw_data_root": tmp_path,
        "market": _market(),
        "start_time": T0 - timedelta(minutes=5),
        "end_time": T0 + timedelta(seconds=3),
        "decision_ts_ns": decisions,
        "ingest_version": INGEST_VERSION,
        "required_venue_sources": ("binance_spot", "binance_perp"),
    }

    retained = build_forward_opening_feature_observations(**arguments)
    bounded = build_forward_opening_readiness_observations(
        **arguments,
    )

    assert bounded.observations == retained.observations
    assert bounded.source_summaries == retained.source_summaries


def test_readiness_feature_interface_cannot_extend_into_exit_lifecycle() -> None:
    """Model features and full-lifecycle exit evidence are separate proofs."""

    parameters = signature(build_forward_opening_readiness_observations).parameters
    assert "polymarket_terminal_end_time" not in parameters
    assert "polymarket_exit_evidence" not in ForwardOpeningReadinessBuild.__dataclass_fields__


def test_bounded_forward_build_matches_all_six_source_path(tmp_path: Path) -> None:
    events = (
        *_events(),
        _okx_book(source="okx_spot", at=T0 + timedelta(seconds=2), bid="101", ask="102"),
        _okx_book(source="okx_swap", at=T0 + timedelta(seconds=2), bid="101", ask="102"),
    )
    PartitionedRawEventWriter(tmp_path).write(events)
    decisions = tuple(
        int((T0 + timedelta(seconds=offset)).timestamp() * 1_000_000_000)
        for offset in (1, 2, 3)
    )
    arguments = {
        "raw_data_root": tmp_path,
        "market": _market(),
        "start_time": T0 - timedelta(minutes=5),
        "end_time": T0 + timedelta(seconds=3),
        "decision_ts_ns": decisions,
        "ingest_version": INGEST_VERSION,
        "required_venue_sources": (
            "binance_spot",
            "binance_perp",
            "okx_spot",
            "okx_swap",
        ),
    }

    retained = build_forward_opening_feature_observations(**arguments)
    bounded = build_forward_opening_readiness_observations(**arguments)

    assert bounded.observations == retained.observations
    assert bounded.source_summaries == retained.source_summaries


def test_forward_raw_epoch_change_marks_opening_feature_observation_ineligible(
    tmp_path: Path,
) -> None:
    PartitionedRawEventWriter(tmp_path).write(_events(spot_epoch_at_decision=1))
    decision = int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000)

    result = build_forward_opening_feature_observations(
        raw_data_root=tmp_path,
        market=_market(),
        start_time=T0 - timedelta(minutes=5),
        end_time=T0 + timedelta(seconds=3),
        decision_ts_ns=(decision,),
        ingest_version=INGEST_VERSION,
        required_venue_sources=("binance_spot", "binance_perp"),
    )

    assert "gap" in result.observations[0].quality_flags


def test_forward_raw_treats_epoch_ids_as_opaque_not_monotonic(tmp_path: Path) -> None:
    non_chainlink = tuple(
        event for event in _events() if event.timing.source != "polymarket_rtds_chainlink"
    )
    PartitionedRawEventWriter(tmp_path).write(
        (
            *non_chainlink,
            _chainlink(at=T0 - timedelta(seconds=2), price=99.0, epoch_id=900),
            _chainlink(at=T0 - timedelta(seconds=1), price=100.0, epoch_id=100),
        )
    )
    decision = int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000)

    result = build_forward_opening_feature_observations(
        raw_data_root=tmp_path,
        market=_market(),
        start_time=T0 - timedelta(minutes=5),
        end_time=T0 + timedelta(seconds=3),
        decision_ts_ns=(decision,),
        ingest_version=INGEST_VERSION,
        required_venue_sources=("binance_spot", "binance_perp"),
    )

    assert "gap" in result.observations[0].quality_flags


def test_forward_raw_rejects_a_completed_epoch_that_reappears(tmp_path: Path) -> None:
    non_chainlink = tuple(
        event for event in _events() if event.timing.source != "polymarket_rtds_chainlink"
    )
    PartitionedRawEventWriter(tmp_path).write(
        (
            *non_chainlink,
            _chainlink(at=T0 - timedelta(seconds=3), price=98.0, epoch_id=900),
            _chainlink(at=T0 - timedelta(seconds=2), price=99.0, epoch_id=100),
            _chainlink(at=T0 - timedelta(seconds=1), price=100.0, epoch_id=900),
        )
    )
    decision = int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000)

    with pytest.raises(RawPayloadError, match="epoch reappeared"):
        build_forward_opening_feature_observations(
            raw_data_root=tmp_path,
            market=_market(),
            start_time=T0 - timedelta(minutes=5),
            end_time=T0 + timedelta(seconds=3),
            decision_ts_ns=(decision,),
            ingest_version=INGEST_VERSION,
            required_venue_sources=("binance_spot", "binance_perp"),
        )


def test_forward_raw_scopes_binance_epochs_to_each_logical_stream(tmp_path: Path) -> None:
    non_spot_events = tuple(event for event in _events() if event.timing.source != "binance_spot")
    spot_events = (
        _binance_trade(source="binance_spot", at=T0, price="100", sequence=1),
        _binance_book(
            source="binance_spot",
            at=T0 + timedelta(seconds=2),
            bid="100",
            ask="101",
            epoch_id=1,
        ),
        _binance_trade(
            source="binance_spot",
            at=T0 + timedelta(seconds=2, milliseconds=500),
            price="102",
            sequence=2,
        ),
    )
    PartitionedRawEventWriter(tmp_path).write((*non_spot_events, *spot_events))
    decision = int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000)

    result = build_forward_opening_feature_observations(
        raw_data_root=tmp_path,
        market=_market(),
        start_time=T0 - timedelta(minutes=5),
        end_time=T0 + timedelta(seconds=3),
        decision_ts_ns=(decision,),
        ingest_version=INGEST_VERSION,
        required_venue_sources=("binance_spot", "binance_perp"),
    )

    assert result.observations[0].eligible


def test_forward_raw_ignores_zero_sized_book_ticker_without_fabricating_depth(
    tmp_path: Path,
) -> None:
    zero_sized_book = _raw_event(
        at=T0 + timedelta(seconds=2, milliseconds=500),
        source="binance_spot",
        instrument="BTCUSDT",
        event_type="book_ticker",
        payload={
            "stream": "btcusdt@bookTicker",
            "data": {
                "s": "BTCUSDT",
                "u": 123,
                "b": "101",
                "B": "0",
                "a": "102",
                "A": "3",
            },
        },
    )
    PartitionedRawEventWriter(tmp_path).write((*_events(), zero_sized_book))
    decision = int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000)

    result = build_forward_opening_feature_observations(
        raw_data_root=tmp_path,
        market=_market(),
        start_time=T0 - timedelta(minutes=5),
        end_time=T0 + timedelta(seconds=3),
        decision_ts_ns=(decision,),
        ingest_version=INGEST_VERSION,
        required_venue_sources=("binance_spot", "binance_perp"),
    )

    assert result.observations[0].eligible
