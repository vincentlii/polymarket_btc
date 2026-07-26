from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.live.paper_execution import PaperExecutionConfig, PaperMarketRules
from btc_short_horizon.live.research_paper import PaperLedgerStore, ResearchPaperEngine
from btc_short_horizon.models import OpeningMispricingPrediction
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.strategy import LayerStructure, MakerStrategyConfig


T0 = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
T0_NS = int(T0.timestamp() * 1_000_000_000)
UP = "1"
DOWN = "2"
CONDITION = "0x" + "ab" * 32


def _market() -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(T0),
        condition_id=CONDITION,
        up_token_id=UP,
        down_token_id=DOWN,
        t0=T0,
        t1=T0 + timedelta(minutes=15),
        rule_epoch="btc-chainlink-v1",
        rule_hash="a" * 64,
    )


def _rules(token: str) -> PaperMarketRules:
    return PaperMarketRules(
        condition_id=CONDITION,
        token_id=token,
        tick_size="0.01",
        minimum_order_size=1.0,
        neg_risk=False,
        maker_fee_rate_bps=0,
        observed_at_ns=T0_NS - 1,
    )


def _book(token: str, *, bid: str, ask: str, second: int) -> RawCollectorEvent:
    available = T0 + timedelta(seconds=second, milliseconds=100)
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=available,
            collector_receive_ts=available,
            available_ts=available,
            sequence_or_hash=f"book-{token}-{second}",
            source="polymarket_clob",
            instrument=token,
            schema_version="polymarket-market-ws-v1",
            ingest_version="test-v1",
        ),
        event_type="book",
        payload={
            "event_type": "book",
            "asset_id": token,
            "timestamp": str(int(available.timestamp() * 1_000)),
            "hash": f"book-{token}-{second}",
            "bids": [{"price": bid, "size": "100"}],
            "asks": [{"price": ask, "size": "100"}],
        },
        collector_session_id="paper-test",
        epoch_id=1,
    )


def _closed_binance_kline(*, second: int, close: str = "64754.55000000") -> RawCollectorEvent:
    available = T0 + timedelta(seconds=second, milliseconds=50)
    open_ms = int((T0 + timedelta(seconds=second)).timestamp() * 1_000)
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=available,
            collector_receive_ts=available,
            available_ts=available,
            sequence_or_hash=f"kline:{open_ms}",
            source="binance_spot",
            instrument="BTCUSDT",
            schema_version="binance-kline-1s-v1",
            ingest_version="test-v1",
        ),
        event_type="kline_1s",
        payload={
            "stream": "btcusdt@kline_1s",
            "data": {
                "e": "kline",
                "s": "BTCUSDT",
                "k": {
                    "t": open_ms,
                    "c": close,
                    "v": "0.00443000",
                    "q": "286.86262030",
                    "V": "0.00081000",
                    "x": True,
                },
            },
        },
        collector_session_id="paper-test",
        epoch_id=1,
    )


def _prediction(observation, *, p_up: float = 0.70):  # type: ignore[no-untyped-def]
    return OpeningMispricingPrediction(
        market_slug=observation.market_slug,
        model_version="proxy-model",
        feature_schema_hash="b" * 64,
        market_window_start_ts_ns=T0_NS,
        trigger_ts_ns=observation.decision_ts_ns,
        p_up=p_up,
        p_boundary_up=0.60,
        p_market_mid_up=observation.p_market_mid_up,
        data_age_seconds=observation.data_age_seconds,
    )


def _predict(_market, _history, observation):  # type: ignore[no-untyped-def]
    return _prediction(observation)


def _engine(
    tmp_path,
    *,
    predictor=_predict,
    starting_balance: float = 1_000.0,
) -> ResearchPaperEngine:  # type: ignore[no-untyped-def]
    return ResearchPaperEngine(
        predictor=predictor,
        model_id="proxy-model",
        maker_config=MakerStrategyConfig(
            structure=LayerStructure.SINGLE,
            max_shares=5.0,
            safety_buffer=0.01,
            minimum_edge=0.10,
            entry_start_seconds=3.0,
            entry_end_seconds=180.0,
            confirmation_signals=2,
            signal_cadence_seconds=5.0,
            signal_cadence_tolerance_seconds=0.25,
            max_work_seconds=15.0,
            stale_after_seconds=1.0,
            cancel_probability_drop=0.03,
            max_visible_depth_fraction=0.05,
            price_level_tick_offsets=(0,),
        ),
        execution_config=PaperExecutionConfig(
            insert_latency_ms=50.0,
            cancel_latency_ms=100.0,
            trade_volume_multiplier=0.5,
        ),
        ledger_store=PaperLedgerStore(tmp_path),
        starting_balance=starting_balance,
    )


def test_research_paper_requires_two_live_cadence_signals_and_one_cycle(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    engine.on_event(_book(UP, bid="0.40", ask="0.42", second=5))
    engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=5))

    first = engine.decide(now_ts_ns=T0_NS + 5_500_000_000)
    engine.on_event(_book(UP, bid="0.40", ask="0.42", second=10))
    engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=10))
    second = engine.decide(now_ts_ns=T0_NS + 10_500_000_000)
    engine.on_event(_book(UP, bid="0.40", ask="0.42", second=15))
    engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=15))
    third = engine.decide(now_ts_ns=T0_NS + 15_500_000_000)

    assert first == "confirmation_pending"
    assert second == "submitted"
    assert third == "continue"
    assert len(engine.records) == 1
    assert engine.records[0].status == "working"


