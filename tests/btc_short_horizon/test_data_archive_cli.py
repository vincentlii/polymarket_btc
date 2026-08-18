from __future__ import annotations

from pathlib import Path

from btc_short_horizon.data.session_inventory import SessionInventoryRepository
from scripts.btc_data_archive import parse_args, run


def _complete_empty_session(root: Path) -> None:
    inventory = SessionInventoryRepository(root).start_session(
        session_id="session-a",
        ingest_version="btc-short-horizon-v9",
    )
    inventory.complete()


def test_archive_cli_audits_and_creates_snapshot(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _complete_empty_session(raw)

    audit = run(parse_args(["--raw-data-root", str(raw), "audit", "--require-closed"]))
    snapshot = run(parse_args(["--raw-data-root", str(raw), "snapshot"]))

    assert audit["complete_session_count"] == 1
    assert snapshot["session_count"] == 1
    assert Path(str(snapshot["snapshot_path"])).is_file()


def test_archive_cli_backup_verifies_and_restores_local_transport(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    remote = tmp_path / "remote"
    restored = tmp_path / "restored"
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    _complete_empty_session(raw)

    backup = run(
        parse_args(
            [
                "--raw-data-root",
                str(raw),
                "backup",
                "--transport",
                "local",
                "--remote",
                str(remote),
                "--temporary-root",
                str(temporary),
            ]
        )
    )
    restore = run(
        parse_args(
            [
                "--raw-data-root",
                str(raw),
                "restore",
                "--snapshot-id",
                str(backup["snapshot_id"]),
                "--destination-root",
                str(restored),
                "--transport",
                "local",
                "--remote",
                str(remote),
                "--temporary-root",
                str(temporary),
            ]
        )
    )

    assert restore["snapshot_id"] == backup["snapshot_id"]
    assert (
        SessionInventoryRepository(restored).audit(allow_open_sessions=False).complete_session_count
        == 1
    )
