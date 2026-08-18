"""Explicit one-shot access contract for sealed model-selection holdouts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SealedHoldoutAccess:
    protocol_hash: str
    receipt_path: Path
    consumed_at: datetime


def acquire_sealed_holdout_access(
    *,
    receipt_path: Path,
    development_gate_accepted: bool,
    protocol_hash: str,
) -> SealedHoldoutAccess:
    """Atomically consume one sealed holdout after development selection is frozen."""

    if development_gate_accepted is not True:
        raise ValueError("sealed holdout requires an accepted development gate")
    if len(protocol_hash) != 64 or any(
        character not in "0123456789abcdef" for character in protocol_hash
    ):
        raise ValueError("protocol_hash must be a lowercase SHA-256 digest")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    consumed_at = datetime.now(UTC)
    payload = {
        "consumed_at": consumed_at.isoformat(),
        "development_gate_accepted": True,
        "protocol_hash": protocol_hash,
    }
    try:
        with receipt_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise FileExistsError(f"sealed holdout has already been consumed: {receipt_path}") from exc
    return SealedHoldoutAccess(
        protocol_hash=protocol_hash,
        receipt_path=receipt_path,
        consumed_at=consumed_at,
    )


__all__ = ["SealedHoldoutAccess", "acquire_sealed_holdout_access"]
