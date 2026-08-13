from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pyarrow as pa
import pytest
from nautilus_trader.core.nautilus_pyo3 import FIXED_SCALAR
from nautilus_trader.model.data import OrderBookDelta, OrderBookDeltas
from nautilus_trader.model.enums import BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId

from scripts.btc_opening_market_audit import parse_args, run

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow
from btc_short_horizon.data.catalog_io import write_market_catalog
from btc_short_horizon.data.collector import PartitionedRawEventWriter, RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.market_catalog import MarketCatalog
from btc_short_horizon.data.session_inventory import (
    SESSION_INVENTORY_MANIFEST_ATTRIBUTE,
    SESSION_INVENTORY_SCHEMA_VERSION,
    SessionInventoryRepository,
)
from btc_short_horizon.data.storage import ImmutableParquetStore
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
    source_at: datetime | None = None,
    admission_sequence: int = 0,
    collector_session_id: str = "test-session",
) -> RawCollectorEvent:
    source_time = source_at or at
    timestamp_millis = int(source_time.timestamp() * 1_000)
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=source_time,
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
        collector_session_id=collector_session_id,
        epoch_id=epoch_id,
        admission_sequence=admission_sequence,
    )


def _raw_hour_directory(root: Path) -> Path:
    return root / "raw" / "polymarket_clob" / UP_TOKEN / "date=2026-04-13" / "hour=00"


def _raw_part_path(root: Path) -> Path:
    return next(_raw_hour_directory(root).glob("part-*.parquet"))


def _raw_manifest_path(root: Path) -> Path:
    return next(_raw_hour_directory(root).glob("manifest-*.json"))


def _v8_raw_writer(root: Path, *, tolerance_seconds: str = "1") -> PartitionedRawEventWriter:
    return PartitionedRawEventWriter(
        root,
        manifest_attributes={
            "polymarket_source_timestamp_regression_tolerance_seconds": (tolerance_seconds)
        },
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
        collector_session_id="test-session",
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
                round(bid * FIXED_SCALAR),
                2,
                10 * FIXED_SCALAR,
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
                round(ask * FIXED_SCALAR),
                2,
                11 * FIXED_SCALAR,
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
    writer.write(
        (_raw_book_event(token_id=UP_TOKEN, at=later, bid="0.61", ask="0.62", epoch_id=1),)
    )
    writer.write((_raw_book_event(token_id=UP_TOKEN, at=early, bid="0.60", ask="0.61"),))

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


def test_forward_reader_skips_legacy_schema_before_validating_v7_columns(tmp_path: Path) -> None:
    event_time = T0 + timedelta(seconds=2)
    PartitionedRawEventWriter(tmp_path).write(
        (
            _raw_book_event(
                token_id=UP_TOKEN,
                at=event_time,
                bid="0.61",
                ask="0.62",
                ingest_version="btc-short-horizon-v7",
            ),
        )
    )
    ts_ns = int((T0 + timedelta(seconds=1)).timestamp() * 1_000_000_000)
    ImmutableParquetStore(tmp_path).write(
        table=pa.table(
            {
                "source_ts_ns": [ts_ns],
                "collector_receive_ts_ns": [ts_ns],
                "available_ts_ns": [ts_ns],
                "sequence_or_hash": ["legacy-v6"],
                "source": ["polymarket_clob"],
                "instrument": [UP_TOKEN],
                "ingest_version": ["btc-short-horizon-v6"],
                "event_type": ["book"],
                "epoch_id": [0],
                "payload_json": ["{}"],
            }
        ),
        source="polymarket_clob",
        instrument=UP_TOKEN,
        partition_date="2026-04-13",
        partition_hour="00",
        schema_version="polymarket-market-ws-v0",
        ingest_version="btc-short-horizon-v6",
        attributes={"collector_session_id": "legacy-session"},
    )

    load = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=UP_TOKEN,
        start_time=T0,
        end_time=T0 + timedelta(seconds=3),
        ingest_version="btc-short-horizon-v7",
    )

    assert load.raw_part_count == 1
    assert load.raw_row_count == 1
    assert len(load.events) == 1
    with pytest.raises(RawPayloadError, match="supported event schema"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0 + timedelta(seconds=3),
            ingest_version="btc-short-horizon-v6",
        )


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
    ts_ns = int(T0.timestamp() * 1_000_000_000)
    ImmutableParquetStore(tmp_path).write(
        table=pa.table(
            {
                "source_ts_ns": [ts_ns],
                "collector_receive_ts_ns": [ts_ns],
                "available_ts_ns": [ts_ns],
                "sequence_or_hash": ["broken"],
                "source": ["polymarket_clob"],
                "instrument": [UP_TOKEN],
                "schema_version": ["polymarket-market-ws-v1"],
                "ingest_version": ["btc-short-horizon-v2"],
                "event_type": ["book"],
                "collector_session_id": ["test-session"],
                "epoch_id": [0],
                "admission_sequence": [0],
                "payload_json": ['{"event_type":"book":"broken"}'],
            }
        ),
        source="polymarket_clob",
        instrument=UP_TOKEN,
        partition_date="2026-04-13",
        partition_hour="00",
        schema_version="polymarket-market-ws-v1",
        ingest_version="btc-short-horizon-v2",
        attributes={"collector_session_id": "test-session"},
    )

    with pytest.raises(RawPayloadError, match="invalid payload_json"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
        )


