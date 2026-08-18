from __future__ import annotations

from decimal import Decimal

import pytest
from nautilus_trader.adapters.polymarket import POLYMARKET_VENUE
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import pUSD
from nautilus_trader.model.data import (
    BookOrder,
    OrderBookDelta,
    OrderBookDeltas,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AccountType,
    AggressorSide,
    AssetClass,
    BookAction,
    BookType,
    OmsType,
    OrderSide,
    RecordFlag,
)
from nautilus_trader.model.identifiers import (
    InstrumentId,
    Symbol,
    TradeId,
    TraderId,
)
from nautilus_trader.model.instruments import BinaryOption
from nautilus_trader.model.objects import Money, Price, Quantity

from backtests.polymarket_btc_15m_opening_mispricing_maker import _validate_fill_ledger
from prediction_market_extensions.adapters.prediction_market.research import (
    _serialize_fill_events,
)
from prediction_market_extensions.adapters.polymarket.fee_model import PolymarketFeeModel
from prediction_market_extensions.backtesting._backtest_runtime import add_engine_data_by_type
from prediction_market_extensions.backtesting._execution_config import StaticLatencyConfig

from btc_short_horizon.backtest import BtcOpeningMispricingSignal, BtcReplayBoundary
from btc_short_horizon.backtest.strategy import (
    BtcOpeningMispricingConfig,
    BtcOpeningMispricingStrategy,
)


_T0 = 1_000_000_000


def _instrument(symbol: str, outcome: str) -> BinaryOption:
    raw_symbol = Symbol(symbol)
    return BinaryOption(
        instrument_id=InstrumentId(symbol=raw_symbol, venue=POLYMARKET_VENUE),
        raw_symbol=raw_symbol,
        asset_class=AssetClass.ALTERNATIVE,
        currency=pUSD,
        price_precision=2,
        size_precision=2,
        price_increment=Price(0.01, 2),
        size_increment=Quantity(0.01, 2),
        activation_ns=0,
        expiration_ns=_T0 + 900_000_000_000,
        min_quantity=Quantity(5, 2),
        maker_fee=Decimal(0),
        taker_fee=Decimal("0.07"),
        outcome=outcome,
        info={"tags": ["Crypto"]},
        ts_event=0,
        ts_init=0,
    )


def _book(instrument: BinaryOption, *, bid: float, ask: float, ts_ns: int) -> OrderBookDeltas:
    return _book_depth(instrument, bids=(bid,), asks=(ask,), ts_ns=ts_ns)


def _book_depth(
    instrument: BinaryOption,
    *,
    bids: tuple[float, ...],
    asks: tuple[float, ...],
    ts_ns: int,
) -> OrderBookDeltas:
    raw_levels = tuple((OrderSide.BUY, price) for price in bids) + tuple(
        (OrderSide.SELL, price) for price in asks
    )
    deltas = []
    for index, (side, price) in enumerate(raw_levels, start=1):
        flags = int(RecordFlag.F_SNAPSHOT) if index == 1 else 0
        if index == len(raw_levels):
            flags |= int(RecordFlag.F_LAST)
        deltas.append(
            OrderBookDelta(
                instrument.id,
                BookAction.ADD,
                BookOrder(
                    side,
                    instrument.make_price(price),
                    instrument.make_qty(100),
                    index,
                ),
                flags,
                index,
                ts_ns,
                ts_ns,
            )
        )
    return OrderBookDeltas(
        instrument.id,
        deltas,
    )


