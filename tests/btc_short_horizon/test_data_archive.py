from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile

import pyarrow as pa
import pytest

from btc_short_horizon.data.archive import (
    BackupIntegrityError,
    LocalObjectTransport,
    RcloneObjectTransport,
    archive_verified_sessions_locally,
    create_backup_snapshot,
    read_backup_snapshot,
    restore_remote_snapshot,
    upload_backup_snapshot,
    verify_remote_snapshot,
)
from btc_short_horizon.data.session_inventory import (
    SESSION_INVENTORY_MANIFEST_ATTRIBUTE,
    SESSION_INVENTORY_SCHEMA_VERSION,
    CollectorStorageLease,
    SessionArchivedError,
    SessionInventoryError,
    SessionInventoryRepository,
)
from btc_short_horizon.data.storage import (
    ImmutableParquetStore,
    canonical_json_bytes,
    write_atomic_json,
)


def _complete_session(root: Path, session_id: str = "session-a") -> None:
    repository = SessionInventoryRepository(root)
    inventory = repository.start_session(
        session_id=session_id,
        ingest_version="btc-short-horizon-v9",
    )
    ImmutableParquetStore(root).write(
        table=pa.table(
            {
                "source_ts_ns": [1, 2],
                "available_ts_ns": [3, 4],
                "price": [100.0, 101.0],
            }
        ),
        source="binance_spot",
        instrument="BTCUSDT",
        partition_date="2026-07-11",
        partition_hour="12",
        schema_version="binance-v1",
        ingest_version="btc-short-horizon-v9",
        attributes={
            "collector_session_id": session_id,
            SESSION_INVENTORY_MANIFEST_ATTRIBUTE: SESSION_INVENTORY_SCHEMA_VERSION,
        },
        inventory=inventory,
    )
    inventory.complete()


