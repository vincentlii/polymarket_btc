from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import numpy as np
import pytest

from btc_short_horizon.config import PaperExecutionVariantConfig, load_btc_project_config
from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.data.forward import AdmittedEventBuffer
from btc_short_horizon.live.paper_execution import PaperExecutionConfig, PaperMarketRules
from btc_short_horizon.live.paper_runtime import (
    ResearchPaperRuntime,
    build_research_paper_portfolio,
)
from btc_short_horizon.live.paper_replay import (
    build_replay_binance_history,
    replay_research_paper,
)
import btc_short_horizon.live.paper_runtime as paper_runtime_module
import scripts.btc_research_paper_replay as paper_replay_script
from btc_short_horizon.live.research_paper import (
    _bounded_equity_curve,
    PaperLedgerSnapshot,
    PaperLedgerStore,
    PaperRuleSnapshotStore,
    PaperTradeRecord,
    ResearchPaperEngine,
    ResearchPaperPortfolio,
)
from btc_short_horizon.live.dashboard_state import EquityPoint, StrategyStage
from btc_short_horizon.live.settlement_trades import (
    PublicSettlementTradesClient,
    SettlementTrade,
    SettlementTradeEvidenceStore,
)
from btc_short_horizon.models import OpeningMispricingPrediction
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_proxy import CausalFeatureUnavailableError
from btc_short_horizon.strategy import LayerStructure, MakerStrategyConfig


T0 = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
T0_NS = int(T0.timestamp() * 1_000_000_000)
UP = "1"
DOWN = "2"
CONDITION = "0x" + "ab" * 32


def test_dashboard_equity_curve_is_bounded_without_losing_endpoints() -> None:
    points = tuple(
        EquityPoint(timestamp=T0 + timedelta(seconds=index), equity=1_000.0 + index)
        for index in range(2_501)
    )

    bounded = _bounded_equity_curve(points)

    assert len(bounded) <= 2_000
    assert bounded[0] == points[0]
    assert bounded[-1] == points[-1]


def test_paper_ledger_rejects_research_identity_change_within_epoch(tmp_path) -> None:
    original = PaperLedgerStore(
        tmp_path,
        "paper-v8",
        "primary",
        research_identity={"model_sha256": "a" * 64},
    )
    original.write(PaperLedgerSnapshot(starting_balance=1_000.0, records=[]))

    changed = PaperLedgerStore(
        tmp_path,
        "paper-v8",
        "primary",
        research_identity={"model_sha256": "b" * 64},
    )
    with pytest.raises(ValueError, match="research identity mismatch"):
        changed.read()


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


def _trade(token: str, *, price: str, size: str, milliseconds: int) -> RawCollectorEvent:
    available = T0 + timedelta(milliseconds=milliseconds)
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=available,
            collector_receive_ts=available,
            available_ts=available,
            sequence_or_hash=f"trade-{token}-{milliseconds}",
            source="polymarket_clob",
            instrument=token,
            schema_version="polymarket-market-ws-v1",
            ingest_version="test-v1",
        ),
        event_type="last_trade_price",
        payload={
            "event_type": "last_trade_price",
            "asset_id": token,
            "price": price,
            "size": size,
            "side": "SELL",
            "timestamp": str(int(available.timestamp() * 1_000)),
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


def _variant(
    variant_id: str = "maker_15s",
    *,
    mode: str = "maker",
    maker_work_seconds: float = 15.0,
    primary: bool = True,
    opportunity_policy: str = "shared_maker",
    confirmation_policy: str = "side_only",
    confirmation_signals: int = 2,
    maximum_edge_decay: float = 0.0,
    maker_expiry_policy: str = "fixed_duration",
    maker_fill_evidence_policy: str = "live_stream",
) -> PaperExecutionVariantConfig:
    uses_independent_filter = opportunity_policy == "independent_taker"
    return PaperExecutionVariantConfig(
        variant_id=variant_id,
        label=variant_id.replace("_", " ").title(),
        mode=mode,
        maker_work_seconds=maker_work_seconds,
        primary=primary,
        minimum_taker_net_edge=0.03 if mode != "maker" or uses_independent_filter else 0.0,
        slippage_buffer=0.005 if mode != "maker" or uses_independent_filter else 0.0,
        model_uncertainty_buffer=0.03 if mode != "maker" or uses_independent_filter else 0.0,
        opportunity_policy=opportunity_policy,
        confirmation_policy=confirmation_policy,
        confirmation_signals=confirmation_signals,
        maximum_edge_decay=maximum_edge_decay,
        maker_expiry_policy=maker_expiry_policy,
        maker_fill_evidence_policy=maker_fill_evidence_policy,
    )


def _engine(
    tmp_path,
    *,
    predictor=_predict,
    starting_balance: float = 1_000.0,
    variant: PaperExecutionVariantConfig | None = None,
    taker_latency_ms: float = 50.0,
) -> ResearchPaperEngine:  # type: ignore[no-untyped-def]
    selected_variant = variant or _variant()
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
            taker_latency_ms=taker_latency_ms,
            trade_volume_multiplier=0.5,
        ),
        variant=selected_variant,
        ledger_store=PaperLedgerStore(
            tmp_path,
            "test-paper-v2",
            selected_variant.variant_id,
        ),
        starting_balance=starting_balance,
        clob_capture_end_seconds=215.0,
        tail_entry_price_threshold=0.35,
        evidence_target_markets=300,
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
    assert engine.records[0].execution_status == "working"
    assert engine.records[0].opportunity_id == engine.records[0].placement_id
    assert engine.records[0].entry_regime == "early_3s_to_30s"
    assert engine.records[0].price_bucket == "core"
    assert engine.records[0].go_eligible is True
    assert [item.signal_number for item in engine.records[0].signal_observations] == [1, 2, 3]


