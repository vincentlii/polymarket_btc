"""Causal market-price evidence shared by forward collection and PMXT research."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from btc_short_horizon.data import MarketWindow
from btc_short_horizon.data.polymarket import PolymarketL2Normalizer, PolymarketL2Status
from btc_short_horizon.data.session_inventory import (
    SESSION_INVENTORY_MANIFEST_ATTRIBUTE,
    SESSION_INVENTORY_SCHEMA_VERSION,
    SessionInventoryError,
    SessionInventoryRepository,
)
from btc_short_horizon.data.storage import DataPartitionManifest, instrument_directory_name
from btc_short_horizon.data.storage import sha256_file as _inventory_sha256_file
from btc_short_horizon.features.events import BtcBookTop

if TYPE_CHECKING:
    from nautilus_trader.model.data import OrderBookDeltas


_NANOS_PER_SECOND = 1_000_000_000
_POLYMARKET_SOURCE_REGRESSION_ATTRIBUTE = "polymarket_source_timestamp_regression_tolerance_seconds"
_POLYMARKET_SOURCE_REGRESSION_REQUIRED_INGEST_VERSIONS = {
    "btc-short-horizon-v8",
    "btc-short-horizon-v9",
    "btc-short-horizon-v10",
    "btc-short-horizon-v11",
    "btc-short-horizon-v12",
    "btc-short-horizon-v13",
    "btc-short-horizon-v14",
    "btc-short-horizon-v15",
    "btc-short-horizon-v16",
    "btc-short-horizon-v17",
}
_RAW_SCAN_BATCH_SIZE = 64
_DEFAULT_DUCKDB_MEMORY_LIMIT = "64MB"
_MIN_DUCKDB_MEMORY_MB = 32
_MAX_DUCKDB_MEMORY_MB = 256
_RAW_COLUMNS = (
    "source_ts_ns",
    "collector_receive_ts_ns",
    "available_ts_ns",
    "sequence_or_hash",
    "source",
    "instrument",
    "schema_version",
    "ingest_version",
    "event_type",
    "collector_session_id",
    "epoch_id",
    "admission_sequence",
    "payload_json",
)


class RawPayloadError(ValueError):
    """Raised when an immutable raw part cannot safely reproduce its source payload."""


@dataclass(frozen=True, slots=True)
class TokenBookStateEvent:
    """One causal state transition for a token's reconstructed L2 book."""

    token_id: str
    source_ts_ns: int
    available_ts_ns: int
    epoch_id: int
    collector_session_id: str
    admission_sequence: int = 0
    collector_receive_ts_ns: int | None = None
    book: BtcBookTop | None = None
    reset_book: bool = False
    tick_size_changed: bool = False

    def __post_init__(self) -> None:
        if not self.token_id:
            raise ValueError("token_id is required")
        if not self.collector_session_id:
            raise ValueError("collector_session_id is required")
        if self.source_ts_ns < 0 or self.available_ts_ns < 0:
            raise ValueError("book state timestamps must be non-negative")
        if self.available_ts_ns < self.source_ts_ns:
            raise ValueError("available_ts_ns cannot precede source_ts_ns")
        if self.collector_receive_ts_ns is not None and self.collector_receive_ts_ns < 0:
            raise ValueError("collector_receive_ts_ns must be non-negative when provided")
        if self.epoch_id < 0:
            raise ValueError("epoch_id must be non-negative")
        if self.admission_sequence < 0:
            raise ValueError("admission_sequence must be non-negative")
        if self.book is not None:
            if self.book.instrument != self.token_id:
                raise ValueError("book instrument must equal token_id")
            if (
                self.book.source_ts_ns != self.source_ts_ns
                or self.book.available_ts_ns != self.available_ts_ns
            ):
                raise ValueError("book timestamps must equal its state-event timestamps")


@dataclass(frozen=True, slots=True)
class ForwardBookEventLoad:
    """Forward raw-data load result, including evidence that a snapshot was unavailable."""

    token_id: str
    events: tuple[TokenBookStateEvent, ...]
    raw_part_count: int
    raw_row_count: int
    duplicate_row_count: int
    awaiting_snapshot_count: int
    polymarket_source_timestamp_regression_tolerance_seconds: float | None = None
    state_event_count: int | None = None


@dataclass(frozen=True, slots=True)
class PmxtBookEventLoad:
    """PMXT L2 reconstruction result for one token without inventing FIFO data."""

    token_id: str
    events: tuple[TokenBookStateEvent, ...]
    source_book_event_count: int
    gap_hour_count: int


@dataclass(frozen=True, slots=True)
class OpeningMarketObservation:
    """Observed dual-token market probability at one causally valid decision time."""

    market_slug: str
    decision_ts_ns: int
    p_market_mid_up: float
    data_age_seconds: float
    up_available_ts_ns: int
    down_available_ts_ns: int
    up_epoch_id: int
    down_epoch_id: int
    has_data_gap: bool
    structure_valid: bool
    tick_unchanged: bool

    def __post_init__(self) -> None:
        if not self.market_slug:
            raise ValueError("market_slug is required")
        if self.decision_ts_ns < 0:
            raise ValueError("decision_ts_ns must be non-negative")
        if not isfinite(self.p_market_mid_up) or not 0.0 < self.p_market_mid_up < 1.0:
            raise ValueError("p_market_mid_up must be finite and in (0, 1)")
        if not isfinite(self.data_age_seconds) or self.data_age_seconds < 0.0:
            raise ValueError("data_age_seconds must be finite and >= 0")
        if min(self.up_available_ts_ns, self.down_available_ts_ns) < 0:
            raise ValueError("book availability timestamps must be non-negative")
        if min(self.up_epoch_id, self.down_epoch_id) < 0:
            raise ValueError("book epochs must be non-negative")


@dataclass(frozen=True, slots=True)
class ForwardRawEvent:
    """One validated, causally ordered raw event from the immutable forward store."""

    path: Path
    row_index: int
    source_ts_ns: int
    collector_receive_ts_ns: int | None
    available_ts_ns: int
    sequence_or_hash: str
    source: str
    instrument: str
    schema_version: str
    ingest_version: str
    event_type: str
    collector_session_id: str
    epoch_id: int
    admission_sequence: int
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ForwardRawEventLoad:
    """Strict raw-event load result before a source-specific normalizer is applied."""

    source: str
    instrument: str
    ingest_version: str | None
    events: tuple[ForwardRawEvent, ...]
    raw_part_count: int
    raw_row_count: int
    polymarket_source_timestamp_regression_tolerance_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ForwardRawEventTypeSummary:
    """Bounded metadata accumulated for one raw event type."""

    event_type: str
    row_count: int
    min_available_ts_ns: int
    max_available_ts_ns: int


@dataclass(frozen=True, slots=True)
class ForwardRawEventScan:
    """Validated raw-stream metadata without retaining event payloads."""

    source: str
    instrument: str
    ingest_version: str | None
    raw_part_count: int
    raw_row_count: int
    event_types: tuple[ForwardRawEventTypeSummary, ...]

    def event_type(self, name: str) -> ForwardRawEventTypeSummary | None:
        return next((item for item in self.event_types if item.event_type == name), None)


