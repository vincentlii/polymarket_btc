from __future__ import annotations

from btc_short_horizon.live.research_paper import (
    PaperOpportunityObservation,
    PaperOpportunityStore,
)


def test_opportunity_store_round_trips_rejected_and_selected_rows(tmp_path) -> None:
    store = PaperOpportunityStore(tmp_path, "paper-v4-stage-aware-taker-v2")
    records = [
        PaperOpportunityObservation(
            variant_id="independent_fak_2x5s",
            opportunity_id="market:100",
            market_slug="market",
            decision_ts_ns=100,
            entry_regime="early_3s_to_30s",
            price_bucket="core",
            reason="edge_below_threshold",
            selected_side=None,
            fair_probability=0.55,
            executable_vwap=0.52,
            net_edge=0.0,
        ),
        PaperOpportunityObservation(
            variant_id="independent_fak_stable_3x5s",
            opportunity_id="market:100",
            market_slug="market",
            decision_ts_ns=100,
            entry_regime="early_3s_to_30s",
            price_bucket="core",
            reason="selected",
            selected_side="up",
            fair_probability=0.65,
            executable_vwap=0.52,
            net_edge=0.08,
        ),
    ]

    store.write(records)

    assert store.read() == records
