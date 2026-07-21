"""Back up, verify, audit, and restore BTC runtime evidence."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data.archive import (  # noqa: E402
    LocalObjectTransport,
    ObjectTransport,
    RcloneObjectTransport,
)
from btc_short_horizon.live.runtime_archive import (  # noqa: E402
    REQUIRED_RUNTIME_CATEGORIES,
    RuntimeBackupRepository,
    audit_runtime_recovery,
    create_runtime_snapshot,
    restore_runtime_snapshot,
    upload_runtime_snapshot,
    verify_runtime_snapshot,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path("deploy/runtime/output/btc_short_horizon/recovery"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser("snapshot", help="Create one immutable local snapshot.")
    _add_snapshot_arguments(snapshot)

    backup = subparsers.add_parser(
        "backup", help="Snapshot, upload, full-download verify, and persist a receipt."
    )
    _add_snapshot_arguments(backup)
    _add_transport_arguments(backup)
    _add_temporary_root(backup)

    verify = subparsers.add_parser(
        "verify", help="Full-download verify an existing remote snapshot."
    )
    verify.add_argument("--snapshot-id", required=True)
    _add_transport_arguments(verify)
    _add_temporary_root(verify)

    restore = subparsers.add_parser(
        "restore", help="Restore into an isolated root without overwriting different files."
    )
    restore.add_argument("--snapshot-id", required=True)
    restore.add_argument("--destination-root", type=Path, required=True)
    _add_transport_arguments(restore)
    _add_temporary_root(restore)

    audit = subparsers.add_parser(
        "audit", help="Check required categories and verification receipt freshness."
    )
    audit.add_argument("--snapshot-id", required=True)
    audit.add_argument("--required-category", action="append")
    audit.add_argument("--max-receipt-age-hours", type=float, default=48.0)
    audit.add_argument("--require-restore-drill", action="store_true")
    audit.add_argument("--max-restore-age-hours", type=float, default=168.0)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    repository = RuntimeBackupRepository(args.repository_root)
    if args.command == "snapshot":
        snapshot, path = create_runtime_snapshot(
            sources=_sources(args.source),
            repository=repository,
            release_revision=args.release_revision,
            rule_epoch=args.rule_epoch,
        )
        return _snapshot_result("snapshot", snapshot, snapshot_path=path)
    if args.command == "backup":
        sources = _sources(args.source)
        snapshot, path = create_runtime_snapshot(
            sources=sources,
            repository=repository,
            release_revision=args.release_revision,
            rule_epoch=args.rule_epoch,
        )
        transport = _transport(args)
        upload_runtime_snapshot(
            snapshot=snapshot,
            snapshot_path=path,
            sources=sources,
            transport=transport,
        )
        _, receipt, receipt_path = verify_runtime_snapshot(
            snapshot_id=snapshot.snapshot_id,
            repository=repository,
            transport=transport,
            temporary_root=args.temporary_root,
        )
        return {
            **_snapshot_result("backup", snapshot, snapshot_path=path),
            "remote_id": receipt.remote_id,
            "receipt_path": str(receipt_path),
            "verified_at": receipt.verified_at,
        }
    if args.command == "verify":
        snapshot, receipt, receipt_path = verify_runtime_snapshot(
            snapshot_id=args.snapshot_id,
            repository=repository,
            transport=_transport(args),
            temporary_root=args.temporary_root,
        )
        return {
            **_snapshot_result("verify", snapshot),
            "remote_id": receipt.remote_id,
            "receipt_path": str(receipt_path),
            "verified_at": receipt.verified_at,
        }
    if args.command == "restore":
        snapshot, receipt, receipt_path = restore_runtime_snapshot(
            snapshot_id=args.snapshot_id,
            destination_root=args.destination_root,
            repository=repository,
            transport=_transport(args),
            temporary_root=args.temporary_root,
        )
        return {
            **_snapshot_result("restore", snapshot),
            "destination_root": str(args.destination_root),
            "restore_receipt_path": str(receipt_path),
            "restored_at": receipt.restored_at,
        }
    if args.command == "audit":
        if args.max_receipt_age_hours <= 0.0:
            raise ValueError("max-receipt-age-hours must be > 0")
        if args.max_restore_age_hours <= 0.0:
            raise ValueError("max-restore-age-hours must be > 0")
        required = args.required_category or list(REQUIRED_RUNTIME_CATEGORIES)
        result = audit_runtime_recovery(
            repository=repository,
            snapshot_id=args.snapshot_id,
            required_categories=required,
            now=datetime.now(UTC),
            maximum_receipt_age=timedelta(hours=args.max_receipt_age_hours),
            require_restore_drill=args.require_restore_drill,
            maximum_restore_age=timedelta(hours=args.max_restore_age_hours),
        )
        return {"command": "audit", **asdict(result)}
    raise AssertionError(f"unsupported runtime archive command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed", True) else 1


def _add_snapshot_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--release-revision", required=True)
    parser.add_argument("--rule-epoch", required=True)
    parser.add_argument(
        "--source",
        action="append",
        required=True,
        metavar="CATEGORY=PATH",
        help="Repeat for model, wal, ledger, and report roots.",
    )


def _add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--transport", choices=("local", "rclone"), required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--rclone-executable", default="rclone")


def _add_temporary_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--temporary-root", type=Path, required=True)


def _sources(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        category, separator, raw_path = value.partition("=")
        if not separator or not category or not raw_path:
            raise ValueError("source must use CATEGORY=PATH")
        if category in result:
            raise ValueError(f"duplicate source category: {category}")
        result[category] = Path(raw_path)
    return result


def _transport(args: argparse.Namespace) -> ObjectTransport:
    if args.transport == "local":
        return LocalObjectTransport(Path(args.remote))
    return RcloneObjectTransport(
        args.remote,
        executable=args.rclone_executable,
    )


def _snapshot_result(
    command: str,
    snapshot,
    *,
    snapshot_path: Path | None = None,  # type: ignore[no-untyped-def]
) -> dict[str, object]:
    result: dict[str, object] = {
        "command": command,
        "snapshot_id": snapshot.snapshot_id,
        "release_revision": snapshot.release_revision,
        "rule_epoch": snapshot.rule_epoch,
        "categories": list(snapshot.categories),
        "file_count": len(snapshot.files),
        "total_bytes": sum(item.size_bytes for item in snapshot.files),
    }
    if snapshot_path is not None:
        result["snapshot_path"] = str(snapshot_path)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