def test_forward_reader_rejects_an_orphan_part(tmp_path: Path) -> None:
    PartitionedRawEventWriter(tmp_path).write(
        (_raw_book_event(token_id=UP_TOKEN, at=T0, bid="0.60", ask="0.61"),)
    )
    (_raw_hour_directory(tmp_path) / "part-orphan.parquet").write_bytes(b"unmanifested")

    with pytest.raises(RawPayloadError, match="no manifest"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
        )


def test_forward_reader_rejects_a_manifest_with_a_missing_part(tmp_path: Path) -> None:
    PartitionedRawEventWriter(tmp_path).write(
        (_raw_book_event(token_id=UP_TOKEN, at=T0, bid="0.60", ask="0.61"),)
    )
    _raw_part_path(tmp_path).unlink()

    with pytest.raises(RawPayloadError, match="missing part"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
        )


def test_forward_reader_rejects_a_tampered_part(tmp_path: Path) -> None:
    PartitionedRawEventWriter(tmp_path).write(
        (_raw_book_event(token_id=UP_TOKEN, at=T0, bid="0.60", ask="0.61"),)
    )
    part_path = _raw_part_path(tmp_path)
    part_path.write_bytes(part_path.read_bytes() + b"tampered")

    with pytest.raises(RawPayloadError, match="sha256 mismatch"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", "other-schema"),
        ("ingest_version", "other-ingest"),
        ("instrument", DOWN_TOKEN),
        ("data_path", "raw/elsewhere/part.parquet"),
    ),
)
def test_forward_reader_rejects_manifest_identity_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    PartitionedRawEventWriter(tmp_path).write(
        (_raw_book_event(token_id=UP_TOKEN, at=T0, bid="0.60", ask="0.61"),)
    )
    manifest_path = _raw_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] = value
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RawPayloadError):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
        )


def test_forward_reader_rejects_manifest_session_mismatch(tmp_path: Path) -> None:
    PartitionedRawEventWriter(tmp_path).write(
        (_raw_book_event(token_id=UP_TOKEN, at=T0, bid="0.60", ask="0.61"),)
    )
    manifest_path = _raw_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["attributes"]["collector_session_id"] = "other-session"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RawPayloadError, match="collector_session_id does not match"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
        )


@pytest.mark.parametrize(
    "field",
    (
        "row_count",
        "min_source_ts_ns",
        "max_source_ts_ns",
        "min_available_ts_ns",
        "max_available_ts_ns",
    ),
)
def test_forward_reader_rejects_manifest_count_or_timestamp_mismatch(
    tmp_path: Path,
    field: str,
) -> None:
    PartitionedRawEventWriter(tmp_path).write(
        (_raw_book_event(token_id=UP_TOKEN, at=T0, bid="0.60", ask="0.61"),)
    )
    manifest_path = _raw_manifest_path(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload[field] += 1
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RawPayloadError, match="manifest"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
        )