def _signal(ts_ns: int) -> BtcOpeningMispricingSignal:
    return BtcOpeningMispricingSignal(
        market_slug="btc-updown-15m-1",
        model_version="model-v1",
        feature_schema_hash="schema-v1",
        market_window_start_ts_ns=_T0,
        p_up=0.80,
        p_boundary_up=0.75,
        p_market_mid_up=0.50,
        data_age_seconds=0.0,
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


def _trade(instrument: BinaryOption, *, size: float, ts_ns: int, trade_id: str) -> TradeTick:
    return TradeTick(
        instrument_id=instrument.id,
        price=instrument.make_price(0.50),
        size=instrument.make_qty(size),
        aggressor_side=AggressorSide.SELLER,
        trade_id=TradeId(trade_id),
        ts_event=ts_ns,
        ts_init=ts_ns,
    )


def _engine(*, insert_latency_ms: int = 0, cancel_latency_ms: int = 0) -> BacktestEngine:
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("BACKTESTER-001"),
            logging=LoggingConfig(log_level="ERROR", bypass_logging=True),
        )
    )
    latency = StaticLatencyConfig(
        insert_latency_ms=insert_latency_ms,
        cancel_latency_ms=cancel_latency_ms,
    ).build_latency_model()
    engine.add_venue(
        venue=POLYMARKET_VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        base_currency=pUSD,
        starting_balances=[Money(100, pUSD)],
        fee_model=PolymarketFeeModel(maker_rebates_enabled=False),
        book_type=BookType.L2_MBP,
        latency_model=latency,
        liquidity_consumption=True,
        queue_position=True,
        bar_execution=False,
        trade_execution=True,
    )
    return engine


def test_partial_fill_cancels_remainder_but_cancel_latency_preserves_race_fill() -> None:
    up = _instrument("UP", "Up")
    down = _instrument("DOWN", "Down")
    strategy = BtcOpeningMispricingStrategy(
        BtcOpeningMispricingConfig(
            market_slug="btc-updown-15m-1",
            up_instrument_id=up.id,
            down_instrument_id=down.id,
            max_shares=Decimal(5),
            minimum_edge=0.10,
            max_visible_depth_fraction=0.05,
            max_work_seconds=15.0,
        )
    )
    engine = _engine(cancel_latency_ms=100)
    engine.add_instrument(up)
    engine.add_instrument(down)
    add_engine_data_by_type(
        engine,
        (
            _book(up, bid=0.50, ask=0.51, ts_ns=_T0 + 4_000_000_000),
            _book(down, bid=0.49, ask=0.50, ts_ns=_T0 + 4_000_000_000),
            _book(up, bid=0.50, ask=0.51, ts_ns=_T0 + 9_000_000_000),
            _book(down, bid=0.49, ask=0.50, ts_ns=_T0 + 9_000_000_000),
            _signal(_T0 + 5_000_000_000),
            _signal(_T0 + 10_000_000_000),
            _trade(
                up,
                size=102,
                ts_ns=_T0 + 11_000_000_000,
                trade_id="partial",
            ),
            _trade(
                up,
                size=1,
                ts_ns=_T0 + 11_050_000_000,
                trade_id="race",
            ),
            BtcReplayBoundary(
                market_slug="btc-updown-15m-1",
                ts_event=_T0 + 20_000_000_000,
                ts_init=_T0 + 20_000_000_000,
            ),
        ),
    )
    engine.add_strategy(strategy)
    try:
        engine.run()
        events = strategy.order_audit_events
        fills_report = engine.trader.generate_order_fills_report()
    finally:
        engine.reset()
        engine.dispose()

    fills = [event for event in events if event["event_type"] == "fill"]
    markouts = [event for event in events if event["event_type"] == "fill_markout"]
    plan = next(event for event in events if event["event_type"] == "plan")
    cancel_request = next(event for event in events if event["event_type"] == "cancel_request")
    cancel_ack = next(event for event in events if event["event_type"] == "cancel_ack")
    assert [event["size"] for event in fills] == [2.0, 1.0]
    assert len(fills_report) == 1
    assert all(event["liquidity_side"] == "MAKER" for event in fills)
    assert all(event["trade_id"] for event in fills)
    assert plan["entry_book_age_seconds_max"] == pytest.approx(1.0)
    assert cancel_request["ts_ns"] <= fills[1]["ts_ns"] < cancel_ack["ts_ns"]
    assert sorted(event["horizon_seconds"] for event in markouts) == [1, 1, 3, 3]
    assert all(event["available"] is True for event in markouts)
    assert all(event["book_ts_ns"] == _T0 + 9_000_000_000 for event in markouts)
    assert [event["book_age_seconds"] for event in markouts] == pytest.approx(
        [3.0, 3.05, 5.0, 5.05]
    )
    assert [event["markout"] for event in markouts] == pytest.approx([0.005] * 4)
    _validate_fill_ledger(
        results=(
            {
                "instrument_id": str(up.id),
                "fills": len(fills_report),
                "fill_events": _serialize_fill_events(
                    market_id="btc-updown-15m-1",
                    fills_report=fills_report,
                ),
            },
            {"instrument_id": str(down.id), "fills": 0, "fill_events": []},
        ),
        order_events=events,
    )


