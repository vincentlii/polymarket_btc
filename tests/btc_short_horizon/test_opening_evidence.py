from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from nautilus_trader.model.data import OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from scripts.btc_opening_market_audit import parse_args, run

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow
from btc_short_horizon.data.catalog_io import write_market_catalog
from btc_short_horizon.data.collector import PartitionedRawEventWriter, RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.market_catalog import MarketCatalog
from btc_short_horizon.features.events import BtcBookTop
from btc_short_horizon.research.opening_evidence import (
    RawPayloadError,
    TokenBookStateEvent,
    build_opening_market_observations,
    load_forward_polymarket_book_events,
    pmxt_order_book_state_events,
)


T0 = datetime(2026, 4, 13, tzinfo=UTC)
UP_TOKEN = "up-token"
DOWN_TOKEN = "down-token"


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


def _raw_book_event(
    *,
    token_id: str,
    at: datetime,
    bid: str,
    ask: str,
    epoch_id: int = 0,
    ingest_version: str = "btc-short-horizon-v2",
) -> RawCollectorEvent:
    timestamp_millis = int(at.timestamp() * 1_000)
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=at,
            collector_receive_ts=at,
            available_ts=at,
            sequence_or_hash=f"{token_id}-{epoch_id}-{timestamp_millis}",
            source="polymarket_clob",
            instrument=token_id,
            schema_version="polymarket-market-ws-v1",
            ingest_version=ingest_version,
        ),
        event_type="book",
        payload={
            "event_type": "book",
            "asset_id": token_id,
            "timestamp": timestamp_millis,
            "bids": [{"price": bid, "size": "10"}],
            "asks": [{"price": ask, "size": "11"}],
        },
        epoch_id=epoch_id,
    )


def _book_state(
    *, token_id: str, at: datetime, bid: float, ask: float, epoch_id: int, reset_book: bool = False
) -> TokenBookStateEvent:
    ts_ns = int(at.timestamp() * 1_000_000_000)
    return TokenBookStateEvent(
        token_id=token_id,
        source_ts_ns=ts_ns,
        available_ts_ns=ts_ns,
        epoch_id=epoch_id,
        book=BtcBookTop(
            source_ts_ns=ts_ns,
            available_ts_ns=ts_ns,
            bid=bid,
            ask=ask,
            bid_size=10.0,
            ask_size=11.0,
            source="polymarket_clob",
            instrument=token_id,
        ),
        reset_book=reset_book,
    )


def _pmxt_book_record(*, token_id: str, bid: float, ask: float, at: datetime) -> OrderBookDeltas:
    instrument = InstrumentId.from_str(f"{token_id}.POLYMARKET")
    ts_event = int(at.timestamp() * 1_000_000_000)
    return OrderBookDeltas(
        instrument,
        [
            OrderBookDelta.from_raw(
                instrument,
                BookAction.ADD,
                OrderSide.BUY,
                round(bid * 1_000_000_000),
                2,
                10 * 1_000_000_000,
                0,
                0,
                0,
                1,
                ts_event,
                ts_event + 1,
            ),
            OrderBookDelta.from_raw(
                instrument,
                BookAction.ADD,
                OrderSide.SELL,
                round(ask * 1_000_000_000),
                2,
                11 * 1_000_000_000,
                0,
                0,
                0,
                2,
                ts_event,
                ts_event + 1,
            ),
        ],
    )


