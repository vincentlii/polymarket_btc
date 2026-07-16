"""Compact event-time-only Binance kline archive access for proxy research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Sequence
import hashlib
import zipfile

import httpx
import numpy as np
import pandas as pd


_KLINE_COLUMNS = (0, 4, 5, 7, 9)
_INTERVAL_SECONDS = {"1s": 1, "1m": 60}
_BINANCE_SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
_MAX_BOOTSTRAP_BARS = 4_000
_PAGE_SIZE = 1_000


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
    return (
        "https://data.binance.vision/data/spot/daily/klines/"
        f"{symbol}/{interval}/{symbol}-{interval}-{day}.zip"
    )


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
    "BinanceKlineHistory",
    "binance_spot_kline_url",
    "fetch_binance_spot_kline_history",
    "load_binance_kline_archives",
]
