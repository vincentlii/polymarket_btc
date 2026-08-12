from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import zipfile

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from btc_short_horizon.research.binance_history import plan_binance_spot_kline_archives
from btc_short_horizon.research.binance_history import download_binance_kline_archive
from btc_short_horizon.research.binance_history import (
    BinanceArchiveDownload,
    BinanceArchiveNotFoundError,
    BinanceArchivePlanItem,
    load_materialized_binance_kline_history,
    materialize_binance_kline_archive,
)
import scripts.btc_binance_history as binance_history_cli
from scripts.btc_binance_history import parse_args, run


def test_binance_archive_plan_prefers_complete_months_without_overlapping_days() -> None:
    plan = plan_binance_spot_kline_archives(
        start_day=date(2025, 10, 9),
        end_day=date(2026, 8, 6),
    )

    assert [(item.kind, item.coverage_start, item.coverage_end) for item in plan] == [
        ("daily", date(2025, 10, 9), date(2025, 10, 9)),
        *[("daily", date(2025, 10, day), date(2025, 10, day)) for day in range(10, 32)],
        *[
            ("monthly", date(2025, month, 1), date(2025, month, 30 if month == 11 else 31))
            for month in (11,)
        ],
        ("monthly", date(2025, 12, 1), date(2025, 12, 31)),
        ("monthly", date(2026, 1, 1), date(2026, 1, 31)),
        ("monthly", date(2026, 2, 1), date(2026, 2, 28)),
        ("monthly", date(2026, 3, 1), date(2026, 3, 31)),
        ("monthly", date(2026, 4, 1), date(2026, 4, 30)),
        ("monthly", date(2026, 5, 1), date(2026, 5, 31)),
        ("monthly", date(2026, 6, 1), date(2026, 6, 30)),
        ("monthly", date(2026, 7, 1), date(2026, 7, 31)),
        *[("daily", date(2026, 8, day), date(2026, 8, day)) for day in range(1, 7)],
    ]
    covered = [day for item in plan for day in item.days]
    assert covered == [date(2025, 10, 9) + timedelta(days=index) for index in range(302)]
    assert plan[0].url.endswith("/daily/klines/BTCUSDT/1s/BTCUSDT-1s-2025-10-09.zip")
    assert plan[23].url.endswith("/monthly/klines/BTCUSDT/1s/BTCUSDT-1s-2025-11.zip")
    assert plan[-1].checksum_url == f"{plan[-1].url}.CHECKSUM"


@pytest.mark.asyncio
async def test_binance_archive_download_resumes_only_with_matching_range_and_checksum(
    tmp_path: Path,
) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    payload = b"verified zip bytes"
    expected = sha256(payload).hexdigest()
    archive_path = tmp_path / "archives" / "daily" / Path(item.url).name
    archive_path.parent.mkdir(parents=True)
    archive_path.with_suffix(".zip.part").write_bytes(payload[:8])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(item.checksum_url):
            return httpx.Response(200, text=f"{expected}  {archive_path.name}\n")
        assert request.headers["Range"] == "bytes=8-"
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes 8-{len(payload) - 1}/{len(payload)}"},
            content=payload[8:],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await download_binance_kline_archive(
            item=item, archive_root=tmp_path / "archives", client=client
        )

    assert result.archive_path.read_bytes() == payload
    assert result.verified_sha256 == expected
    assert result.resumed
    assert result.manifest_path.is_file()
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_binance_archive_download_restarts_safely_when_range_is_not_honored(
    tmp_path: Path,
) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    payload = b"replacement zip bytes"
    expected = sha256(payload).hexdigest()
    archive_path = tmp_path / "archives" / "daily" / Path(item.url).name
    archive_path.parent.mkdir(parents=True)
    archive_path.with_suffix(".zip.part").write_bytes(b"interrupted")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(item.checksum_url):
            return httpx.Response(200, text=f"{expected}  {archive_path.name}\n")
        assert request.headers["Range"] == "bytes=11-"
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await download_binance_kline_archive(
            item=item, archive_root=tmp_path / "archives", client=client
        )

    assert result.archive_path.read_bytes() == payload
    assert not result.resumed


@pytest.mark.asyncio
async def test_binance_archive_download_restarts_without_range_after_malformed_206(
    tmp_path: Path,
) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    payload = b"full verified ZIP"
    expected = sha256(payload).hexdigest()
    archive_path = tmp_path / "archives" / "daily" / Path(item.url).name
    archive_path.parent.mkdir(parents=True)
    archive_path.with_suffix(".zip.part").write_bytes(b"part")
    archive_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal archive_requests
        if request.url == httpx.URL(item.checksum_url):
            return httpx.Response(200, text=f"{expected}  {archive_path.name}\n")
        archive_requests += 1
        if archive_requests == 1:
            assert request.headers["Range"] == "bytes=4-"
            return httpx.Response(206, headers={"Content-Range": "bytes 3-16/17"}, content=b"tail")
        assert "Range" not in request.headers
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await download_binance_kline_archive(
            item=item, archive_root=tmp_path / "archives", client=client, retry_count=1
        )

    assert result.archive_path.read_bytes() == payload
    assert archive_requests == 2