def test_forward_raw_l2_builds_causal_dual_token_market_observation(tmp_path: Path) -> None:
    event_time = T0 + timedelta(seconds=2)
    PartitionedRawEventWriter(tmp_path).write(
        (
            _raw_book_event(token_id=UP_TOKEN, at=event_time, bid="0.60", ask="0.61"),
            _raw_book_event(token_id=DOWN_TOKEN, at=event_time, bid="0.39", ask="0.40"),
        )
    )

    up = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=UP_TOKEN,
        start_time=T0,
        end_time=T0 + timedelta(seconds=3),
    )
    down = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=DOWN_TOKEN,
        start_time=T0,
        end_time=T0 + timedelta(seconds=3),
    )
    observations = build_opening_market_observations(
        market=_market(),
        up_events=up.events,
        down_events=down.events,
        decision_ts_ns=(int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000),),
    )

    assert up.raw_part_count == 1
    assert down.raw_part_count == 1
    assert len(observations) == 1
    assert observations[0].p_market_mid_up == pytest.approx(0.605)
    assert observations[0].data_age_seconds == pytest.approx(1.0)
    assert not observations[0].has_data_gap
    assert observations[0].structure_valid


def test_forward_reader_orders_hashed_parts_by_causal_timestamps(tmp_path: Path) -> None:
    early = T0 + timedelta(seconds=1)
    later = T0 + timedelta(seconds=2)
    writer = PartitionedRawEventWriter(tmp_path)
    writer.write((_raw_book_event(token_id=UP_TOKEN, at=early, bid="0.60", ask="0.61"),))
    writer.write(
        (_raw_book_event(token_id=UP_TOKEN, at=later, bid="0.61", ask="0.62", epoch_id=1),)
    )
    directory = tmp_path / "raw" / "polymarket_clob" / UP_TOKEN / "date=2026-04-13" / "hour=00"
    for path in directory.glob("part-*.parquet"):
        epoch_id = pq.read_table(path, columns=["epoch_id"]).column("epoch_id")[0].as_py()
        target = directory / ("part-z.parquet" if epoch_id == 0 else "part-a.parquet")
        path.rename(target)

    load = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=UP_TOKEN,
        start_time=T0,
        end_time=T0 + timedelta(seconds=3),
    )

    assert [event.epoch_id for event in load.events] == [0, 1]
    assert load.events[1].reset_book


def test_forward_reader_finds_long_token_id_in_bounded_directory(tmp_path: Path) -> None:
    token_id = "9" * 78
    PartitionedRawEventWriter(tmp_path).write(
        (
            _raw_book_event(
                token_id=token_id,
                at=T0 + timedelta(seconds=1),
                bid="0.60",
                ask="0.61",
            ),
        )
    )

    load = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=token_id,
        start_time=T0,
        end_time=T0 + timedelta(seconds=2),
    )

    assert load.raw_part_count == 1
    assert len(load.events) == 1
    assert load.events[0].token_id == token_id


def test_forward_reader_filters_to_one_explicit_ingest_version(tmp_path: Path) -> None:
    writer = PartitionedRawEventWriter(tmp_path)
    writer.write(
        (
            _raw_book_event(
                token_id=UP_TOKEN,
                at=T0 + timedelta(seconds=1),
                bid="0.60",
                ask="0.61",
                ingest_version="btc-short-horizon-v1",
            ),
            _raw_book_event(
                token_id=UP_TOKEN,
                at=T0 + timedelta(seconds=2),
                bid="0.61",
                ask="0.62",
                ingest_version="btc-short-horizon-v2",
            ),
        )
    )

    load = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=UP_TOKEN,
        start_time=T0,
        end_time=T0 + timedelta(seconds=3),
        ingest_version="btc-short-horizon-v2",
    )

    assert load.raw_row_count == 1
    assert len(load.events) == 1
    assert load.events[0].book is not None
    assert load.events[0].book.bid == pytest.approx(0.61)


def test_market_observation_marks_a_post_baseline_epoch_reset_as_a_gap() -> None:
    up_events = (
        _book_state(
            token_id=UP_TOKEN,
            at=T0,
            bid=0.60,
            ask=0.61,
            epoch_id=0,
        ),
        _book_state(
            token_id=UP_TOKEN,
            at=T0 + timedelta(seconds=1),
            bid=0.59,
            ask=0.60,
            epoch_id=1,
            reset_book=True,
        ),
    )
    down_events = (
        _book_state(
            token_id=DOWN_TOKEN,
            at=T0,
            bid=0.39,
            ask=0.40,
            epoch_id=0,
        ),
    )

    observations = build_opening_market_observations(
        market=_market(),
        up_events=up_events,
        down_events=down_events,
        decision_ts_ns=(int((T0 + timedelta(seconds=2)).timestamp() * 1_000_000_000),),
    )

    assert observations[0].has_data_gap
    assert observations[0].up_epoch_id == 1