def test_content_addressed_backup_verifies_and_restores_complete_session(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    restored = tmp_path / "restored"
    work = tmp_path / "work"
    work.mkdir()
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(
        raw_data_root=source,
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    transport = LocalObjectTransport(remote)

    upload_backup_snapshot(
        raw_data_root=source,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        transport=transport,
    )
    verified, receipt, receipt_path = verify_remote_snapshot(
        raw_data_root=source,
        snapshot_id=snapshot.snapshot_id,
        transport=transport,
        temporary_root=work,
    )
    restored_snapshot = restore_remote_snapshot(
        destination_root=restored,
        snapshot_id=snapshot.snapshot_id,
        transport=transport,
        temporary_root=work,
    )

    assert verified == snapshot == restored_snapshot
    assert receipt.file_count == len(snapshot.files)
    assert receipt_path.is_file()
    assert (restored / "inventory" / "backup-snapshots" / f"{snapshot.snapshot_id}.json").is_file()
    audit = SessionInventoryRepository(restored).audit(allow_open_sessions=False)
    assert audit.complete_session_count == 1
    assert audit.committed_part_count == 1


def test_restore_installs_atomically_when_temporary_root_is_on_another_volume(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    restored = tmp_path / "restored"
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(raw_data_root=source)
    transport = LocalObjectTransport(remote)
    upload_backup_snapshot(
        raw_data_root=source,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        transport=transport,
    )

    restore_remote_snapshot(
        destination_root=restored,
        snapshot_id=snapshot.snapshot_id,
        transport=transport,
        temporary_root=Path(tempfile.gettempdir()),
    )

    assert SessionInventoryRepository(restored).audit(allow_open_sessions=False).row_count == 2


def test_remote_verification_detects_tampered_content_addressed_object(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    work = tmp_path / "work"
    work.mkdir()
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(raw_data_root=source)
    transport = LocalObjectTransport(remote)
    upload_backup_snapshot(
        raw_data_root=source,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        transport=transport,
    )
    first = snapshot.files[0]
    (remote / first.object_key).write_bytes(b"tampered")

    with pytest.raises(BackupIntegrityError, match="integrity mismatch"):
        verify_remote_snapshot(
            raw_data_root=source,
            snapshot_id=snapshot.snapshot_id,
            transport=transport,
            temporary_root=work,
        )


def test_remote_verification_recreates_missing_local_snapshot_record(tmp_path: Path) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    work = tmp_path / "work"
    work.mkdir()
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(raw_data_root=source)
    transport = LocalObjectTransport(remote)
    upload_backup_snapshot(
        raw_data_root=source,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        transport=transport,
    )
    snapshot_path.unlink()

    verify_remote_snapshot(
        raw_data_root=source,
        snapshot_id=snapshot.snapshot_id,
        transport=transport,
        temporary_root=work,
    )

    assert read_backup_snapshot(snapshot_path) == snapshot


def test_backup_rejects_open_collector_session(tmp_path: Path) -> None:
    SessionInventoryRepository(tmp_path).start_session(
        session_id="session-a",
        ingest_version="btc-short-horizon-v9",
    )

    with pytest.raises(BackupIntegrityError, match="complete collector session"):
        create_backup_snapshot(raw_data_root=tmp_path)


def test_explicit_backup_rejects_incomplete_session(tmp_path: Path) -> None:
    SessionInventoryRepository(tmp_path).start_session(
        session_id="session-a",
        ingest_version="btc-short-horizon-v9",
    )

    with pytest.raises(SessionInventoryError, match="not complete"):
        create_backup_snapshot(raw_data_root=tmp_path, session_ids=("session-a",))


def test_rclone_transport_uses_immutable_checksum_commands_without_shell(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    transport = RcloneObjectTransport(
        "btc-archive:polymarket/raw",
        executable="rclone-test",
        runner=run,
    )
    local = tmp_path / "part"
    local.write_bytes(b"data")

    transport.put(local, "objects/aa/hash")
    transport.get("objects/aa/hash", tmp_path / "restored")

    assert calls[0][0] == [
        "rclone-test",
        "copyto",
        str(local),
        "btc-archive:polymarket/raw/objects/aa/hash",
        "--immutable",
        "--checksum",
        "--retries",
        "3",
    ]
    assert calls[1][0][1:3] == [
        "copyto",
        "btc-archive:polymarket/raw/objects/aa/hash",
    ]
    assert all(kwargs["shell"] is False for _, kwargs in calls)


@pytest.mark.parametrize("object_key", ("..\\escaped", "C:\\escaped", "objects//bad"))
def test_local_object_transport_rejects_noncanonical_cross_platform_paths(
    tmp_path: Path,
    object_key: str,
) -> None:
    local = tmp_path / "part"
    local.write_bytes(b"data")

    with pytest.raises(BackupIntegrityError, match="relative POSIX path"):
        LocalObjectTransport(tmp_path / "remote").put(local, object_key)


def test_backup_snapshot_rejects_files_outside_raw_and_session_inventory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(raw_data_root=source)
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["files"][0]["relative_path"] = "inventory/collector-lease.json"
    core = {key: value for key, value in payload.items() if key != "snapshot_id"}
    payload["snapshot_id"] = sha256(canonical_json_bytes(core)).hexdigest()
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BackupIntegrityError, match="out-of-scope"):
        read_backup_snapshot(snapshot_path)


def test_verified_archive_frees_local_parts_and_restore_rehydrates_them(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    work = tmp_path / "work"
    work.mkdir()
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(raw_data_root=source)
    transport = LocalObjectTransport(remote)
    upload_backup_snapshot(
        raw_data_root=source,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        transport=transport,
    )
    _, _, receipt_path = verify_remote_snapshot(
        raw_data_root=source,
        snapshot_id=snapshot.snapshot_id,
        transport=transport,
        temporary_root=work,
    )

    preview = archive_verified_sessions_locally(
        raw_data_root=source,
        receipt_path=receipt_path,
    )
    applied = archive_verified_sessions_locally(
        raw_data_root=source,
        receipt_path=receipt_path,
        apply=True,
    )

    assert preview.applied is False
    assert applied.applied is True
    assert not list(source.rglob("part-*.parquet"))
    assert not list(source.rglob("manifest-*.json"))
    repository = SessionInventoryRepository(source)
    audit = repository.audit(allow_open_sessions=False)
    assert audit.archived_session_count == 1
    assert audit.archived_part_count == 1
    with pytest.raises(SessionArchivedError, match="must be restored"):
        repository.expected_parts(
            source="binance_spot",
            instrument="BTCUSDT",
            start_available_ts_ns=0,
            end_available_ts_ns=10,
            ingest_version="btc-short-horizon-v9",
        )

    restore_remote_snapshot(
        destination_root=source,
        snapshot_id=snapshot.snapshot_id,
        transport=transport,
        temporary_root=work,
    )

    restored = repository.audit(allow_open_sessions=False)
    assert restored.archived_part_count == 0
    assert restored.committed_part_count == 1
    assert not repository.archive_marker_path("session-a").exists()


def test_archive_rejects_conflicting_existing_marker_before_deleting_local_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    work = tmp_path / "work"
    work.mkdir()
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(raw_data_root=source)
    transport = LocalObjectTransport(remote)
    upload_backup_snapshot(
        raw_data_root=source,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        transport=transport,
    )
    _, _, receipt_path = verify_remote_snapshot(
        raw_data_root=source,
        snapshot_id=snapshot.snapshot_id,
        transport=transport,
        temporary_root=work,
    )
    repository = SessionInventoryRepository(source)
    write_atomic_json(
        repository.archive_marker_path("session-a"),
        {
            "schema_version": "btc-session-archive-marker-v1",
            "session_id": "session-a",
            "snapshot_id": snapshot.snapshot_id,
            "remote_id": "different-remote",
            "archived_at": datetime.now(UTC).isoformat(),
            "files": [],
        },
    )

    with pytest.raises(BackupIntegrityError, match="marker conflicts"):
        archive_verified_sessions_locally(
            raw_data_root=source,
            receipt_path=receipt_path,
            apply=True,
        )

    assert list(source.rglob("part-*.parquet"))
    assert list(source.rglob("manifest-*.json"))


def test_archive_rejects_tampered_verification_receipt_before_deleting_local_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    work = tmp_path / "work"
    work.mkdir()
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(raw_data_root=source)
    transport = LocalObjectTransport(remote)
    upload_backup_snapshot(
        raw_data_root=source,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        transport=transport,
    )
    _, _, receipt_path = verify_remote_snapshot(
        raw_data_root=source,
        snapshot_id=snapshot.snapshot_id,
        transport=transport,
        temporary_root=work,
    )
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["file_count"] += 1
    write_atomic_json(receipt_path, payload)

    with pytest.raises(BackupIntegrityError, match="receipt counts"):
        archive_verified_sessions_locally(
            raw_data_root=source,
            receipt_path=receipt_path,
            apply=True,
        )

    assert list(source.rglob("part-*.parquet"))
    assert list(source.rglob("manifest-*.json"))


def test_restore_refuses_to_mutate_destination_while_collector_lease_is_held(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    remote = tmp_path / "remote"
    destination = tmp_path / "destination"
    work = tmp_path / "work"
    work.mkdir()
    _complete_session(source)
    snapshot, snapshot_path = create_backup_snapshot(raw_data_root=source)
    transport = LocalObjectTransport(remote)
    upload_backup_snapshot(
        raw_data_root=source,
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        transport=transport,
    )

    with CollectorStorageLease(destination):
        with pytest.raises(SessionInventoryError, match="already owns"):
            restore_remote_snapshot(
                destination_root=destination,
                snapshot_id=snapshot.snapshot_id,
                transport=transport,
                temporary_root=work,
            )

    assert not list(destination.rglob("part-*.parquet"))
