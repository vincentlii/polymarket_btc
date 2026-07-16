from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq

from btc_short_horizon.data.collector import PartitionedRawEventWriter, RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.quality import EventQualityValidator


T0 = datetime(2026, 4, 13, tzinfo=UTC)


def _event(
    *,
    sequence: str,
    available_offset_ms: int = 0,
    receive_offset_ms: int = 0,
    schema_version: str = "v1",
) -> TimedMarketEvent:
    return TimedMarketEvent(
        source_ts=T0,
        collector_receive_ts=T0 + timedelta(milliseconds=receive_offset_ms),
        available_ts=T0 + timedelta(milliseconds=available_offset_ms),
        sequence_or_hash=sequence,
        source="binance",
        instrument="BTCUSDT",
        schema_version=schema_version,
        ingest_version="v1",
    )


def test_quality_validator_bounds_deduplication_and_starts_epoch_on_order_regression() -> None:
    validator = EventQualityValidator(
        max_seen_identifiers=2, max_transport_delay=timedelta(milliseconds=10)
    )
    assert validator.observe(_event(sequence="one")).accepted
    assert validator.observe(_event(sequence="one")).reason == "duplicate"
    stale = validator.observe(_event(sequence="two", available_offset_ms=20, receive_offset_ms=20))
    assert stale.accepted and stale.stale
    unordered = validator.observe(_event(sequence="three", available_offset_ms=10))
    assert not unordered.accepted
    assert unordered.reason == "out_of_order"
    assert validator.epoch_id == 1
    assert validator.stats.duplicate_events == 1
    assert validator.stats.out_of_order_events == 1
    assert validator.stats.stale_events == 1


def test_quality_validator_deduplicates_within_one_event_schema() -> None:
    validator = EventQualityValidator()

    first = validator.observe(_event(sequence="101", schema_version="binance-depth-update-v1"))
    second = validator.observe(
        _event(sequence="101", schema_version="binance-book-ticker-receive-v1")
    )

    assert first.accepted
    assert second.accepted


def test_quality_validator_tolerates_bounded_source_clock_lead() -> None:
    validator = EventQualityValidator(max_transport_delay=timedelta(seconds=1))

    bounded = validator.observe(
        _event(sequence="bounded", receive_offset_ms=-100, available_offset_ms=1)
    )
    excessive = validator.observe(
        _event(sequence="excessive", receive_offset_ms=-1_100, available_offset_ms=2)
    )

    assert bounded.accepted and not bounded.stale
    assert excessive.accepted and excessive.stale


def test_raw_event_writer_outputs_immutable_partition_and_timing_columns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    event = RawCollectorEvent(
        timing=_event(sequence="one"),
        event_type="trade",
        payload={"price": "100000"},
        epoch_id=0,
    )
    manifests = PartitionedRawEventWriter(tmp_path).write((event,))

    assert len(manifests) == 1
    table = pq.read_table(tmp_path / manifests[0].data_path)
    assert table.column("sequence_or_hash")[0].as_py() == "one"
    assert table.column("epoch_id")[0].as_py() == 0
