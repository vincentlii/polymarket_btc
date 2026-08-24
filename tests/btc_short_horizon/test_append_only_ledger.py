from pathlib import Path

import pytest

from btc_short_horizon.live.append_only_ledger import (
    READ_ONLY_SNAPSHOT_NAME,
    AppendOnlyLedgerRepository,
)


def _payload(*records: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 6,
        "execution_epoch": "paper-v9",
        "variant_id": "primary",
        "starting_balance": 1000.0,
        "research_identity": {"model": "m1"},
        "records": list(records),
    }


def _record(placement_id: str, *, status: str = "working") -> dict[str, object]:
    return {
        "placement_id": placement_id,
        "market_slug": "btc-updown-15m-1",
        "token_id": "up",
        "side": "up",
        "placed_at_ns": 1,
        "execution_status": status,
    }


def test_repository_appends_only_changed_record_revisions_and_can_rollback(tmp_path: Path) -> None:
    store = AppendOnlyLedgerRepository(
        tmp_path / "ledger.sqlite3", execution_epoch="paper-v9", variant_id="primary"
    )
    first_snapshot = store.append_snapshot(_payload(_record("one")))
    store.append_snapshot(_payload(_record("one")))
    store.append_snapshot(_payload(_record("one", status="filled"), _record("two")))
    assert store.revision_count() == 3
    assert len(store.latest()["records"]) == 2

    store.rollback(first_snapshot)
    assert store.latest()["records"] == [_record("one")]
    store.append_snapshot(_payload(_record("one"), _record("three")))
    assert store.latest()["records"] == [_record("one"), _record("three")]
    store.close()


def test_repository_rejects_identity_mutation_or_record_removal(tmp_path: Path) -> None:
    store = AppendOnlyLedgerRepository(
        tmp_path / "ledger.sqlite3", execution_epoch="paper-v9", variant_id="primary"
    )
    store.append_snapshot(_payload(_record("one")))
    changed = _record("one")
    changed["side"] = "down"
    with pytest.raises(ValueError, match="immutable record identity"):
        store.append_snapshot(_payload(changed))
    store.append_snapshot(_payload(_record("one"), _record("two")))
    with pytest.raises(ValueError, match="cannot be removed"):
        store.append_snapshot(_payload(_record("one")))
    store.close()


def test_rollback_then_reapply_identical_prior_state_reuses_revision(tmp_path: Path) -> None:
    store = AppendOnlyLedgerRepository(
        tmp_path / "ledger.sqlite3", execution_epoch="paper-v9", variant_id="primary"
    )
    first = store.append_snapshot(_payload(_record("one")))
    filled = _record("one", status="filled")
    store.append_snapshot(_payload(filled))
    assert store.revision_count() == 2
    store.rollback(first)
    store.append_snapshot(_payload(filled))
    assert store.latest()["records"] == [filled]
    assert store.revision_count() == 2
    store.close()


def test_read_only_repository_never_creates_or_mutates_files(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    writer = AppendOnlyLedgerRepository(path, execution_epoch="paper-v9", variant_id="primary")
    writer.append_snapshot(_payload(_record("one")))
    before = {item.name: item.stat().st_size for item in tmp_path.iterdir()}

    reader = AppendOnlyLedgerRepository(
        path, execution_epoch="paper-v9", variant_id="primary", read_only=True
    )
    assert reader.latest()["records"] == [_record("one")]
    with pytest.raises(PermissionError):
        reader.append_snapshot(_payload(_record("one")))
    reader.close()
    assert {item.name: item.stat().st_size for item in tmp_path.iterdir()} == before
    writer.close()


def test_writer_publishes_closed_read_only_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    writer = AppendOnlyLedgerRepository(path, execution_epoch="paper-v9", variant_id="primary")
    writer.append_snapshot(_payload(_record("one")))

    projection = path.with_name(READ_ONLY_SNAPSHOT_NAME)
    assert projection.is_file()
    reader = AppendOnlyLedgerRepository(
        projection,
        execution_epoch="paper-v9",
        variant_id="primary",
        read_only=True,
    )
    assert reader.latest()["records"] == [_record("one")]
    reader.close()
    writer.close()
