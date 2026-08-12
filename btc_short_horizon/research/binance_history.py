"""Compact event-time-only Binance kline archive access for proxy research."""

from __future__ import annotations

from dataclasses import dataclass
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from pathlib import Path
from typing import Sequence
import asyncio
import hashlib
import json
import os
import zipfile

import httpx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


_KLINE_COLUMNS = (0, 4, 5, 7, 9)
_INTERVAL_SECONDS = {"1s": 1, "1m": 60}
_BINANCE_SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
_MAX_BOOTSTRAP_BARS = 4_000
_PAGE_SIZE = 1_000


@dataclass(frozen=True, slots=True)
class BinanceArchivePlanItem:
    """One non-overlapping official Binance kline archive acquisition unit."""

    kind: str
    coverage_start: date
    coverage_end: date
    url: str

    def __post_init__(self) -> None:
        if self.kind not in {"daily", "monthly"}:
            raise ValueError("archive kind must be daily or monthly")
        if self.coverage_end < self.coverage_start:
            raise ValueError("archive coverage must be ordered")
        if not self.url.startswith("https://"):
            raise ValueError("archive URL must use HTTPS")

    @property
    def checksum_url(self) -> str:
        return f"{self.url}.CHECKSUM"

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(
            self.coverage_start + timedelta(days=offset)
            for offset in range((self.coverage_end - self.coverage_start).days + 1)
        )


@dataclass(frozen=True, slots=True)
class BinanceArchiveDownload:
    archive_path: Path
    manifest_path: Path
    verified_sha256: str
    checksum_text: str
    resumed: bool
    reused: bool


class BinanceArchiveNotFoundError(FileNotFoundError):
    """The official archive inventory explicitly reports an unavailable file."""


class _ArchiveRestartRequired(ValueError):
    """A partial response cannot safely be resumed."""


class _ArchiveChecksumMismatch(ValueError):
    """The downloaded bytes do not match the official checksum."""


@dataclass(frozen=True, slots=True)
class BinanceKlineMaterialization:
    part_paths: tuple[Path, ...]
    manifest_paths: tuple[Path, ...]
    row_count: int
    gap_count: int


_MATERIALIZED_KLINE_SCHEMA = pa.schema(
    (
        pa.field("open_time_ns", pa.int64()),
        pa.field("close_time_ns", pa.int64()),
        pa.field("open", pa.float64()),
        pa.field("high", pa.float64()),
        pa.field("low", pa.float64()),
        pa.field("close", pa.float64()),
        pa.field("volume", pa.float64()),
        pa.field("quote_volume", pa.float64()),
        pa.field("trade_count", pa.int64()),
        pa.field("taker_buy_volume", pa.float64()),
        pa.field("taker_buy_quote_volume", pa.float64()),
    )
)
_MATERIALIZER_VERSION = "binance-kline-materializer-v1"


def plan_binance_spot_kline_archives(
    *,
    start_day: date,
    end_day: date,
    symbol: str = "BTCUSDT",
    interval: str = "1s",
) -> tuple[BinanceArchivePlanItem, ...]:
    """Plan exact, non-overlapping official daily/monthly archive coverage."""

    if end_day < start_day:
        raise ValueError("end_day must not precede start_day")
    if not symbol.strip() or interval not in _INTERVAL_SECONDS:
        raise ValueError("symbol is required; interval must be 1s or 1m")
    items: list[BinanceArchivePlanItem] = []
    cursor = start_day
    while cursor <= end_day:
        month_end = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        if cursor.day == 1 and month_end <= end_day:
            stamp = cursor.strftime("%Y-%m")
            items.append(
                BinanceArchivePlanItem(
                    kind="monthly",
                    coverage_start=cursor,
                    coverage_end=month_end,
                    url=_binance_archive_url(
                        kind="monthly", symbol=symbol, interval=interval, stamp=stamp
                    ),
                )
            )
            cursor = month_end + timedelta(days=1)
            continue
        stamp = cursor.isoformat()
        items.append(
            BinanceArchivePlanItem(
                kind="daily",
                coverage_start=cursor,
                coverage_end=cursor,
                url=_binance_archive_url(
                    kind="daily", symbol=symbol, interval=interval, stamp=stamp
                ),
            )
        )
        cursor += timedelta(days=1)
    return tuple(items)


def _binance_archive_url(*, kind: str, symbol: str, interval: str, stamp: str) -> str:
    normalized_symbol = symbol.strip().upper()
    return (
        f"https://data.binance.vision/data/spot/{kind}/klines/"
        f"{normalized_symbol}/{interval}/{normalized_symbol}-{interval}-{stamp}.zip"
    )


