from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from btc_short_horizon.data.archive import LocalObjectTransport
from btc_short_horizon.live.runtime_archive import (
    REQUIRED_RUNTIME_CATEGORIES,
    RuntimeArchiveIntegrityError,
    RuntimeBackupRepository,
    audit_runtime_recovery,
    create_runtime_snapshot,
    restore_runtime_snapshot,
    upload_runtime_snapshot,
    verify_runtime_snapshot,
)


REVISION = "a" * 40
NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


def _sources(tmp_path: Path) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    payloads = {
        "model": ("champion/model.json", b'{"model":"logistic"}\n'),
        "wal": ("segments/000001.jsonl", b'{"sequence":1}\n'),
        "ledger": ("2026-07-21/ledger.json", b'{"equity":"100"}\n'),
        "report": ("shadow/report.html", b"<html>shadow</html>\n"),
    }
    for category, (relative, payload) in payloads.items():
        root = tmp_path / "source" / category
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        sources[category] = root
    return sources


def test_runtime_snapshot_upload_verify_restore_and_recovery_gate(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    repository = RuntimeBackupRepository(tmp_path / "repository")
    transport = LocalObjectTransport(tmp_path / "remote")

    snapshot, snapshot_path = create_runtime_snapshot(
        sources=sources,
        repository=repository,
        release_revision=REVISION,
        rule_epoch="btc-rule-v1",
        created_at=NOW,
    )
    upload_runtime_snapshot(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        sources=sources,
        transport=transport,
    )
    verified, receipt, _ = verify_runtime_snapshot(
        snapshot_id=snapshot.snapshot_id,
        repository=repository,
        transport=transport,
        temporary_root=tmp_path / "verify",
        verified_at=NOW + timedelta(minutes=1),
    )
    restored, restore_receipt, _ = restore_runtime_snapshot(
        snapshot_id=snapshot.snapshot_id,
        destination_root=tmp_path / "restored",
        repository=repository,
        transport=transport,
        temporary_root=tmp_path / "restore-download",
        restored_at=NOW + timedelta(minutes=2),
    )

    assert verified == restored == snapshot
    assert receipt.file_count == 4
    assert restore_receipt.file_count == 4
    assert (tmp_path / "restored/model/champion/model.json").read_bytes() == (
        b'{"model":"logistic"}\n'
    )
    audit = audit_runtime_recovery(
        repository=repository,
        snapshot_id=snapshot.snapshot_id,
        required_categories=REQUIRED_RUNTIME_CATEGORIES,
        now=NOW + timedelta(hours=1),
        maximum_receipt_age=timedelta(days=1),
        require_restore_drill=True,
        maximum_restore_age=timedelta(days=1),
    )
    assert audit.passed
    assert audit.categories == REQUIRED_RUNTIME_CATEGORIES


def test_runtime_remote_tampering_and_stale_receipt_fail_closed(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    repository = RuntimeBackupRepository(tmp_path / "repository")
    remote = tmp_path / "remote"
    transport = LocalObjectTransport(remote)
    snapshot, snapshot_path = create_runtime_snapshot(
        sources=sources,
        repository=repository,
        release_revision=REVISION,
        rule_epoch="btc-rule-v1",
        created_at=NOW,
    )
    upload_runtime_snapshot(
        snapshot=snapshot,
        snapshot_path=snapshot_path,
        sources=sources,
        transport=transport,
    )
    _, _, _ = verify_runtime_snapshot(
        snapshot_id=snapshot.snapshot_id,
        repository=repository,
        transport=transport,
        temporary_root=tmp_path / "verify",
        verified_at=NOW,
    )

    audit = audit_runtime_recovery(
        repository=repository,
        snapshot_id=snapshot.snapshot_id,
        required_categories=REQUIRED_RUNTIME_CATEGORIES,
        now=NOW + timedelta(days=2),
        maximum_receipt_age=timedelta(days=1),
    )
    assert not audit.passed
    assert audit.reason == "verification_receipt_stale"

    first = snapshot.files[0]
    (remote / first.object_key).write_bytes(b"tampered")
    with pytest.raises(RuntimeArchiveIntegrityError, match="hash mismatch"):
        verify_runtime_snapshot(
            snapshot_id=snapshot.snapshot_id,
            repository=repository,
            transport=transport,
            temporary_root=tmp_path / "verify-again",
        )


def test_runtime_snapshot_rejects_secrets_symlinks_and_overlapping_roots(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / ".env").write_text("POLY_PRIVATE_KEY=secret", encoding="utf-8")
    with pytest.raises(RuntimeArchiveIntegrityError, match="secret-like"):
        create_runtime_snapshot(
            sources={"model": model},
            repository=RuntimeBackupRepository(tmp_path / "repository"),
            release_revision=REVISION,
            rule_epoch="btc-rule-v1",
        )

    (model / ".env").unlink()
    (model / "metadata.json").write_text(
        '{"POLY_API_SECRET":"must-not-leave-host"}', encoding="utf-8"
    )
    with pytest.raises(RuntimeArchiveIntegrityError, match="secret-like content"):
        create_runtime_snapshot(
            sources={"model": model},
            repository=RuntimeBackupRepository(tmp_path / "repository"),
            release_revision=REVISION,
            rule_epoch="btc-rule-v1",
        )
    (model / "metadata.json").unlink()

    nested = model / "nested"
    nested.mkdir()
    with pytest.raises(RuntimeArchiveIntegrityError, match="overlap"):
        create_runtime_snapshot(
            sources={"model": model, "report": nested},
            repository=RuntimeBackupRepository(tmp_path / "repository"),
            release_revision=REVISION,
            rule_epoch="btc-rule-v1",
        )

    target = model / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = model / "linked.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(RuntimeArchiveIntegrityError, match="symlink"):
        create_runtime_snapshot(
            sources={"model": model},
            repository=RuntimeBackupRepository(tmp_path / "repository"),
            release_revision=REVISION,
            rule_epoch="btc-rule-v1",
        )


def test_restore_never_overwrites_different_existing_file(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    repository = RuntimeBackupRepository(tmp_path / "repository")
    transport = LocalObjectTransport(tmp_path / "remote")
    snapshot, path = create_runtime_snapshot(
        sources=sources,
        repository=repository,
        release_revision=REVISION,
        rule_epoch="btc-rule-v1",
        created_at=NOW,
    )
    upload_runtime_snapshot(
        snapshot=snapshot,
        snapshot_path=path,
        sources=sources,
        transport=transport,
    )
    conflict = tmp_path / "restored/model/champion/model.json"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"different")

    with pytest.raises(RuntimeArchiveIntegrityError, match="different existing"):
        restore_runtime_snapshot(
            snapshot_id=snapshot.snapshot_id,
            destination_root=tmp_path / "restored",
            repository=RuntimeBackupRepository(tmp_path / "restore-repository"),
            transport=transport,
            temporary_root=tmp_path / "restore-download",
        )
    assert not (tmp_path / "restored/ledger").exists()
