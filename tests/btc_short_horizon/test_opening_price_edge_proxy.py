from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from scripts.btc_opening_price_edge_proxy import (
    _build_candidates,
    _select_positive_confidence_threshold,
    _select_one_entry_per_market,
    _select_one_entry_per_market_by_regime,
    _sensitivity_entries,
)


def test_regime_threshold_requires_positive_development_confidence() -> None:
    sweep = [
        {
            "threshold": 0.0,
            "entry_count": 200,
            "realized_ev_ci95_lower": -0.001,
            "realized_ev_per_share": 0.04,
        },
        {
            "threshold": 0.04,
            "entry_count": 150,
            "realized_ev_ci95_lower": 0.012,
            "realized_ev_per_share": 0.03,
        },
    ]

    selected = _select_positive_confidence_threshold(sweep, min_development_entries=100)

    assert selected is not None
    assert selected["threshold"] == 0.04
    assert (
        _select_positive_confidence_threshold(
            [sweep[0]],
            min_development_entries=100,
        )
        is None
    )


def test_sparse_price_proxy_uses_only_causally_available_price_pairs() -> None:
    t0 = datetime(2026, 4, 13, tzinfo=UTC)
    market = MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id="condition",
        up_token_id="up-token",
        down_token_id="down-token",
        t0=t0,
        t1=t0 + timedelta(minutes=15),
        rule_epoch="rule-v1",
        rule_hash="a" * 64,
        resolution=MarketOutcome.UP,
        label_available_ts=t0 + timedelta(minutes=16),
    )
    start_ns = int(t0.timestamp() * 1_000_000_000)
    predictions = pd.DataFrame(
        [
            {
                "split": "development_oof",
                "sample_id": f"{market.slug}@{start_ns + seconds * 1_000_000_000}",
                "market_slug": market.slug,
                "feature_ts_ns": start_ns + seconds * 1_000_000_000,
                "p_up": probability,
                "label": 1,
            }
            for seconds, probability in ((5, 0.65), (10, 0.66), (65, 0.75))
        ]
    )
    start_ts = int(t0.timestamp())
    prices = pd.DataFrame(
        [
            {"token_id": "up-token", "ts_seconds": start_ts + 4, "price": 0.50},
            {"token_id": "down-token", "ts_seconds": start_ts + 4, "price": 0.50},
            {"token_id": "up-token", "ts_seconds": start_ts + 60, "price": 0.60},
            {"token_id": "down-token", "ts_seconds": start_ts + 60, "price": 0.40},
        ]
    )

    candidates, coverage = _build_candidates(
        predictions=predictions,
        prices=prices,
        markets=(market,),
        entry_price_buffer=0.01,
        max_price_age_seconds=75,
    )
    entries = _select_one_entry_per_market(
        candidates,
        threshold=0.10,
        minimum_consecutive_signals=2,
        maximum_signal_gap_seconds=5,
    )
    sensitivity = _sensitivity_entries(
        candidates,
        threshold=0.10,
        additional_entry_cost=0.01,
        max_price_age_seconds=15,
        minimum_consecutive_signals=2,
        maximum_signal_gap_seconds=5,
    )

    assert coverage["candidate_rows"] == 3
    assert list(candidates["elapsed_seconds"]) == [5, 10, 65]
    assert list(candidates["regime"]) == [
        "early_3s_to_30s",
        "early_3s_to_30s",
        "price_discovery_35s_to_90s",
    ]
    assert len(entries) == 1
    assert entries.iloc[0]["elapsed_seconds"] == 10
    assert sensitivity.iloc[0]["elapsed_seconds"] == 10
    assert sensitivity.iloc[0]["entry_price"] == pytest.approx(0.52)


def test_regime_thresholds_preserve_the_earliest_qualifying_market_entry() -> None:
    candidates = pd.DataFrame(
        [
            {
                "market_slug": "market-a",
                "decision_ts_ns": 10_000_000_000,
                "side": "down",
                "predicted_edge": 0.11,
                "regime": "early_3s_to_30s",
            },
            {
                "market_slug": "market-a",
                "decision_ts_ns": 15_000_000_000,
                "side": "down",
                "predicted_edge": 0.12,
                "regime": "early_3s_to_30s",
            },
            {
                "market_slug": "market-a",
                "decision_ts_ns": 65_000_000_000,
                "side": "down",
                "predicted_edge": 0.06,
                "regime": "price_discovery_35s_to_90s",
            },
            {
                "market_slug": "market-a",
                "decision_ts_ns": 70_000_000_000,
                "side": "down",
                "predicted_edge": 0.07,
                "regime": "price_discovery_35s_to_90s",
            },
        ]
    )

    entries = _select_one_entry_per_market_by_regime(
        candidates,
        thresholds_by_regime={
            "early_3s_to_30s": 0.15,
            "price_discovery_35s_to_90s": 0.05,
        },
        minimum_consecutive_signals=2,
        maximum_signal_gap_seconds=5,
    )

    assert len(entries) == 1
    assert entries.iloc[0]["decision_ts_ns"] == 70_000_000_000
    assert entries.iloc[0]["regime"] == "price_discovery_35s_to_90s"