@pytest.mark.parametrize(
    ("stage_seconds", "expected_regime"),
    (
        ((5, 10, 15), "early_3s_to_30s"),
        ((40, 45, 50), "price_discovery_35s_to_90s"),
        ((100, 105, 110), "mid_early_95s_to_180s"),
    ),
)
@pytest.mark.parametrize("required_signals", (1, 2, 3))
def test_variant_confirmation_count_is_exact_in_every_stage(
    tmp_path,
    stage_seconds: tuple[int, int, int],
    expected_regime: str,
    required_signals: int,
) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            f"independent_fak_{required_signals}x5s",
            mode="immediate_fak",
            maker_work_seconds=0.0,
            opportunity_policy="independent_taker",
            confirmation_policy=("edge_stable" if required_signals == 3 else "side_only"),
            confirmation_signals=required_signals,
            maximum_edge_decay=(0.01 if required_signals == 3 else 0.0),
        ),
    )
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})

    results: list[str] = []
    for second in stage_seconds[:required_signals]:
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        results.append(engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000))

    assert results == ["confirmation_pending"] * (required_signals - 1) + ["submitted"]
    assert len(engine.records) == 1
    assert engine.records[0].entry_regime == expected_regime
    assert len(engine.records[0].signal_observations) == required_signals


def test_market_activation_persists_frozen_rules_for_replay(tmp_path) -> None:
    market = _market()
    rules = {UP: _rules(UP), DOWN: _rules(DOWN)}
    engine = _engine(tmp_path)

    engine.activate_market(market, rules=rules)

    restored = PaperRuleSnapshotStore(tmp_path, "test-paper-v2").read(market)
    assert restored == rules

    conflicting = dict(rules)
    conflicting[UP] = PaperMarketRules(
        condition_id=CONDITION,
        token_id=UP,
        tick_size="0.001",
        minimum_order_size=1.0,
        neg_risk=False,
        maker_fee_rate_bps=0,
        observed_at_ns=T0_NS - 1,
    )
    with pytest.raises(ValueError, match="immutable rule snapshot conflict"):
        PaperRuleSnapshotStore(tmp_path, "test-paper-v2").write(market, conflicting)


def test_market_reactivation_reuses_existing_frozen_rules_after_restart(tmp_path) -> None:
    market = _market()
    initial = {UP: _rules(UP), DOWN: _rules(DOWN)}
    _engine(tmp_path).activate_market(market, rules=initial)

    refreshed = {
        token_id: replace(rule, observed_at_ns=rule.observed_at_ns + 1_000_000_000)
        for token_id, rule in initial.items()
    }
    restarted = _engine(tmp_path)

    restarted.activate_market(market, rules=refreshed)

    assert restarted.rules == initial


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "unsupported Research Paper rule snapshot schema"),
        ("market.rule_hash", "b" * 64, "rule snapshot market identity mismatch"),
        ("rules.0.rules_sha256", "0" * 64, "paper market rules hash mismatch"),
    ],
)
def test_market_reactivation_fails_closed_for_invalid_frozen_snapshot(
    tmp_path,
    field: str,
    value: object,
    message: str,
) -> None:
    market = _market()
    rules = {UP: _rules(UP), DOWN: _rules(DOWN)}
    store = PaperRuleSnapshotStore(tmp_path, "test-paper-v2")
    store.write(market, rules)
    path = store._path(market.slug)
    raw = json.loads(path.read_text(encoding="utf-8"))
    target: object = raw
    for part in field.split(".")[:-1]:
        target = target[int(part)] if part.isdigit() else target[part]
    target[field.rsplit(".", 1)[-1]] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _engine(tmp_path).activate_market(market, rules=rules)


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