def test_pmxt_l2_adapter_reuses_nautilus_book_state_for_market_observation() -> None:
    up = pmxt_order_book_state_events(
        token_id=UP_TOKEN,
        records=(_pmxt_book_record(token_id=UP_TOKEN, bid=0.60, ask=0.61, at=T0),),
        gap_hours=("missing-hour",),
    )
    down = pmxt_order_book_state_events(
        token_id=DOWN_TOKEN,
        records=(_pmxt_book_record(token_id=DOWN_TOKEN, bid=0.39, ask=0.40, at=T0),),
    )
    observations = build_opening_market_observations(
        market=_market(),
        up_events=up.events,
        down_events=down.events,
        decision_ts_ns=(int((T0 + timedelta(seconds=1)).timestamp() * 1_000_000_000),),
        initial_data_gap=bool(up.gap_hour_count or down.gap_hour_count),
    )

    assert up.source_book_event_count == 1
    assert up.events[0].book is not None
    assert up.events[0].book.bid == pytest.approx(0.60)
    assert observations[0].p_market_mid_up == pytest.approx(0.605)
    assert observations[0].has_data_gap


def test_forward_reader_rejects_invalid_payload_json_instead_of_repairing_it(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "raw" / "polymarket_clob" / UP_TOKEN / "date=2026-04-13" / "hour=00"
    directory.mkdir(parents=True)
    ts_ns = int(T0.timestamp() * 1_000_000_000)
    pq.write_table(
        pa.table(
            {
                "source_ts_ns": [ts_ns],
                "collector_receive_ts_ns": [ts_ns],
                "available_ts_ns": [ts_ns],
                "sequence_or_hash": ["broken"],
                "source": ["polymarket_clob"],
                "instrument": [UP_TOKEN],
                "ingest_version": ["btc-short-horizon-v2"],
                "event_type": ["book"],
                "epoch_id": [0],
                "payload_json": ['{"event_type":"book":"broken"}'],
            }
        ),
        directory / "part-broken.parquet",
    )

    with pytest.raises(RawPayloadError, match="invalid payload_json"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
        )


def test_opening_market_audit_writes_observations_and_coverage(tmp_path: Path) -> None:
    event_time = T0 + timedelta(seconds=2)
    PartitionedRawEventWriter(tmp_path).write(
        (
            _raw_book_event(
                token_id=UP_TOKEN,
                at=event_time,
                bid="0.60",
                ask="0.61",
                ingest_version="btc-short-horizon-v6",
            ),
            _raw_book_event(
                token_id=DOWN_TOKEN,
                at=event_time,
                bid="0.39",
                ask="0.40",
                ingest_version="btc-short-horizon-v6",
            ),
        )
    )
    catalog_path = tmp_path / "catalog.json"
    market = _market()
    write_market_catalog(
        path=catalog_path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
        collected_at=T0,
    )
    output = tmp_path / "audit"

    result = run(
        parse_args(
            (
                "--market-catalog",
                str(catalog_path),
                "--market-slug",
                market.slug,
                "--raw-data-root",
                str(tmp_path),
                "--lookback-seconds",
                "0",
                "--entry-start-seconds",
                "3",
                "--entry-end-seconds",
                "3",
                "--cadence-milliseconds",
                "1000",
                "--output-directory",
                str(output),
            )
        )
    )

    assert result["decision_coverage"]["observed"] == 1
    assert (output / "opening_market_observations.parquet").is_file()
    assert (output / "metrics.json").is_file()