@dataclass(frozen=True, slots=True)
class ForwardRawEventStream:
    """Verified bounded raw iterator produced by a bounded external sort."""

    source: str
    instrument: str
    ingest_version: str | None
    raw_part_count: int
    events: Iterator[ForwardRawEvent]
    polymarket_source_timestamp_regression_tolerance_seconds: float | None = None


def stream_forward_raw_events(
    *,
    raw_data_root: Path,
    source: str,
    instrument: str,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str | None = None,
    expected_polymarket_source_timestamp_regression_tolerance_seconds: float | None = None,
) -> ForwardRawEventStream:
    """Stream a verified raw source when its persisted order proves the causal sort order."""

    if not source or not instrument:
        raise ValueError("source and instrument are required")
    start_ns, end_ns = _window_ns(start_time=start_time, end_time=end_time)
    expected_ingest_version = ingest_version.strip() if ingest_version is not None else None
    if expected_ingest_version == "":
        raise ValueError("ingest_version must be non-empty when provided")
    if expected_polymarket_source_timestamp_regression_tolerance_seconds is not None and (
        not isfinite(expected_polymarket_source_timestamp_regression_tolerance_seconds)
        or expected_polymarket_source_timestamp_regression_tolerance_seconds < 0.0
    ):
        raise ValueError(
            "expected Polymarket source timestamp regression tolerance must be finite and >= 0"
        )
    parts: list[tuple[Path, DataPartitionManifest]] = []
    selected_ingest_versions: set[str] = set()
    tolerances: set[float] = set()
    for path, manifest in _raw_manifest_parts(
        raw_data_root=raw_data_root,
        source=source,
        instrument=instrument,
        start_time=start_time,
        end_time=end_time,
    ):
        if (
            expected_ingest_version is not None
            and manifest.ingest_version != expected_ingest_version
        ):
            continue
        selected_ingest_versions.add(manifest.ingest_version)
        tolerance_text = manifest.attributes.get(_POLYMARKET_SOURCE_REGRESSION_ATTRIBUTE)
        if (
            source == "polymarket_clob"
            and manifest.ingest_version in _POLYMARKET_SOURCE_REGRESSION_REQUIRED_INGEST_VERSIONS
            and tolerance_text is None
        ):
            raise RawPayloadError(f"raw manifest has no Polymarket timestamp tolerance: {path}")
        if tolerance_text is not None:
            try:
                tolerance = float(tolerance_text)
            except ValueError as exc:
                raise RawPayloadError(
                    f"raw manifest has an invalid Polymarket timestamp tolerance: {path}"
                ) from exc
            if not isfinite(tolerance) or tolerance < 0.0:
                raise RawPayloadError(
                    f"raw manifest has an invalid Polymarket timestamp tolerance: {path}"
                )
            tolerances.add(tolerance)
        parts.append((path, manifest))
    if len(selected_ingest_versions) > 1:
        raise RawPayloadError("raw manifests mix ingest versions")
    if len(tolerances) > 1:
        raise RawPayloadError("raw manifests mix Polymarket source timestamp regression tolerances")
    resolved_tolerance = next(iter(tolerances), None)
    if (
        expected_polymarket_source_timestamp_regression_tolerance_seconds is not None
        and resolved_tolerance is not None
        and resolved_tolerance != expected_polymarket_source_timestamp_regression_tolerance_seconds
    ):
        raise RawPayloadError(
            "raw Polymarket source timestamp regression tolerance "
            "does not match the expected collector configuration"
        )
    ordered_parts = tuple(
        sorted(
            parts,
            key=lambda item: (
                item[1].min_available_ts_ns,
                item[1].created_at,
                item[1].data_path,
            ),
        )
    )
    return ForwardRawEventStream(
        source=source,
        instrument=instrument,
        ingest_version=(
            expected_ingest_version
            if expected_ingest_version is not None
            else next(iter(selected_ingest_versions), None)
        ),
        raw_part_count=len(ordered_parts),
        events=_stream_verified_raw_rows(
            rows=_duckdb_sorted_raw_rows(
                parts=ordered_parts,
                start_ns=start_ns,
                end_ns=end_ns,
            ),
            source=source,
            instrument=instrument,
            expected_ingest_version=expected_ingest_version,
        ),
        polymarket_source_timestamp_regression_tolerance_seconds=resolved_tolerance,
    )


def scan_forward_raw_event_metadata(
    *,
    raw_data_root: Path,
    source: str,
    instrument: str,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str | None = None,
) -> ForwardRawEventScan:
    """Validate one raw stream while retaining only fixed-size event-type metadata."""

    if not source or not instrument:
        raise ValueError("source and instrument are required")
    start_ns, end_ns = _window_ns(start_time=start_time, end_time=end_time)
    expected_ingest_version = ingest_version.strip() if ingest_version is not None else None
    if expected_ingest_version == "":
        raise ValueError("ingest_version must be non-empty when provided")
    raw_part_count = 0
    raw_row_count = 0
    selected_ingest_versions: set[str] = set()
    polymarket_regression_tolerances: set[float] = set()
    event_types: dict[str, list[int]] = {}
    seen_admission_sequences: set[tuple[str, int]] = set()
    parts = sorted(
        _raw_manifest_parts(
            raw_data_root=raw_data_root,
            source=source,
            instrument=instrument,
            start_time=start_time,
            end_time=end_time,
        ),
        key=lambda item: (item[1].min_available_ts_ns, item[1].created_at, item[1].data_path),
    )
    for path, manifest in parts:
        if (
            expected_ingest_version is not None
            and manifest.ingest_version != expected_ingest_version
        ):
            continue
        selected_ingest_versions.add(manifest.ingest_version)
        tolerance_text = manifest.attributes.get(_POLYMARKET_SOURCE_REGRESSION_ATTRIBUTE)
        if (
            source == "polymarket_clob"
            and manifest.ingest_version in _POLYMARKET_SOURCE_REGRESSION_REQUIRED_INGEST_VERSIONS
            and tolerance_text is None
        ):
            raise RawPayloadError(f"raw manifest has no Polymarket timestamp tolerance: {path}")
        if tolerance_text is not None:
            try:
                tolerance = float(tolerance_text)
            except ValueError as exc:
                raise RawPayloadError(
                    f"raw manifest has an invalid Polymarket timestamp tolerance: {path}"
                ) from exc
            if not isfinite(tolerance) or tolerance < 0.0:
                raise RawPayloadError(
                    f"raw manifest has an invalid Polymarket timestamp tolerance: {path}"
                )
            polymarket_regression_tolerances.add(tolerance)
        raw_part_count += 1
        for row_index, row in _raw_rows(path, manifest=manifest):
            available_ts_ns = _required_int(row, "available_ts_ns", path, row_index)
            if not start_ns <= available_ts_ns <= end_ns:
                continue
            _validate_raw_identity(
                row,
                expected_source=source,
                expected_instrument=instrument,
                path=path,
                row_index=row_index,
            )
            row_ingest_version = _required_text(row, "ingest_version", path, row_index)
            if (
                expected_ingest_version is not None
                and row_ingest_version != expected_ingest_version
            ):
                continue
            _payload_mapping(row, path=path, row_index=row_index)
            session_id = _required_text(row, "collector_session_id", path, row_index)
            admission_sequence = _required_int(row, "admission_sequence", path, row_index)
            if row_ingest_version in _POLYMARKET_SOURCE_REGRESSION_REQUIRED_INGEST_VERSIONS:
                admission_identity = (session_id, admission_sequence)
                if admission_identity in seen_admission_sequences:
                    raise RawPayloadError(
                        "raw events repeat a collector-session admission_sequence "
                        f"at {path}:{row_index}"
                    )
                seen_admission_sequences.add(admission_identity)
            event_type = _required_text(row, "event_type", path, row_index)
            summary = event_types.setdefault(
                event_type,
                [0, available_ts_ns, available_ts_ns],
            )
            summary[0] += 1
            summary[1] = min(summary[1], available_ts_ns)
            summary[2] = max(summary[2], available_ts_ns)
            raw_row_count += 1
    if len(selected_ingest_versions) > 1:
        raise RawPayloadError("raw manifests mix ingest versions")
    if len(polymarket_regression_tolerances) > 1:
        raise RawPayloadError("raw manifests mix Polymarket source timestamp regression tolerances")
    resolved_ingest_version = (
        expected_ingest_version
        if expected_ingest_version is not None
        else next(iter(selected_ingest_versions), None)
    )
    return ForwardRawEventScan(
        source=source,
        instrument=instrument,
        ingest_version=resolved_ingest_version,
        raw_part_count=raw_part_count,
        raw_row_count=raw_row_count,
        event_types=tuple(
            ForwardRawEventTypeSummary(
                event_type=name,
                row_count=values[0],
                min_available_ts_ns=values[1],
                max_available_ts_ns=values[2],
            )
            for name, values in sorted(event_types.items())
        ),
    )