async def download_binance_kline_archive(
    *,
    item: BinanceArchivePlanItem,
    archive_root: Path,
    client: httpx.AsyncClient,
    retry_count: int = 2,
) -> BinanceArchiveDownload:
    """Fetch one ZIP with verified checksum and crash-safe resume semantics."""

    if retry_count < 0:
        raise ValueError("retry_count must be >= 0")
    archive_path = archive_root / item.kind / Path(item.url).name
    manifest_path = archive_path.with_suffix(".zip.manifest.json")
    inventory_path = archive_path.with_name(f"{archive_path.name}.inventory.json")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        checksum_text = await _download_checksum(item=item, client=client)
    except BinanceArchiveNotFoundError:
        _write_atomic_json(
            inventory_path,
            {
                "schema_version": "binance-kline-inventory-v1",
                "status": "missing",
                "source_url": item.url,
                "checksum_url": item.checksum_url,
                "checked_at": datetime.now(UTC).isoformat(),
            },
        )
        raise
    expected_sha256 = _checksum_sha256(checksum_text, filename=archive_path.name)
    if archive_path.is_file() and _sha256_file(archive_path) == expected_sha256:
        if manifest_path.is_file():
            existing = _read_json_mapping(manifest_path)
            if (
                existing.get("source_url") != item.url
                or existing.get("checksum_text") != checksum_text
                or existing.get("verified_zip_sha256") != expected_sha256
            ):
                raise ValueError("existing archive manifest conflicts with current CHECKSUM")
        else:
            _write_archive_manifest(
                path=manifest_path,
                item=item,
                checksum_text=checksum_text,
                verified_sha256=expected_sha256,
            )
        return BinanceArchiveDownload(
            archive_path=archive_path,
            manifest_path=manifest_path,
            verified_sha256=expected_sha256,
            checksum_text=checksum_text,
            resumed=False,
            reused=True,
        )
    part_path = archive_path.with_suffix(".zip.part")
    resumed = False
    for attempt in range(retry_count + 1):
        try:
            resumed = await _download_archive_to_part(
                item=item,
                part_path=part_path,
                client=client,
            )
            if _sha256_file(part_path) != expected_sha256:
                raise _ArchiveChecksumMismatch(
                    "Binance archive SHA-256 does not match its CHECKSUM"
                )
            os.replace(part_path, archive_path)
            _write_archive_manifest(
                path=manifest_path,
                item=item,
                checksum_text=checksum_text,
                verified_sha256=expected_sha256,
            )
            return BinanceArchiveDownload(
                archive_path=archive_path,
                manifest_path=manifest_path,
                verified_sha256=expected_sha256,
                checksum_text=checksum_text,
                resumed=resumed,
                reused=False,
            )
        except (_ArchiveRestartRequired, _ArchiveChecksumMismatch):
            part_path.unlink(missing_ok=True)
            if attempt >= retry_count:
                raise
            await asyncio.sleep(0.25 * (2**attempt))
        except BinanceArchiveNotFoundError:
            _write_missing_inventory(path=inventory_path, item=item)
            raise
        except (httpx.HTTPError, OSError, ValueError):
            if attempt >= retry_count:
                raise
            await asyncio.sleep(0.25 * (2**attempt))
    raise AssertionError("unreachable")


async def _download_checksum(*, item: BinanceArchivePlanItem, client: httpx.AsyncClient) -> str:
    response = await client.get(item.checksum_url)
    if response.status_code == 404:
        raise BinanceArchiveNotFoundError(item.checksum_url)
    response.raise_for_status()
    return response.text


def _write_missing_inventory(*, path: Path, item: BinanceArchivePlanItem) -> None:
    _write_atomic_json(
        path,
        {
            "schema_version": "binance-kline-inventory-v1",
            "status": "missing",
            "source_url": item.url,
            "checksum_url": item.checksum_url,
            "checked_at": datetime.now(UTC).isoformat(),
        },
    )


async def _download_archive_to_part(
    *,
    item: BinanceArchivePlanItem,
    part_path: Path,
    client: httpx.AsyncClient,
) -> bool:
    offset = part_path.stat().st_size if part_path.is_file() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else None
    async with client.stream("GET", item.url, headers=headers) as response:
        if response.status_code == 404:
            raise BinanceArchiveNotFoundError(item.url)
        if offset and response.status_code == 416:
            raise _ArchiveRestartRequired("Binance archive range resume was rejected")
        response.raise_for_status()
        if (
            offset
            and response.status_code == 206
            and not _valid_content_range(response.headers.get("Content-Range"), offset=offset)
        ):
            raise _ArchiveRestartRequired("Binance archive range response is invalid")
        resumed = offset > 0 and response.status_code == 206
        mode = "ab" if resumed else "wb"
        with part_path.open(mode) as handle:
            async for chunk in response.aiter_bytes():
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if resumed and part_path.stat().st_size <= offset:
            raise _ArchiveRestartRequired("Binance archive range response made no progress")
    return resumed