def test_insert_latency_rejects_post_only_order_that_becomes_marketable() -> None:
    up = _instrument("UP", "Up")
    down = _instrument("DOWN", "Down")
    strategy = BtcOpeningMispricingStrategy(
        BtcOpeningMispricingConfig(
            market_slug="btc-updown-15m-1",
            up_instrument_id=up.id,
            down_instrument_id=down.id,
            max_shares=Decimal(5),
            minimum_edge=0.10,
            max_visible_depth_fraction=0.05,
            max_work_seconds=15.0,
        )
    )
    engine = _engine(insert_latency_ms=100)
    engine.add_instrument(up)
    engine.add_instrument(down)
    add_engine_data_by_type(
        engine,
        (
            _book(up, bid=0.50, ask=0.51, ts_ns=_T0 + 4_000_000_000),
            _book(down, bid=0.49, ask=0.50, ts_ns=_T0 + 4_000_000_000),
            _book(up, bid=0.50, ask=0.51, ts_ns=_T0 + 9_000_000_000),
            _book(down, bid=0.49, ask=0.50, ts_ns=_T0 + 9_000_000_000),
            _signal(_T0 + 5_000_000_000),
            _signal(_T0 + 10_000_000_000),
            _book(up, bid=0.49, ask=0.50, ts_ns=_T0 + 10_050_000_000),
            BtcReplayBoundary(
                market_slug="btc-updown-15m-1",
                ts_event=_T0 + 20_000_000_000,
                ts_init=_T0 + 20_000_000_000,
            ),
        ),
    )
    engine.add_strategy(strategy)
    try:
        engine.run()
        events = strategy.order_audit_events
    finally:
        engine.reset()
        engine.dispose()

    assert any(event["event_type"] == "submit" for event in events)
    assert any(event["event_type"] == "rejected" for event in events)
    assert not any(event["event_type"] == "accepted" for event in events)
    assert not any(event["event_type"] == "fill" for event in events)


def test_synchronous_multi_layer_rejections_close_only_after_every_order_event() -> None:
    up = _instrument("UP", "Up")
    down = _instrument("DOWN", "Down")
    strategy = BtcOpeningMispricingStrategy(
        BtcOpeningMispricingConfig(
            market_slug="btc-updown-15m-1",
            up_instrument_id=up.id,
            down_instrument_id=down.id,
            max_shares=Decimal(10),
            layer_structure="two_level",
            minimum_edge=0.10,
            max_visible_depth_fraction=0.05,
            max_work_seconds=15.0,
            price_level_tick_offsets=(0, 1),
        )
    )
    engine = _engine(insert_latency_ms=100)
    engine.add_instrument(up)
    engine.add_instrument(down)
    add_engine_data_by_type(
        engine,
        (
            _book_depth(
                up,
                bids=(0.50, 0.49),
                asks=(0.51,),
                ts_ns=_T0 + 4_000_000_000,
            ),
            _book_depth(
                down,
                bids=(0.48, 0.47),
                asks=(0.49,),
                ts_ns=_T0 + 4_000_000_000,
            ),
            _book_depth(
                up,
                bids=(0.50, 0.49),
                asks=(0.51,),
                ts_ns=_T0 + 9_000_000_000,
            ),
            _book_depth(
                down,
                bids=(0.48, 0.47),
                asks=(0.49,),
                ts_ns=_T0 + 9_000_000_000,
            ),
            _signal(_T0 + 5_000_000_000),
            _signal(_T0 + 10_000_000_000),
            _book(up, bid=0.48, ask=0.49, ts_ns=_T0 + 10_050_000_000),
            BtcReplayBoundary(
                market_slug="btc-updown-15m-1",
                ts_event=_T0 + 20_000_000_000,
                ts_init=_T0 + 20_000_000_000,
            ),
        ),
    )
    engine.add_strategy(strategy)
    try:
        engine.run()
        events = strategy.order_audit_events
        phase = strategy._execution.phase  # type: ignore[attr-defined]
        active_orders = dict(strategy._active_orders)  # type: ignore[attr-defined]
    finally:
        engine.reset()
        engine.dispose()

    assert sum(event["event_type"] == "submit" for event in events) == 2
    assert sum(event["event_type"] == "rejected" for event in events) == 2
    assert phase.value == "rejected"
    assert not active_orders