@pytest.mark.asyncio
async def test_binance_archive_checksum_mismatch_discards_part_before_full_retry(
    tmp_path: Path,
) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    payload = b"good verified ZIP"
    expected = sha256(payload).hexdigest()
    archive_path = tmp_path / "archives" / "daily" / Path(item.url).name
    archive_path.parent.mkdir(parents=True)
    archive_path.with_suffix(".zip.part").write_bytes(b"part")
    archive_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal archive_requests
        if request.url == httpx.URL(item.checksum_url):
            return httpx.Response(200, text=f"{expected}  {archive_path.name}\n")
        archive_requests += 1
        if archive_requests == 1:
            assert request.headers["Range"] == "bytes=4-"
            return httpx.Response(
                206,
                headers={"Content-Range": "bytes 4-6/7"},
                content=b"bad",
            )
        assert "Range" not in request.headers
        return httpx.Response(200, content=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await download_binance_kline_archive(
            item=item, archive_root=tmp_path / "archives", client=client, retry_count=1
        )

    assert result.archive_path.read_bytes() == payload
    assert archive_requests == 2


@pytest.mark.asyncio
async def test_binance_archive_download_records_404_without_retrying_inventory(
    tmp_path: Path,
) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BinanceArchiveNotFoundError, match="CHECKSUM"):
            await download_binance_kline_archive(
                item=item, archive_root=tmp_path / "archives", client=client
            )

    assert calls == 1
    inventory = tmp_path / "archives" / "daily" / f"{Path(item.url).name}.inventory.json"
    assert '"status":"missing"' in inventory.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_binance_archive_body_404_is_persisted_without_retry(tmp_path: Path) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url == httpx.URL(item.checksum_url):
            return httpx.Response(200, text=f"{'a' * 64}  {Path(item.url).name}\n")
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BinanceArchiveNotFoundError):
            await download_binance_kline_archive(
                item=item, archive_root=tmp_path / "archives", client=client
            )

    assert calls == 2
    inventory = tmp_path / "archives" / "daily" / f"{Path(item.url).name}.inventory.json"
    assert '"status":"missing"' in inventory.read_text(encoding="utf-8")


def test_binance_archive_materializes_streamed_microsecond_rows_to_atomic_partitions(
    tmp_path: Path,
) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    archive_path = tmp_path / "BTCUSDT-1s-2026-08-06.zip"
    start_us = 1_786_032_000_000_000
    _write_zip(
        archive_path,
        [
            _kline_row(start_us),
            _kline_row(start_us + 1_000_000),
            _kline_row(start_us + 3_000_000),
        ],
    )
    source_hash = sha256(archive_path.read_bytes()).hexdigest()
    source_manifest = tmp_path / "archive.json"
    source_manifest.write_text(
        '{"source_url":"https://data.binance.vision/example.zip","checksum_text":"x",'
        f'"verified_zip_sha256":"{source_hash}"}}\n',
        encoding="utf-8",
    )
    download = BinanceArchiveDownload(
        archive_path=archive_path,
        manifest_path=source_manifest,
        verified_sha256=source_hash,
        checksum_text="x",
        resumed=False,
        reused=False,
    )

    result = materialize_binance_kline_archive(
        item=item, download=download, materialized_root=tmp_path / "parquet", batch_size=2
    )

    assert result.row_count == 3
    # The manifest audits the complete UTC day, including missing leading and
    # trailing seconds, rather than reporting only the one internal gap.
    assert result.gap_count == 86_397
    assert len(result.part_paths) == len(result.manifest_paths) == 1
    assert result.part_paths[0].is_file()
    manifest = result.manifest_paths[0].read_text(encoding="utf-8")
    assert '"min_open_time_ns":1786032000000000000' in manifest
    assert '"gap_count":86397' in manifest
    assert not list(tmp_path.rglob("*.csv"))
    assert (
        materialize_binance_kline_archive(
            item=item, download=download, materialized_root=tmp_path / "parquet", batch_size=2
        ).part_paths
        == result.part_paths
    )


def test_materialized_history_loads_verified_parts_without_restoring_zip(tmp_path: Path) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    archive_path = tmp_path / "BTCUSDT-1s-2026-08-06.zip"
    start_us = 1_786_032_000_000_000
    _write_zip(archive_path, [_kline_row(start_us), _kline_row(start_us + 1_000_000)])
    download = _download_fixture(tmp_path, archive_path)
    result = materialize_binance_kline_archive(
        item=item, download=download, materialized_root=tmp_path / "parquet", batch_size=2
    )
    manifest = json.loads(result.manifest_paths[0].read_text(encoding="utf-8"))
    manifest["gap_count"] = 0
    result.manifest_paths[0].write_text(json.dumps(manifest), encoding="utf-8")

    start_time = datetime.fromtimestamp(start_us / 1_000_000, tz=UTC)
    history = load_materialized_binance_kline_history(
        tmp_path / "parquet",
        start_time=start_time,
        end_time=start_time + timedelta(seconds=2),
    )

    assert history.open_ts_ns.tolist() == [start_us * 1_000, (start_us + 1_000_000) * 1_000]


