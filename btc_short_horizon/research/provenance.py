"""Reproducible Git lineage for BTC research artifacts."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import subprocess


def git_provenance(*, repository: Path | None = None) -> dict[str, object]:
    """Describe the exact repository state without claiming a dirty tree is a commit."""

    root = repository or Path(__file__).resolve().parents[2]
    try:
        head = subprocess.check_output(
            ("git", "rev-parse", "HEAD"),
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        status = subprocess.check_output(
            ("git", "status", "--porcelain=v1", "--untracked-files=all"),
            cwd=root,
            stderr=subprocess.DEVNULL,
        )
        tracked_diff = subprocess.check_output(
            ("git", "diff", "--binary", "HEAD", "--"),
            cwd=root,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return {
            "head_revision": None,
            "dirty": True,
            "revision_label": "working-tree-unknown",
            "status_sha256": None,
            "tracked_diff_sha256": None,
            "status_entry_count": None,
        }
    dirty = bool(status)
    status_hash = sha256(status).hexdigest()
    diff_hash = sha256(tracked_diff).hexdigest()
    return {
        "head_revision": head,
        "dirty": dirty,
        "revision_label": head if not dirty else f"{head}-dirty:{status_hash[:12]}",
        "status_sha256": status_hash,
        "tracked_diff_sha256": diff_hash,
        "status_entry_count": len(status.splitlines()),
    }


__all__ = ["git_provenance"]