def load_forward_raw_events(
    *,
    raw_data_root: Path,
    source: str,
    instrument: str,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str | None = None,
    expected_polymarket_source_timestamp_regression_tolerance_seconds: float | None = None,
) -> ForwardRawEventLoad:
    """Load one forward stream without repairing malformed rows or mixing versions."""

    if not source or not instrument:
        raise ValueError("source and instrument are required")
    start_ns, end_ns = _window_ns(start_time=start_time, end_time=end_time)
    if ingest_version is not None and not ingest_version.strip():
        raise ValueError("ingest_version must be non-empty when provided")
    expected_ingest_version = ingest_version.strip() if ingest_version is not None else None
    if expected_polymarket_source_timestamp_regression_tolerance_seconds is not None and (
        not isfinite(expected_polymarket_source_timestamp_regression_tolerance_seconds)
        or expected_polymarket_source_timestamp_regression_tolerance_seconds < 0.0
    ):
        raise ValueError(
            "expected Polymarket source timestamp regression tolerance must be finite and >= 0"
        )
    raw_part_count = 0
    raw_row_count = 0
    events: list[ForwardRawEvent] = []
    selected_ingest_versions: set[str] = set()
    polymarket_regression_tolerances: set[float] = set()
    for path, manifest in _raw_manifest_parts(
        raw_data_root=raw_data_root,
        source=source,
        instrument=instrument,
        start_time=start_time,
        end_time=end_time,
    ):
        if (
            expected_ingest_version is not None
            and manifest.ingest_version != expected_ingest_version
        ):
            continue
        selected_ingest_versions.add(manifest.ingest_version)
        tolerance_text = manifest.attributes.get(_POLYMARKET_SOURCE_REGRESSION_ATTRIBUTE)
        if (
            source == "polymarket_clob"
            and manifest.ingest_version in _POLYMARKET_SOURCE_REGRESSION_REQUIRED_INGEST_VERSIONS
            and tolerance_text is None
        ):
            raise RawPayloadError(f"raw manifest has no Polymarket timestamp tolerance: {path}")
        if tolerance_text is not None:
            try:
                tolerance = float(tolerance_text)
            except ValueError as exc:
                raise RawPayloadError(
                    f"raw manifest has an invalid Polymarket timestamp tolerance: {path}"
                ) from exc
            if not isfinite(tolerance) or tolerance < 0.0:
                raise RawPayloadError(
                    f"raw manifest has an invalid Polymarket timestamp tolerance: {path}"
                )
            polymarket_regression_tolerances.add(tolerance)
        raw_part_count += 1
        for row_index, row in _raw_rows(path, manifest=manifest):
            available_ts_ns = _required_int(row, "available_ts_ns", path, row_index)
            if not start_ns <= available_ts_ns <= end_ns:
                continue
            _validate_raw_identity(
                row,
                expected_source=source,
                expected_instrument=instrument,
                path=path,
                row_index=row_index,
            )
            row_ingest_version = _required_text(row, "ingest_version", path, row_index)
            if (
                expected_ingest_version is not None
                and row_ingest_version != expected_ingest_version
            ):
                continue
            raw_row_count += 1
            events.append(
                ForwardRawEvent(
                    path=path,
                    row_index=row_index,
                    source_ts_ns=_required_int(row, "source_ts_ns", path, row_index),
                    collector_receive_ts_ns=_optional_int(
                        row, "collector_receive_ts_ns", path, row_index
                    ),
                    available_ts_ns=available_ts_ns,
                    sequence_or_hash=_required_text(row, "sequence_or_hash", path, row_index),
                    source=source,
                    instrument=instrument,
                    schema_version=_required_text(row, "schema_version", path, row_index),
                    ingest_version=row_ingest_version,
                    event_type=_required_text(row, "event_type", path, row_index),
                    collector_session_id=_required_text(
                        row,
                        "collector_session_id",
                        path,
                        row_index,
                    ),
                    epoch_id=_required_int(row, "epoch_id", path, row_index),
                    admission_sequence=_required_int(
                        row,
                        "admission_sequence",
                        path,
                        row_index,
                    ),
                    payload=_payload_mapping(row, path=path, row_index=row_index),
                )
            )
    if len(polymarket_regression_tolerances) > 1:
        raise RawPayloadError("raw manifests mix Polymarket source timestamp regression tolerances")
    if len(selected_ingest_versions) > 1:
        raise RawPayloadError("raw manifests mix ingest versions")
    resolved_ingest_version = (
        expected_ingest_version
        if expected_ingest_version is not None
        else next(iter(selected_ingest_versions), None)
    )
    resolved_tolerance = (
        next(iter(polymarket_regression_tolerances)) if polymarket_regression_tolerances else None
    )
    if (
        expected_polymarket_source_timestamp_regression_tolerance_seconds is not None
        and resolved_tolerance is not None
        and resolved_tolerance != expected_polymarket_source_timestamp_regression_tolerance_seconds
    ):
        raise RawPayloadError(
            "raw Polymarket source timestamp regression tolerance "
            "does not match the expected collector configuration"
        )
    if resolved_ingest_version in _POLYMARKET_SOURCE_REGRESSION_REQUIRED_INGEST_VERSIONS:
        seen_admission_sequences: set[tuple[str, int]] = set()
        for event in events:
            key = (event.collector_session_id, event.admission_sequence)
            if key in seen_admission_sequences:
                raise RawPayloadError("raw events repeat a collector-session admission_sequence")
            seen_admission_sequences.add(key)
    return ForwardRawEventLoad(
        source=source,
        instrument=instrument,
        ingest_version=resolved_ingest_version,
        events=tuple(sorted(events, key=_raw_payload_sort_key)),
        raw_part_count=raw_part_count,
        raw_row_count=raw_row_count,
        polymarket_source_timestamp_regression_tolerance_seconds=resolved_tolerance,
    )