def test_trade_event_activates_pending_order_before_matching(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    engine.on_event(_trade(UP, price="0.40", size="210", milliseconds=10_600))

    assert engine.active_placement is not None
    assert engine.active_placement.status == "filled"
    assert engine.records[0].filled_shares == pytest.approx(5.0)


def test_trade_at_cancel_ack_is_processed_before_the_cancel_tie_break(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)
    placement = engine.active_placement
    assert placement is not None
    engine.advance(now_ts_ns=T0_NS + 10_550_000_000)
    engine.simulator.request_cancel(
        placement,
        now_ts_ns=T0_NS + 10_600_000_000,
        reason="test_cancel",
    )

    engine.on_event(_trade(UP, price="0.40", size="210", milliseconds=10_700))

    assert placement.status == "filled"
    assert placement.cancel_race_filled_size == pytest.approx(5.0)
    assert engine.records[0].filled_shares == pytest.approx(5.0)


def test_quiet_book_cancels_working_order_at_the_frozen_age_limit(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    engine.advance(now_ts_ns=T0_NS + 11_200_000_000)

    assert engine.active_placement is not None
    assert engine.active_placement.status == "cancel_pending"
    assert engine.active_placement.terminal_reason == "book_stale_connection_unobserved"


def test_quiet_book_marks_submitted_fak_execution_evidence_invalid(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "immediate_fak",
            mode="immediate_fak",
            maker_work_seconds=0.0,
        ),
        taker_latency_ms=800.0,
    )
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    engine.advance(now_ts_ns=T0_NS + 11_300_000_000)

    assert engine.active_placement is not None
    assert engine.active_placement.status == "evidence_invalid"
    assert engine.active_placement.terminal_reason == "book_stale_connection_unobserved"
    assert engine.records[0].filled_shares == 0.0
    assert engine.records[0].execution_evidence_valid is False
    assert engine.records[0].settlement_status == "evidence_invalid"

    engine.settle(
        market_slug=_market().slug,
        outcome=MarketOutcome.UP,
        label_available_ts_ns=int(_market().t1.timestamp() * 1_000_000_000),
    )

    assert engine.records[0].realized_pnl is None
    assert engine.variant_performance().resolved_opportunity_count == 0


def test_paper_trade_record_fails_closed_without_execution_evidence_flag(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)
    raw = engine.records[0].to_json()
    raw.pop("execution_evidence_valid")

    with pytest.raises(ValueError, match="execution_evidence_valid"):
        PaperTradeRecord.from_json(raw)


def test_market_rotation_retires_terminal_execution_state(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "immediate_fak",
            mode="immediate_fak",
            maker_work_seconds=0.0,
        ),
    )
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)
    engine.advance(now_ts_ns=T0_NS + 10_550_000_000)
    assert engine.active_placement is not None
    assert engine.active_placement.status == "filled"
    assert len(engine.simulator.placements) == 1
    next_t0 = T0 + timedelta(minutes=15)
    next_market = replace(
        _market(),
        slug=BTC_15M_MARKET_FAMILY.slug_for(next_t0),
        t0=next_t0,
        t1=next_t0 + timedelta(minutes=15),
    )

    engine.activate_market(next_market, rules={UP: _rules(UP), DOWN: _rules(DOWN)})

    assert engine.market == next_market
    assert engine.simulator.placements == ()


def test_market_rotation_rejects_an_unfinished_execution_cycle(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)
    next_t0 = T0 + timedelta(minutes=15)
    next_market = replace(
        _market(),
        slug=BTC_15M_MARKET_FAMILY.slug_for(next_t0),
        t0=next_t0,
        t1=next_t0 + timedelta(minutes=15),
    )

    with pytest.raises(RuntimeError, match="unfinished paper placement"):
        engine.activate_market(next_market, rules={UP: _rules(UP), DOWN: _rules(DOWN)})

    assert engine.market == _market()


def test_regime_boundary_accepts_only_configured_scheduler_tolerance(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (25, 30):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        result = engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 100_000_000)

    assert result == "submitted"
    assert engine.records[0].entry_regime == "early_3s_to_30s"


def test_direction_evidence_uses_the_same_boundary_tolerance_as_execution(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "independent_fak_1x5s",
            mode="immediate_fak",
            maker_work_seconds=0.0,
            opportunity_policy="independent_taker",
            confirmation_signals=1,
        ),
    )
    portfolio = ResearchPaperPortfolio((engine,))
    portfolio.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    portfolio.on_event(_book(UP, bid="0.40", ask="0.42", second=29))
    portfolio.on_event(_book(DOWN, bid="0.56", ask="0.58", second=29))

    result = portfolio.decide(now_ts_ns=T0_NS + 30_058_000_000)

    assert result == "submitted"
    summary = portfolio.direction_store.snapshot()
    assert summary.prediction_count == 1
    assert [item.stage for item in summary.stage_summaries] == ["early_3s_to_30s"]


@pytest.mark.parametrize(
    ("confirmation_policy", "maximum_edge_decay"),
    (("side_only", 0.0), ("edge_stable", 0.02)),
)
@pytest.mark.parametrize("seconds", ((25, 30, 35), (85, 90, 95)))
def test_confirmation_never_crosses_stage_boundary(
    tmp_path,
    confirmation_policy: str,
    maximum_edge_decay: float,
    seconds: tuple[int, int, int],
) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "independent",
            mode="immediate_fak",
            maker_work_seconds=0.0,
            opportunity_policy="independent_taker",
            confirmation_policy=confirmation_policy,
            confirmation_signals=3,
            maximum_edge_decay=maximum_edge_decay,
        ),
    )
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})

    results = []
    for second in seconds:
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        results.append(engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 100_000_000))

    assert results == ["confirmation_pending"] * 3
    assert engine.records == []


def test_regime_gap_is_not_silently_assigned_to_a_neighboring_segment(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for book_second, decision_second in ((26, 27), (31, 32)):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=book_second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=book_second))
        result = engine.decide(now_ts_ns=T0_NS + decision_second * 1_000_000_000)

    assert result == "outside_frozen_regime"
    assert engine.records == []


