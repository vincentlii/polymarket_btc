from __future__ import annotations

from pathlib import Path

from scripts.btc_runtime_archive import parse_args, run


def test_runtime_archive_cli_backup_audit_and_restore(tmp_path: Path) -> None:
    sources: list[str] = []
    for category in ("model", "wal", "ledger", "report"):
        root = tmp_path / "source" / category
        root.mkdir(parents=True)
        (root / f"{category}.json").write_text("{}\n", encoding="utf-8")
        sources.extend(("--source", f"{category}={root}"))
    common = ["--repository-root", str(tmp_path / "repository")]
    backup = run(
        parse_args(
            [
                *common,
                "backup",
                "--release-revision",
                "b" * 40,
                "--rule-epoch",
                "btc-rule-v1",
                *sources,
                "--transport",
                "local",
                "--remote",
                str(tmp_path / "remote"),
                "--temporary-root",
                str(tmp_path / "verify"),
            ]
        )
    )
    snapshot_id = str(backup["snapshot_id"])
    assert backup["file_count"] == 4

    audit = run(parse_args([*common, "audit", "--snapshot-id", snapshot_id]))
    assert audit["passed"] is True

    restore = run(
        parse_args(
            [
                *common,
                "restore",
                "--snapshot-id",
                snapshot_id,
                "--destination-root",
                str(tmp_path / "restored"),
                "--transport",
                "local",
                "--remote",
                str(tmp_path / "remote"),
                "--temporary-root",
                str(tmp_path / "restore-work"),
            ]
        )
    )
    assert restore["file_count"] == 4
    assert (tmp_path / "restored/model/model.json").is_file()
    drill_audit = run(
        parse_args(
            [
                *common,
                "audit",
                "--snapshot-id",
                snapshot_id,
                "--require-restore-drill",
            ]
        )
    )
    assert drill_audit["passed"] is True
