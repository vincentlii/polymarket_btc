"""Assembly of one BTC 15-minute dual-token L2 replay into NautilusTrader."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Sequence

from prediction_market_extensions.adapters.prediction_market import LoadedReplay
from prediction_market_extensions.backtesting._execution_config import ExecutionModelConfig
from prediction_market_extensions.backtesting._market_data_config import MarketDataConfig
from prediction_market_extensions.backtesting._prediction_market_backtest import (
    PredictionMarketBacktest,
)
from prediction_market_extensions.backtesting._replay_specs import BookReplay

from btc_short_horizon.backtest.signals import (
    BtcOpeningMispricingSignal,
    validate_opening_mispricing_signal,
)
from btc_short_horizon.backtest.strategy import (
    BtcOpeningMispricingConfig,
    BtcOpeningMispricingStrategy,
)
from btc_short_horizon.data import MarketWindow
from btc_short_horizon.strategy import MakerStrategyConfig


_AUDIT_PROVIDER_ATTRIBUTE = "_btc_order_events_provider"


def _as_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class BtcJointReplayConfig:
    """All explicit inputs required to replay one 15-minute BTC market pair."""

    market: MarketWindow
    start_time: datetime
    end_time: datetime
    up_token_index: int
    down_token_index: int
    signals: tuple[BtcOpeningMispricingSignal, ...]
    maker: MakerStrategyConfig
    execution: ExecutionModelConfig
    initial_cash: float = 100.0
    probability_window: int = 30

    def __post_init__(self) -> None:
        if self.market.family.is_collection_only:
            raise ValueError("collection-only market families cannot create a trading replay")
        start_time = _as_utc(self.start_time, "start_time")
        end_time = _as_utc(self.end_time, "end_time")
        if start_time >= end_time:
            raise ValueError("start_time must precede end_time")
        if min(self.up_token_index, self.down_token_index) < 0:
            raise ValueError("token indexes must be non-negative")
        if self.up_token_index == self.down_token_index:
            raise ValueError("up_token_index and down_token_index must differ")
        if not self.execution.queue_position:
            raise ValueError("BTC maker replay requires queue_position=True")
        if self.initial_cash <= 0.0:
            raise ValueError("initial_cash must be > 0")
        if self.probability_window < 1:
            raise ValueError("probability_window must be >= 1")
        start_ns = int(start_time.timestamp() * 1_000_000_000)
        end_ns = int(end_time.timestamp() * 1_000_000_000)
        for signal in self.signals:
            validate_opening_mispricing_signal(signal)
            if signal.market_slug != self.market.slug:
                raise ValueError("every signal must belong to the replay market")
            if not start_ns <= signal.ts_init <= end_ns:
                raise ValueError("signal ts_init must lie within the replay window")
        object.__setattr__(self, "start_time", start_time)
        object.__setattr__(self, "end_time", end_time)


def build_btc_joint_backtest(
    *,
    name: str,
    data: MarketDataConfig,
    config: BtcJointReplayConfig,
) -> PredictionMarketBacktest:
    """Build a one-market joint replay without duplicating Nautilus' engine."""

    if not name or not name.strip():
        raise ValueError("name is required")
    replays = (
        BookReplay(
            market_slug=config.market.slug,
            token_index=config.up_token_index,
            start_time=config.start_time,
            end_time=config.end_time,
        ),
        BookReplay(
            market_slug=config.market.slug,
            token_index=config.down_token_index,
            start_time=config.start_time,
            end_time=config.end_time,
        ),
    )

    created_strategy: BtcOpeningMispricingStrategy | None = None

    def joint_strategy_factory(loaded_sims: Sequence[LoadedReplay]) -> BtcOpeningMispricingStrategy:
        nonlocal created_strategy
        instruments_by_token_index = {
            int(getattr(loaded.replay, "token_index")): loaded.instrument.id
            for loaded in loaded_sims
        }
        try:
            up_instrument_id = instruments_by_token_index[config.up_token_index]
            down_instrument_id = instruments_by_token_index[config.down_token_index]
        except KeyError as exc:
            raise ValueError("joint replay did not load both configured token indexes") from exc
        created_strategy = BtcOpeningMispricingStrategy(
            BtcOpeningMispricingConfig(
                market_slug=config.market.slug,
                up_instrument_id=up_instrument_id,
                down_instrument_id=down_instrument_id,
                max_shares=Decimal(str(config.maker.max_shares)),
                layer_structure=config.maker.structure.value,
                safety_buffer=config.maker.safety_buffer,
                minimum_edge=config.maker.minimum_edge,
                maker_fee_per_share=config.maker.maker_fee_per_share,
                entry_start_seconds=config.maker.entry_start_seconds,
                entry_end_seconds=config.maker.entry_end_seconds,
                edge_persistence_seconds=config.maker.edge_persistence_seconds,
                max_work_seconds=config.maker.max_work_seconds,
                stale_after_seconds=config.maker.stale_after_seconds,
                cancel_probability_drop=config.maker.cancel_probability_drop,
                price_level_tick_offsets=config.maker.price_level_tick_offsets,
            )
        )
        return created_strategy

    backtest = PredictionMarketBacktest(
        name=name,
        data=data,
        replays=replays,
        joint_strategy_factory=joint_strategy_factory,
        auxiliary_data_factory=lambda loaded_sims: config.signals,
        initial_cash=config.initial_cash,
        probability_window=config.probability_window,
        nautilus_log_level="INFO",
        execution=config.execution,
        return_summary_series=True,
    )
    setattr(
        backtest,
        _AUDIT_PROVIDER_ATTRIBUTE,
        lambda: () if created_strategy is None else created_strategy.order_audit_events,
    )
    return backtest


def collect_btc_order_events(
    backtest: PredictionMarketBacktest,
) -> tuple[dict[str, object], ...]:
    """Return strategy lifecycle events captured by a BTC joint replay, if it has run."""

    provider = getattr(backtest, _AUDIT_PROVIDER_ATTRIBUTE, None)
    if not callable(provider):
        return ()
    events = provider()
    if not isinstance(events, tuple):
        return ()
    return tuple(dict(event) for event in events if isinstance(event, dict))