def test_portfolio_runs_three_execution_policies_on_the_same_events(tmp_path) -> None:
    prediction_calls = 0

    def counting_predictor(_market, _history, observation):  # type: ignore[no-untyped-def]
        nonlocal prediction_calls
        prediction_calls += 1
        return _prediction(observation)

    engines = (
        _engine(tmp_path, predictor=counting_predictor, variant=_variant()),
        _engine(
            tmp_path,
            predictor=counting_predictor,
            variant=_variant(
                "immediate_fak",
                mode="immediate_fak",
                maker_work_seconds=0.0,
                primary=False,
            ),
        ),
        _engine(
            tmp_path,
            predictor=counting_predictor,
            variant=_variant(
                "maker_5s_then_fak",
                mode="maker_then_fak",
                maker_work_seconds=5.0,
                primary=False,
            ),
        ),
    )
    portfolio = ResearchPaperPortfolio(engines)
    portfolio.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        portfolio.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        portfolio.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        portfolio.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    snapshot = portfolio.dashboard_snapshot(now=T0 + timedelta(seconds=11))

    assert len(portfolio.records) == 3
    assert prediction_calls == 2
    assert set(portfolio.last_decisions) == {
        "maker_15s",
        "immediate_fak",
        "maker_5s_then_fak",
    }
    assert snapshot.performance is not None
    assert len(snapshot.performance.variant_summaries) == 3
    assert len(snapshot.performance.recent_orders) == 3
    assert len({record.opportunity_id for record in portfolio.records}) == 1
    assert snapshot.performance.decision_funnel is not None
    assert snapshot.performance.decision_funnel.opportunities == 1
    assert snapshot.performance.decision_funnel.placements == 1