def load_forward_polymarket_book_events(
    *,
    raw_data_root: Path,
    token_id: str,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str | None = None,
    expected_source_timestamp_regression_tolerance_seconds: float | None = None,
) -> ForwardBookEventLoad:
    """Rebuild one token's causal L2 state from valid append-only raw events.

    Invalid JSON is an evidence failure, not a recoverable data value: a caller
    must exclude that part rather than attempting lossy repair.
    """

    raw_load = load_forward_raw_events(
        raw_data_root=raw_data_root,
        source="polymarket_clob",
        instrument=token_id,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
        expected_polymarket_source_timestamp_regression_tolerance_seconds=(
            expected_source_timestamp_regression_tolerance_seconds
        ),
    )
    events: list[TokenBookStateEvent] = []
    counts = _fold_polymarket_raw_load(raw_load, token_id=token_id, on_event=events.append)
    return ForwardBookEventLoad(
        token_id=token_id,
        events=tuple(sorted(events, key=_state_event_sort_key)),
        raw_part_count=raw_load.raw_part_count,
        raw_row_count=raw_load.raw_row_count,
        duplicate_row_count=counts.duplicate_row_count,
        awaiting_snapshot_count=counts.awaiting_snapshot_count,
        polymarket_source_timestamp_regression_tolerance_seconds=(
            raw_load.polymarket_source_timestamp_regression_tolerance_seconds
        ),
        state_event_count=counts.state_event_count,
    )


@dataclass(frozen=True, slots=True)
class _PolymarketBookFoldCounts:
    raw_row_count: int
    state_event_count: int
    duplicate_row_count: int
    awaiting_snapshot_count: int


def fold_forward_polymarket_book_events(
    *,
    raw_data_root: Path,
    token_id: str,
    start_time: datetime,
    end_time: datetime,
    on_event: Callable[[TokenBookStateEvent], None],
    ingest_version: str | None = None,
    expected_source_timestamp_regression_tolerance_seconds: float | None = None,
) -> ForwardBookEventLoad:
    """Rebuild one token while emitting state transitions without retaining them."""

    raw_stream = stream_forward_raw_events(
        raw_data_root=raw_data_root,
        source="polymarket_clob",
        instrument=token_id,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
        expected_polymarket_source_timestamp_regression_tolerance_seconds=(
            expected_source_timestamp_regression_tolerance_seconds
        ),
    )
    counts = _fold_polymarket_events(
        raw_stream.events,
        token_id=token_id,
        source_timestamp_regression_tolerance_seconds=(
            raw_stream.polymarket_source_timestamp_regression_tolerance_seconds
        ),
        on_event=on_event,
        # Current forward collection rejects duplicate sequence identities before
        # persistence; the bounded stream independently proves strictly increasing
        # admission_sequence, so an unbounded replay-time sequence set is redundant.
        deduplicate_sequence=False,
    )
    return ForwardBookEventLoad(
        token_id=token_id,
        events=(),
        raw_part_count=raw_stream.raw_part_count,
        raw_row_count=counts.raw_row_count,
        duplicate_row_count=counts.duplicate_row_count,
        awaiting_snapshot_count=counts.awaiting_snapshot_count,
        polymarket_source_timestamp_regression_tolerance_seconds=(
            raw_stream.polymarket_source_timestamp_regression_tolerance_seconds
        ),
        state_event_count=counts.state_event_count,
    )


def _fold_polymarket_raw_load(
    raw_load: ForwardRawEventLoad,
    *,
    token_id: str,
    on_event: Callable[[TokenBookStateEvent], None],
) -> _PolymarketBookFoldCounts:
    return _fold_polymarket_events(
        raw_load.events,
        token_id=token_id,
        source_timestamp_regression_tolerance_seconds=(
            raw_load.polymarket_source_timestamp_regression_tolerance_seconds
        ),
        on_event=on_event,
    )


def _fold_polymarket_events(
    raw_events: Iterable[ForwardRawEvent],
    *,
    token_id: str,
    source_timestamp_regression_tolerance_seconds: float | None,
    on_event: Callable[[TokenBookStateEvent], None],
    deduplicate_sequence: bool = True,
) -> _PolymarketBookFoldCounts:
    normalizer = PolymarketL2Normalizer(
        token_id=token_id,
        source_timestamp_regression_tolerance=timedelta(
            seconds=(
                source_timestamp_regression_tolerance_seconds
                if source_timestamp_regression_tolerance_seconds is not None
                else 1.0
            )
        ),
        in_place_updates=not deduplicate_sequence,
    )
    seen_sequences: set[tuple[str, int, str]] = set()
    completed_epochs: set[tuple[str, int]] = set()
    current_epoch: tuple[str, int] | None = None
    duplicate_row_count = 0
    awaiting_snapshot_count = 0
    state_event_count = 0
    raw_row_count = 0

    def emit(event: TokenBookStateEvent) -> None:
        nonlocal state_event_count
        state_event_count += 1
        on_event(event)

    for raw in raw_events:
        raw_row_count += 1
        if raw.payload.get("event_type") != raw.event_type:
            raise RawPayloadError(f"raw payload event_type mismatch at {raw.path}:{raw.row_index}")
        epoch_identity = (raw.collector_session_id, raw.epoch_id)
        sequence_key = (*epoch_identity, raw.sequence_or_hash)
        if deduplicate_sequence and sequence_key in seen_sequences:
            duplicate_row_count += 1
            continue
        if deduplicate_sequence:
            seen_sequences.add(sequence_key)
        reset_book = current_epoch is not None and epoch_identity != current_epoch
        if reset_book:
            completed_epochs.add(current_epoch)
            if epoch_identity in completed_epochs:
                raise RawPayloadError(
                    "raw epoch reappeared after a newer causal epoch at "
                    f"{raw.path}:{raw.row_index}: {epoch_identity}"
                )
            normalizer.reset()
        current_epoch = epoch_identity
        if raw.event_type == "continuity_gap":
            if raw.payload.get("stream_id") != "market":
                raise RawPayloadError(f"invalid CLOB continuity gap at {raw.path}:{raw.row_index}")
            reason = raw.payload.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise RawPayloadError(f"invalid CLOB continuity gap at {raw.path}:{raw.row_index}")
            normalizer.reset()
            emit(
                TokenBookStateEvent(
                    token_id=token_id,
                    source_ts_ns=raw.source_ts_ns,
                    available_ts_ns=raw.available_ts_ns,
                    epoch_id=raw.epoch_id,
                    collector_session_id=raw.collector_session_id,
                    admission_sequence=raw.admission_sequence,
                    collector_receive_ts_ns=raw.collector_receive_ts_ns,
                    reset_book=True,
                )
            )
            continue
        receive_ts_ns = raw.collector_receive_ts_ns or raw.available_ts_ns
        try:
            result = normalizer.apply(
                raw.payload,
                collector_receive_ts=_datetime_from_ns(receive_ts_ns),
            )
        except ValueError as exc:
            raise RawPayloadError(
                f"invalid CLOB payload at {raw.path}:{raw.row_index}: {exc}"
            ) from exc
        if result.status is PolymarketL2Status.AWAITING_SNAPSHOT:
            awaiting_snapshot_count += 1
        if result.status is PolymarketL2Status.INVALID or (
            result.starts_new_epoch and result.book_top is not None
        ):
            reset_book = True
        if result.book_top is not None:
            top = result.book_top
            emit(
                TokenBookStateEvent(
                    token_id=token_id,
                    source_ts_ns=raw.source_ts_ns,
                    available_ts_ns=raw.available_ts_ns,
                    epoch_id=raw.epoch_id,
                    collector_session_id=raw.collector_session_id,
                    admission_sequence=raw.admission_sequence,
                    collector_receive_ts_ns=raw.collector_receive_ts_ns,
                    book=BtcBookTop(
                        source_ts_ns=raw.source_ts_ns,
                        available_ts_ns=raw.available_ts_ns,
                        bid=top.bid,
                        ask=top.ask,
                        bid_size=top.bid_size,
                        ask_size=top.ask_size,
                        source="polymarket_clob",
                        instrument=token_id,
                    ),
                    reset_book=reset_book,
                    tick_size_changed=result.tick_size_changed,
                )
            )
        elif reset_book or result.tick_size_changed:
            emit(
                TokenBookStateEvent(
                    token_id=token_id,
                    source_ts_ns=raw.source_ts_ns,
                    available_ts_ns=raw.available_ts_ns,
                    epoch_id=raw.epoch_id,
                    collector_session_id=raw.collector_session_id,
                    admission_sequence=raw.admission_sequence,
                    collector_receive_ts_ns=raw.collector_receive_ts_ns,
                    reset_book=reset_book,
                    tick_size_changed=result.tick_size_changed,
                )
            )
    return _PolymarketBookFoldCounts(
        raw_row_count=raw_row_count,
        state_event_count=state_event_count,
        duplicate_row_count=duplicate_row_count,
        awaiting_snapshot_count=awaiting_snapshot_count,
    )


