"""Audit, back up, verify, and restore BTC forward raw collector sessions."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import tempfile

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data.archive import (  # noqa: E402
    LocalObjectTransport,
    ObjectTransport,
    RcloneObjectTransport,
    archive_verified_sessions_locally,
    create_backup_snapshot,
    restore_remote_snapshot,
    upload_backup_snapshot,
    verify_remote_snapshot,
)
from btc_short_horizon.data.session_inventory import (  # noqa: E402
    SessionInventoryRepository,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
    )
    parser.add_argument("--raw-data-root", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit", help="Verify all registered local sessions and parts.")
    audit.add_argument("--require-closed", action="store_true")
    audit.add_argument("--allow-failed", action="store_true")

    snapshot = subparsers.add_parser(
        "snapshot",
        help="Create a local content-addressed snapshot for complete sessions.",
    )
    snapshot.add_argument("--session-id", action="append")

    backup = subparsers.add_parser(
        "backup",
        help="Upload immutable objects, then download-verify every object and write a receipt.",
    )
    backup.add_argument("--session-id", action="append")
    _add_transport_arguments(backup)
    _add_temporary_root(backup)

    verify = subparsers.add_parser(
        "verify",
        help="Download-hash an existing remote snapshot and write a local verification receipt.",
    )
    verify.add_argument("--snapshot-id", required=True)
    _add_transport_arguments(verify)
    _add_temporary_root(verify)

    restore = subparsers.add_parser(
        "restore",
        help="Restore one verified snapshot without overwriting differing local files.",
    )
    restore.add_argument("--snapshot-id", required=True)
    restore.add_argument("--destination-root", type=Path, required=True)
    _add_transport_arguments(restore)
    _add_temporary_root(restore)

    archive = subparsers.add_parser(
        "archive",
        help="Preview or remove local parts covered by a full-download verification receipt.",
    )
    archive.add_argument("--receipt-path", type=Path, required=True)
    archive.add_argument("--session-id", action="append")
    archive.add_argument("--completed-before", type=_datetime)
    archive.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete verified local part/manifest pairs; default is a dry-run plan.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    raw_root = args.raw_data_root or load_btc_project_config(args.config).paths.raw_data_root
    if args.command == "audit":
        report = SessionInventoryRepository(raw_root).audit(
            allow_open_sessions=not args.require_closed,
            allow_failed_sessions=args.allow_failed,
        )
        return {"command": "audit", "raw_data_root": str(raw_root), **asdict(report)}
    if args.command == "snapshot":
        snapshot, path = create_backup_snapshot(
            raw_data_root=raw_root,
            session_ids=args.session_id,
        )
        return {
            "command": "snapshot",
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_path": str(path),
            "session_count": len(snapshot.session_ids),
            "file_count": len(snapshot.files),
            "total_bytes": sum(item.size_bytes for item in snapshot.files),
        }
    if args.command == "archive":
        plan = archive_verified_sessions_locally(
            raw_data_root=raw_root,
            receipt_path=args.receipt_path,
            session_ids=args.session_id,
            completed_before=args.completed_before,
            apply=args.apply,
        )
        return {"command": "archive", "raw_data_root": str(raw_root), **asdict(plan)}
    transport = _transport(args)
    temporary_root = args.temporary_root or Path(tempfile.gettempdir())
    if args.command == "backup":
        snapshot, path = create_backup_snapshot(
            raw_data_root=raw_root,
            session_ids=args.session_id,
        )
        upload_backup_snapshot(
            raw_data_root=raw_root,
            snapshot=snapshot,
            snapshot_path=path,
            transport=transport,
        )
        _, receipt, receipt_path = verify_remote_snapshot(
            raw_data_root=raw_root,
            snapshot_id=snapshot.snapshot_id,
            transport=transport,
            temporary_root=temporary_root,
        )
        return {
            "command": "backup",
            "snapshot_id": snapshot.snapshot_id,
            "remote_id": transport.remote_id,
            "receipt_path": str(receipt_path),
            "file_count": receipt.file_count,
            "total_bytes": receipt.total_bytes,
        }
    if args.command == "verify":
        _, receipt, receipt_path = verify_remote_snapshot(
            raw_data_root=raw_root,
            snapshot_id=args.snapshot_id,
            transport=transport,
            temporary_root=temporary_root,
        )
        return {
            "command": "verify",
            "snapshot_id": receipt.snapshot_id,
            "remote_id": receipt.remote_id,
            "receipt_path": str(receipt_path),
            "file_count": receipt.file_count,
            "total_bytes": receipt.total_bytes,
        }
    if args.command == "restore":
        snapshot = restore_remote_snapshot(
            destination_root=args.destination_root,
            snapshot_id=args.snapshot_id,
            transport=transport,
            temporary_root=temporary_root,
        )
        return {
            "command": "restore",
            "snapshot_id": snapshot.snapshot_id,
            "destination_root": str(args.destination_root),
            "session_count": len(snapshot.session_ids),
            "file_count": len(snapshot.files),
        }
    raise ValueError(f"unsupported command: {args.command}")


def _add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transport", choices=("local", "rclone"), required=True)
    parser.add_argument(
        "--remote",
        required=True,
        help="Local directory or an rclone configured remote:path prefix.",
    )
    parser.add_argument("--rclone-executable", default="rclone")


def _add_temporary_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--temporary-root", type=Path)


def _transport(args: argparse.Namespace) -> ObjectTransport:
    if args.transport == "local":
        return LocalObjectTransport(Path(args.remote))
    return RcloneObjectTransport(
        args.remote,
        executable=args.rclone_executable,
    )


def _datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must be timezone-aware")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
