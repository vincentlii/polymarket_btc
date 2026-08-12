"""Plan, verify, resume, and materialize bounded Binance BTCUSDT spot 1s archives."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import date
import json
from pathlib import Path

import httpx

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.research.binance_history import (  # noqa: E402
    BinanceArchiveDownload,
    BinanceArchiveNotFoundError,
    download_binance_kline_archive,
    materialize_binance_kline_archive,
    plan_binance_spot_kline_archives,
)


_START_DAY = date(2025, 10, 9)
_END_DAY = date(2026, 8, 6)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "download", "materialize", "resume"))
    parser.add_argument("--start-day", type=_parse_day, default=_START_DAY)
    parser.add_argument("--end-day", type=_parse_day, default=_END_DAY)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--materialized-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10_000)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    plan = plan_binance_spot_kline_archives(
        start_day=args.start_day,
        end_day=args.end_day,
        symbol="BTCUSDT",
        interval="1s",
    )
    result: dict[str, object] = {
        "action": args.action,
        "dry_run": args.dry_run,
        "archive_count": len(plan),
        "coverage": {"start": args.start_day.isoformat(), "end": args.end_day.isoformat()},
        "archives": [
            {
                "kind": item.kind,
                "start": item.coverage_start.isoformat(),
                "end": item.coverage_end.isoformat(),
                "url": item.url,
                "checksum_url": item.checksum_url,
            }
            for item in plan
        ],
    }
    if args.action == "plan" or args.dry_run:
        return result
    if args.archive_root is None:
        raise ValueError("--archive-root is required for download, resume, and materialize")
    if args.action in {"download", "resume"}:
        downloads, missing = asyncio.run(_download(plan=plan, archive_root=args.archive_root))
        result["downloads"] = [
            {
                "archive_path": str(item.archive_path),
                "verified_zip_sha256": item.verified_sha256,
                "resumed": item.resumed,
                "reused": item.reused,
            }
            for item in downloads
        ]
        result["missing_archives"] = list(missing)
        result["complete"] = not missing
        return result
    if args.materialized_root is None:
        raise ValueError("--materialized-root is required for materialize")
    result["materializations"] = _materialize(
        plan=plan,
        archive_root=args.archive_root,
        materialized_root=args.materialized_root,
        batch_size=args.batch_size,
    )
    return result


async def _download(
    *, plan: Sequence[object], archive_root: Path
) -> tuple[tuple[BinanceArchiveDownload, ...], tuple[str, ...]]:
    downloads: list[BinanceArchiveDownload] = []
    missing: list[str] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        for item in plan:
            try:
                downloads.append(
                    await download_binance_kline_archive(
                        item=item, archive_root=archive_root, client=client
                    )
                )
            except BinanceArchiveNotFoundError:
                missing.append(item.url)
    return tuple(downloads), tuple(missing)


def _materialize(
    *,
    plan: Sequence[object],
    archive_root: Path,
    materialized_root: Path,
    batch_size: int,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for item in plan:
        archive_path = archive_root / item.kind / Path(item.url).name
        manifest_path = archive_path.with_suffix(".zip.manifest.json")
        if not archive_path.is_file() or not manifest_path.is_file():
            raise FileNotFoundError(f"verified archive and manifest required for {item.url}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        verified_sha256 = (
            manifest.get("verified_zip_sha256") if isinstance(manifest, dict) else None
        )
        checksum_text = manifest.get("checksum_text") if isinstance(manifest, dict) else None
        if not isinstance(verified_sha256, str) or not isinstance(checksum_text, str):
            raise ValueError(f"archive manifest is invalid for {item.url}")
        materialization = materialize_binance_kline_archive(
            item=item,
            download=BinanceArchiveDownload(
                archive_path=archive_path,
                manifest_path=manifest_path,
                verified_sha256=verified_sha256,
                checksum_text=checksum_text,
                resumed=False,
                reused=True,
            ),
            materialized_root=materialized_root,
            batch_size=batch_size,
        )
        results.append(
            {
                "row_count": materialization.row_count,
                "gap_count": materialization.gap_count,
                "part_paths": [str(path) for path in materialization.part_paths],
                "manifest_paths": [str(path) for path in materialization.manifest_paths],
            }
        )
    return results


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO day: {value!r}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("complete", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
