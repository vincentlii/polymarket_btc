from __future__ import annotations

from pathlib import Path

import pytest

from prediction_market_extensions.adapters.prediction_market import (
    replay_records_sha256,
    verify_replay_records_sha256,
)
from scripts.btc_pmxt_coverage_audit import _validate_token_mapping, parse_args


class _SerializableRecord:
    def __init__(self, value: int) -> None:
        self.value = value

    @staticmethod
    def to_dict(record: _SerializableRecord) -> dict[str, int]:
        return {"value": record.value}


def test_pmxt_coverage_audit_parses_explicit_window_and_output_directory() -> None:
    args = parse_args(
        (
            "--market-catalog",
            "catalog.json",
            "--market-slug",
            "btc-updown-15m-1776038400",
            "--start-time",
            "2026-04-13T00:00:00Z",
            "--end-time",
            "2026-04-13T00:03:00Z",
            "--output-directory",
            "audit-output",
        )
    )

    assert args.market_catalog == Path("catalog.json")
    assert args.start_time.isoformat() == "2026-04-13T00:00:00+00:00"
    assert args.output_directory == Path("audit-output")


def test_pmxt_coverage_audit_rejects_unverified_token_index_mapping() -> None:
    with pytest.raises(ValueError, match="token indexes"):
        _validate_token_mapping(
            expected_up_token_id="up",
            expected_down_token_id="down",
            actual_up_token_id="down",
            actual_down_token_id="up",
        )


def test_replay_record_digest_binds_content_without_merge_order_noise() -> None:
    records = (_SerializableRecord(1), _SerializableRecord(2))
    digest = replay_records_sha256(records)

    assert replay_records_sha256(tuple(reversed(records))) == digest
    assert verify_replay_records_sha256(records, digest.upper()) == digest
    with pytest.raises(ValueError, match="mismatch"):
        verify_replay_records_sha256((_SerializableRecord(3),), digest)
