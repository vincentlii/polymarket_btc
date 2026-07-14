"""Parquet interchange for Opening Mispricing replay signals."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from btc_short_horizon.backtest.signals import (
    BtcOpeningMispricingSignal,
    validate_opening_mispricing_signal,
)


_FIELDS = (
    "market_slug",
    "model_version",
    "feature_schema_hash",
    "market_window_start_ts_ns",
    "p_up",
    "p_boundary_up",
    "p_market_mid_up",
    "data_age_seconds",
    "has_data_gap",
    "structure_valid",
    "tick_unchanged",
    "fee_unchanged",
    "latency_healthy",
    "ts_event",
    "ts_init",
)


def write_opening_mispricing_signals(
    path: Path, signals: Sequence[BtcOpeningMispricingSignal]
) -> None:
    ordered = tuple(sorted(signals, key=_signal_sort_key))
    for signal in ordered:
        validate_opening_mispricing_signal(signal)
    table = pa.Table.from_pylist([_signal_record(signal) for signal in ordered])
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")


def read_opening_mispricing_signals(
    path: Path, *, market_slug: str | None = None
) -> tuple[BtcOpeningMispricingSignal, ...]:
    records = pq.read_table(path).to_pylist()
    signals: list[BtcOpeningMispricingSignal] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("signal parquet records must be objects")
        signal = _signal_from_record(record)
        if market_slug is None or signal.market_slug == market_slug:
            signals.append(signal)
    ordered = tuple(sorted(signals, key=_signal_sort_key))
    if len({(item.market_slug, item.ts_init) for item in ordered}) != len(ordered):
        raise ValueError("signal parquet must not contain duplicate market/timestamp rows")
    return ordered


def _signal_record(signal: BtcOpeningMispricingSignal) -> dict[str, object]:
    return {field: getattr(signal, field) for field in _FIELDS}


def _signal_from_record(record: dict[str, object]) -> BtcOpeningMispricingSignal:
    missing = [field for field in _FIELDS if field not in record]
    if missing:
        raise ValueError(f"signal parquet is missing required fields: {missing}")
    signal = BtcOpeningMispricingSignal(
        market_slug=str(record["market_slug"]),
        model_version=str(record["model_version"]),
        feature_schema_hash=str(record["feature_schema_hash"]),
        market_window_start_ts_ns=int(record["market_window_start_ts_ns"]),
        p_up=float(record["p_up"]),
        p_boundary_up=float(record["p_boundary_up"]),
        p_market_mid_up=float(record["p_market_mid_up"]),
        data_age_seconds=float(record["data_age_seconds"]),
        has_data_gap=bool(record["has_data_gap"]),
        structure_valid=bool(record["structure_valid"]),
        tick_unchanged=bool(record["tick_unchanged"]),
        fee_unchanged=bool(record["fee_unchanged"]),
        latency_healthy=bool(record["latency_healthy"]),
        ts_event=int(record["ts_event"]),
        ts_init=int(record["ts_init"]),
    )
    validate_opening_mispricing_signal(signal)
    return signal


def _signal_sort_key(signal: BtcOpeningMispricingSignal) -> tuple[int, int, str, str, str]:
    return (
        int(signal.ts_init),
        int(signal.ts_event),
        signal.market_slug,
        signal.model_version,
        signal.feature_schema_hash,
    )