def test_research_paper_accepts_binance_decimal_strings_from_admitted_wire_event(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.kline_history = BinanceKlineHistory(
        open_ts_ns=np.array([T0_NS - 1_000_000_000], dtype=np.int64),
        close=np.array([64750.0]),
        volume=np.array([0.0]),
        quote_volume=np.array([0.0]),
        taker_buy_volume=np.array([0.0]),
        interval_seconds=1,
    )

    engine.on_event(_closed_binance_kline(second=0))

    assert engine.kline_history.close.tolist() == [64750.0, 64754.55]
    assert engine.kline_history.volume[-1] == 0.00443


def test_research_paper_reevaluates_working_order_and_cancels_probability_drop(tmp_path) -> None:
    probabilities = iter((0.70, 0.70, 0.60))

    def changing_predictor(_market, _history, observation):  # type: ignore[no-untyped-def]
        return _prediction(observation, p_up=next(probabilities))

    engine = _engine(tmp_path, predictor=changing_predictor)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)
    engine.on_event(_book(UP, bid="0.40", ask="0.42", second=15))
    engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=15))

    result = engine.decide(now_ts_ns=T0_NS + 15_500_000_000)

    assert result == "probability_drop"
    assert engine.active_placement is not None
    assert engine.active_placement.status == "cancel_pending"
    assert engine.records[0].status == "cancel_pending"


def test_research_paper_reserves_actual_working_order_notional(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    snapshot = engine.dashboard_snapshot(now=T0 + timedelta(seconds=11))

    assert engine.records[0].planned_notional == 2.0
    assert snapshot.performance is not None
    assert snapshot.performance.available_balance == 998.0
    assert snapshot.performance.open_exposure == 2.0


def test_research_paper_rejects_plan_above_virtual_available_balance(tmp_path) -> None:
    engine = _engine(tmp_path, starting_balance=1.0)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        result = engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    assert result == "insufficient_virtual_balance"
    assert engine.records == []
    assert engine.active_placement is None


def test_unfilled_resolved_order_does_not_count_as_a_losing_trade(tmp_path) -> None:
    engine = _engine(tmp_path)
    market = _market()
    engine.activate_market(market, rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)
    engine.settle(
        market_slug=market.slug,
        outcome=MarketOutcome.DOWN,
        label_available_ts_ns=int(market.t1.timestamp() * 1_000_000_000),
    )

    performance = engine.dashboard_snapshot(now=market.t1).performance

    assert performance is not None
    assert performance.fill_count == 0
    assert performance.win_rate is None


def test_research_paper_settlement_is_persisted_and_projected_as_simulated_pnl(tmp_path) -> None:
    engine = _engine(tmp_path)
    market = _market()
    engine.activate_market(market, rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    engine.on_event(_book(UP, bid="0.40", ask="0.42", second=5))
    engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=5))
    engine.decide(now_ts_ns=T0_NS + 5_500_000_000)
    engine.on_event(_book(UP, bid="0.40", ask="0.42", second=10))
    engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=10))
    engine.decide(now_ts_ns=T0_NS + 10_500_000_000)
    placement = engine.active_placement
    assert placement is not None
    engine.advance(now_ts_ns=placement.active_ts_ns)
    engine.on_trade(
        token_id=UP,
        aggressor_side="sell",
        price=0.40,
        size=210.0,
        available_ts_ns=placement.active_ts_ns + 1,
    )

    engine.settle(
        market_slug=market.slug,
        outcome=MarketOutcome.UP,
        label_available_ts_ns=int(market.t1.timestamp() * 1_000_000_000),
    )
    snapshot = engine.dashboard_snapshot(now=market.t1 + timedelta(seconds=1))
    restored = PaperLedgerStore(tmp_path).read()

    assert snapshot.run_mode == "research_paper"
    assert snapshot.performance is not None
    assert snapshot.performance.realized_pnl == 3.0
    assert snapshot.performance.equity == 1_003.0
    assert snapshot.performance.recent_trades[0].status == "simulated_resolved"
    assert restored is not None
    assert restored.records[0].realized_pnl == 3.0


def test_research_paper_restart_never_reuses_a_market_cycle(tmp_path) -> None:
    first = _engine(tmp_path)
    first.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    first.on_event(_book(UP, bid="0.40", ask="0.42", second=5))
    first.on_event(_book(DOWN, bid="0.56", ask="0.58", second=5))
    first.decide(now_ts_ns=T0_NS + 5_500_000_000)
    first.on_event(_book(UP, bid="0.40", ask="0.42", second=10))
    first.on_event(_book(DOWN, bid="0.56", ask="0.58", second=10))
    first.decide(now_ts_ns=T0_NS + 10_500_000_000)

    restarted = _engine(tmp_path)
    restarted.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})

    assert restarted.decide(now_ts_ns=T0_NS + 15_000_000_000) == "placement_cycle_used"
    assert restarted.records[0].status == "recovery_canceled"
