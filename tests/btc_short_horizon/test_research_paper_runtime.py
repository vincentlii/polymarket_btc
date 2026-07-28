from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.forward import AdmittedEventBuffer
from btc_short_horizon.live.paper_execution import PaperExecutionConfig, PaperMarketRules
from btc_short_horizon.live.paper_runtime import ResearchPaperRuntime
import btc_short_horizon.live.paper_runtime as paper_runtime_module
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


@pytest.mark.asyncio
async def test_paper_bootstrap_anchors_to_first_admitted_closed_kline(
    tmp_path,
    monkeypatch,
) -> None:
    anchor = _closed_binance_kline(second=0)
    buffer = AdmittedEventBuffer(max_events=10)
    buffer.publish(anchor)
    bootstrap = BinanceKlineHistory(
        open_ts_ns=np.array([T0_NS - 1_000_000_000], dtype=np.int64),
        close=np.array([64750.0]),
        volume=np.array([0.0]),
        quote_volume=np.array([0.0]),
        taker_buy_volume=np.array([0.0]),
        interval_seconds=1,
    )
    observed: dict[str, datetime] = {}

    async def fetch_history(*, start_time, end_time, **_kwargs):  # type: ignore[no-untyped-def]
        observed["start"] = start_time
        observed["end"] = end_time
        return bootstrap

    monkeypatch.setattr(
        paper_runtime_module,
        "fetch_binance_spot_kline_history",
        fetch_history,
    )
    runtime = object.__new__(ResearchPaperRuntime)
    runtime.project = SimpleNamespace(
        research_timing=SimpleNamespace(max_feature_lookback_seconds=3_600)
    )
    runtime.event_buffer = buffer
    runtime.engine = _engine(tmp_path)
    runtime._recent_events = defaultdict(lambda: deque(maxlen=20_000))

    ready = await runtime._bootstrap_history(stop_event=asyncio.Event())

    assert ready is True
    assert observed == {"start": T0 - timedelta(seconds=3_600), "end": T0}
    assert runtime.engine.kline_history is not None
    assert runtime.engine.kline_history.open_ts_ns.tolist() == [
        T0_NS - 1_000_000_000,
        T0_NS,
    ]


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


def test_paper_runtime_surfaces_prediction_failure_until_a_successful_decision() -> None:
    class PredictorFailureEngine:
        def __init__(self) -> None:
            self.market = _market()
            self.fail = True
            self.result = "book_unavailable"

        def decide(self, *, now_ts_ns: int) -> str:
            assert now_ts_ns >= T0_NS
            if self.fail:
                raise ValueError("causal Binance history contains a kline gap")
            return self.result

    runtime = object.__new__(ResearchPaperRuntime)
    runtime.engine = PredictorFailureEngine()
    runtime.project = SimpleNamespace(
        maker=SimpleNamespace(
            entry_end_seconds=180.0,
            max_work_seconds=15.0,
            signal_cadence_seconds=5.0,
        )
    )
    runtime._next_decision_ns = T0_NS + 5_000_000_000
    runtime._prediction_errors = 0
    runtime._recoverable_errors = {}
    runtime._last_prediction_error = None
    runtime._last_decision_result = "not_started"

    runtime._run_due_decision(T0 + timedelta(seconds=5))

    assert runtime._prediction_errors == 1
    assert runtime._recoverable_errors == {
        "prediction": "ValueError: causal Binance history contains a kline gap"
    }
    assert runtime._last_prediction_error == {
        "market_slug": _market().slug,
        "observed_at": (T0 + timedelta(seconds=5)).isoformat(),
        "message": "ValueError: causal Binance history contains a kline gap",
    }

    runtime.engine.fail = False
    runtime._run_due_decision(T0 + timedelta(seconds=10))

    assert runtime._last_decision_result == "book_unavailable"
    assert "prediction" in runtime._recoverable_errors

    runtime.engine.result = "unsafe_prediction"
    runtime._run_due_decision(T0 + timedelta(seconds=15))

    assert runtime._last_decision_result == "unsafe_prediction"
    assert "prediction" not in runtime._recoverable_errors


def test_paper_runtime_retires_old_market_books_when_catalog_advances() -> None:
    previous = _market()
    current_t0 = previous.t1
    current = MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(current_t0),
        condition_id="0x" + "cd" * 32,
        up_token_id="3",
        down_token_id="4",
        t0=current_t0,
        t1=current_t0 + timedelta(minutes=15),
        rule_epoch=previous.rule_epoch,
        rule_hash="b" * 64,
    )
    runtime = object.__new__(ResearchPaperRuntime)
    runtime._markets = {previous.slug: previous}
    runtime._rules = {previous.slug: {UP: _rules(UP), DOWN: _rules(DOWN)}}
    runtime._next_rule_retry = {previous.slug: T0}
    runtime._rule_tasks = {}
    runtime._recoverable_errors = {f"rules:{previous.slug}": "temporary failure"}
    runtime._active_slug = previous.slug
    runtime._next_decision_ns = T0_NS + 5_000_000_000
    runtime._recent_events = defaultdict(lambda: deque(maxlen=20_000))
    runtime._recent_events[previous.up_token_id].append(_book(UP, bid="0.40", ask="0.42", second=5))
    runtime._recent_events["BTCUSDT"].append(_closed_binance_kline(second=0))
    scheduled: list[str] = []
    runtime._schedule_rule_fetch = lambda market, *, now: scheduled.append(market.slug)

    runtime.register_markets(current, None)

    assert tuple(runtime._markets) == (current.slug,)
    assert previous.slug not in runtime._rules
    assert previous.up_token_id not in runtime._recent_events
    assert "BTCUSDT" in runtime._recent_events
    assert runtime._recoverable_errors == {}
    assert runtime._active_slug is None
    assert runtime._next_decision_ns is None
    assert scheduled == [current.slug]
