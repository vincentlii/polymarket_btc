from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from prediction_market_extensions.adapters.prediction_market import (
    LoadedReplay,
    ReplayCoverageStats,
    ReplayWindow,
)
from prediction_market_extensions.backtesting._execution_config import ExecutionModelConfig
from prediction_market_extensions.backtesting._market_data_config import MarketDataConfig

from btc_short_horizon.backtest import (
    BtcJointReplayConfig,
    build_btc_joint_backtest,
    collect_btc_order_events,
    to_opening_mispricing_signal,
)
from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.models import OpeningMispricingPrediction
from btc_short_horizon.strategy import MakerStrategyConfig


T0 = datetime(2026, 4, 13, tzinfo=UTC)


def _market() -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(T0),
        condition_id="condition",
        up_token_id="up-token",
        down_token_id="down-token",
        t0=T0,
        t1=T0 + timedelta(minutes=15),
        rule_epoch="chainlink-v1",
        rule_hash="a" * 64,
        resolution=MarketOutcome.UP,
        label_available_ts=T0 + timedelta(minutes=16),
    )


def _signal() -> object:
    return to_opening_mispricing_signal(
        OpeningMispricingPrediction(
            market_slug=_market().slug,
            model_version="model-v1",
            feature_schema_hash="schema-v1",
            market_window_start_ts_ns=int(T0.timestamp() * 1_000_000_000),
            trigger_ts_ns=int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000),
            p_up=0.64,
            p_boundary_up=0.62,
            p_market_mid_up=0.60,
            data_age_seconds=0.1,
        )
    )


def _config(**overrides: object) -> BtcJointReplayConfig:
    values: dict[str, object] = {
        "market": _market(),
        "start_time": T0,
        "end_time": T0 + timedelta(seconds=60),
        "up_token_index": 0,
        "down_token_index": 1,
        "signals": (_signal(),),
        "maker": MakerStrategyConfig(max_shares=5.0),
        "execution": ExecutionModelConfig(queue_position=True),
    }
    values.update(overrides)
    return BtcJointReplayConfig(**values)


def _loaded(token_index: int, instrument_id: str) -> LoadedReplay:
    replay = SimpleNamespace(token_index=token_index)
    return LoadedReplay(
        replay=replay,
        instrument=SimpleNamespace(id=InstrumentId.from_str(instrument_id)),
        records=(),
        outcome=None,
        realized_outcome=None,
        metadata={},
        requested_window=ReplayWindow(),
        loaded_window=ReplayWindow(),
        coverage_stats=ReplayCoverageStats(
            count=1,
            count_key="book_events",
            market_key="slug",
            market_id="market",
        ),
    )


def test_joint_backtest_uses_two_book_replays_one_strategy_and_causal_signals() -> None:
    config = _config()
    backtest = build_btc_joint_backtest(
        name="btc-15m-opening-mispricing",
        data=MarketDataConfig(platform="polymarket", data_type="book", vendor="pmxt"),
        config=config,
    )

    assert [replay.token_index for replay in backtest.replays] == [0, 1]
    assert backtest.strategy_factory is None
    assert backtest.joint_strategy_factory is not None
    assert backtest.auxiliary_data_factory is not None
    assert backtest.auxiliary_data_factory(()) == config.signals
    assert collect_btc_order_events(backtest) == ()
    strategy = backtest.joint_strategy_factory(
        (_loaded(0, "UP.POLYMARKET"), _loaded(1, "DOWN.POLYMARKET"))
    )
    assert strategy.config.up_instrument_id == InstrumentId.from_str("UP.POLYMARKET")
    assert strategy.config.down_instrument_id == InstrumentId.from_str("DOWN.POLYMARKET")


def test_joint_replay_rejects_non_queue_execution_and_mismatched_signals() -> None:
    with pytest.raises(ValueError, match="queue_position"):
        _config(execution=ExecutionModelConfig(queue_position=False))

    mismatched = _signal()
    mismatched.market_slug = "other-market"
    with pytest.raises(ValueError, match="belong to the replay market"):
        _config(signals=(mismatched,))
