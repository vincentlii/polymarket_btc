from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pyarrow as pa
import pytest

from btc_short_horizon.data.collector import PartitionedRawEventWriter, RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data import storage as storage_module
from btc_short_horizon.data.session_inventory import (
    SESSION_INVENTORY_MANIFEST_ATTRIBUTE,
    SESSION_INVENTORY_SCHEMA_VERSION,
    SESSION_STATUS_COMPLETE,
    CollectorStorageLease,
    SessionInventoryError,
    SessionInventoryRepository,
)
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


@pytest.mark.parametrize("relative_path", ("..\\outside.json", "C:\\outside.json"))
def test_store_manifest_reader_rejects_cross_platform_path_escape(
    tmp_path: Path,
    relative_path: str,
) -> None:
    with pytest.raises(ValueError, match="relative POSIX path"):
        ImmutableParquetStore(tmp_path).read_manifest(relative_path)


def test_store_manifest_reader_rejects_invalid_statistics(tmp_path: Path) -> None:
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
    relative_path = (
        f"raw/binance_spot/btcusdt/date=2026-07-11/hour=12/manifest-{manifest.sha256[:32]}.json"
    )
    path = tmp_path / relative_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["row_count"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="row_count"):
        store.read_manifest(relative_path)


@pytest.mark.parametrize("source", ("../binance", ".", ".."))
def test_store_rejects_unsafe_partition_component(tmp_path: Path, source: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ImmutableParquetStore(tmp_path).write(
            table=_table(),
            source=source,
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
        / f"sha256-{storage_module._instrument_digest(instrument)[:32]}"
        / "date=2026-07-11"
        / "hour=12"
        / f"manifest-{manifest.sha256[:32]}.json"
    )
    assert manifest.instrument == instrument
    assert manifest_path.exists()
    assert len(str(manifest_path.relative_to(tmp_path))) < 180
    assert store.read_manifest(manifest_path.relative_to(tmp_path).as_posix()) == manifest


def test_store_fails_closed_on_instrument_hash_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage_module, "_instrument_digest", lambda _instrument: "a" * 64)
    store = ImmutableParquetStore(tmp_path)
    store.write(
        table=_table(),
        source="polymarket_clob",
        instrument="x" * 78,
        partition_date="2026-07-11",
        partition_hour="12",
        schema_version="v1",
        ingest_version="v1",
    )

    with pytest.raises(FileExistsError, match="instrument path collision"):
        store.write(
            table=_table(),
            source="polymarket_clob",
            instrument="y" * 78,
            partition_date="2026-07-11",
            partition_hour="12",
            schema_version="v1",
            ingest_version="v1",
        )


def test_store_removes_new_part_when_manifest_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_manifest_write(_path: Path, _payload: object) -> None:
        raise OSError("manifest write failed")

    monkeypatch.setattr(storage_module, "write_atomic_json", fail_manifest_write)

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


def _session_manifest_attributes(session_id: str) -> dict[str, str]:
    return {
        "collector_session_id": session_id,
        SESSION_INVENTORY_MANIFEST_ATTRIBUTE: SESSION_INVENTORY_SCHEMA_VERSION,
    }


def test_session_inventory_commits_part_and_closes_only_after_integrity_check(
    tmp_path: Path,
) -> None:
    repository = SessionInventoryRepository(tmp_path)
    inventory = repository.start_session(
        session_id="session-a",
        ingest_version="ingest-v9",
        attributes={"code_revision": "abc123"},
    )

    manifest = ImmutableParquetStore(tmp_path).write(
        table=_table(),
        source="binance_spot",
        instrument="btcusdt",
        partition_date="2026-07-11",
        partition_hour="12",
        schema_version="v1",
        ingest_version="ingest-v9",
        attributes=_session_manifest_attributes("session-a"),
        inventory=inventory,
    )
    inventory.complete()

    session = inventory.snapshot()
    assert session.status == SESSION_STATUS_COMPLETE
    assert len(session.parts) == 1
    assert session.parts[0].manifest.data_path == manifest.data_path
    audit = repository.audit(allow_open_sessions=False)
    assert audit.session_count == 1
    assert audit.committed_part_count == 1
    assert audit.row_count == 2


