from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.live.direction_health import DirectionEvidenceStore
from btc_short_horizon.models.opening_mispricing import OpeningMispricingPrediction


T0 = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)


def _market(index: int = 0) -> MarketWindow:
    start = T0 + timedelta(minutes=15 * index)
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=f"btc-updown-15m-{int(start.timestamp())}",
        condition_id=f"condition-{index}",
        up_token_id=f"up-{index}",
        down_token_id=f"down-{index}",
        t0=start,
        t1=start + timedelta(minutes=15),
        rule_epoch="chainlink-btc-usd-v1",
        rule_hash=f"{index + 1:064x}",
    )


def _prediction(market: MarketWindow, *, second: int, p_up: float) -> OpeningMispricingPrediction:
    start_ns = int(market.t0.timestamp() * 1_000_000_000)
    return OpeningMispricingPrediction(
        market_slug=market.slug,
        model_version="model-v1",
        feature_schema_hash="schema-v1",
        market_window_start_ts_ns=start_ns,
        trigger_ts_ns=start_ns + second * 1_000_000_000,
        p_up=p_up,
        p_boundary_up=0.5,
        p_market_mid_up=0.5,
        data_age_seconds=0.1,
    )


def test_direction_store_pairs_predictions_and_results_by_market(tmp_path) -> None:
    store = DirectionEvidenceStore(tmp_path, "paper-v6")
    up_market = _market(0)
    down_market = _market(1)
    no_prediction = _market(2)
    for market in (up_market, down_market, no_prediction):
        store.register_market(market)

    store.append_prediction(_prediction(up_market, second=5, p_up=0.7), stage="early_3s_to_30s")
    store.append_prediction(_prediction(up_market, second=10, p_up=0.6), stage="early_3s_to_30s")
    store.append_prediction(
        _prediction(down_market, second=35, p_up=0.4), stage="price_discovery_35s_to_90s"
    )
    store.settle(up_market.slug, MarketOutcome.UP, label_available_ts_ns=1)
    store.settle(down_market.slug, MarketOutcome.DOWN, label_available_ts_ns=2)
    store.settle(no_prediction.slug, MarketOutcome.UP, label_available_ts_ns=3)

    snapshot = store.snapshot()

    assert snapshot.activated_market_count == 3
    assert snapshot.resolved_market_count == 3
    assert snapshot.paired_market_count == 2
    assert snapshot.prediction_count == 3
    assert snapshot.actual_up_count == 1
    assert snapshot.actual_down_count == 1
    assert snapshot.predicted_up_count == 1
    assert snapshot.predicted_down_count == 1
    assert snapshot.mean_p_up == pytest.approx(0.525)
    assert snapshot.bias_state == "insufficient_data"
    assert {item.stage for item in snapshot.stage_summaries} == {
        "early_3s_to_30s",
        "price_discovery_35s_to_90s",
    }


def test_direction_store_is_idempotent_restart_safe_and_tracks_unresolved(tmp_path) -> None:
    market = _market()
    prediction = _prediction(market, second=5, p_up=0.55)
    first = DirectionEvidenceStore(tmp_path, "paper-v6")
    first.register_market(market)
    first.register_market(market)
    first.append_prediction(prediction, stage="early_3s_to_30s")
    first.append_prediction(prediction, stage="early_3s_to_30s")
    assert first.unresolved_market_slugs() == (market.slug,)
    first.close()

    restarted = DirectionEvidenceStore(tmp_path, "paper-v6")
    assert restarted.snapshot().prediction_count == 1
    restarted.settle(market.slug, MarketOutcome.UP, label_available_ts_ns=1)
    restarted.settle(market.slug, MarketOutcome.UP, label_available_ts_ns=1)
    assert restarted.unresolved_market_slugs() == ()
    restarted.close()


def test_direction_store_rejects_conflicting_immutable_evidence(tmp_path) -> None:
    market = _market()
    store = DirectionEvidenceStore(tmp_path, "paper-v6")
    store.register_market(market)
    store.append_prediction(_prediction(market, second=5, p_up=0.55), stage="early_3s_to_30s")

    with pytest.raises(ValueError, match="prediction conflict"):
        store.append_prediction(_prediction(market, second=5, p_up=0.65), stage="early_3s_to_30s")

    store.settle(market.slug, MarketOutcome.UP, label_available_ts_ns=1)
    with pytest.raises(ValueError, match="outcome conflict"):
        store.settle(market.slug, MarketOutcome.DOWN, label_available_ts_ns=2)


def test_direction_store_only_flags_bias_after_minimum_paired_sample(tmp_path) -> None:
    store = DirectionEvidenceStore(tmp_path, "paper-v6")
    for index in range(100):
        market = _market(index)
        store.register_market(market)
        store.append_prediction(_prediction(market, second=5, p_up=0.5), stage="early_3s_to_30s")
        store.settle(market.slug, MarketOutcome.UP, label_available_ts_ns=index)

    snapshot = store.snapshot()

    assert snapshot.paired_market_count == 100
    assert snapshot.actual_up_count == 100
    assert snapshot.predicted_up_count == 100
    assert snapshot.calibration_z == pytest.approx(10.0)
    assert snapshot.bias_state == "investigate"