def build_opening_market_observations(
    *,
    market: MarketWindow,
    up_events: Sequence[TokenBookStateEvent],
    down_events: Sequence[TokenBookStateEvent],
    decision_ts_ns: Sequence[int],
    initial_data_gap: bool = False,
) -> tuple[OpeningMarketObservation, ...]:
    """Join two token books only at decision times after both states were available."""

    _validate_token_events(up_events, expected_token_id=market.up_token_id)
    _validate_token_events(down_events, expected_token_id=market.down_token_id)
    decisions = tuple(sorted(set(int(value) for value in decision_ts_ns)))
    start_ns = _datetime_to_ns(market.t0)
    end_ns = _datetime_to_ns(market.t1)
    if any(value < start_ns or value > end_ns for value in decisions):
        raise ValueError("decision timestamps must lie within the market window")

    events = tuple(sorted((*up_events, *down_events), key=_state_event_sort_key))
    latest: dict[str, TokenBookStateEvent | None] = {
        market.up_token_id: None,
        market.down_token_id: None,
    }
    baseline_epochs: dict[str, tuple[str, int] | None] = {
        market.up_token_id: None,
        market.down_token_id: None,
    }
    gap_seen = initial_data_gap
    tick_changed = False
    event_index = 0
    observations: list[OpeningMarketObservation] = []

    for decision in decisions:
        while event_index < len(events) and events[event_index].available_ts_ns <= decision:
            event = events[event_index]
            event_index += 1
            previous = latest[event.token_id]
            if event.reset_book:
                if previous is not None:
                    gap_seen = True
                latest[event.token_id] = None
            if event.book is not None:
                baseline = baseline_epochs[event.token_id]
                epoch_identity = (event.collector_session_id, event.epoch_id)
                if baseline is None:
                    baseline_epochs[event.token_id] = epoch_identity
                elif baseline != epoch_identity:
                    gap_seen = True
                latest[event.token_id] = event
            if event.tick_size_changed:
                tick_changed = True

        up = latest[market.up_token_id]
        down = latest[market.down_token_id]
        if up is None or down is None or up.book is None or down.book is None:
            continue
        up_midpoint = (up.book.bid + up.book.ask) / 2.0
        down_midpoint = (down.book.bid + down.book.ask) / 2.0
        p_market_mid_up = (up_midpoint + 1.0 - down_midpoint) / 2.0
        data_age_seconds = max(
            0.0,
            (decision - min(up.available_ts_ns, down.available_ts_ns)) / _NANOS_PER_SECOND,
        )
        observations.append(
            OpeningMarketObservation(
                market_slug=market.slug,
                decision_ts_ns=decision,
                p_market_mid_up=p_market_mid_up,
                data_age_seconds=data_age_seconds,
                up_available_ts_ns=up.available_ts_ns,
                down_available_ts_ns=down.available_ts_ns,
                up_epoch_id=up.epoch_id,
                down_epoch_id=down.epoch_id,
                has_data_gap=gap_seen,
                structure_valid=True,
                tick_unchanged=not tick_changed,
            )
        )
    return tuple(observations)


def pmxt_order_book_state_events(
    *,
    token_id: str,
    records: Sequence[OrderBookDeltas],
    gap_hours: Sequence[object] = (),
) -> PmxtBookEventLoad:
    """Derive causal L1 observations by replaying PMXT L2 deltas through Nautilus."""

    from nautilus_trader.model.book import OrderBook
    from nautilus_trader.model.enums import BookType

    book: OrderBook | None = None
    events: list[TokenBookStateEvent] = []
    for admission_sequence, record in enumerate(
        sorted(records, key=lambda item: (int(item.ts_init), int(item.ts_event)))
    ):
        if book is None:
            book = OrderBook(record.instrument_id, book_type=BookType.L2_MBP)
        elif record.instrument_id != book.instrument_id:
            raise ValueError("PMXT records must contain one instrument per token")
        source_ts_ns = int(record.ts_event)
        available_ts_ns = int(record.ts_init)
        if available_ts_ns < source_ts_ns:
            raise ValueError("PMXT receive timestamp cannot precede source timestamp")
        book.apply_deltas(record)
        bid = _as_float(book.best_bid_price())
        ask = _as_float(book.best_ask_price())
        bid_size = _as_float(book.best_bid_size())
        ask_size = _as_float(book.best_ask_size())
        if None in {bid, ask, bid_size, ask_size}:
            continue
        assert bid is not None and ask is not None and bid_size is not None and ask_size is not None
        try:
            top = BtcBookTop(
                source_ts_ns=source_ts_ns,
                available_ts_ns=available_ts_ns,
                bid=bid,
                ask=ask,
                bid_size=bid_size,
                ask_size=ask_size,
                source="pmxt",
                instrument=token_id,
            )
        except ValueError:
            continue
        events.append(
            TokenBookStateEvent(
                token_id=token_id,
                source_ts_ns=source_ts_ns,
                available_ts_ns=available_ts_ns,
                epoch_id=0,
                collector_session_id="pmxt",
                admission_sequence=admission_sequence,
                collector_receive_ts_ns=available_ts_ns,
                book=top,
            )
        )
    return PmxtBookEventLoad(
        token_id=token_id,
        events=tuple(sorted(events, key=_state_event_sort_key)),
        source_book_event_count=len(records),
        gap_hour_count=len(tuple(gap_hours)),
    )