def test_session_inventory_recovers_idempotently_after_commit_record_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionInventoryRepository(tmp_path)
    inventory = repository.start_session(
        session_id="session-a",
        ingest_version="ingest-v9",
    )
    original_commit = inventory.commit_part
    commit_calls = 0

    def fail_first_commit(*, manifest_path: str, manifest_sha256: str) -> None:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise OSError("inventory commit failed")
        original_commit(
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
        )

    monkeypatch.setattr(inventory, "commit_part", fail_first_commit)
    write = lambda: ImmutableParquetStore(tmp_path).write(  # noqa: E731
        table=_table(),
        source="binance_spot",
        instrument="btcusdt",
        partition_date="2026-07-11",
        partition_hour="12",
        schema_version="v1",
        ingest_version="ingest-v9",
        attributes=_session_manifest_attributes("session-a"),
        inventory=inventory,
    )

    with pytest.raises(OSError, match="inventory commit failed"):
        write()
    assert inventory.snapshot().parts[0].status == "prepared"
    assert len(list(tmp_path.rglob("part-*.parquet"))) == 1
    assert len(list(tmp_path.rglob("manifest-*.json"))) == 1

    first = write()
    inventory.complete()

    assert inventory.snapshot().parts[0].status == "committed"
    assert first.row_count == 2
    assert len(list(tmp_path.rglob("part-*.parquet"))) == 1


def test_session_inventory_detects_deletion_of_part_and_adjacent_manifest(
    tmp_path: Path,
) -> None:
    repository = SessionInventoryRepository(tmp_path)
    inventory = repository.start_session(
        session_id="session-a",
        ingest_version="ingest-v9",
    )
    manifest = ImmutableParquetStore(tmp_path).write(
        table=_table(),
        source="binance_spot",
        instrument="btcusdt",
        partition_date="2026-07-11",
        partition_hour="12",
        schema_version="v1",
        ingest_version="ingest-v9",
        attributes=_session_manifest_attributes("session-a"),
        inventory=inventory,
    )
    inventory.complete()
    part_path = tmp_path / manifest.data_path
    manifest_path = part_path.with_name(f"manifest-{manifest.sha256[:32]}.json")
    part_path.unlink()
    manifest_path.unlink()

    with pytest.raises(SessionInventoryError, match="missing manifest"):
        repository.audit(allow_open_sessions=False)


def test_session_inventory_rejects_missing_session_file(tmp_path: Path) -> None:
    repository = SessionInventoryRepository(tmp_path)
    inventory = repository.start_session(
        session_id="session-a",
        ingest_version="ingest-v9",
    )
    inventory.path.unlink()

    with pytest.raises(SessionInventoryError, match="missing_inventory"):
        repository.read_all()


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "..\\outside.json",
        "C:\\outside.json",
        "inventory//sessions/session-a.json",
    ),
)
def test_session_registry_rejects_noncanonical_cross_platform_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    repository = SessionInventoryRepository(tmp_path)
    repository.start_session(
        session_id="session-a",
        ingest_version="ingest-v9",
    )
    registry_path = repository.registry_path("session-a")
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    payload["inventory_path"] = unsafe_path
    registry_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SessionInventoryError, match="relative POSIX path"):
        repository.read_session("session-a")


def test_session_inventory_reconciles_durable_pair_before_failing_interrupted_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionInventoryRepository(tmp_path)
    inventory = repository.start_session(
        session_id="session-a",
        ingest_version="ingest-v9",
    )

    def fail_commit(*, manifest_path: str, manifest_sha256: str) -> None:
        del manifest_path, manifest_sha256
        raise OSError("simulated process loss after manifest")

    monkeypatch.setattr(inventory, "commit_part", fail_commit)
    with pytest.raises(OSError, match="simulated process loss"):
        ImmutableParquetStore(tmp_path).write(
            table=_table(),
            source="binance_spot",
            instrument="btcusdt",
            partition_date="2026-07-11",
            partition_hour="12",
            schema_version="v1",
            ingest_version="ingest-v9",
            attributes=_session_manifest_attributes("session-a"),
            inventory=inventory,
        )

    assert repository.audit().prepared_part_count == 1
    assert repository.recover_interrupted_sessions() == ("session-a",)
    recovered = repository.read_session("session-a")
    assert recovered.status == "failed"
    assert recovered.parts[0].status == "committed"
    assert repository.audit(allow_failed_sessions=True).committed_part_count == 1