def test_forward_reader_rejects_mixed_ingest_versions_without_selection(
    tmp_path: Path,
) -> None:
    PartitionedRawEventWriter(tmp_path).write(
        (
            _raw_book_event(
                token_id=UP_TOKEN,
                at=T0,
                bid="0.60",
                ask="0.61",
                ingest_version="btc-short-horizon-v2",
            ),
            _raw_book_event(
                token_id=UP_TOKEN,
                at=T0 + timedelta(milliseconds=1),
                bid="0.60",
                ask="0.61",
                ingest_version="btc-short-horizon-v3",
            ),
        )
    )

    with pytest.raises(RawPayloadError, match="mix ingest versions"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0 + timedelta(seconds=1),
        )


def test_forward_reader_selects_v10_with_inventory_managed_v9_in_same_hour(
    tmp_path: Path,
) -> None:
    for session_id, ingest_version, bid in (
        ("session-v9", "btc-short-horizon-v9", "0.40"),
        ("session-v10", "btc-short-horizon-v10", "0.60"),
    ):
        inventory = SessionInventoryRepository(tmp_path).start_session(
            session_id=session_id,
            ingest_version=ingest_version,
        )
        PartitionedRawEventWriter(
            tmp_path,
            manifest_attributes={
                "polymarket_source_timestamp_regression_tolerance_seconds": "1",
                SESSION_INVENTORY_MANIFEST_ATTRIBUTE: SESSION_INVENTORY_SCHEMA_VERSION,
            },
            inventory=inventory,
        ).write(
            (
                _raw_book_event(
                    token_id=UP_TOKEN,
                    at=T0,
                    bid=bid,
                    ask="0.61",
                    ingest_version=ingest_version,
                    collector_session_id=session_id,
                ),
            )
        )
        inventory.complete()

    loaded = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=UP_TOKEN,
        start_time=T0,
        end_time=T0,
        ingest_version="btc-short-horizon-v10",
    )

    assert loaded.raw_part_count == 1
    assert len(loaded.events) == 1
    assert loaded.events[0].book is not None
    assert loaded.events[0].book.bid == 0.60


def test_forward_reader_audits_inventoried_same_hour_part_outside_requested_window(
    tmp_path: Path,
) -> None:
    for session_id, ingest_version, at, bid in (
        ("session-v10", "btc-short-horizon-v10", T0, "0.60"),
        (
            "session-v9-later",
            "btc-short-horizon-v9",
            T0 + timedelta(minutes=5),
            "0.40",
        ),
    ):
        inventory = SessionInventoryRepository(tmp_path).start_session(
            session_id=session_id,
            ingest_version=ingest_version,
        )
        PartitionedRawEventWriter(
            tmp_path,
            manifest_attributes={
                "polymarket_source_timestamp_regression_tolerance_seconds": "1",
                SESSION_INVENTORY_MANIFEST_ATTRIBUTE: SESSION_INVENTORY_SCHEMA_VERSION,
            },
            inventory=inventory,
        ).write(
            (
                _raw_book_event(
                    token_id=UP_TOKEN,
                    at=at,
                    bid=bid,
                    ask="0.61",
                    ingest_version=ingest_version,
                    collector_session_id=session_id,
                ),
            )
        )
        inventory.complete()

    loaded = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=UP_TOKEN,
        start_time=T0,
        end_time=T0 + timedelta(minutes=3),
        ingest_version="btc-short-horizon-v10",
    )

    assert loaded.raw_part_count == 1
    assert len(loaded.events) == 1
    assert loaded.events[0].book is not None
    assert loaded.events[0].book.bid == 0.60