def _valid_content_range(value: str | None, *, offset: int) -> bool:
    if value is None:
        return False
    prefix = f"bytes {offset}-"
    return value.startswith(prefix) and "/" in value


def _checksum_sha256(checksum_text: str, *, filename: str) -> str:
    parts = checksum_text.strip().split()
    if len(parts) < 2 or parts[1].lstrip("*") != filename:
        raise ValueError("Binance CHECKSUM does not name the requested archive")
    digest = parts[0].lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Binance CHECKSUM must contain a SHA-256 digest")
    return digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_archive_manifest(
    *,
    path: Path,
    item: BinanceArchivePlanItem,
    checksum_text: str,
    verified_sha256: str,
) -> None:
    payload = {
        "schema_version": "binance-kline-archive-v1",
        "source_url": item.url,
        "checksum_url": item.checksum_url,
        "checksum_text": checksum_text,
        "verified_zip_sha256": verified_sha256,
        "fetched_at": datetime.now(UTC).isoformat(),
        "coverage": {
            "start": item.coverage_start.isoformat(),
            "end": item.coverage_end.isoformat(),
        },
    }
    _write_atomic_json(path, payload)


def _write_atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def materialize_binance_kline_archive(
    *,
    item: BinanceArchivePlanItem,
    download: BinanceArchiveDownload,
    materialized_root: Path,
    batch_size: int = 10_000,
) -> BinanceKlineMaterialization:
    """Stream one verified ZIP into daily atomic Parquet parts and manifests."""

    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if _sha256_file(download.archive_path) != download.verified_sha256:
        raise ValueError("archive no longer matches its verified SHA-256")
    source = _read_archive_manifest(download.manifest_path)
    if source["verified_zip_sha256"] != download.verified_sha256:
        raise ValueError("archive manifest does not match the verified ZIP")
    allowed_days = set(item.days)
    states: dict[date, _KlineDayMaterializer] = {}
    previous_open_us: int | None = None
    try:
        with zipfile.ZipFile(download.archive_path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if len(names) != 1:
                raise ValueError("Binance archive must contain exactly one CSV member")
            with archive.open(names[0], "r") as raw_csv:
                import csv
                import io

                reader = csv.reader(io.TextIOWrapper(raw_csv, encoding="utf-8", newline=""))
                for row in reader:
                    parsed = _parse_archive_kline_row(row)
                    open_us = parsed[0]
                    row_day = datetime.fromtimestamp(open_us / 1_000_000, UTC).date()
                    if row_day not in allowed_days:
                        raise ValueError(
                            "Binance archive row falls outside the planned date coverage"
                        )
                    if previous_open_us is not None:
                        delta = open_us - previous_open_us
                        if delta <= 0:
                            raise ValueError(
                                "Binance archive contains duplicate or unordered klines"
                            )
                        if delta % 1_000_000:
                            raise ValueError("Binance archive klines must align to whole seconds")
                    previous_open_us = open_us
                    state = states.get(row_day)
                    if state is None:
                        state = _KlineDayMaterializer(
                            root=materialized_root,
                            day=row_day,
                            source=source,
                            batch_size=batch_size,
                        )
                        states[row_day] = state
                    state.append(parsed)
    except zipfile.BadZipFile as exc:
        for state in states.values():
            state.abort()
        raise ValueError("Binance archive is not a readable ZIP") from exc
    except Exception:
        for state in states.values():
            state.abort()
        raise
    results = tuple(
        (
            states[day].finish()
            if day in states
            else _write_missing_day_manifest(root=materialized_root, day=day, source=source)
        )
        for day in sorted(allowed_days)
    )
    return BinanceKlineMaterialization(
        part_paths=tuple(result[0] for result in results if result[0] is not None),
        manifest_paths=tuple(result[1] for result in results),
        row_count=sum(result[2] for result in results),
        gap_count=sum(result[3] for result in results),
    )


class _KlineDayMaterializer:
    def __init__(
        self,
        *,
        root: Path,
        day: date,
        source: dict[str, str],
        batch_size: int,
    ) -> None:
        self._day = day
        self._source = source
        self._batch_size = batch_size
        partition = root / f"date={day.isoformat()}"
        source_hash = source["verified_zip_sha256"]
        self._part_path = partition / f"part-{source_hash[:16]}.parquet"
        self._manifest_path = partition / f"manifest-{source_hash[:16]}.json"
        self._rows: list[dict[str, int | float]] = []
        self._row_count = 0
        self._gap_count = 0
        self._min_open_time_ns: int | None = None
        self._max_open_time_ns: int | None = None
        self._previous_open_us: int | None = None
        self._writer: pq.ParquetWriter | None = None
        self._temporary_path: Path | None = None
        self._reused = self._existing_manifest_or_raise()
        if not self._reused:
            partition.mkdir(parents=True, exist_ok=True)
            self._temporary_path = self._part_path.with_name(f".{self._part_path.name}.tmp")
            self._writer = pq.ParquetWriter(self._temporary_path, _MATERIALIZED_KLINE_SCHEMA)

    def append(
        self, row: tuple[int, int, float, float, float, float, float, float, int, float, float]
    ) -> None:
        open_us = row[0]
        if self._previous_open_us is not None:
            delta = open_us - self._previous_open_us
            if delta > 1_000_000:
                self._gap_count += delta // 1_000_000 - 1
        self._previous_open_us = open_us
        open_ns = open_us * 1_000
        self._min_open_time_ns = (
            open_ns if self._min_open_time_ns is None else self._min_open_time_ns
        )
        self._max_open_time_ns = open_ns
        self._row_count += 1
        if self._reused:
            return
        self._rows.append(
            {
                "open_time_ns": open_ns,
                "close_time_ns": row[1] * 1_000,
                "open": row[2],
                "high": row[3],
                "low": row[4],
                "close": row[5],
                "volume": row[6],
                "quote_volume": row[7],
                "trade_count": row[8],
                "taker_buy_volume": row[9],
                "taker_buy_quote_volume": row[10],
            }
        )
        if len(self._rows) >= self._batch_size:
            self._flush()

    def finish(self) -> tuple[Path, Path, int, int]:
        if self._reused:
            manifest = _read_json_mapping(self._manifest_path)
            return (
                self._part_path,
                self._manifest_path,
                _manifest_int(manifest, "row_count"),
                _manifest_int(manifest, "gap_count"),
            )
        self._flush()
        assert self._writer is not None and self._temporary_path is not None
        self._writer.close()
        with self._temporary_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(self._temporary_path, self._part_path)
        gap_count = self._coverage_gap_count()
        _write_atomic_json(
            self._manifest_path,
            {
                "schema_version": "binance-kline-parquet-v1",
                "tool_version": _MATERIALIZER_VERSION,
                "source_archive_url": self._source["source_url"],
                "source_checksum_text": self._source["checksum_text"],
                "verified_zip_sha256": self._source["verified_zip_sha256"],
                "part_sha256": _sha256_file(self._part_path),
                "row_count": self._row_count,
                "min_open_time_ns": self._min_open_time_ns,
                "max_open_time_ns": self._max_open_time_ns,
                "coverage_start_ns": int(
                    datetime(self._day.year, self._day.month, self._day.day, tzinfo=UTC).timestamp()
                )
                * 1_000_000_000,
                "coverage_end_ns": int(
                    datetime(self._day.year, self._day.month, self._day.day, tzinfo=UTC).timestamp()
                )
                * 1_000_000_000
                + 86_399 * 1_000_000_000,
                "gap_count": gap_count,
                "duplicate_count": 0,
                "schema": str(_MATERIALIZED_KLINE_SCHEMA),
                "event_time_only": True,
            },
        )
        return self._part_path, self._manifest_path, self._row_count, gap_count

    def _flush(self) -> None:
        if not self._rows:
            return
        assert self._writer is not None
        self._writer.write_table(
            pa.Table.from_pylist(self._rows, schema=_MATERIALIZED_KLINE_SCHEMA)
        )
        self._rows.clear()

    def abort(self) -> None:
        """Close an incomplete writer and remove its unpublished temporary part."""

        if self._writer is not None:
            self._writer.close()
            self._writer = None
        if self._temporary_path is not None:
            self._temporary_path.unlink(missing_ok=True)

    def _existing_manifest_or_raise(self) -> bool:
        if self._manifest_path.is_file():
            manifest = _read_json_mapping(self._manifest_path)
            _verify_reused_materialization(
                manifest=manifest,
                part_path=self._part_path,
                source=self._source,
            )
            return True
        if not self._manifest_path.parent.is_dir():
            return False
        for path in self._manifest_path.parent.glob("manifest-*.json"):
            manifest = _read_json_mapping(path)
            if (
                manifest.get("source_archive_url") == self._source["source_url"]
                and manifest.get("verified_zip_sha256") != self._source["verified_zip_sha256"]
            ):
                raise ValueError(
                    "existing materialized lineage conflicts with the archive checksum"
                )
        return False

    def _coverage_gap_count(self) -> int:
        if self._min_open_time_ns is None or self._max_open_time_ns is None:
            return 86_400
        day_start_ns = int(
            datetime(self._day.year, self._day.month, self._day.day, tzinfo=UTC).timestamp()
        ) * (1_000_000_000)
        day_end_ns = day_start_ns + 86_399 * 1_000_000_000
        return (
            self._gap_count
            + (self._min_open_time_ns - day_start_ns) // 1_000_000_000
            + (day_end_ns - self._max_open_time_ns) // 1_000_000_000
        )


def _write_missing_day_manifest(
    *, root: Path, day: date, source: dict[str, str]
) -> tuple[None, Path, int, int]:
    partition = root / f"date={day.isoformat()}"
    manifest_path = partition / f"manifest-{source['verified_zip_sha256'][:16]}.json"
    if manifest_path.is_file():
        manifest = _read_json_mapping(manifest_path)
        if manifest.get("verified_zip_sha256") != source["verified_zip_sha256"]:
            raise ValueError("existing materialized lineage conflicts with the archive checksum")
        return (
            None,
            manifest_path,
            _manifest_int(manifest, "row_count"),
            _manifest_int(manifest, "gap_count"),
        )
    partition.mkdir(parents=True, exist_ok=True)
    day_start_ns = (
        int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) * 1_000_000_000
    )
    _write_atomic_json(
        manifest_path,
        {
            "schema_version": "binance-kline-parquet-v1",
            "tool_version": _MATERIALIZER_VERSION,
            "source_archive_url": source["source_url"],
            "source_checksum_text": source["checksum_text"],
            "verified_zip_sha256": source["verified_zip_sha256"],
            "part_sha256": None,
            "row_count": 0,
            "min_open_time_ns": None,
            "max_open_time_ns": None,
            "gap_count": 86_400,
            "duplicate_count": 0,
            "schema": str(_MATERIALIZED_KLINE_SCHEMA),
            "event_time_only": True,
            "coverage_start_ns": day_start_ns,
            "coverage_end_ns": day_start_ns + 86_399 * 1_000_000_000,
        },
    )
    return None, manifest_path, 0, 86_400


