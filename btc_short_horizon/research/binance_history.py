"""Compact event-time-only Binance kline archive access for proxy research."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
import hashlib
import zipfile

import numpy as np
import pandas as pd


_KLINE_COLUMNS = (0, 4, 5, 7, 9)
_INTERVAL_SECONDS = {"1s": 1, "1m": 60}


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


def _epoch_to_ns(values: np.ndarray) -> np.ndarray:
    maximum = int(np.max(values))
    if maximum >= 1_000_000_000_000_000:
        return values * 1_000
    if maximum >= 1_000_000_000_000:
        return values * 1_000_000
    raise ValueError("Binance kline open timestamps must be milliseconds or microseconds")


__all__ = ["BinanceKlineHistory", "binance_spot_kline_url", "load_binance_kline_archives"]