def test_last_signal_cannot_leave_a_gtc_order_working_past_max_age() -> None:
    up = _instrument("UP", "Up")
    down = _instrument("DOWN", "Down")
    strategy = BtcOpeningMispricingStrategy(
        BtcOpeningMispricingConfig(
            market_slug="btc-updown-15m-1",
            up_instrument_id=up.id,
            down_instrument_id=down.id,
            max_shares=Decimal(5),
            minimum_edge=0.10,
            max_visible_depth_fraction=0.05,
            max_work_seconds=15.0,
        )
    )
    engine = _engine()
    engine.add_instrument(up)
    engine.add_instrument(down)
    add_engine_data_by_type(
        engine,
        (
            _book(up, bid=0.50, ask=0.51, ts_ns=_T0 + 4_000_000_000),
            _book(down, bid=0.49, ask=0.50, ts_ns=_T0 + 4_000_000_000),
            _book(up, bid=0.50, ask=0.51, ts_ns=_T0 + 9_000_000_000),
            _book(down, bid=0.49, ask=0.50, ts_ns=_T0 + 9_000_000_000),
            _signal(_T0 + 5_000_000_000),
            _signal(_T0 + 10_000_000_000),
            BtcReplayBoundary(
                market_slug="btc-updown-15m-1",
                ts_event=_T0 + 30_000_000_000,
                ts_init=_T0 + 30_000_000_000,
            ),
        ),
    )
    engine.add_strategy(strategy)
    try:
        engine.run()
        events = strategy.order_audit_events
        phase = strategy._execution.phase  # type: ignore[attr-defined]
        active_orders = dict(strategy._active_orders)  # type: ignore[attr-defined]
    finally:
        engine.reset()
        engine.dispose()

    cancel_request = next(event for event in events if event["event_type"] == "cancel_request")
    assert cancel_request["reason"] == "max_work_age"
    assert cancel_request["ts_ns"] == _T0 + 25_000_000_000
    assert any(event["event_type"] == "cancel_ack" for event in events)
    assert phase.value == "canceled"
    assert not active_orders


def test_stale_execution_books_cannot_create_a_maker_order() -> None:
    up = _instrument("UP", "Up")
    down = _instrument("DOWN", "Down")
    strategy = BtcOpeningMispricingStrategy(
        BtcOpeningMispricingConfig(
            market_slug="btc-updown-15m-1",
            up_instrument_id=up.id,
            down_instrument_id=down.id,
            max_shares=Decimal(5),
            minimum_edge=0.10,
            max_visible_depth_fraction=0.05,
            stale_after_seconds=1.0,
        )
    )
    engine = _engine()
    engine.add_instrument(up)
    engine.add_instrument(down)
    add_engine_data_by_type(
        engine,
        (
            _book(up, bid=0.50, ask=0.51, ts_ns=_T0 + 1_000_000_000),
            _book(down, bid=0.49, ask=0.50, ts_ns=_T0 + 1_000_000_000),
            _signal(_T0 + 5_000_000_000),
            _signal(_T0 + 10_000_000_000),
            BtcReplayBoundary(
                market_slug="btc-updown-15m-1",
                ts_event=_T0 + 20_000_000_000,
                ts_init=_T0 + 20_000_000_000,
            ),
        ),
    )
    engine.add_strategy(strategy)
    try:
        engine.run()
        events = strategy.order_audit_events
    finally:
        engine.reset()
        engine.dispose()

    assert not any(event["event_type"] == "plan" for event in events)
    assert not any(event["event_type"] == "submit" for event in events)