def _parse_archive_kline_row(
    row: Sequence[str],
) -> tuple[int, int, float, float, float, float, float, float, int, float, float]:
    if len(row) != 12:
        raise ValueError("Binance archive kline rows must contain exactly 12 fields")
    open_us = _integer_field(row[0], name="open time")
    close_us = _integer_field(row[6], name="close time")
    if open_us < 1_000_000_000_000_000 or open_us % 1_000_000:
        raise ValueError("Binance archive kline open time must be an aligned microsecond timestamp")
    if close_us != open_us + 999_999:
        raise ValueError("Binance archive kline close time does not match one second")
    open_price = _finite_float(row[1], name="open", positive=True)
    high = _finite_float(row[2], name="high", positive=True)
    low = _finite_float(row[3], name="low", positive=True)
    close = _finite_float(row[4], name="close", positive=True)
    volume = _finite_float(row[5], name="volume")
    quote_volume = _finite_float(row[7], name="quote volume")
    trade_count = _integer_field(row[8], name="trade count")
    taker_buy_volume = _finite_float(row[9], name="taker buy volume")
    taker_buy_quote_volume = _finite_float(row[10], name="taker buy quote volume")
    if taker_buy_volume > volume:
        raise ValueError("Binance taker buy volume cannot exceed total volume")
    return (
        open_us,
        close_us,
        open_price,
        high,
        low,
        close,
        volume,
        quote_volume,
        trade_count,
        taker_buy_volume,
        taker_buy_quote_volume,
    )