def test_failed_session_rejects_late_prepare_and_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = SessionInventoryRepository(tmp_path)
    inventory = repository.start_session(session_id="session-a", ingest_version="ingest-v9")
    original_commit = inventory.commit_part
    monkeypatch.setattr(
        inventory,
        "commit_part",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("pause after prepare")),
    )
    with pytest.raises(OSError, match="pause after prepare"):
        ImmutableParquetStore(tmp_path).write(
            table=_table(),
            source="binance_spot",
            instrument="btcusdt",
            partition_date="2026-07-11",
            partition_hour="12",
            schema_version="v1",
            ingest_version="ingest-v9",
            attributes=_session_manifest_attributes("session-a"),
            inventory=inventory,
        )
    monkeypatch.setattr(inventory, "commit_part", original_commit)
    part = inventory.snapshot().parts[0]
    inventory.fail("shutdown")

    with pytest.raises(SessionInventoryError, match="open collector session"):
        inventory.prepare_part(manifest_path=part.manifest_path, manifest=part.manifest)
    with pytest.raises(SessionInventoryError, match="open collector session"):
        inventory.commit_part(
            manifest_path=part.manifest_path,
            manifest_sha256=part.manifest_sha256,
        )


def test_collector_storage_lease_prevents_two_writers_and_releases_cleanly(
    tmp_path: Path,
) -> None:
    first = CollectorStorageLease(tmp_path, owner={"service": "test"}).acquire()
    try:
        with pytest.raises(SessionInventoryError, match="already owns"):
            CollectorStorageLease(tmp_path).acquire()
        assert (tmp_path / "inventory" / "collector-lease.json").is_file()
    finally:
        first.release()

    with CollectorStorageLease(tmp_path):
        assert (tmp_path / "inventory" / "collector-lease.json").is_file()
    assert not (tmp_path / "inventory" / "collector-lease.json").exists()


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
            collector_session_id="test-session",
            epoch_id=0,
        )

    manifests = PartitionedRawEventWriter(tmp_path).write((event("schema-v1"), event("schema-v2")))

    assert {manifest.schema_version for manifest in manifests} == {"schema-v1", "schema-v2"}
    assert {manifest.row_count for manifest in manifests} == {1}


def test_raw_event_writer_attributes_quality_to_each_collector_session(tmp_path: Path) -> None:
    timestamp = datetime(2026, 7, 11, 12, tzinfo=UTC)

    def event(session_id: str) -> RawCollectorEvent:
        return RawCollectorEvent(
            timing=TimedMarketEvent(
                source_ts=timestamp,
                collector_receive_ts=timestamp,
                available_ts=timestamp,
                sequence_or_hash=session_id,
                source="binance_spot",
                instrument="BTCUSDT",
                schema_version="schema-v1",
                ingest_version="ingest-v1",
            ),
            event_type="trade",
            payload={},
            collector_session_id=session_id,
            epoch_id=0,
        )

    manifests = PartitionedRawEventWriter(tmp_path).write(
        (event("session-a"), event("session-b")),
        quality_counts={
            (
                "binance_spot",
                "BTCUSDT",
                "2026-07-11",
                "12",
                "schema-v1",
                "ingest-v1",
                "session-a",
            ): (1, 0),
            (
                "binance_spot",
                "BTCUSDT",
                "2026-07-11",
                "12",
                "schema-v1",
                "ingest-v1",
                "session-b",
            ): (0, 2),
        },
    )
    by_session = {manifest.attributes["collector_session_id"]: manifest for manifest in manifests}

    assert by_session["session-a"].duplicate_count == 1
    assert by_session["session-a"].gap_count == 0
    assert by_session["session-b"].duplicate_count == 0
    assert by_session["session-b"].gap_count == 2


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
        collector_session_id="test-session",
        epoch_id=0,
    )
    payload["price_changes"][0]["size"] = "999"  # type: ignore[index]

    row = event.as_row()
    frozen_changes = event.payload["price_changes"]
    assert isinstance(frozen_changes, tuple)
    with pytest.raises(TypeError):
        frozen_changes[0]["size"] = "888"  # type: ignore[index]

    assert json.loads(str(row["payload_json"]))["price_changes"][0]["size"] == "10"
    assert row["collector_session_id"] == "test-session"
    assert event.estimated_size_bytes > len(str(row["payload_json"]))


@pytest.mark.parametrize("invalid", [float("nan"), {"not-json"}, object()])
def test_raw_event_payload_rejects_non_json_evidence(invalid: object) -> None:
    timestamp = datetime(2026, 7, 11, 12, tzinfo=UTC)

    with pytest.raises(ValueError, match="finite JSON object"):
        RawCollectorEvent(
            timing=TimedMarketEvent(
                source_ts=timestamp,
                collector_receive_ts=timestamp,
                available_ts=timestamp,
                sequence_or_hash="invalid-json",
                source="polymarket_clob",
                instrument="up-token",
                schema_version="polymarket-market-ws-v1",
                ingest_version="ingest-v1",
            ),
            event_type="book",
            payload={"invalid": invalid},
            collector_session_id="test-session",
            epoch_id=0,
        )