def test_immediate_fak_uses_same_confirmation_then_executes_once_after_latency(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "immediate_fak",
            mode="immediate_fak",
            maker_work_seconds=0.0,
        ),
    )
    market = _market()
    engine.activate_market(market, rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        result = engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    assert result == "submitted"
    assert engine.active_placement is not None
    assert engine.active_placement.status == "fak_pending"
    assert engine.active_placement.layers == ()
    assert engine.records[0].execution_route == "direct_fak"

    engine.advance(now_ts_ns=T0_NS + 10_550_000_000)
    engine.settle(
        market_slug=market.slug,
        outcome=MarketOutcome.UP,
        label_available_ts_ns=int(market.t1.timestamp() * 1_000_000_000),
    )

    assert engine.active_placement.status == "filled"
    assert engine.records[0].taker_filled_shares == pytest.approx(5.0)
    assert engine.records[0].fak_client_latency_ms == pytest.approx(50.0)
    assert engine.records[0].fak_server_delay_ms == 0.0
    assert engine.records[0].fak_total_latency_ms == pytest.approx(50.0)
    assert engine.records[0].model_version == "proxy-model"
    assert PaperTradeRecord.from_json(engine.records[0].to_json()) == engine.records[0]
    performance = engine.variant_performance()
    assert performance.resolved_opportunity_count == 1
    assert performance.resolved_ev_per_opportunity == pytest.approx(2.9)
    assert performance.core_resolved_ev_per_opportunity == pytest.approx(2.9)
    assert performance.tail_resolved_ev_per_opportunity is None
    assert performance.conditional_ev_per_filled_share == pytest.approx(0.58)


def test_independent_fak_submits_when_shared_maker_gate_has_no_plan(tmp_path) -> None:
    control = _engine(
        tmp_path,
        predictor=lambda _market, _history, observation: _prediction(observation, p_up=0.63),
        variant=_variant("control", primary=True),
    )
    challenger = _engine(
        tmp_path,
        predictor=lambda _market, _history, observation: _prediction(observation, p_up=0.63),
        variant=_variant(
            "independent_fak_2x5s",
            mode="immediate_fak",
            maker_work_seconds=0.0,
            primary=False,
            opportunity_policy="independent_taker",
        ),
    )
    portfolio = ResearchPaperPortfolio((control, challenger))
    portfolio.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        portfolio.on_event(_book(UP, bid="0.54", ask="0.55", second=second))
        portfolio.on_event(_book(DOWN, bid="0.43", ask="0.45", second=second))
        portfolio.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    assert control.records == []
    assert portfolio.last_decisions["control"] == "no_passive_price_with_required_edge"
    assert portfolio.last_decisions["independent_fak_2x5s"] == "submitted"
    assert len(challenger.records) == 1
    assert challenger.records[0].execution_route == "direct_fak"
    assert challenger.records[0].side == "up"
    summary = next(
        item
        for item in portfolio.dashboard_snapshot(
            now=T0 + timedelta(seconds=11)
        ).performance.variant_summaries
        if item.variant_id == "independent_fak_2x5s"
    )
    assert summary.evaluation_count == 2
    assert summary.qualified_signal_count == 2
    assert summary.opportunity_count == 1
    assert summary.fill_count == 0
    by_side = {item.side: item for item in summary.direction_summaries}
    assert by_side["up"].qualified_signal_count == 2
    assert by_side["up"].opportunity_count == 1
    assert by_side["up"].fill_count == 0
    assert by_side["down"].qualified_signal_count == 0
    assert by_side["down"].opportunity_count == 0
    diagnostic_paths = tuple(
        (tmp_path / "paper" / "epochs" / "test-paper-v2").glob(
            "variants/independent_fak_2x5s/diagnostics/*.json"
        )
    )
    assert len(diagnostic_paths) == 1
    diagnostic = json.loads(diagnostic_paths[0].read_text(encoding="utf-8"))
    assert diagnostic["market_slug"] == _market().slug
    assert [item["decision_ts_ns"] for item in diagnostic["entries"]] == [
        T0_NS + 5_500_000_000,
        T0_NS + 10_500_000_000,
    ]
    assert all(len(item["evaluations"]) == 2 for item in diagnostic["entries"])


def test_independent_maker_reuses_1x5_filter_and_works_until_market_end(tmp_path) -> None:
    maker = _engine(
        tmp_path,
        predictor=lambda _market, _history, observation: _prediction(observation, p_up=0.68),
        variant=_variant(
            "independent_maker_1x5s_market_end",
            mode="maker",
            maker_work_seconds=0.0,
            opportunity_policy="independent_taker",
            confirmation_signals=1,
            maker_expiry_policy="market_end",
            maker_fill_evidence_policy="settlement_trades",
        ),
    )
    fak = _engine(
        tmp_path,
        predictor=lambda _market, _history, observation: _prediction(observation, p_up=0.68),
        variant=_variant(
            "independent_fak_1x5s",
            mode="immediate_fak",
            maker_work_seconds=0.0,
            primary=False,
            opportunity_policy="independent_taker",
            confirmation_signals=1,
        ),
    )
    portfolio = ResearchPaperPortfolio((maker, fak))
    market = _market()
    portfolio.activate_market(market, rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    portfolio.on_event(_book(UP, bid="0.58", ask="0.59", second=5))
    portfolio.on_event(_book(DOWN, bid="0.40", ask="0.41", second=5))

    portfolio.decide(now_ts_ns=T0_NS + 5_500_000_000)
    maker.advance(now_ts_ns=T0_NS + 5_550_000_000)

    assert maker.records[0].side == fak.records[0].side == "up"
    assert maker.records[0].execution_route == "maker"
    assert maker.active_placement is not None
    assert maker.active_placement.plan.expires_ts_ns == int(market.t1.timestamp() * 1_000_000_000)
    assert maker.active_placement.status == "working"

    maker.advance(now_ts_ns=T0_NS + 300_200_000_000)

    assert maker.active_placement.status == "working"
    maker.advance(now_ts_ns=int(market.t1.timestamp() * 1_000_000_000))
    maker.advance(now_ts_ns=int(market.t1.timestamp() * 1_000_000_000) + 100_000_000)
    assert maker.requires_settlement_trade_evidence(market.slug)
    record = maker.records[0]
    assert record.maker_limit_price is not None
    maker.apply_settlement_trade_evidence(
        market_slug=market.slug,
        trades=(
            SettlementTrade(
                token_id=UP,
                price=record.maker_limit_price,
                size=210.0,
                timestamp_seconds=int((T0 + timedelta(seconds=400)).timestamp()),
                evidence_id="a" * 64,
            ),
        ),
        assessed_at_ns=T0_NS + 901_000_000_000,
    )

    assert record.filled_shares == 5.0
    assert record.maker_filled_shares == 5.0
    assert record.execution_status == "filled"
    assert not maker.requires_settlement_trade_evidence(market.slug)


def test_baseline_portfolio_contains_only_the_two_enabled_1x_variants(tmp_path) -> None:
    project = load_btc_project_config(Path("configs/btc_short_horizon/baseline.toml"))
    enabled_variant_ids = {
        "independent_fak_1x5s",
        "independent_fak_1x5s_2_0",
    }
    disabled_variant_ids = {
        "independent_maker_1x5s_market_end",
        "independent_fak_2x5s",
        "independent_fak_stable_3x5s",
    }

    class Predictor:
        model_id = "proxy-model"
        market_relative_model_id = "market-relative-v1"
        include_interval = False

        def __call__(self, market, history, observation):  # type: ignore[no-untyped-def]
            prediction = _prediction(observation)
            return (
                replace(
                    prediction,
                    market_relative_model_version="market-relative-v1",
                    market_relative_feature_schema_hash="c" * 64,
                    market_relative_p_up=0.20,
                    market_relative_p_up_lower=0.15,
                    market_relative_p_up_upper=0.25,
                )
                if self.include_interval
                else prediction
            )

    predictor = Predictor()
    portfolio = build_research_paper_portfolio(
        project=project,
        predictor=predictor,  # type: ignore[arg-type]
        runtime_root=tmp_path,
        starting_balance=1_000.0,
    )
    assert [engine.variant.variant_id for engine in portfolio.engines] == [
        "independent_fak_1x5s",
        "independent_fak_1x5s_2_0",
    ]
    assert portfolio.primary.variant.variant_id == "independent_fak_1x5s_2_0"
    initial_ledgers = tuple(
        tmp_path.glob(f"paper/epochs/{project.paper_execution_epoch}/variants/*/ledger.json")
    )
    assert len(initial_ledgers) == 2
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 6
        for path in initial_ledgers
    )
    portfolio.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    portfolio.on_event(_book(UP, bid="0.40", ask="0.42", second=5))
    portfolio.on_event(_book(DOWN, bid="0.56", ask="0.58", second=5))
    assert portfolio.decide(now_ts_ns=T0_NS + 5_500_000_000) == "probability_interval_unavailable"
    assert (
        portfolio.last_decisions["independent_fak_1x5s_2_0"] == "probability_interval_unavailable"
    )
    predictor.include_interval = True
    portfolio.on_event(_book(UP, bid="0.40", ask="0.42", second=10))
    portfolio.on_event(_book(DOWN, bid="0.56", ask="0.58", second=10))
    assert portfolio.decide(now_ts_ns=T0_NS + 10_500_000_000) == "submitted"
    assert portfolio.last_decisions["independent_fak_1x5s_2_0"] == "submitted"
    legacy = next(item for item in portfolio.records if item.variant_id == "independent_fak_1x5s")
    challenger = next(
        item for item in portfolio.records if item.variant_id == "independent_fak_1x5s_2_0"
    )
    assert legacy.side == "up"
    assert challenger.side == "down"
    assert legacy.model_version == "proxy-model"
    assert challenger.model_version == "market-relative-v1"
    portfolio.checkpoint_evaluations()

    snapshot = portfolio.dashboard_snapshot(now=T0 + timedelta(seconds=6))

    assert {record.variant_id for record in portfolio.records} == enabled_variant_ids
    assert snapshot.performance is not None
    assert snapshot.strategy.model_id == "market-relative-v1"
    assert snapshot.strategy.challenger_model_id == "proxy-model"
    assert {
        item.variant_id for item in snapshot.performance.variant_summaries
    } == enabled_variant_ids
    robust_summary = next(
        item
        for item in snapshot.performance.variant_summaries
        if item.variant_id == "independent_fak_1x5s_2_0"
    )
    assert dict(robust_summary.rejection_counts) == {"probability_interval_unavailable": 1}
    assert robust_summary.mean_gross_edge is not None
    assert robust_summary.mean_fee_per_share is not None
    assert robust_summary.mean_net_edge is not None
    assert portfolio.requires_settlement_trade_evidence(_market().slug) is False

    epoch_root = tmp_path / "paper" / "epochs" / project.paper_execution_epoch
    assert {path.name for path in (epoch_root / "variants").iterdir()} == enabled_variant_ids
    with sqlite3.connect(epoch_root / "evaluations.sqlite3") as connection:
        persisted_evaluation_variants = {
            row[0] for row in connection.execute("SELECT DISTINCT variant_id FROM evaluations")
        }
    assert persisted_evaluation_variants == enabled_variant_ids
    frozen_rules = (epoch_root / "rules" / f"{_market().slug}.json").read_text(encoding="utf-8")
    assert all(variant_id not in frozen_rules for variant_id in disabled_variant_ids)


def test_replay_final_decision_horizon_ignores_disabled_maker_variants() -> None:
    project = load_btc_project_config(Path("configs/btc_short_horizon/baseline.toml"))

    final_decision_ns = paper_replay_script._final_decision_ts_ns(project, T0_NS)

    assert final_decision_ns == T0_NS + 180_000_000_000


def test_public_settlement_trade_client_validates_deduplicates_and_persists(tmp_path) -> None:
    market = _market()
    timestamp = int((T0 + timedelta(seconds=300)).timestamp())
    row = {
        "asset": UP,
        "conditionId": CONDITION,
        "side": "SELL",
        "price": 0.58,
        "size": 12.0,
        "timestamp": timestamp,
        "transactionHash": "0xtrade",
        "proxyWallet": "0xwallet",
    }

    async def run() -> object:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["market"] == CONDITION
            assert request.url.params["side"] == "SELL"
            assert request.url.params["takerOnly"] == "true"
            return httpx.Response(200, json=[row, row])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await PublicSettlementTradesClient().fetch(
                market,
                start_seconds=timestamp - 1,
                end_seconds=timestamp + 1,
                client=client,
            )

    evidence = asyncio.run(run())
    assert len(evidence.trades) == 1  # type: ignore[union-attr]
    path = SettlementTradeEvidenceStore(tmp_path, "paper-v7").write(evidence)  # type: ignore[arg-type]
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["trade_count"] == 1
    assert persisted["trades"][0]["token_id"] == UP


def test_balance_rejection_is_a_zero_fill_confirmed_opportunity(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        starting_balance=1.0,
        variant=_variant(
            "immediate_fak",
            mode="immediate_fak",
            maker_work_seconds=0.0,
        ),
    )
    portfolio = ResearchPaperPortfolio((engine,))
    portfolio.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        portfolio.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        portfolio.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        result = portfolio.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    assert result == "insufficient_virtual_balance"
    assert len(engine.records) == 1
    assert engine.records[0].execution_status == "rejected"
    assert engine.records[0].filled_shares == 0.0
    snapshot = portfolio.dashboard_snapshot(now=T0 + timedelta(seconds=11))
    assert snapshot.performance is not None
    assert snapshot.performance.decision_funnel is not None
    assert snapshot.performance.decision_funnel.opportunities == 1
    assert snapshot.performance.decision_funnel.placements == 0
    assert snapshot.performance.decision_funnel.rejected == 1


def test_event_after_fak_latency_cannot_reprice_prior_execution(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "immediate_fak",
            mode="immediate_fak",
            maker_work_seconds=0.0,
        ),
    )
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    engine.on_event(_book(UP, bid="0.88", ask="0.90", second=11))

    assert engine.active_placement is not None
    assert engine.active_placement.status == "filled"
    assert engine.records[0].taker_filled_shares == pytest.approx(5.0)
    assert engine.records[0].taker_filled_notional == pytest.approx(2.1)