def test_forward_reader_requires_v8_polymarket_tolerance_manifest_attribute(
    tmp_path: Path,
) -> None:
    PartitionedRawEventWriter(tmp_path).write(
        (
            _raw_book_event(
                token_id=UP_TOKEN,
                at=T0,
                bid="0.60",
                ask="0.61",
                ingest_version="btc-short-horizon-v8",
            ),
        )
    )

    with pytest.raises(RawPayloadError, match="no Polymarket timestamp tolerance"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
            ingest_version="btc-short-horizon-v8",
        )


def test_forward_reader_detects_deleted_part_and_manifest_from_session_inventory(
    tmp_path: Path,
) -> None:
    inventory = SessionInventoryRepository(tmp_path).start_session(
        session_id="test-session",
        ingest_version="btc-short-horizon-v10",
    )
    writer = PartitionedRawEventWriter(
        tmp_path,
        manifest_attributes={
            "polymarket_source_timestamp_regression_tolerance_seconds": "1",
            SESSION_INVENTORY_MANIFEST_ATTRIBUTE: SESSION_INVENTORY_SCHEMA_VERSION,
        },
        inventory=inventory,
    )
    manifest = writer.write(
        (
            _raw_book_event(
                token_id=UP_TOKEN,
                at=T0,
                bid="0.60",
                ask="0.61",
                ingest_version="btc-short-horizon-v10",
            ),
        )
    )[0]
    data_path = tmp_path / manifest.data_path
    data_path.unlink()
    data_path.with_name(f"manifest-{manifest.sha256[:32]}.json").unlink()

    with pytest.raises(RawPayloadError, match="references missing raw manifests"):
        load_forward_polymarket_book_events(
            raw_data_root=tmp_path,
            token_id=UP_TOKEN,
            start_time=T0,
            end_time=T0,
            ingest_version="btc-short-horizon-v10",
            expected_source_timestamp_regression_tolerance_seconds=1.0,
        )


def test_forward_reader_replays_polymarket_timestamp_tolerance_from_manifest(
    tmp_path: Path,
) -> None:
    _v8_raw_writer(tmp_path, tolerance_seconds="0.25").write(
        (
            _raw_book_event(
                token_id=UP_TOKEN,
                at=T0 + timedelta(milliseconds=300),
                bid="0.60",
                ask="0.61",
                ingest_version="btc-short-horizon-v8",
            ),
            _raw_book_event(
                token_id=UP_TOKEN,
                at=T0 + timedelta(milliseconds=400),
                source_at=T0,
                bid="0.59",
                ask="0.60",
                ingest_version="btc-short-horizon-v8",
                admission_sequence=1,
            ),
        )
    )

    loaded = load_forward_polymarket_book_events(
        raw_data_root=tmp_path,
        token_id=UP_TOKEN,
        start_time=T0,
        end_time=T0 + timedelta(seconds=1),
        ingest_version="btc-short-horizon-v8",
    )

    assert len(loaded.events) == 2
    assert not loaded.events[0].reset_book
    assert loaded.events[1].reset_book
    assert loaded.events[1].book is not None
    assert loaded.events[1].book.bid == 0.59


def test_opening_market_audit_writes_observations_and_coverage(tmp_path: Path) -> None:
    event_time = T0 + timedelta(seconds=2)
    inventory = SessionInventoryRepository(tmp_path).start_session(
        session_id="test-session",
        ingest_version="btc-short-horizon-v16",
    )
    writer = PartitionedRawEventWriter(
        tmp_path,
        manifest_attributes={
            "polymarket_source_timestamp_regression_tolerance_seconds": "1",
            SESSION_INVENTORY_MANIFEST_ATTRIBUTE: SESSION_INVENTORY_SCHEMA_VERSION,
        },
        inventory=inventory,
    )
    writer.write(
        (
            _raw_book_event(
                token_id=UP_TOKEN,
                at=event_time,
                bid="0.60",
                ask="0.61",
                ingest_version="btc-short-horizon-v16",
            ),
            _raw_book_event(
                token_id=DOWN_TOKEN,
                at=event_time,
                bid="0.39",
                ask="0.40",
                ingest_version="btc-short-horizon-v16",
            ),
        )
    )
    inventory.complete()
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
