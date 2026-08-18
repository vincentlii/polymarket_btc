from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from nautilus_trader.model.data import CustomData, TradeTick
from nautilus_trader.model.enums import AggressorSide
from nautilus_trader.model.identifiers import InstrumentId, TradeId
from nautilus_trader.model.objects import Price, Quantity

from prediction_market_extensions.backtesting import _prediction_market_backtest as backtest_module
from prediction_market_extensions.backtesting._backtest_runtime import (
    BACKTEST_CUSTOM_DATA_CLIENT_ID,
    add_engine_data_by_type,
    build_backtest_run_state,
    print_backtest_result_warnings,
)
from prediction_market_extensions.backtesting._execution_config import (
    ExecutionModelConfig,
    SameTimestampPriority,
    StaticLatencyConfig,
)
from prediction_market_extensions.backtesting._prediction_market_backtest import (
    PredictionMarketBacktest,
)
from prediction_market_extensions.backtesting._prediction_market_runner import MarketDataConfig
from prediction_market_extensions.backtesting._replay_specs import BookReplay


class _FakeMessageBus:
    def __init__(self) -> None:
        self.handlers = {}
        self.published: list[tuple[str, str, bool]] = []

    def subscribe(self, topic, handler):  # type: ignore[no-untyped-def]
        self.handlers[topic] = handler

    def publish(self, topic, message, external_pub=True):  # type: ignore[no-untyped-def]
        self.published.append((topic, message, external_pub))
        self.handlers[topic](message)


class _FakeLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)


class _EngineStub:
    def __init__(self, *, config) -> None:  # type: ignore[no-untyped-def]
        self.config = config
        self.venues: list[dict[str, object]] = []

    def add_venue(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.venues.append(kwargs)


def test_add_engine_data_by_type_splits_mixed_replay_records() -> None:
    class _BookRecord:
        instrument_id = "BOOK.POLYMARKET"

    class _TradeRecord:
        instrument_id = "TRADE.POLYMARKET"

    class _DataEngineStub:
        def __init__(self) -> None:
            self.added: list[tuple[list[object], object | None, bool]] = []
            self.sort_calls = 0

        def add_data(  # type: ignore[no-untyped-def]
            self,
            records,
            *,
            client_id=None,
            sort=True,
        ):
            self.added.append((list(records), client_id, bool(sort)))

        def sort_data(self) -> None:
            self.sort_calls += 1

    trade = _TradeRecord()
    first_book = _BookRecord()
    second_book = _BookRecord()
    engine = _DataEngineStub()

    add_engine_data_by_type(engine, [trade, first_book, second_book])

    assert engine.added[0][0] == [trade]
    assert engine.added[1][0] == [first_book, second_book]
    assert all(client_id is None for _, client_id, _ in engine.added)
    assert all(sort is False for _, _, sort in engine.added)
    assert engine.sort_calls == 1


def test_add_engine_data_by_type_applies_explicit_priority_to_timestamp_ties() -> None:
    class _BookRecord:
        instrument_id = "BOOK.POLYMARKET"

    class _TradeRecord:
        instrument_id = "TRADE.POLYMARKET"

    class _DataEngineStub:
        def __init__(self) -> None:
            self.added_types: list[type[object]] = []

        def add_data(self, records, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            self.added_types.append(type(records[0]))

        def sort_data(self) -> None:
            pass

    engine = _DataEngineStub()

    add_engine_data_by_type(
        engine,
        [_BookRecord(), _TradeRecord()],
        preferred_type_order=(_TradeRecord, _BookRecord),
    )

    assert engine.added_types == [_TradeRecord, _BookRecord]


def test_add_engine_data_by_type_wraps_and_routes_custom_data() -> None:
    from btc_short_horizon.backtest import BtcReplayBoundary

    class _DataEngineStub:
        def __init__(self) -> None:
            self.added: list[tuple[list[object], object | None, bool]] = []
            self.sort_calls = 0

        def add_data(  # type: ignore[no-untyped-def]
            self,
            records,
            *,
            client_id=None,
            sort=True,
        ):
            self.added.append((list(records), client_id, bool(sort)))

        def sort_data(self) -> None:
            self.sort_calls += 1

    boundary = BtcReplayBoundary(
        market_slug="btc-updown-15m-1",
        ts_event=10,
        ts_init=10,
    )
    engine = _DataEngineStub()

    add_engine_data_by_type(engine, [boundary])

    records, client_id, sort = engine.added[0]
    assert len(records) == 1
    assert isinstance(records[0], CustomData)
    assert records[0].data is boundary
    assert client_id == BACKTEST_CUSTOM_DATA_CLIENT_ID
    assert sort is False
    assert engine.sort_calls == 1


def test_add_engine_data_by_type_does_not_sort_empty_records() -> None:
    class _DataEngineStub:
        def __init__(self) -> None:
            self.sort_calls = 0

        def add_data(self, records, *, sort=True):  # type: ignore[no-untyped-def]
            raise AssertionError("add_data should not be called")

        def sort_data(self) -> None:
            self.sort_calls += 1

    engine = _DataEngineStub()

    add_engine_data_by_type(engine, [])

    assert engine.sort_calls == 0


def test_execution_trade_volume_stress_rounds_down_without_mutating_original_tick() -> None:
    trade = TradeTick(
        instrument_id=InstrumentId.from_str("TOKEN.POLYMARKET"),
        price=Price(0.5, 2),
        size=Quantity(5.0, 2),
        aggressor_side=AggressorSide.SELLER,
        trade_id=TradeId("trade-1"),
        ts_event=10,
        ts_init=11,
    )

    stressed = backtest_module._records_for_execution(  # type: ignore[attr-defined]
        (trade,),
        ExecutionModelConfig(trade_execution_size_multiplier=0.5),
    )

    assert len(stressed) == 1
    assert float(stressed[0].size) == 2.5
    assert float(trade.size) == 5.0
    assert stressed[0].trade_id == trade.trade_id


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("slippage_ticks", True),
        ("entry_slippage_pct", float("nan")),
        ("exit_slippage_pct", True),
        ("prob_fill_on_limit", float("inf")),
        ("min_synthetic_book_size", True),
        ("synthetic_book_depth_multiplier", float("nan")),
    ),
)
def test_execution_config_rejects_non_finite_and_boolean_numeric_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ExecutionModelConfig(**{field: value})  # type: ignore[arg-type]