def test_causal_replay_runs_unordered_events_through_the_live_portfolio_seam(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "immediate_fak",
            mode="immediate_fak",
            maker_work_seconds=0.0,
        ),
    )
    portfolio = ResearchPaperPortfolio((engine,))
    chronological = [
        _book(UP, bid="0.40", ask="0.42", second=5),
        _book(DOWN, bid="0.56", ask="0.58", second=5),
        _book(UP, bid="0.40", ask="0.42", second=10),
        _book(DOWN, bid="0.56", ask="0.58", second=10),
        _book(UP, bid="0.88", ask="0.90", second=11),
    ]
    admitted = tuple(
        event.with_admission_sequence(index) for index, event in enumerate(chronological, start=1)
    )

    result = replay_research_paper(
        portfolio=portfolio,
        market=_market(),
        rules={UP: _rules(UP), DOWN: _rules(DOWN)},
        events=tuple(reversed(admitted)),
        decision_ts_ns=(T0_NS + 5_500_000_000, T0_NS + 10_500_000_000),
        replay_end_ts_ns=T0_NS + 11_100_000_000,
        outcome=MarketOutcome.UP,
        label_available_ts_ns=int(_market().t1.timestamp() * 1_000_000_000),
    )

    assert [item.result for item in result.decisions] == [
        "confirmation_pending",
        "submitted",
    ]
    assert result.processed_event_count == 5
    assert len(portfolio.records) == 1
    assert portfolio.records[0].execution_status == "filled"
    assert portfolio.records[0].realized_pnl == pytest.approx(2.9)


