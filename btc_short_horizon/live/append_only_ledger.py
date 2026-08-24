"""Normalized, crash-safe append-only storage for the Paper ledger."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from typing import Mapping
from uuid import uuid4


SCHEMA_VERSION = 1
READ_ONLY_SNAPSHOT_NAME = "ledger.snapshot.sqlite3"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class AppendOnlyLedgerRepository:
    """Append record lifecycle revisions and immutable snapshot activations."""

    def __init__(
        self,
        path: Path,
        *,
        execution_epoch: str,
        variant_id: str,
        read_only: bool = False,
    ) -> None:
        self.path = path
        self.execution_epoch = execution_epoch
        self.variant_id = variant_id
        self._read_only = read_only
        if read_only:
            self._connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            self._validate_schema()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, SCHEMA_VERSION}:
            self.close()
            raise ValueError("unsupported append-only Paper ledger schema")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_header (
                execution_epoch TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                header_json TEXT NOT NULL,
                migration_source_sha256 TEXT,
                PRIMARY KEY (execution_epoch, variant_id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS record_revisions (
                revision INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_epoch TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                placement_id TEXT NOT NULL,
                record_json TEXT NOT NULL,
                record_sha256 TEXT NOT NULL,
                previous_revision INTEGER,
                UNIQUE (execution_epoch, variant_id, placement_id, record_sha256)
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_epoch TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                max_record_revision INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshot_records (
                snapshot_id INTEGER NOT NULL,
                placement_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                PRIMARY KEY (snapshot_id, placement_id)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS activations (
                activation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_epoch TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                snapshot_id INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )
        self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._connection.commit()
        self._publish_read_only_snapshot()

    def _validate_schema(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            self.close()
            raise ValueError("unsupported append-only Paper ledger schema")

    def append_snapshot(
        self,
        payload: Mapping[str, object],
        *,
        reason: str = "snapshot",
        migration_source_sha256: str | None = None,
    ) -> int:
        if self._read_only:
            raise PermissionError("Paper ledger repository is read-only")
        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError("Paper ledger records must be an array")
        header = {key: value for key, value in payload.items() if key != "records"}
        header_json = _canonical(header)
        identities: set[str] = set()
        changed = False
        with self._connection:
            existing_header = self._connection.execute(
                "SELECT header_json FROM ledger_header WHERE execution_epoch=? AND variant_id=?",
                (self.execution_epoch, self.variant_id),
            ).fetchone()
            if existing_header is None:
                changed = True
                self._connection.execute(
                    "INSERT INTO ledger_header VALUES (?, ?, ?, ?)",
                    (
                        self.execution_epoch,
                        self.variant_id,
                        header_json,
                        migration_source_sha256,
                    ),
                )
            elif str(existing_header[0]) != header_json:
                raise ValueError("Paper ledger immutable header changed")

            active = self._active_snapshot()
            previous_records: dict[str, tuple[int, str, str]] = {}
            if active is not None:
                previous_records = {
                    str(row[0]): (int(row[1]), str(row[2]), str(row[3]))
                    for row in self._connection.execute(
                        """SELECT r.placement_id, r.revision, r.record_sha256, r.record_json
                           FROM record_revisions r
                           JOIN (
                             SELECT placement_id, MAX(revision) revision FROM record_revisions
                             WHERE execution_epoch=? AND variant_id=? AND revision<=?
                             GROUP BY placement_id
                           ) latest ON latest.revision=r.revision""",
                        (self.execution_epoch, self.variant_id, active[1]),
                    )
                }
            chosen_revisions = {
                placement_id: values[0] for placement_id, values in previous_records.items()
            }
            for record in records:
                if not isinstance(record, Mapping):
                    raise ValueError("Paper ledger record must be an object")
                placement_id = record.get("placement_id")
                if not isinstance(placement_id, str) or not placement_id:
                    raise ValueError("Paper ledger placement_id is required")
                if placement_id in identities:
                    raise ValueError("Paper ledger placement IDs must be unique")
                identities.add(placement_id)
                record_json = _canonical(record)
                digest = sha256(record_json.encode()).hexdigest()
                previous = previous_records.get(placement_id)
                if previous is not None:
                    prior_record = json.loads(str(previous[2]))
                    identity_fields = (
                        "placement_id",
                        "market_slug",
                        "token_id",
                        "side",
                        "placed_at_ns",
                    )
                    if any(prior_record.get(key) != record.get(key) for key in identity_fields):
                        raise ValueError("Paper ledger immutable record identity changed")
                if previous is None or previous[1] != digest:
                    existing_revision = self._connection.execute(
                        """SELECT revision FROM record_revisions
                           WHERE execution_epoch=? AND variant_id=? AND placement_id=?
                             AND record_sha256=?""",
                        (
                            self.execution_epoch,
                            self.variant_id,
                            placement_id,
                            digest,
                        ),
                    ).fetchone()
                    changed = True
                    if existing_revision is None:
                        inserted = self._connection.execute(
                            """INSERT INTO record_revisions
                               (execution_epoch, variant_id, placement_id, record_json,
                                record_sha256, previous_revision) VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                self.execution_epoch,
                                self.variant_id,
                                placement_id,
                                record_json,
                                digest,
                                None if previous is None else previous[0],
                            ),
                        )
                        chosen_revisions[placement_id] = int(inserted.lastrowid)
                    else:
                        chosen_revisions[placement_id] = int(existing_revision[0])
            if not previous_records.keys() <= identities:
                raise ValueError("Paper ledger records cannot be removed from an active epoch")
            if not changed and active is not None:
                return active[0]
            max_revision = int(
                self._connection.execute(
                    """SELECT COALESCE(MAX(revision), 0) FROM record_revisions
                       WHERE execution_epoch=? AND variant_id=?""",
                    (self.execution_epoch, self.variant_id),
                ).fetchone()[0]
            )
            cursor = self._connection.execute(
                "INSERT INTO snapshots (execution_epoch, variant_id, max_record_revision, reason) VALUES (?, ?, ?, ?)",
                (self.execution_epoch, self.variant_id, max_revision, reason),
            )
            snapshot_id = int(cursor.lastrowid)
            for placement_id in sorted(identities):
                self._connection.execute(
                    "INSERT INTO snapshot_records VALUES (?, ?, ?)",
                    (snapshot_id, placement_id, chosen_revisions[placement_id]),
                )
            self._connection.execute(
                "INSERT INTO activations (execution_epoch, variant_id, snapshot_id, reason) VALUES (?, ?, ?, ?)",
                (self.execution_epoch, self.variant_id, snapshot_id, reason),
            )
        self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self._publish_read_only_snapshot()
        return snapshot_id

    def latest(self) -> dict[str, object] | None:
        snapshot = self._active_snapshot()
        if snapshot is None:
            return None
        header_row = self._connection.execute(
            "SELECT header_json FROM ledger_header WHERE execution_epoch=? AND variant_id=?",
            (self.execution_epoch, self.variant_id),
        ).fetchone()
        if header_row is None:
            raise ValueError("Paper ledger snapshot has no header")
        header = json.loads(str(header_row[0]))
        rows = self._connection.execute(
            """SELECT r.record_json FROM record_revisions r
               JOIN snapshot_records selected ON selected.revision=r.revision
               WHERE selected.snapshot_id=?
               ORDER BY r.revision""",
            (snapshot[0],),
        ).fetchall()
        return {**header, "records": [json.loads(str(row[0])) for row in rows]}

    def rollback(self, snapshot_id: int) -> None:
        if self._read_only:
            raise PermissionError("Paper ledger repository is read-only")
        row = self._connection.execute(
            "SELECT snapshot_id FROM snapshots WHERE snapshot_id=? AND execution_epoch=? AND variant_id=?",
            (snapshot_id, self.execution_epoch, self.variant_id),
        ).fetchone()
        if row is None:
            raise ValueError("Paper ledger rollback snapshot does not exist")
        with self._connection:
            self._connection.execute(
                "INSERT INTO activations (execution_epoch, variant_id, snapshot_id, reason) VALUES (?, ?, ?, ?)",
                (self.execution_epoch, self.variant_id, snapshot_id, f"rollback:{snapshot_id}"),
            )
        self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self._publish_read_only_snapshot()

    def revision_count(self) -> int:
        return int(
            self._connection.execute(
                """SELECT COUNT(*) FROM record_revisions
                   WHERE execution_epoch=? AND variant_id=?""",
                (self.execution_epoch, self.variant_id),
            ).fetchone()[0]
        )

    def _active_snapshot(self) -> tuple[int, int] | None:
        row = self._connection.execute(
            """SELECT s.snapshot_id, s.max_record_revision FROM snapshots s
               JOIN activations a ON a.snapshot_id=s.snapshot_id
               WHERE a.execution_epoch=? AND a.variant_id=?
                 AND s.execution_epoch=a.execution_epoch AND s.variant_id=a.variant_id
               ORDER BY a.activation_id DESC LIMIT 1""",
            (self.execution_epoch, self.variant_id),
        ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def _publish_read_only_snapshot(self) -> None:
        """Publish a closed rollback-journal copy for the read-only dashboard mount."""

        if self._read_only:
            return
        target = self.path.with_name(READ_ONLY_SNAPSHOT_NAME)
        staging = target.with_name(f".{target.name}.staging-{uuid4().hex}")
        destination = sqlite3.connect(staging)
        try:
            self._connection.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.execute("PRAGMA synchronous=FULL")
            destination.commit()
        finally:
            destination.close()
        try:
            with staging.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)

    def close(self) -> None:
        self._connection.close()


__all__ = ["AppendOnlyLedgerRepository", "READ_ONLY_SNAPSHOT_NAME", "SCHEMA_VERSION"]