def _raw_manifest_parts(
    *,
    raw_data_root: Path,
    source: str,
    instrument: str,
    start_time: datetime,
    end_time: datetime,
) -> Iterator[tuple[Path, DataPartitionManifest]]:
    """Yield verified manifest/part pairs and reject unreferenced parts.

    This proves integrity for every manifest or part still present in the
    selected hourly directories. The inventory query uses those same complete
    physical partitions; the caller applies the narrower logical event window.
    """

    root = raw_data_root.resolve()
    start_hour = _as_utc(start_time, "start_time").replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    end_hour = _as_utc(end_time, "end_time").replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    directory_start_ns = _datetime_to_ns(start_hour)
    directory_end_ns = _datetime_to_ns(end_hour + timedelta(hours=1)) - 1
    repository = SessionInventoryRepository(root)
    try:
        inventoried = {
            item.manifest_path: item
            for item in repository.expected_parts(
                source=source,
                instrument=instrument,
                start_available_ts_ns=directory_start_ns,
                end_available_ts_ns=directory_end_ns,
                ingest_version=None,
            )
        }
    except SessionInventoryError as exc:
        raise RawPayloadError(f"raw session inventory is invalid: {exc}") from exc
    instrument_path = instrument_directory_name(instrument)
    for hour in _hours_between(start_time=start_time, end_time=end_time):
        directory = (
            root / "raw" / source / instrument_path / f"date={hour:%Y-%m-%d}" / f"hour={hour:%H}"
        )
        referenced_parts: set[Path] = set()
        verified: list[tuple[Path, DataPartitionManifest]] = []
        for manifest_path in sorted(directory.glob("manifest-*.json")):
            manifest = _read_raw_manifest(manifest_path)
            relative_manifest_path = manifest_path.relative_to(root).as_posix()
            is_inventory_managed = (
                manifest.attributes.get(SESSION_INVENTORY_MANIFEST_ATTRIBUTE)
                == SESSION_INVENTORY_SCHEMA_VERSION
            )
            inventory_record = inventoried.pop(relative_manifest_path, None)
            if is_inventory_managed and inventory_record is None:
                raise RawPayloadError(
                    f"managed raw manifest is not covered by a session inventory: {manifest_path}"
                )
            if inventory_record is not None and (
                inventory_record.manifest != manifest
                or _inventory_sha256_file(manifest_path) != inventory_record.manifest_sha256
            ):
                raise RawPayloadError(
                    f"raw manifest differs from its session inventory: {manifest_path}"
                )
            part_path = _validate_raw_manifest(
                manifest,
                manifest_path=manifest_path,
                raw_data_root=root,
                expected_source=source,
                expected_instrument=instrument,
            )
            referenced_parts.add(part_path)
            verified.append((part_path, manifest))
        orphan_parts = set(directory.glob("part-*.parquet")) - referenced_parts
        if orphan_parts:
            paths = ", ".join(str(path) for path in sorted(orphan_parts))
            raise RawPayloadError(f"raw parts have no manifest: {paths}")
        yield from verified
    if inventoried:
        missing = ", ".join(sorted(inventoried))
        raise RawPayloadError(f"session inventory references missing raw manifests: {missing}")


def _read_raw_manifest(path: Path) -> DataPartitionManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise RawPayloadError(f"raw manifest must contain an object: {path}")
        return DataPartitionManifest(**payload)
    except RawPayloadError:
        raise
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RawPayloadError(f"cannot read raw manifest {path}: {exc}") from exc


def _validate_raw_manifest(
    manifest: DataPartitionManifest,
    *,
    manifest_path: Path,
    raw_data_root: Path,
    expected_source: str,
    expected_instrument: str,
) -> Path:
    for name in ("source", "instrument", "schema_version", "ingest_version", "data_path"):
        value = getattr(manifest, name)
        if not isinstance(value, str) or not value.strip():
            raise RawPayloadError(f"raw manifest has invalid {name}: {manifest_path}")
    if manifest.source != expected_source or manifest.instrument != expected_instrument:
        raise RawPayloadError(
            "raw manifest identity mismatch at "
            f"{manifest_path}: {manifest.source}/{manifest.instrument}"
        )
    if (
        not isinstance(manifest.sha256, str)
        or len(manifest.sha256) != 64
        or any(character not in "0123456789abcdef" for character in manifest.sha256)
    ):
        raise RawPayloadError(f"raw manifest has invalid sha256: {manifest_path}")
    for name in ("row_count", "duplicate_count", "gap_count"):
        value = getattr(manifest, name)
        minimum = 1 if name == "row_count" else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise RawPayloadError(f"raw manifest has invalid {name}: {manifest_path}")
    timestamps = (
        manifest.min_source_ts_ns,
        manifest.max_source_ts_ns,
        manifest.min_available_ts_ns,
        manifest.max_available_ts_ns,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in timestamps
    ):
        raise RawPayloadError(f"raw manifest has invalid timestamp bounds: {manifest_path}")
    if (
        manifest.min_source_ts_ns > manifest.max_source_ts_ns
        or manifest.min_available_ts_ns > manifest.max_available_ts_ns
    ):
        raise RawPayloadError(f"raw manifest has reversed timestamp bounds: {manifest_path}")
    if not isinstance(manifest.created_at, str) or not manifest.created_at.strip():
        raise RawPayloadError(f"raw manifest has invalid created_at: {manifest_path}")
    if not isinstance(manifest.attributes, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in manifest.attributes.items()
    ):
        raise RawPayloadError(f"raw manifest has invalid attributes: {manifest_path}")
    collector_session_id = manifest.attributes.get("collector_session_id")
    if not collector_session_id or not collector_session_id.strip():
        raise RawPayloadError(
            f"raw manifest has no collector_session_id attribute: {manifest_path}"
        )

    content_id = manifest.sha256[:32]
    if manifest_path.name != f"manifest-{content_id}.json":
        raise RawPayloadError(f"raw manifest filename does not match sha256: {manifest_path}")
    expected_part_path = manifest_path.with_name(f"part-{content_id}.parquet")
    try:
        expected_data_path = expected_part_path.relative_to(raw_data_root).as_posix()
    except ValueError as exc:
        raise RawPayloadError(f"raw manifest is outside the data root: {manifest_path}") from exc
    if manifest.data_path != expected_data_path:
        raise RawPayloadError(f"raw manifest data_path mismatch: {manifest_path}")
    if not expected_part_path.is_file():
        raise RawPayloadError(f"raw manifest references a missing part: {expected_part_path}")
    if _sha256_path(expected_part_path) != manifest.sha256:
        raise RawPayloadError(f"raw part sha256 mismatch: {expected_part_path}")
    _validate_raw_part_statistics(expected_part_path, manifest=manifest)
    return expected_part_path