def test_prediction_market_backtest_build_engine_forwards_execution(monkeypatch):
    monkeypatch.setattr(backtest_module, "BacktestEngine", _EngineStub)

    backtest = PredictionMarketBacktest(
        name="demo",
        data=MarketDataConfig(platform="polymarket", data_type="book", vendor="pmxt"),
        replays=(BookReplay(market_slug="demo-market"),),
        strategy_factory=lambda instrument_id: SimpleNamespace(instrument_id=instrument_id),
        initial_cash=100.0,
        probability_window=16,
        execution=ExecutionModelConfig(
            queue_position=True,
            maker_rebates_enabled=False,
            trade_execution_size_multiplier=0.5,
            same_timestamp_priority=SameTimestampPriority.TRADE_BEFORE_BOOK,
            latency_model=StaticLatencyConfig(
                base_latency_ms=25.0,
                insert_latency_ms=10.0,
                update_latency_ms=5.0,
                cancel_latency_ms=2.0,
            ),
        ),
    )

    engine = backtest._build_engine()

    assert len(engine.venues) == 1
    venue_kwargs = engine.venues[0]
    assert venue_kwargs["queue_position"] is True
    assert venue_kwargs["liquidity_consumption"] is True
    assert venue_kwargs["fee_model"]._maker_rebates_enabled is False

    latency_model = venue_kwargs["latency_model"]
    assert latency_model is not None
    assert latency_model.base_latency_nanos == 25_000_000
    assert latency_model.insert_latency_nanos == 35_000_000
    assert latency_model.update_latency_nanos == 30_000_000
    assert latency_model.cancel_latency_nanos == 27_000_000
    assert engine.config.risk_engine.bypass is False