def test_replay_binance_bootstrap_requires_contiguous_fresh_closed_bars() -> None:
    history = build_replay_binance_history(
        (_closed_binance_kline(second=-2), _closed_binance_kline(second=-1)),
        cutoff_ts_ns=T0_NS,
        minimum_bars=2,
    )

    assert history.open_ts_ns.tolist() == [
        T0_NS - 2_000_000_000,
        T0_NS - 1_000_000_000,
    ]
    with pytest.raises(ValueError, match="kline gap"):
        build_replay_binance_history(
            (_closed_binance_kline(second=-3), _closed_binance_kline(second=-1)),
            cutoff_ts_ns=T0_NS,
            minimum_bars=2,
        )


def test_tail_price_is_recorded_but_excluded_from_initial_go_bucket(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "immediate_fak",
            mode="immediate_fak",
            maker_work_seconds=0.0,
        ),
    )
    market = _market()
    engine.activate_market(market, rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10):
        engine.on_event(_book(UP, bid="0.17", ask="0.19", second=second))
        engine.on_event(_book(DOWN, bid="0.79", ask="0.81", second=second))
        engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)
    engine.advance(now_ts_ns=T0_NS + 10_550_000_000)
    engine.settle(
        market_slug=market.slug,
        outcome=MarketOutcome.UP,
        label_available_ts_ns=int(market.t1.timestamp() * 1_000_000_000),
    )

    assert engine.records[0].price_bucket == "tail_low"
    assert engine.records[0].go_eligible is False
    performance = engine.variant_performance()
    assert performance.resolved_ev_per_opportunity == pytest.approx(4.05)
    assert performance.core_resolved_ev_per_opportunity is None
    assert performance.tail_resolved_ev_per_opportunity == pytest.approx(4.05)


def test_maker_then_fak_rechecks_probability_after_cancel_ack(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        variant=_variant(
            "maker_5s_then_fak",
            mode="maker_then_fak",
            maker_work_seconds=5.0,
        ),
    )
    engine.activate_market(_market(), rules={UP: _rules(UP), DOWN: _rules(DOWN)})
    for second in (5, 10, 15):
        engine.on_event(_book(UP, bid="0.40", ask="0.42", second=second))
        engine.on_event(_book(DOWN, bid="0.56", ask="0.58", second=second))
        result = engine.decide(now_ts_ns=T0_NS + second * 1_000_000_000 + 500_000_000)

    assert result == "fak_cancel_pending"
    assert engine.active_placement is not None
    engine.advance(now_ts_ns=T0_NS + 15_600_000_000)
    assert engine.active_placement.status == "fak_pending"
    engine.advance(now_ts_ns=T0_NS + 15_650_000_000)

    assert engine.active_placement.status == "filled"
    assert engine.records[0].execution_route == "maker_then_fak"
    assert engine.records[0].taker_filled_shares == pytest.approx(5.0)
    assert engine.records[0].terminal_reason == "fak_filled"
    assert engine.records[0].fak_client_latency_ms == pytest.approx(50.0)
    assert engine.records[0].fak_server_delay_ms == 0.0
    assert engine.records[0].fak_total_latency_ms == pytest.approx(50.0)


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
    assert engine.records[0].execution_status == "cancel_pending"


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
    assert len(engine.records) == 1
    assert engine.records[0].execution_status == "rejected"
    assert engine.records[0].terminal_reason == "insufficient_virtual_balance"
    assert engine.active_placement is None
    snapshot = engine.dashboard_snapshot(now=T0 + timedelta(seconds=11))
    assert snapshot.performance is not None
    assert snapshot.performance.order_count == 0
    assert snapshot.performance.variant_summaries[0].opportunity_count == 1


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
    restored = PaperLedgerStore(tmp_path, "test-paper-v2", "maker_15s").read()

    assert snapshot.run_mode == "research_paper"
    assert snapshot.strategy is not None
    assert snapshot.strategy.stage is StrategyStage.PAPER
    assert snapshot.performance is not None
    assert snapshot.performance.realized_pnl == 3.0
    assert snapshot.performance.equity == 1_003.0
    assert snapshot.performance.recent_orders[0].execution_status == "filled"
    assert snapshot.performance.recent_orders[0].settlement_status == "resolved"
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
    assert restarted.records[0].execution_status == "recovery_canceled"


