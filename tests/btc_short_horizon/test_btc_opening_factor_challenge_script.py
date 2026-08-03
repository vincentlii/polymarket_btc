from __future__ import annotations

import json
from pathlib import Path

from scripts.btc_opening_factor_challenge import _write_progress_atomic


def test_candidate_progress_is_replaced_atomically(tmp_path: Path) -> None:
    path = tmp_path / "candidate_progress.json"

    _write_progress_atomic(path, {"completed": ["control_logistic"]})
    _write_progress_atomic(
        path,
        {"completed": ["control_logistic", "boundary_logistic"]},
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "completed": ["control_logistic", "boundary_logistic"]
    }
    assert not path.with_suffix(".json.tmp").exists()
