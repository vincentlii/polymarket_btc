from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY
from scripts.btc_pmxt_coverage_pilot import evaluate_pmxt_coverage_pilot


START = datetime(2026, 7, 1, tzinfo=UTC)


def _reports(eligible_count: int) -> dict[str, dict[str, object]]:
    return {
        BTC_15M_MARKET_FAMILY.slug_for(START + timedelta(minutes=15 * index)): {
            "coverage": {"eligible_for_joint_replay": index < eligible_count}
        }
        for index in range(96)
    }


@pytest.mark.parametrize(("eligible", "allowed"), ((86, False), (87, True), (96, True)))
def test_pmxt_24h_pilot_applies_ninety_percent_gate(eligible: int, allowed: bool) -> None:
    result = evaluate_pmxt_coverage_pilot(
        start=START, end=START + timedelta(hours=24), reports=_reports(eligible)
    )

    assert result["expected_market_count"] == 96
    assert result["eligible_market_count"] == eligible
    assert result["bulk_acquisition_allowed"] is allowed


def test_pmxt_24h_pilot_counts_missing_reports_as_ineligible() -> None:
    reports = _reports(96)
    reports.pop(next(iter(reports)))

    result = evaluate_pmxt_coverage_pilot(
        start=START, end=START + timedelta(hours=24), reports=reports
    )

    assert result["reported_market_count"] == 95
    assert len(result["missing_markets"]) == 1


def test_pmxt_pilot_rejects_non_24h_or_unaligned_windows() -> None:
    with pytest.raises(ValueError, match="exactly 24 hours"):
        evaluate_pmxt_coverage_pilot(start=START, end=START + timedelta(hours=23), reports={})
    with pytest.raises(ValueError, match="align"):
        evaluate_pmxt_coverage_pilot(
            start=START + timedelta(minutes=1),
            end=START + timedelta(hours=24, minutes=1),
            reports={},
        )