def test_emit_engine_status_uses_nautilus_message_bus() -> None:
    msgbus = _FakeMessageBus()
    logger = _FakeLogger()
    engine = SimpleNamespace(kernel=SimpleNamespace(msgbus=msgbus, logger=logger))

    backtest_module._emit_engine_status(engine, "demo status")

    assert msgbus.published == [
        (backtest_module.REPO_STATUS_TOPIC, "demo status", False),
    ]
    assert logger.messages == ["demo status"]


def test_build_backtest_run_state_marks_forced_stop_with_partial_coverage():
    data = [SimpleNamespace(ts_init=0), SimpleNamespace(ts_init=10), SimpleNamespace(ts_init=20)]

    state = build_backtest_run_state(
        data=data,
        backtest_end_ns=10,
        forced_stop=True,
    )

    assert state["terminated_early"] is True
    assert state["stop_reason"] == "account_error"
    assert state["planned_start"] == "1970-01-01T00:00:00+00:00"
    assert state["planned_end"] == "1970-01-01T00:00:00.000000020+00:00"
    assert state["loaded_start"] == "1970-01-01T00:00:00+00:00"
    assert state["loaded_end"] == "1970-01-01T00:00:00.000000020+00:00"
    assert state["simulated_through"] == "1970-01-01T00:00:00.000000010+00:00"
    assert state["coverage_ratio"] == 0.5
    assert state["requested_coverage_ratio"] == 0.5


def test_build_backtest_run_state_uses_requested_window_for_coverage():
    data = [SimpleNamespace(ts_init=10), SimpleNamespace(ts_init=20)]

    state = build_backtest_run_state(
        data=data, backtest_end_ns=20, forced_stop=False, requested_start_ns=0, requested_end_ns=30
    )

    assert state["terminated_early"] is False
    assert state["stop_reason"] is None
    assert state["planned_start"] == "1970-01-01T00:00:00+00:00"
    assert state["planned_end"] == "1970-01-01T00:00:00.000000030+00:00"
    assert state["loaded_start"] == "1970-01-01T00:00:00.000000010+00:00"
    assert state["loaded_end"] == "1970-01-01T00:00:00.000000020+00:00"
    assert state["simulated_through"] == "1970-01-01T00:00:00.000000020+00:00"
    assert state["coverage_ratio"] == 1.0
    assert state["requested_coverage_ratio"] == 2 / 3


def test_book_pmxt_runner_pins_passive_execution_heuristics(monkeypatch):
    from prediction_market_extensions.backtesting import _experiments

    captured: dict[str, object] = {}

    def capture_run_experiment(experiment):  # type: ignore[no-untyped-def]
        captured["experiment"] = experiment

    monkeypatch.setattr(_experiments, "run_experiment", capture_run_experiment)
    module = importlib.import_module("backtests.polymarket_book_ema_crossover")
    module.run()
    experiment = captured["experiment"]

    assert experiment.execution.queue_position is True

    latency_model = experiment.execution.build_latency_model()
    assert latency_model is not None
    assert latency_model.base_latency_nanos == 75_000_000
    assert latency_model.insert_latency_nanos == 85_000_000
    assert latency_model.update_latency_nanos == 80_000_000
    assert latency_model.cancel_latency_nanos == 80_000_000


def test_result_warnings_distinguish_loaded_and_requested_coverage(capsys):
    print_backtest_result_warnings(
        results=[
            {
                "terminated_early": True,
                "stop_reason": "incomplete_window",
                "slug": "demo-market",
                "simulated_through": "2026-03-24T06:00:00+00:00",
                "coverage_ratio": 0.5,
                "requested_coverage_ratio": 0.25,
            }
        ],
        market_key="slug",
    )

    output = capsys.readouterr().out
    assert "50.0% of the loaded-data window" in output
    assert "25.0% of the requested window" in output


def test_result_warnings_include_explicit_result_warning_messages(capsys):
    print_backtest_result_warnings(
        results=[
            {
                "terminated_early": False,
                "slug": "demo-market",
                "warnings": ["Settlement outcome exists after the replay window."],
            }
        ],
        market_key="slug",
    )

    output = capsys.readouterr().out
    assert "demo-market" in output
    assert "Settlement outcome exists after the replay window." in output
