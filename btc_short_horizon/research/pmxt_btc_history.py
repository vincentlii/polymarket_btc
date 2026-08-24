"""BTC-only PMXT extraction and causal dual-token BBO reconstruction."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import time
from uuid import uuid4

import duckdb
import pyarrow.parquet as pq

from btc_short_horizon.data import MarketWindow
from btc_short_horizon.data.storage import sha256_file, write_atomic_json
from btc_short_horizon.features.market_relative import DualTokenBookSnapshot


_CONDITION_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ARCHIVE_FILE = "polymarket_orderbook_{hour}.parquet"


@dataclass(frozen=True, slots=True)
class PmxtMarketWindow:
    condition_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class PmxtDecisionBook:
    decision_ts_ns: int
    market_p_up: float
    data_age_seconds: float
    up: DualTokenBookSnapshot
    down: DualTokenBookSnapshot


def extract_btc_pmxt_history(
    *,
    markets: Sequence[MarketWindow],
    destination: Path,
    archive_base_url: str = "https://r2v2.pmxt.dev",
    lookback_seconds: int = 90,
    entry_end_seconds: int = 180,
    memory_limit: str = "1GB",
    workers: int = 4,
) -> dict[str, object]:
    """Stream only selected BTC condition rows from remote PMXT hourly Parquet."""

    if not archive_base_url.startswith("https://"):
        raise ValueError("PMXT archive_base_url must use https")
    if lookback_seconds < 0 or entry_end_seconds < 0:
        raise ValueError("PMXT extraction horizons must be non-negative")
    if workers < 1 or workers > 8:
        raise ValueError("PMXT extraction workers must be in [1, 8]")
    windows = tuple(
        PmxtMarketWindow(
            condition_id=_condition_id(market.condition_id),
            start=_utc(market.t0) - timedelta(seconds=lookback_seconds),
            end=_utc(market.t0) + timedelta(seconds=entry_end_seconds),
        )
        for market in markets
    )
    if not windows:
        raise ValueError("at least one market is required")
    destination.mkdir(parents=True, exist_ok=True)
    grouped = _windows_by_hour(windows)
    limit = _safe_memory_limit(memory_limit)
    jobs = tuple(sorted(grouped.items()))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pmxt-btc") as executor:
        hour_receipts = list(
            executor.map(
                lambda job: _extract_hour(
                    hour=job[0],
                    selected=job[1],
                    destination=destination,
                    archive_base_url=archive_base_url,
                    memory_limit=limit,
                ),
                jobs,
            )
        )
    summary = {
        "schema_version": "btc-pmxt-filter-inventory-v1",
        "archive_base_url": archive_base_url.rstrip("/"),
        "market_count": len(windows),
        "hour_count": len(hour_receipts),
        "row_count": sum(int(item["row_count"]) for item in hour_receipts),
        "lookback_seconds": lookback_seconds,
        "entry_end_seconds": entry_end_seconds,
        "workers": workers,
        "hours": hour_receipts,
    }
    write_atomic_json(destination / "btc_pmxt_inventory.json", summary)
    return summary


def _extract_hour(
    *,
    hour: datetime,
    selected: Sequence[PmxtMarketWindow],
    destination: Path,
    archive_base_url: str,
    memory_limit: str,
) -> dict[str, object]:
    target = _hour_path(destination, hour)
    receipt_path = target.with_suffix(".manifest.json")
    expected_windows = [
        {
            "condition_id": item.condition_id,
            "start": item.start.isoformat(),
            "end": item.end.isoformat(),
        }
        for item in selected
    ]
    existing = _verified_existing_hour(
        target,
        receipt_path,
        expected_windows=expected_windows,
    )
    if existing is not None:
        return existing
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    url = f"{archive_base_url.rstrip('/')}/" + _ARCHIVE_FILE.format(
        hour=hour.strftime("%Y-%m-%dT%H")
    )
    condition_values = ",".join("encode(" + _sql_text(item.condition_id) + ")" for item in selected)
    query = (
        "SELECT * FROM read_parquet("
        + _sql_text(url)
        + ") WHERE market IN ("
        + condition_values
        + ") AND timestamp_received>="
        + _sql_timestamp(min(item.start for item in selected))
        + " AND timestamp_received<="
        + _sql_timestamp(max(item.end for item in selected))
    )
    try:
        for attempt in range(5):
            temporary.unlink(missing_ok=True)
            connection = duckdb.connect()
            try:
                connection.execute(f"SET memory_limit='{memory_limit}'")
                connection.execute("SET threads=1")
                connection.execute(
                    "COPY ("
                    + query
                    + ") TO "
                    + _sql_text(str(temporary))
                    + " (FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                break
            except duckdb.IOException:
                if attempt == 4:
                    raise
                time.sleep(2**attempt)
            finally:
                connection.close()
        if not temporary.is_file():
            raise RuntimeError(f"PMXT extraction did not produce an hour file: {hour.isoformat()}")
        metadata = pq.ParquetFile(temporary).metadata
        row_count = int(metadata.num_rows)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    receipt = {
        "schema_version": "btc-pmxt-filter-v1",
        "hour": hour.isoformat(),
        "source_url": url,
        "path": str(target),
        "sha256": sha256_file(target),
        "row_count": row_count,
        "conditions": [item.condition_id for item in selected],
        "windows": expected_windows,
    }
    write_atomic_json(receipt_path, receipt)
    return receipt


def reconstruct_pmxt_decision_books(
    *,
    raw_root: Path,
    market: MarketWindow,
    decision_ts_ns: Sequence[int],
) -> tuple[PmxtDecisionBook, ...]:
    """Replay filtered fixed-schema PMXT rows in receive order to causal BBOs."""

    decisions = tuple(sorted(int(value) for value in decision_ts_ns))
    if not decisions:
        return ()
    start = datetime.fromtimestamp(decisions[0] / 1_000_000_000, tz=UTC) - timedelta(seconds=90)
    end = datetime.fromtimestamp(decisions[-1] / 1_000_000_000, tz=UTC)
    paths = tuple(
        path for hour in _hours(start, end) if (path := _hour_path(raw_root, hour)).is_file()
    )
    if len(paths) != len(_hours(start, end)):
        return ()
    condition = _condition_id(market.condition_id)
    path_sql = "[" + ",".join(_sql_text(str(path)) for path in paths) + "]"
    connection = duckdb.connect()
    try:
        connection.execute("SET threads=1")
        rows = connection.execute(
            "SELECT timestamp_received, timestamp, event_type, asset_id, bids, asks, "
            "price, size, side, best_bid, best_ask FROM read_parquet(" + path_sql + ") "
            "WHERE market=encode(" + _sql_text(condition) + ") "
            "ORDER BY timestamp_received, timestamp, asset_id, event_type"
        ).fetchall()
    finally:
        connection.close()
    books: dict[str, dict[str, dict[Decimal, Decimal]]] = defaultdict(
        lambda: {"bids": {}, "asks": {}}
    )
    available_by_token: dict[str, int] = {}
    best_by_token: dict[str, tuple[Decimal, Decimal]] = {}
    output: list[PmxtDecisionBook] = []
    row_index = 0
    for decision in decisions:
        while row_index < len(rows) and _timestamp_ns(rows[row_index][0]) <= decision:
            _apply_pmxt_row(
                row=rows[row_index],
                books=books,
                available_by_token=available_by_token,
                best_by_token=best_by_token,
            )
            row_index += 1
        up = _top(books.get(market.up_token_id), best_by_token.get(market.up_token_id))
        down = _top(books.get(market.down_token_id), best_by_token.get(market.down_token_id))
        if up is None or down is None:
            continue
        available = min(
            available_by_token.get(market.up_token_id, 0),
            available_by_token.get(market.down_token_id, 0),
        )
        if available <= 0:
            continue
        up_mid = (up.bid + up.ask) / 2.0
        down_mid = (down.bid + down.ask) / 2.0
        output.append(
            PmxtDecisionBook(
                decision_ts_ns=decision,
                market_p_up=(up_mid + 1.0 - down_mid) / 2.0,
                data_age_seconds=max(0.0, (decision - available) / 1_000_000_000),
                up=up,
                down=down,
            )
        )
    return tuple(output)


def _apply_pmxt_row(*, row, books, available_by_token, best_by_token) -> None:  # type: ignore[no-untyped-def]
    received, _source, event_type, asset_id, bids, asks, price, size, side, best_bid, best_ask = row
    token = str(asset_id or "")
    if not token:
        return
    book = books[token]
    if event_type == "book":
        book["bids"] = _levels(bids)
        book["asks"] = _levels(asks)
        if book["bids"] and book["asks"]:
            best_by_token[token] = (max(book["bids"]), min(book["asks"]))
    elif event_type == "price_change" and price is not None and size is not None:
        side_name = "bids" if str(side).upper() == "BUY" else "asks"
        level_price, level_size = Decimal(str(price)), Decimal(str(size))
        if level_size == 0:
            book[side_name].pop(level_price, None)
        else:
            book[side_name][level_price] = level_size
        if best_bid is None or best_ask is None:
            return
        best_by_token[token] = (Decimal(str(best_bid)), Decimal(str(best_ask)))
    else:
        return
    available_by_token[token] = _timestamp_ns(received)


def _levels(value: object) -> dict[Decimal, Decimal]:
    if value is None:
        return {}
    payload = json.loads(str(value))
    result: dict[Decimal, Decimal] = {}
    for item in payload:
        if isinstance(item, Mapping):
            price, size = item.get("price"), item.get("size")
        else:
            price, size = item[0], item[1]
        quantity = Decimal(str(size))
        if quantity > 0:
            result[Decimal(str(price))] = quantity
    return result


def _top(
    book: Mapping[str, Mapping[Decimal, Decimal]] | None,
    best: tuple[Decimal, Decimal] | None,
) -> DualTokenBookSnapshot | None:
    if not book or best is None:
        return None
    bid, ask = best
    if bid > ask or bid not in book["bids"] or ask not in book["asks"]:
        return None
    return DualTokenBookSnapshot(
        bid=float(bid),
        ask=float(ask),
        bid_size=float(book["bids"][bid]),
        ask_size=float(book["asks"][ask]),
    )


def _windows_by_hour(
    windows: Sequence[PmxtMarketWindow],
) -> dict[datetime, tuple[PmxtMarketWindow, ...]]:
    grouped: dict[datetime, list[PmxtMarketWindow]] = defaultdict(list)
    for window in windows:
        for hour in _hours(window.start, window.end):
            grouped[hour].append(window)
    return {hour: tuple(values) for hour, values in grouped.items()}


def _hours(start: datetime, end: datetime) -> tuple[datetime, ...]:
    cursor = _utc(start).replace(minute=0, second=0, microsecond=0)
    last = _utc(end).replace(minute=0, second=0, microsecond=0)
    values: list[datetime] = []
    while cursor <= last:
        values.append(cursor)
        cursor += timedelta(hours=1)
    return tuple(values)


def _hour_path(root: Path, hour: datetime) -> Path:
    filename = _ARCHIVE_FILE.format(hour=hour.strftime("%Y-%m-%dT%H"))
    return root / hour.strftime("%Y") / hour.strftime("%m") / hour.strftime("%d") / filename


def _sql_timestamp(value: datetime) -> str:
    return "TIMESTAMPTZ " + _sql_text(_utc(value).isoformat())


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _condition_id(value: str) -> str:
    if not _CONDITION_RE.fullmatch(value):
        raise ValueError(f"invalid condition ID: {value!r}")
    return value.lower()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_ns(value: datetime) -> int:
    return int(_utc(value).timestamp() * 1_000_000_000)


def _safe_memory_limit(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*(?:MB|GB)", value):
        raise ValueError("memory_limit must look like 512MB or 1GB")
    return value


def _verified_existing_hour(
    path: Path,
    manifest_path: Path,
    *,
    expected_windows: Sequence[Mapping[str, str]],
) -> dict[str, object] | None:
    if not path.exists() and not manifest_path.exists():
        return None
    if not path.is_file() or not manifest_path.is_file():
        raise ValueError(f"incomplete PMXT filtered hour: {path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "btc-pmxt-filter-v1":
        raise ValueError(f"unsupported PMXT filtered manifest: {manifest_path}")
    if payload.get("windows") != list(expected_windows):
        raise ValueError(f"existing PMXT filtered hour has a different market window set: {path}")
    if payload.get("sha256") != sha256_file(path):
        raise ValueError(f"PMXT filtered hour checksum mismatch: {path}")
    if int(payload.get("row_count", -1)) != pq.ParquetFile(path).metadata.num_rows:
        raise ValueError(f"PMXT filtered hour row count mismatch: {path}")
    return payload


__all__ = [
    "PmxtDecisionBook",
    "extract_btc_pmxt_history",
    "reconstruct_pmxt_decision_books",
]