def _validate_raw_part_statistics(path: Path, *, manifest: DataPartitionManifest) -> None:
    try:
        parquet = pq.ParquetFile(path)
        timestamp_columns = ("source_ts_ns", "available_ts_ns")
        if not set(timestamp_columns).issubset(parquet.schema_arrow.names):
            raise RawPayloadError(f"raw part has no timestamp columns: {path}")
        if parquet.metadata.num_rows != manifest.row_count:
            raise RawPayloadError(f"raw part row_count does not match manifest: {path}")
        min_source_ts_ns: int | None = None
        max_source_ts_ns: int | None = None
        min_available_ts_ns: int | None = None
        max_available_ts_ns: int | None = None
        row_index = 0
        for batch in parquet.iter_batches(
            batch_size=_RAW_SCAN_BATCH_SIZE,
            columns=list(timestamp_columns),
        ):
            for row in batch.to_pylist():
                source_ts_ns = _required_int(row, "source_ts_ns", path, row_index)
                available_ts_ns = _required_int(row, "available_ts_ns", path, row_index)
                min_source_ts_ns = (
                    source_ts_ns
                    if min_source_ts_ns is None
                    else min(min_source_ts_ns, source_ts_ns)
                )
                max_source_ts_ns = (
                    source_ts_ns
                    if max_source_ts_ns is None
                    else max(max_source_ts_ns, source_ts_ns)
                )
                min_available_ts_ns = (
                    available_ts_ns
                    if min_available_ts_ns is None
                    else min(min_available_ts_ns, available_ts_ns)
                )
                max_available_ts_ns = (
                    available_ts_ns
                    if max_available_ts_ns is None
                    else max(max_available_ts_ns, available_ts_ns)
                )
                row_index += 1
        actual_bounds = (
            min_source_ts_ns,
            max_source_ts_ns,
            min_available_ts_ns,
            max_available_ts_ns,
        )
        manifest_bounds = (
            manifest.min_source_ts_ns,
            manifest.max_source_ts_ns,
            manifest.min_available_ts_ns,
            manifest.max_available_ts_ns,
        )
        if actual_bounds != manifest_bounds:
            raise RawPayloadError(f"raw part timestamp bounds do not match manifest: {path}")
    except RawPayloadError:
        raise
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise RawPayloadError(f"cannot verify raw part statistics {path}: {exc}") from exc


def _raw_rows(
    path: Path,
    *,
    manifest: DataPartitionManifest,
) -> Iterator[tuple[int, dict[str, object]]]:
    try:
        parquet = pq.ParquetFile(path)
        if not set(_RAW_COLUMNS).issubset(parquet.schema_arrow.names):
            raise RawPayloadError(f"raw part has no supported event schema: {path}")
        row_index = 0
        collector_session_id = manifest.attributes["collector_session_id"]
        for batch in parquet.iter_batches(
            batch_size=_RAW_SCAN_BATCH_SIZE,
            columns=list(_RAW_COLUMNS),
        ):
            for row in batch.to_pylist():
                expected_identity = {
                    "source": manifest.source,
                    "instrument": manifest.instrument,
                    "schema_version": manifest.schema_version,
                    "ingest_version": manifest.ingest_version,
                    "collector_session_id": collector_session_id,
                }
                for name, expected in expected_identity.items():
                    if _required_text(row, name, path, row_index) != expected:
                        raise RawPayloadError(
                            f"raw part {name} does not match manifest at {path}:{row_index}"
                        )
                yield row_index, row
                row_index += 1
    except RawPayloadError:
        raise
    except (OSError, ValueError, pa.ArrowException) as exc:
        raise RawPayloadError(f"cannot read raw part {path}: {exc}") from exc


def _duckdb_sorted_raw_rows(
    *,
    parts: Sequence[tuple[Path, DataPartitionManifest]],
    start_ns: int,
    end_ns: int,
) -> Iterator[tuple[Path, int, Mapping[str, object]]]:
    """Read verified parts through DuckDB's bounded external sort.

    The collector writes append-only partitions, not globally sorted runs.
    Sorting in Python would recreate the unbounded payload list that caused
    the readiness worker OOM.  DuckDB spills its ORDER BY to a short-lived
    scratch directory while fetching small Arrow batches.
    """

    if not parts:
        return
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - dependency is pinned in the project
        raise RawPayloadError("duckdb is required for bounded raw streaming") from exc

    scratch_root = os.environ.get("BTC_READINESS_TEMP_DIRECTORY")
    scratch_parent = Path(scratch_root) if scratch_root else None
    if scratch_parent is not None:
        scratch_parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(database=":memory:")
    temp_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        temp_directory = tempfile.TemporaryDirectory(
            prefix="btc-readiness-sort-",
            dir=str(scratch_parent) if scratch_parent is not None else None,
        )
        scratch_path = temp_directory.name
        connection.execute(
            "SET memory_limit=?",
            [_duckdb_memory_limit()],
        )
        connection.execute("SET preserve_insertion_order=false")
        connection.execute("SET threads=1")
        connection.execute("SET temp_directory=?", [scratch_path])
        columns = ", ".join(f'"{name}"' for name in _RAW_COLUMNS)
        query = f"""
            SELECT {columns}, filename AS __raw_path, file_row_number AS __raw_row_index
            FROM read_parquet(?, filename=true, file_row_number=true, union_by_name=true)
            WHERE available_ts_ns BETWEEN ? AND ?
            ORDER BY
                available_ts_ns,
                coalesce(collector_receive_ts_ns, available_ts_ns),
                collector_session_id,
                admission_sequence,
                CASE WHEN event_type = 'continuity_gap' THEN 0 ELSE 1 END,
                source_ts_ns,
                sequence_or_hash,
                __raw_path,
                __raw_row_index
        """
        reader = connection.execute(
            query,
            [[str(path) for path, _manifest in parts], start_ns, end_ns],
        ).to_arrow_reader(batch_size=_RAW_SCAN_BATCH_SIZE)
        for batch in reader:
            for row in batch.to_pylist():
                path = Path(str(row.pop("__raw_path")))
                row_index = int(row.pop("__raw_row_index"))
                yield path, row_index, row
    except RawPayloadError:
        raise
    except Exception as exc:
        raise RawPayloadError(f"bounded raw external sort failed: {exc}") from exc
    finally:
        connection.close()
        if temp_directory is not None:
            temp_directory.cleanup()


def _duckdb_memory_limit() -> str:
    """Return a bounded DuckDB sort budget for the 512 MiB readiness worker.

    The external sort must spill to its temporary directory instead of
    competing with Python/Arrow object memory for the entire container limit.
    Operators may tune the budget, but values outside the reviewed range are
    rejected rather than silently reintroducing an unbounded setting.
    """

    value = os.environ.get("BTC_READINESS_DUCKDB_MEMORY_LIMIT", _DEFAULT_DUCKDB_MEMORY_LIMIT)
    normalized = value.strip().upper()
    if not normalized.endswith("MB"):
        raise RawPayloadError("BTC_READINESS_DUCKDB_MEMORY_LIMIT must use an MB value")
    try:
        megabytes = int(normalized[:-2])
    except ValueError as exc:
        raise RawPayloadError(
            "BTC_READINESS_DUCKDB_MEMORY_LIMIT must use an integer MB value"
        ) from exc
    if not _MIN_DUCKDB_MEMORY_MB <= megabytes <= _MAX_DUCKDB_MEMORY_MB:
        raise RawPayloadError(
            "BTC_READINESS_DUCKDB_MEMORY_LIMIT must be between "
            f"{_MIN_DUCKDB_MEMORY_MB}MB and {_MAX_DUCKDB_MEMORY_MB}MB"
        )
    return f"{megabytes}MB"


