from pathlib import Path
from datetime import datetime

import pytest

from btc_short_horizon.data.coverage_index import SessionCoverageIndex
from btc_short_horizon.data.session_inventory import CollectorSessionRecord, SessionPartRecord
from btc_short_horizon.data.storage import DataPartitionManifest


def test_index_survives_restart_and_selects_overlapping_sessions(tmp_path: Path) -> None:
    digest = "a" * 64
    lower = int(datetime.fromisoformat("2026-08-13T00:00:00+00:00").timestamp() * 1e9)
    upper = int(datetime.fromisoformat("2026-08-13T00:01:00+00:00").timestamp() * 1e9)
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}")
    manifest = DataPartitionManifest(
        source="binance_spot",
        instrument="BTC",
        schema_version="v1",
        ingest_version="v16",
        data_path=f"raw/binance_spot/BTC/date=2026-08-13/hour=00/part-{digest[:32]}.parquet",
        sha256=digest,
        row_count=1,
        min_source_ts_ns=lower,
        max_source_ts_ns=upper,
        min_available_ts_ns=lower,
        max_available_ts_ns=upper,
        duplicate_count=0,
        gap_count=0,
        created_at="2026-08-13T00:00:00+00:00",
        attributes={},
    )
    part = SessionPartRecord(
        "manifest.json",
        digest,
        manifest,
        "committed",
        "2026-08-13T00:00:00+00:00",
        "2026-08-13T00:00:01+00:00",
    )
    session = CollectorSessionRecord(
        "s1",
        "v16",
        "2026-08-13T00:00:00+00:00",
        "2026-08-13T00:01:00+00:00",
        "complete",
        "2026-08-13T00:01:00+00:00",
        None,
        {},
        (part,),
    )
    SessionCoverageIndex(tmp_path).append(session, inventory_path=inventory)
    start = int(datetime.fromisoformat("2026-08-13T00:00:10+00:00").timestamp() * 1e9)
    end = int(datetime.fromisoformat("2026-08-13T00:00:20+00:00").timestamp() * 1e9)
    assert SessionCoverageIndex(tmp_path).overlapping_session_ids(
        start_ns=start, end_ns=end, required_sources=("binance_spot",)
    ) == ("s1",)


def test_index_fails_closed_when_prior_session_leaves_lookback_gap(tmp_path: Path) -> None:
    index = SessionCoverageIndex(tmp_path)
    for session_id, started, completed in (
        ("before", "2026-08-13T00:00:00+00:00", "2026-08-13T00:30:00+00:00"),
        ("closing", "2026-08-13T00:45:00+00:00", "2026-08-13T01:00:00+00:00"),
    ):
        inventory = tmp_path / f"{session_id}.json"
        inventory.write_text(session_id)
        digest = ("a" if session_id == "before" else "b") * 64
        lower = int(datetime.fromisoformat(started).timestamp() * 1e9) + 1
        upper = int(datetime.fromisoformat(completed).timestamp() * 1e9) - 1
        manifest = DataPartitionManifest(
            source="binance_spot",
            instrument="BTC",
            schema_version="v1",
            ingest_version="v16",
            data_path=f"raw/binance_spot/BTC/date=2026-08-13/hour=00/part-{digest[:32]}.parquet",
            sha256=digest,
            row_count=1,
            min_source_ts_ns=lower,
            max_source_ts_ns=upper,
            min_available_ts_ns=lower,
            max_available_ts_ns=upper,
            duplicate_count=0,
            gap_count=0,
            created_at=started,
            attributes={},
        )
        part = SessionPartRecord("manifest.json", digest, manifest, "committed", started, completed)
        session = CollectorSessionRecord(
            session_id, "v16", started, completed, "complete", completed, None, {}, (part,)
        )
        index.append(session, inventory_path=inventory)

    with pytest.raises(ValueError, match="session coverage gap"):
        index.coverage_evidence(
            start_ns=int(datetime.fromisoformat("2026-08-13T00:00:00+00:00").timestamp() * 1e9),
            end_ns=int(datetime.fromisoformat("2026-08-13T01:00:00+00:00").timestamp() * 1e9),
            required_sources=("binance_spot",),
        )


