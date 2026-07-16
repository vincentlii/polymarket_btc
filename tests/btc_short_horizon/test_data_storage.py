from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pyarrow as pa
import pytest

from btc_short_horizon.data.collector import PartitionedRawEventWriter, RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data import storage as storage_module
from btc_short_horizon.data.storage import ImmutableParquetStore


def _table() -> pa.Table:
    return pa.table(
        {
            "source_ts_ns": [1, 2],
            "available_ts_ns": [3, 4],
            "price": [100.0, 101.0],
        }
    )


def test_store_writes_content_addressed_part_and_manifest(tmp_path: Path) -> None:
    store = ImmutableParquetStore(tmp_path)
    manifest = store.write(
        table=_table(),
        source="binance_spot",
        instrument="btcusdt",
        partition_date="2026-07-11",
        partition_hour="12",
        schema_version="v1",
        ingest_version="v1",
    )

    assert manifest.row_count == 2
    assert manifest.min_source_ts_ns == 1
    assert (tmp_path / manifest.data_path).exists()
    loaded = store.read_manifest(
        f"raw/binance_spot/btcusdt/date=2026-07-11/hour=12/manifest-{manifest.sha256[:32]}.json"
    )
    assert loaded == manifest


def test_store_rejects_unsafe_partition_component(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ImmutableParquetStore(tmp_path).write(
            table=_table(),
            source="../binance",
            instrument="btcusdt",
            partition_date="2026-07-11",
            partition_hour="12",
            schema_version="v1",
            ingest_version="v1",
        )


def test_store_url_encodes_instrument_path_without_changing_manifest_identity(
    tmp_path: Path,
) -> None:
    store = ImmutableParquetStore(tmp_path)

    manifest = store.write(
        table=_table(),
        source="polymarket_rtds_chainlink",
        instrument="btc/usd",
        partition_date="2026-07-11",
        partition_hour="12",
        schema_version="v1",
        ingest_version="v1",
    )

    assert manifest.instrument == "btc/usd"
    assert "/btc%2Fusd/" in manifest.data_path
    assert (tmp_path / manifest.data_path).exists()


def test_store_writes_long_token_id_without_overlong_manifest_temp_path(tmp_path: Path) -> None:
    instrument = "9" * 78
    store = ImmutableParquetStore(tmp_path)

    manifest = store.write(
        table=_table(),
        source="polymarket_clob",
        instrument=instrument,
        partition_date="2026-07-11",
        partition_hour="12",
        schema_version="v1",
        ingest_version="v1",
    )

    manifest_path = (
        tmp_path
        / "raw"
        / "polymarket_clob"
        / instrument
        / "date=2026-07-11"
        / "hour=12"
        / f"manifest-{manifest.sha256[:32]}.json"
    )
    assert manifest_path.exists()
    assert len(str(manifest_path)) < 260
    assert store.read_manifest(str(manifest_path.relative_to(tmp_path))) == manifest


def test_store_removes_new_part_when_manifest_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_manifest_write(_path: Path, _payload: object) -> None:
        raise OSError("manifest write failed")

    monkeypatch.setattr(storage_module, "_atomic_write_json", fail_manifest_write)

    with pytest.raises(OSError, match="manifest write failed"):
        ImmutableParquetStore(tmp_path).write(
            table=_table(),
            source="binance_spot",
            instrument="btcusdt",
            partition_date="2026-07-11",
            partition_hour="12",
            schema_version="v1",
            ingest_version="v1",
        )

    assert not list(tmp_path.rglob("part-*.parquet"))


def test_raw_event_writer_does_not_mix_schema_versions_in_one_manifest(tmp_path: Path) -> None:
    timestamp = datetime(2026, 7, 11, 12, tzinfo=UTC)

    def event(schema_version: str) -> RawCollectorEvent:
        return RawCollectorEvent(
            timing=TimedMarketEvent(
                source_ts=timestamp,
                collector_receive_ts=timestamp,
                available_ts=timestamp,
                sequence_or_hash=schema_version,
                source="binance_spot",
                instrument="BTCUSDT",
                schema_version=schema_version,
                ingest_version="ingest-v1",
            ),
            event_type="event",
            payload={},
            epoch_id=0,
        )

    manifests = PartitionedRawEventWriter(tmp_path).write((event("schema-v1"), event("schema-v2")))

    assert {manifest.schema_version for manifest in manifests} == {"schema-v1", "schema-v2"}
    assert {manifest.row_count for manifest in manifests} == {1}


def test_raw_event_payload_json_round_trips_without_losing_nested_values() -> None:
    timestamp = datetime(2026, 7, 11, 12, tzinfo=UTC)
    payload: dict[str, object] = {
        "event_type": "price_change",
        "asset_id": "up-token",
        "price_changes": [
            {"price": "0.49", "side": "BUY", "size": "10"},
            {"price": "0.51", "side": "SELL", "size": "12"},
        ],
    }
    event = RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=timestamp,
            collector_receive_ts=timestamp,
            available_ts=timestamp,
            sequence_or_hash="event-1",
            source="polymarket_clob",
            instrument="up-token",
            schema_version="polymarket-market-ws-v1",
            ingest_version="ingest-v1",
        ),
        event_type="price_change",
        payload=payload,
        epoch_id=0,
    )

    row = event.as_row()

    assert json.loads(str(row["payload_json"])) == payload
