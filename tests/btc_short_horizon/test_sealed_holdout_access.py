from __future__ import annotations

import json

import pytest

from btc_short_horizon.research.sealed_holdout import acquire_sealed_holdout_access


def test_sealed_holdout_requires_accepted_development_gate_and_is_once_only(tmp_path) -> None:
    receipt = tmp_path / "sealed-holdout-consumed.json"

    with pytest.raises(ValueError, match="accepted development gate"):
        acquire_sealed_holdout_access(
            receipt_path=receipt,
            development_gate_accepted=False,
            protocol_hash="a" * 64,
        )

    access = acquire_sealed_holdout_access(
        receipt_path=receipt,
        development_gate_accepted=True,
        protocol_hash="a" * 64,
    )

    assert access.protocol_hash == "a" * 64
    assert json.loads(receipt.read_text("utf-8"))["protocol_hash"] == "a" * 64
    with pytest.raises(FileExistsError, match="already been consumed"):
        acquire_sealed_holdout_access(
            receipt_path=receipt,
            development_gate_accepted=True,
            protocol_hash="a" * 64,
        )