def _stream_verified_raw_rows(
    *,
    rows: Iterable[tuple[Path, int, Mapping[str, object]]],
    source: str,
    instrument: str,
    expected_ingest_version: str | None,
) -> Iterator[ForwardRawEvent]:
    previous_key: tuple[int, int, str, int, int, int, str, str, int] | None = None
    seen_admission_sequences: set[tuple[str, int]] = set()
    for path, row_index, row in rows:
        available_ts_ns = _required_int(row, "available_ts_ns", path, row_index)
        _validate_raw_identity(
            row,
            expected_source=source,
            expected_instrument=instrument,
            path=path,
            row_index=row_index,
        )
        row_ingest_version = _required_text(row, "ingest_version", path, row_index)
        if expected_ingest_version is not None and row_ingest_version != expected_ingest_version:
            continue
        event = ForwardRawEvent(
            path=path,
            row_index=row_index,
            source_ts_ns=_required_int(row, "source_ts_ns", path, row_index),
            collector_receive_ts_ns=_optional_int(row, "collector_receive_ts_ns", path, row_index),
            available_ts_ns=available_ts_ns,
            sequence_or_hash=_required_text(row, "sequence_or_hash", path, row_index),
            source=source,
            instrument=instrument,
            schema_version=_required_text(row, "schema_version", path, row_index),
            ingest_version=row_ingest_version,
            event_type=_required_text(row, "event_type", path, row_index),
            collector_session_id=_required_text(row, "collector_session_id", path, row_index),
            epoch_id=_required_int(row, "epoch_id", path, row_index),
            admission_sequence=_required_int(row, "admission_sequence", path, row_index),
            payload=_payload_mapping(row, path=path, row_index=row_index),
        )
        key = _raw_payload_sort_key(event)
        if previous_key is not None and key < previous_key:
            raise RawPayloadError(
                "externally sorted raw events are not in causal order at "
                f"{path}:{row_index}"
            )
        previous_key = key
        if row_ingest_version in _POLYMARKET_SOURCE_REGRESSION_REQUIRED_INGEST_VERSIONS:
            admission_identity = (event.collector_session_id, event.admission_sequence)
            if admission_identity in seen_admission_sequences:
                raise RawPayloadError(
                    "raw events repeat a collector-session admission_sequence "
                    f"admission_sequence at {path}:{row_index}"
                )
            seen_admission_sequences.add(admission_identity)
        yield event


def _sha256_path(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RawPayloadError(f"cannot hash raw part {path}: {exc}") from exc
    return digest.hexdigest()


def _validate_raw_identity(
    row: Mapping[str, object],
    *,
    expected_source: str,
    expected_instrument: str,
    path: Path,
    row_index: int,
) -> None:
    source = _required_text(row, "source", path, row_index)
    instrument = _required_text(row, "instrument", path, row_index)
    if source != expected_source or instrument != expected_instrument:
        raise RawPayloadError(
            f"raw event identity mismatch at {path}:{row_index}: {source}/{instrument}"
        )


def _payload_mapping(
    row: Mapping[str, object], *, path: Path, row_index: int
) -> Mapping[str, object]:
    payload_text = _required_text(row, "payload_json", path, row_index)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RawPayloadError(f"invalid payload_json at {path}:{row_index}: {exc.msg}") from exc
    if not isinstance(payload, Mapping):
        raise RawPayloadError(f"payload_json must contain an object at {path}:{row_index}")
    return payload


def _validate_token_events(
    events: Sequence[TokenBookStateEvent], *, expected_token_id: str
) -> None:
    if any(event.token_id != expected_token_id for event in events):
        raise ValueError("token events do not match the selected market")


def _state_event_sort_key(
    event: TokenBookStateEvent,
) -> tuple[int, int, str, int, int, int, str, int]:
    return (
        event.available_ts_ns,
        event.collector_receive_ts_ns or event.available_ts_ns,
        event.collector_session_id,
        event.admission_sequence,
        0 if event.reset_book else 1,
        event.source_ts_ns,
        event.token_id,
        event.epoch_id,
    )


def _raw_payload_sort_key(
    row: ForwardRawEvent,
) -> tuple[int, int, str, int, int, int, str, str, int]:
    return (
        row.available_ts_ns,
        row.collector_receive_ts_ns or row.available_ts_ns,
        row.collector_session_id,
        row.admission_sequence,
        0 if row.event_type == "continuity_gap" else 1,
        row.source_ts_ns,
        row.sequence_or_hash,
        str(row.path),
        row.row_index,
    )


def _window_ns(*, start_time: datetime, end_time: datetime) -> tuple[int, int]:
    start = _as_utc(start_time, "start_time")
    end = _as_utc(end_time, "end_time")
    if end < start:
        raise ValueError("end_time cannot precede start_time")
    return _datetime_to_ns(start), _datetime_to_ns(end)


def _hours_between(*, start_time: datetime, end_time: datetime) -> Iterator[datetime]:
    current = _as_utc(start_time, "start_time").replace(minute=0, second=0, microsecond=0)
    end = _as_utc(end_time, "end_time").replace(minute=0, second=0, microsecond=0)
    while current <= end:
        yield current
        current += timedelta(hours=1)


def _required_text(row: Mapping[str, object], name: str, path: Path, row_index: int) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RawPayloadError(f"raw {name} must be a non-empty string at {path}:{row_index}")
    return value.strip()


def _required_int(row: Mapping[str, object], name: str, path: Path, row_index: int) -> int:
    value = _optional_int(row, name, path, row_index)
    if value is None:
        raise RawPayloadError(f"raw {name} is required at {path}:{row_index}")
    return value


def _optional_int(row: Mapping[str, object], name: str, path: Path, row_index: int) -> int | None:
    value = row.get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        raise RawPayloadError(f"raw {name} must be an integer at {path}:{row_index}")
    try:
        integer = int(value)
    except (TypeError, ValueError) as exc:
        raise RawPayloadError(f"raw {name} must be an integer at {path}:{row_index}") from exc
    if integer < 0:
        raise RawPayloadError(f"raw {name} must be non-negative at {path}:{row_index}")
    return integer


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _datetime_to_ns(value: datetime) -> int:
    return int(value.timestamp() * _NANOS_PER_SECOND)


def _datetime_from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / _NANOS_PER_SECOND, tz=UTC)


def _as_float(value: object | None) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


__all__ = [
    "ForwardBookEventLoad",
    "ForwardRawEvent",
    "ForwardRawEventLoad",
    "OpeningMarketObservation",
    "PmxtBookEventLoad",
    "RawPayloadError",
    "TokenBookStateEvent",
    "build_opening_market_observations",
    "load_forward_raw_events",
    "load_forward_polymarket_book_events",
    "pmxt_order_book_state_events",
]