def test_index_uses_source_continuity_across_storage_session_rotation(tmp_path: Path) -> None:
    index = SessionCoverageIndex(tmp_path)
    source_boundary = "2026-08-13T00:15:00+00:00"
    for session_id, started, completed, source_min, source_max, digest_char in (
        (
            "first",
            "2026-08-13T00:00:00+00:00",
            "2026-08-13T00:15:06+00:00",
            "2026-08-13T00:00:00+00:00",
            source_boundary,
            "e",
        ),
        (
            "second",
            "2026-08-13T00:15:06.065000+00:00",
            "2026-08-13T00:30:00+00:00",
            source_boundary,
            "2026-08-13T00:30:00+00:00",
            "f",
        ),
    ):
        inventory = tmp_path / f"{session_id}.json"
        inventory.write_text(session_id)
        digest = digest_char * 64
        manifest = DataPartitionManifest(
            source="polymarket_clob",
            instrument="BTC-15M",
            schema_version="v1",
            ingest_version="v16",
            data_path=(
                f"raw/polymarket_clob/BTC-15M/date=2026-08-13/hour=00/part-{digest[:32]}.parquet"
            ),
            sha256=digest,
            row_count=10,
            min_source_ts_ns=int(datetime.fromisoformat(source_min).timestamp() * 1e9),
            max_source_ts_ns=int(datetime.fromisoformat(source_max).timestamp() * 1e9),
            min_available_ts_ns=int(datetime.fromisoformat(source_min).timestamp() * 1e9),
            max_available_ts_ns=int(datetime.fromisoformat(source_max).timestamp() * 1e9),
            duplicate_count=0,
            gap_count=0,
            created_at=started,
            attributes={},
        )
        part = SessionPartRecord("manifest.json", digest, manifest, "committed", started, completed)
        session = CollectorSessionRecord(
            session_id, "v16", started, completed, "complete", completed, None, {}, (part,)
        )
        index.append(session, inventory_path=inventory)

    start_ns = int(datetime.fromisoformat("2026-08-13T00:00:00+00:00").timestamp() * 1e9)
    end_ns = int(datetime.fromisoformat("2026-08-13T00:30:00+00:00").timestamp() * 1e9)
    assert index.overlapping_session_ids(
        start_ns=start_ns,
        end_ns=end_ns,
        required_sources=("polymarket_clob",),
    ) == ("first", "second")


@pytest.mark.parametrize("gap_count", [1, 3])
def test_index_rejects_recorded_source_gap_even_when_session_bounds_overlap(
    tmp_path: Path, gap_count: int
) -> None:
    index = SessionCoverageIndex(tmp_path)
    started = "2026-08-13T00:00:00+00:00"
    completed = "2026-08-13T00:15:00+00:00"
    lower = int(datetime.fromisoformat(started).timestamp() * 1e9)
    upper = int(datetime.fromisoformat(completed).timestamp() * 1e9)
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}")
    digest = "c" * 64
    manifest = DataPartitionManifest(
        source="binance_spot",
        instrument="BTC",
        schema_version="v1",
        ingest_version="v16",
        data_path=f"raw/binance_spot/BTC/date=2026-08-13/hour=00/part-{digest[:32]}.parquet",
        sha256=digest,
        row_count=10,
        min_source_ts_ns=lower,
        max_source_ts_ns=upper,
        min_available_ts_ns=lower,
        max_available_ts_ns=upper,
        duplicate_count=0,
        gap_count=gap_count,
        created_at=started,
        attributes={},
    )
    part = SessionPartRecord("manifest.json", digest, manifest, "committed", started, completed)
    session = CollectorSessionRecord(
        "gap", "v16", started, completed, "complete", completed, None, {}, (part,)
    )
    index.append(session, inventory_path=inventory)

    with pytest.raises(ValueError, match="recorded source gap"):
        index.coverage_evidence(
            start_ns=lower,
            end_ns=upper,
            required_sources=("binance_spot",),
        )


def test_index_does_not_treat_event_silence_as_a_transport_gap(
    tmp_path: Path,
) -> None:
    index = SessionCoverageIndex(tmp_path)
    started = "2026-08-13T00:00:00+00:00"
    completed = "2026-08-13T00:15:00+00:00"
    lower = int(datetime.fromisoformat(started).timestamp() * 1e9)
    upper = int(datetime.fromisoformat(completed).timestamp() * 1e9)
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}")
    digest = "d" * 64
    manifest = DataPartitionManifest(
        source="binance_spot",
        instrument="BTC",
        schema_version="v1",
        ingest_version="v16",
        data_path=f"raw/binance_spot/BTC/date=2026-08-13/hour=00/part-{digest[:32]}.parquet",
        sha256=digest,
        row_count=1,
        min_source_ts_ns=upper - 1_000_000_000,
        max_source_ts_ns=upper,
        min_available_ts_ns=upper - 1_000_000_000,
        max_available_ts_ns=upper,
        duplicate_count=0,
        gap_count=0,
        created_at=started,
        attributes={},
    )
    part = SessionPartRecord("manifest.json", digest, manifest, "committed", started, completed)
    session = CollectorSessionRecord(
        "sparse", "v16", started, completed, "complete", completed, None, {}, (part,)
    )
    index.append(session, inventory_path=inventory)

    assert index.overlapping_session_ids(
        start_ns=lower,
        end_ns=upper,
        required_sources=("binance_spot",),
    ) == ("sparse",)