def test_binance_archive_materialization_writes_explicit_manifest_for_missing_planned_days(
    tmp_path: Path,
) -> None:
    item = BinanceArchivePlanItem(
        kind="monthly",
        coverage_start=date(2026, 8, 6),
        coverage_end=date(2026, 8, 7),
        url="https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1s/example.zip",
    )
    archive_path = tmp_path / "example.zip"
    _write_zip(archive_path, [_kline_row(1_786_118_400_000_000)])
    download = _download_fixture(tmp_path, archive_path)

    result = materialize_binance_kline_archive(
        item=item,
        download=download,
        materialized_root=tmp_path / "parquet",
        batch_size=1,
    )

    manifests = {
        path.parent.name: path.read_text(encoding="utf-8") for path in result.manifest_paths
    }
    assert set(manifests) == {"date=2026-08-06", "date=2026-08-07"}
    assert '"row_count":0' in manifests["date=2026-08-06"]
    assert '"gap_count":86400' in manifests["date=2026-08-06"]
    assert '"row_count":1' in manifests["date=2026-08-07"]
    assert '"gap_count":86399' in manifests["date=2026-08-07"]


def test_binance_archive_materialization_reuse_rejects_tampered_part_and_schema(
    tmp_path: Path,
) -> None:
    item = plan_binance_spot_kline_archives(start_day=date(2026, 8, 6), end_day=date(2026, 8, 6))[0]
    archive_path = tmp_path / "example.zip"
    _write_zip(archive_path, [_kline_row(1_786_032_000_000_000)])
    download = _download_fixture(tmp_path, archive_path)
    root = tmp_path / "parquet"
    result = materialize_binance_kline_archive(
        item=item, download=download, materialized_root=root, batch_size=1
    )
    original_part = result.part_paths[0].read_bytes()
    result.part_paths[0].write_bytes(b"tampered")

    with pytest.raises(ValueError, match="materialized part"):
        materialize_binance_kline_archive(
            item=item, download=download, materialized_root=root, batch_size=1
        )

    result.part_paths[0].write_bytes(original_part)
    pq.write_table(pa.table({"wrong": [1]}), result.part_paths[0])
    manifest = result.manifest_paths[0]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["part_sha256"] = sha256(result.part_paths[0].read_bytes()).hexdigest()
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical schema"):
        materialize_binance_kline_archive(
            item=item, download=download, materialized_root=root, batch_size=1
        )


def test_binance_archive_cli_dry_run_prints_the_full_requested_plan() -> None:
    args = parse_args(("plan", "--dry-run"))

    result = run(args)

    assert result["action"] == "plan"
    assert result["dry_run"] is True
    assert result["archive_count"] == 38
    assert result["coverage"] == {"start": "2025-10-09", "end": "2026-08-06"}


@pytest.mark.asyncio
async def test_binance_archive_cli_inventory_continues_after_missing_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_binance_spot_kline_archives(start_day=date(2026, 8, 5), end_day=date(2026, 8, 6))
    calls: list[str] = []

    async def fake_download(*, item, archive_root, client):  # type: ignore[no-untyped-def]
        del archive_root, client
        calls.append(item.url)
        if item == plan[0]:
            raise BinanceArchiveNotFoundError(item.url)
        return BinanceArchiveDownload(
            archive_path=tmp_path / "ok.zip",
            manifest_path=tmp_path / "ok.json",
            verified_sha256="a" * 64,
            checksum_text="checksum",
            resumed=False,
            reused=False,
        )

    monkeypatch.setattr(binance_history_cli, "download_binance_kline_archive", fake_download)

    downloads, missing = await binance_history_cli._download(plan=plan, archive_root=tmp_path)

    assert len(calls) == 2
    assert len(downloads) == 1
    assert missing == (plan[0].url,)


def _kline_row(open_time_us: int) -> str:
    return ",".join(
        (
            str(open_time_us),
            "100000",
            "100001",
            "99999",
            "100000.5",
            "2",
            str(open_time_us + 999_999),
            "200000",
            "3",
            "1.2",
            "120000",
            "0",
        )
    )


def _write_zip(path: Path, rows: list[str]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("BTCUSDT-1s.csv", "\n".join(rows) + "\n")


def _download_fixture(root: Path, archive_path: Path) -> BinanceArchiveDownload:
    source_hash = sha256(archive_path.read_bytes()).hexdigest()
    source_manifest = root / f"{archive_path.stem}.manifest.json"
    source_manifest.write_text(
        '{"source_url":"https://data.binance.vision/example.zip","checksum_text":"x",'
        f'"verified_zip_sha256":"{source_hash}"}}\n',
        encoding="utf-8",
    )
    return BinanceArchiveDownload(
        archive_path=archive_path,
        manifest_path=source_manifest,
        verified_sha256=source_hash,
        checksum_text="x",
        resumed=False,
        reused=False,
    )