def test_paper_runtime_surfaces_prediction_failure_until_a_successful_decision() -> None:
    class PredictorFailureEngine:
        def __init__(self) -> None:
            self.market = _market()
            self.fail = True
            self.result = "book_unavailable"

        def advance(self, *, now_ts_ns: int) -> None:
            assert now_ts_ns >= T0_NS

        def decide(self, *, now_ts_ns: int) -> str:
            assert now_ts_ns >= T0_NS
            if self.fail:
                raise CausalFeatureUnavailableError(
                    "causal Binance history contains a kline gap; "
                    "decision_ts_ns=1 available_tail_ts_ns=0"
                )
            return self.result

    runtime = object.__new__(ResearchPaperRuntime)
    runtime.engine = PredictorFailureEngine()
    runtime.project = SimpleNamespace(
        maker=SimpleNamespace(
            entry_end_seconds=180.0,
            max_work_seconds=15.0,
            signal_cadence_seconds=5.0,
        ),
        paper_execution_variants=(SimpleNamespace(maker_work_seconds=15.0, enabled=True),),
    )
    runtime._next_decision_ns = T0_NS + 5_000_000_000
    runtime._prediction_errors = 0
    runtime._recoverable_errors = {}
    runtime._last_prediction_error = None
    runtime._last_decision_result = "not_started"

    runtime._run_due_decision(T0 + timedelta(seconds=5))

    assert runtime._prediction_errors == 1
    assert runtime._recoverable_errors == {
        "prediction": (
            "CausalFeatureUnavailableError: causal Binance history contains a kline gap; "
            "decision_ts_ns=1 available_tail_ts_ns=0"
        )
    }
    assert runtime._last_prediction_error == {
        "market_slug": _market().slug,
        "observed_at": (T0 + timedelta(seconds=5)).isoformat(),
        "message": (
            "CausalFeatureUnavailableError: causal Binance history contains a kline gap; "
            "decision_ts_ns=1 available_tail_ts_ns=0"
        ),
    }

    runtime._run_due_decision(T0 + timedelta(seconds=10))

    assert runtime._prediction_errors == 1

    runtime.engine.fail = False
    runtime._run_due_decision(T0 + timedelta(seconds=15))

    assert runtime._last_decision_result == "book_unavailable"
    assert "prediction" in runtime._recoverable_errors

    runtime.engine.result = "unsafe_prediction"
    runtime._run_due_decision(T0 + timedelta(seconds=20))

    assert runtime._last_decision_result == "unsafe_prediction"
    assert "prediction" not in runtime._recoverable_errors
    assert runtime._prediction_errors == 1
    assert runtime._last_prediction_error is None

    runtime.engine.fail = True
    runtime._run_due_decision(T0 + timedelta(seconds=25))

    assert runtime._prediction_errors == 2


def test_paper_runtime_fails_closed_for_a_non_data_prediction_value_error() -> None:
    class InvariantFailureEngine:
        market = _market()

        def advance(self, *, now_ts_ns: int) -> None:
            assert now_ts_ns >= T0_NS

        def decide(self, *, now_ts_ns: int) -> str:
            assert now_ts_ns >= T0_NS
            raise ValueError("model artifact invariant failed")

    runtime = object.__new__(ResearchPaperRuntime)
    runtime.engine = InvariantFailureEngine()
    runtime.project = SimpleNamespace(
        maker=SimpleNamespace(
            entry_end_seconds=180.0,
            max_work_seconds=15.0,
            signal_cadence_seconds=5.0,
        ),
        paper_execution_variants=(SimpleNamespace(maker_work_seconds=15.0, enabled=True),),
    )
    runtime._next_decision_ns = T0_NS + 5_000_000_000

    with pytest.raises(ValueError, match="model artifact invariant failed"):
        runtime._run_due_decision(T0 + timedelta(seconds=5))


def test_paper_runtime_advances_latency_deadlines_between_signal_ticks() -> None:
    class RecordingEngine:
        def __init__(self) -> None:
            self.market = _market()
            self.advanced: list[int] = []

        def advance(self, *, now_ts_ns: int) -> None:
            self.advanced.append(now_ts_ns)

    runtime = object.__new__(ResearchPaperRuntime)
    runtime.engine = RecordingEngine()
    runtime._next_decision_ns = T0_NS + 10_000_000_000

    runtime._run_due_decision(T0 + timedelta(seconds=7))

    assert runtime.engine.advanced == [T0_NS + 7_000_000_000]


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