def _read_archive_manifest(path: Path) -> dict[str, str]:
    payload = _read_json_mapping(path)
    required = ("source_url", "checksum_text", "verified_zip_sha256")
    if any(not isinstance(payload.get(name), str) or not payload[name] for name in required):
        raise ValueError("archive manifest is missing required provenance")
    return {name: str(payload[name]) for name in required}


def _read_json_mapping(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("materialized manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("materialized manifest must be an object")
    return payload


def _manifest_int(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("materialized manifest is invalid")
    return value


def _verify_reused_materialization(
    *, manifest: dict[str, object], part_path: Path, source: dict[str, str]
) -> None:
    if (
        manifest.get("source_archive_url") != source["source_url"]
        or manifest.get("source_checksum_text") != source["checksum_text"]
        or manifest.get("verified_zip_sha256") != source["verified_zip_sha256"]
    ):
        raise ValueError("existing materialized lineage conflicts with the archive checksum")
    expected_hash = manifest.get("part_sha256")
    if not isinstance(expected_hash, str) or not part_path.is_file():
        raise ValueError("materialized manifest references a missing Parquet part")
    if _sha256_file(part_path) != expected_hash:
        raise ValueError("materialized part SHA-256 does not match its manifest")
    try:
        parquet = pq.ParquetFile(part_path)
        if parquet.schema_arrow != _MATERIALIZED_KLINE_SCHEMA:
            raise ValueError("materialized part does not use the canonical schema")
        row_count = _manifest_int(manifest, "row_count")
        if parquet.metadata is None or parquet.metadata.num_rows != row_count:
            raise ValueError("materialized part row count does not match its manifest")
        rows = parquet.read(columns=["open_time_ns"])["open_time_ns"].to_pylist()
    except (OSError, pa.ArrowException) as exc:
        raise ValueError("materialized part is unreadable") from exc
    if not rows:
        raise ValueError("materialized part must contain event-time rows")
    if rows[0] != manifest.get("min_open_time_ns") or rows[-1] != manifest.get("max_open_time_ns"):
        raise ValueError("materialized part bounds do not match its manifest")


@dataclass(frozen=True, slots=True)
class BinanceKlineHistory:
    """Aligned Binance spot kline fields with no fabricated receive timestamp."""

    open_ts_ns: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    quote_volume: np.ndarray
    taker_buy_volume: np.ndarray
    interval_seconds: int = 1

    def __post_init__(self) -> None:
        if self.interval_seconds not in set(_INTERVAL_SECONDS.values()):
            raise ValueError("supported kline intervals are 1 second and 1 minute")
        fields = (
            self.open_ts_ns,
            self.close,
            self.volume,
            self.quote_volume,
            self.taker_buy_volume,
        )
        lengths = {len(np.asarray(field)) for field in fields}
        if len(lengths) != 1 or next(iter(lengths), 0) == 0:
            raise ValueError("kline fields must be non-empty and aligned")
        if not np.isfinite(np.asarray(self.close, dtype=float)).all() or np.any(self.close <= 0.0):
            raise ValueError("kline close values must be finite and positive")
        if any(np.any(np.asarray(field, dtype=float) < 0.0) for field in fields[2:]):
            raise ValueError("kline volumes must be non-negative")
        if np.any(np.diff(np.asarray(self.open_ts_ns, dtype=np.int64)) <= 0):
            raise ValueError("kline timestamps must be strictly increasing")

    @property
    def source_hash(self) -> str:
        digest = hashlib.sha256()
        for field in (
            self.open_ts_ns,
            self.close,
            self.volume,
            self.quote_volume,
            self.taker_buy_volume,
        ):
            digest.update(np.asarray(field).tobytes())
        return digest.hexdigest()


def binance_spot_kline_url(*, day: str, symbol: str = "BTCUSDT", interval: str = "1m") -> str:
    if not day or not symbol or interval not in _INTERVAL_SECONDS:
        raise ValueError("day and symbol are required; interval must be 1s or 1m")
    return _binance_archive_url(kind="daily", symbol=symbol, interval=interval, stamp=day)


def load_binance_kline_archives(
    paths: Sequence[Path], *, interval: str = "1m"
) -> BinanceKlineHistory:
    if interval not in _INTERVAL_SECONDS:
        raise ValueError("interval must be 1s or 1m")
    if not paths:
        raise ValueError("at least one Binance kline archive is required")
    frames: list[pd.DataFrame] = []
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".csv")]
            if len(names) != 1:
                raise ValueError(f"{path} must contain exactly one CSV kline file")
        frames.append(
            pd.read_csv(
                path,
                compression="zip",
                header=None,
                usecols=list(_KLINE_COLUMNS),
                names=("open_time", "close", "volume", "quote_volume", "taker_buy_volume"),
                dtype={
                    "open_time": "int64",
                    "close": "float64",
                    "volume": "float64",
                    "quote_volume": "float64",
                    "taker_buy_volume": "float64",
                },
            )
        )
    combined = pd.concat(frames, ignore_index=True).sort_values("open_time", kind="stable")
    combined = combined.drop_duplicates(subset="open_time", keep=False)
    if combined.empty:
        raise ValueError("Binance archives contain no unique kline rows")
    return BinanceKlineHistory(
        open_ts_ns=_epoch_to_ns(combined["open_time"].to_numpy(dtype=np.int64)),
        close=combined["close"].to_numpy(dtype=float),
        volume=combined["volume"].to_numpy(dtype=float),
        quote_volume=combined["quote_volume"].to_numpy(dtype=float),
        taker_buy_volume=combined["taker_buy_volume"].to_numpy(dtype=float),
        interval_seconds=_INTERVAL_SECONDS[interval],
    )


def load_materialized_binance_kline_history(
    directory: Path,
    *,
    start_time: datetime,
    end_time: datetime,
    interval: str = "1s",
) -> BinanceKlineHistory:
    """Load verified daily Parquet parts without restoring their source ZIPs."""

    if interval not in _INTERVAL_SECONDS:
        raise ValueError("interval must be 1s or 1m")
    if start_time.tzinfo is None or end_time.tzinfo is None or end_time <= start_time:
        raise ValueError("materialized history bounds must be ordered and timezone-aware")
    start_day = start_time.astimezone(UTC).date()
    end_day = (end_time.astimezone(UTC) - timedelta(microseconds=1)).date()
    tables: list[pa.Table] = []
    for offset in range((end_day - start_day).days + 1):
        day = start_day + timedelta(days=offset)
        partition = directory / f"date={day.isoformat()}"
        manifests = tuple(sorted(partition.glob("manifest-*.json")))
        parts = tuple(sorted(partition.glob("part-*.parquet")))
        if len(manifests) != 1 or len(parts) != 1:
            raise FileNotFoundError(f"complete materialized partition unavailable: {partition}")
        manifest = _read_json_mapping(manifests[0])
        _verify_reused_materialization(
            manifest=manifest,
            part_path=parts[0],
            source={
                "source_url": str(manifest.get("source_archive_url", "")),
                "checksum_text": str(manifest.get("source_checksum_text", "")),
                "verified_zip_sha256": str(manifest.get("verified_zip_sha256", "")),
            },
        )
        if _manifest_int(manifest, "gap_count") != 0:
            raise ValueError(f"materialized partition contains a kline gap: {partition}")
        tables.append(
            pq.ParquetFile(parts[0]).read(
                columns=(
                    "open_time_ns",
                    "close",
                    "volume",
                    "quote_volume",
                    "taker_buy_volume",
                )
            )
        )
    combined = pa.concat_tables(tables)
    opens = combined["open_time_ns"].to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    lower = int(start_time.timestamp() * 1_000_000_000)
    upper = int(end_time.timestamp() * 1_000_000_000)
    keep = (opens >= lower) & (opens < upper)
    if not np.any(keep):
        raise ValueError("materialized history contains no rows in the requested interval")
    return BinanceKlineHistory(
        open_ts_ns=opens[keep],
        close=combined["close"].to_numpy(zero_copy_only=False)[keep],
        volume=combined["volume"].to_numpy(zero_copy_only=False)[keep],
        quote_volume=combined["quote_volume"].to_numpy(zero_copy_only=False)[keep],
        taker_buy_volume=combined["taker_buy_volume"].to_numpy(zero_copy_only=False)[keep],
        interval_seconds=_INTERVAL_SECONDS[interval],
    )


async def fetch_binance_spot_kline_history(
    *,
    start_time: datetime,
    end_time: datetime,
    symbol: str = "BTCUSDT",
    interval: str = "1s",
    maximum_bars: int = _MAX_BOOTSTRAP_BARS,
    timeout_seconds: float = 10.0,
    client: httpx.AsyncClient | None = None,
) -> BinanceKlineHistory:
    """Fetch a small, final-only REST bootstrap for the deployed model.

    The hard ceiling prevents this live bootstrap from becoming another bulk
    history downloader. Historical training continues to use immutable public
    archives.
    """

    if start_time.tzinfo is None or end_time.tzinfo is None or end_time <= start_time:
        raise ValueError("start_time and end_time must be ordered timezone-aware datetimes")
    if not symbol.strip() or interval not in _INTERVAL_SECONDS:
        raise ValueError("symbol is required; interval must be 1s or 1m")
    if not 1 <= maximum_bars <= _MAX_BOOTSTRAP_BARS:
        raise ValueError(f"maximum_bars must be in [1, {_MAX_BOOTSTRAP_BARS}]")
    if not isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise ValueError("timeout_seconds must be finite and > 0")
    interval_ms = _INTERVAL_SECONDS[interval] * 1_000
    start_ms = int(start_time.timestamp() * 1_000)
    end_ms = int(end_time.timestamp() * 1_000)
    if start_ms % interval_ms != 0:
        raise ValueError("start_time must align to the requested kline interval")
    requested_bars = (end_ms - start_ms + interval_ms - 1) // interval_ms
    if requested_bars > maximum_bars:
        raise ValueError(
            f"requested interval exceeds maximum_bars={maximum_bars}; use archives for bulk data"
        )

    active_client = client or httpx.AsyncClient(timeout=timeout_seconds)
    owns_client = client is None
    raw_rows: list[Sequence[object]] = []
    cursor_ms = start_ms
    try:
        while cursor_ms < end_ms:
            response = await active_client.get(
                _BINANCE_SPOT_KLINES_URL,
                params={
                    "symbol": symbol.strip().upper(),
                    "interval": interval,
                    "startTime": cursor_ms,
                    "endTime": end_ms - 1,
                    "limit": _PAGE_SIZE,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Binance kline response must be a JSON array")
            if not payload:
                break
            if any(
                not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) != 12
                for row in payload
            ):
                raise ValueError("Binance kline rows must contain exactly 12 fields")
            page = [tuple(row) for row in payload]
            first_open_ms = _integer_field(page[0][0], name="open time")
            last_open_ms = _integer_field(page[-1][0], name="open time")
            if first_open_ms < cursor_ms or last_open_ms < first_open_ms:
                raise ValueError("Binance kline response is not causally ordered")
            raw_rows.extend(page)
            next_cursor_ms = last_open_ms + interval_ms
            if next_cursor_ms <= cursor_ms:
                raise ValueError("Binance kline pagination did not advance")
            cursor_ms = next_cursor_ms
            if len(page) < _PAGE_SIZE:
                break
    finally:
        if owns_client:
            await active_client.aclose()

    parsed = _parse_rest_klines(
        rows=raw_rows,
        start_ms=start_ms,
        end_ms=end_ms,
        interval_ms=interval_ms,
    )
    if not parsed:
        raise ValueError("Binance REST bootstrap contains no final klines")
    open_ms = np.asarray([row[0] for row in parsed], dtype=np.int64)
    if np.any(np.diff(open_ms) != interval_ms):
        raise ValueError("Binance REST bootstrap must contain contiguous klines")
    return BinanceKlineHistory(
        open_ts_ns=open_ms * 1_000_000,
        close=np.asarray([row[1] for row in parsed], dtype=float),
        volume=np.asarray([row[2] for row in parsed], dtype=float),
        quote_volume=np.asarray([row[3] for row in parsed], dtype=float),
        taker_buy_volume=np.asarray([row[4] for row in parsed], dtype=float),
        interval_seconds=_INTERVAL_SECONDS[interval],
    )


def _parse_rest_klines(
    *,
    rows: Sequence[Sequence[object]],
    start_ms: int,
    end_ms: int,
    interval_ms: int,
) -> list[tuple[int, float, float, float, float]]:
    parsed: list[tuple[int, float, float, float, float]] = []
    for row in rows:
        open_ms = _integer_field(row[0], name="open time")
        close_ms = _integer_field(row[6], name="close time")
        if close_ms != open_ms + interval_ms - 1:
            raise ValueError("Binance kline close time does not match its interval")
        if open_ms < start_ms or open_ms >= end_ms:
            raise ValueError("Binance kline response contains an out-of-range row")
        if close_ms >= end_ms:
            continue
        close = _finite_float(row[4], name="close", positive=True)
        volume = _finite_float(row[5], name="volume")
        quote_volume = _finite_float(row[7], name="quote volume")
        taker_buy_volume = _finite_float(row[9], name="taker buy volume")
        if taker_buy_volume > volume:
            raise ValueError("Binance taker buy volume cannot exceed total volume")
        parsed.append((open_ms, close, volume, quote_volume, taker_buy_volume))
    return parsed


def _integer_field(value: object, *, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Binance kline {name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Binance kline {name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"Binance kline {name} must be non-negative")
    return parsed


def _finite_float(value: object, *, name: str, positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Binance kline {name} must be numeric") from exc
    if not isfinite(parsed) or parsed < 0.0 or (positive and parsed <= 0.0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"Binance kline {name} must be finite and {qualifier}")
    return parsed


def _epoch_to_ns(values: np.ndarray) -> np.ndarray:
    maximum = int(np.max(values))
    if maximum >= 1_000_000_000_000_000:
        return values * 1_000
    if maximum >= 1_000_000_000_000:
        return values * 1_000_000
    raise ValueError("Binance kline open timestamps must be milliseconds or microseconds")


__all__ = [
    "BinanceArchivePlanItem",
    "BinanceArchiveDownload",
    "BinanceKlineMaterialization",
    "BinanceArchiveNotFoundError",
    "BinanceKlineHistory",
    "binance_spot_kline_url",
    "download_binance_kline_archive",
    "fetch_binance_spot_kline_history",
    "load_binance_kline_archives",
    "load_materialized_binance_kline_history",
    "materialize_binance_kline_archive",
    "plan_binance_spot_kline_archives",
]
