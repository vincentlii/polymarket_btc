"""Assembly of one BTC 15-minute dual-token L2 replay into NautilusTrader."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from math import isfinite
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
    BtcReplayBoundary,
    validate_opening_mispricing_signal,
)
from btc_short_horizon.backtest.strategy import (
    BtcOpeningMispricingConfig,
    BtcOpeningMispricingStrategy,
)
from btc_short_horizon.data import MarketOutcome, MarketWindow
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
    up_records_sha256: str
    down_records_sha256: str
    initial_cash: float = 100.0
    probability_window: int = 30
    formal_grid_component: bool = False

    def __post_init__(self) -> None:
        if self.market.family.is_collection_only:
            raise ValueError("collection-only market families cannot create a trading replay")
        start_time = _as_utc(self.start_time, "start_time")
        end_time = _as_utc(self.end_time, "end_time")
        if start_time >= end_time:
            raise ValueError("start_time must precede end_time")
        if any(
            isinstance(index, bool) or not isinstance(index, int)
            for index in (self.up_token_index, self.down_token_index)
        ):
            raise TypeError("token indexes must be integers")
        if min(self.up_token_index, self.down_token_index) < 0:
            raise ValueError("token indexes must be non-negative")
        if self.up_token_index == self.down_token_index:
            raise ValueError("up_token_index and down_token_index must differ")
        if not self.execution.queue_position:
            raise ValueError("BTC maker replay requires queue_position=True")
        if (
            isinstance(self.initial_cash, bool)
            or not isfinite(self.initial_cash)
            or self.initial_cash <= 0.0
        ):
            raise ValueError("initial_cash must be finite and > 0")
        if (
            isinstance(self.probability_window, bool)
            or not isinstance(self.probability_window, int)
            or self.probability_window < 1
        ):
            raise ValueError("probability_window must be an integer >= 1")
        if not isinstance(self.formal_grid_component, bool):
            raise TypeError("formal_grid_component must be bool")
        for name in ("up_records_sha256", "down_records_sha256"):
            digest = getattr(self, name)
            normalized_digest = digest.casefold() if isinstance(digest, str) else ""
            if len(normalized_digest) != 64 or any(
                character not in "0123456789abcdef" for character in normalized_digest
            ):
                raise ValueError(f"{name} must be a SHA-256 hexadecimal digest")
            object.__setattr__(self, name, normalized_digest)
        start_ns = int(start_time.timestamp() * 1_000_000_000)
        end_ns = int(end_time.timestamp() * 1_000_000_000)
        signal_timestamps: list[int] = []
        market_start_ns = int(self.market.t0.timestamp() * 1_000_000_000)
        for signal in self.signals:
            validate_opening_mispricing_signal(signal)
            if signal.market_slug != self.market.slug:
                raise ValueError("every signal must belong to the replay market")
            if int(signal.market_window_start_ts_ns) != market_start_ns:
                raise ValueError("every signal must bind the replay market-window start")
            if not start_ns <= signal.ts_init <= end_ns:
                raise ValueError("signal ts_init must lie within the replay window")
            signal_timestamps.append(int(signal.ts_init))
        if signal_timestamps != sorted(set(signal_timestamps)):
            raise ValueError("signal timestamps must be unique and strictly increasing")
        if len({(signal.model_version, signal.feature_schema_hash) for signal in self.signals}) > 1:
            raise ValueError("one replay requires one model version and feature schema")
        if self.formal_grid_component:
            latency = self.execution.latency_model
            if self.execution.maker_rebates_enabled:
                raise ValueError("formal replay must disable maker rebates")
            if (
                latency is None
                or latency.insert_latency_ms <= 0.0
                or latency.cancel_latency_ms <= 0.0
            ):
                raise ValueError("formal replay requires positive insert and cancel latency")
            if self.market.resolution not in {MarketOutcome.UP, MarketOutcome.DOWN}:
                raise ValueError("formal replay requires a final Up or Down resolution")
            if self.market.label_available_ts is None:
                raise ValueError("formal replay requires label_available_ts")
            if start_time > self.market.t0:
                raise ValueError("formal replay must start no later than market open")
            if end_time < self.market.label_available_ts:
                raise ValueError("formal replay must run through label_available_ts")
            cadence_ns = round(self.maker.signal_cadence_seconds * 1_000_000_000)
            first_offset_ns = (
                (round(self.maker.entry_start_seconds * 1_000_000_000) + cadence_ns - 1)
                // cadence_ns
            ) * cadence_ns
            final_offset_ns = round(self.maker.entry_end_seconds * 1_000_000_000)
            expected = tuple(
                market_start_ns + offset
                for offset in range(first_offset_ns, final_offset_ns + 1, cadence_ns)
            )
            if tuple(signal_timestamps) != expected:
                raise ValueError("formal replay requires the complete cadence-aligned signal grid")
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
    book_end_time = min(config.end_time, config.market.t1)
    settlement_observable_ns = (
        int(config.market.label_available_ts.timestamp() * 1_000_000_000)
        if config.market.label_available_ts is not None
        else None
    )
    shared_replay_metadata = {
        "btc_rule_hash": config.market.rule_hash,
        "btc_rule_epoch": config.market.rule_epoch,
        "settlement_observable_ns": settlement_observable_ns,
        "settlement_observable_time": (
            config.market.label_available_ts.isoformat()
            if config.market.label_available_ts is not None
            else None
        ),
    }
    replays = (
        BookReplay(
            market_slug=config.market.slug,
            token_index=config.up_token_index,
            start_time=config.start_time,
            end_time=book_end_time,
            metadata={
                **shared_replay_metadata,
                "expected_records_sha256": config.up_records_sha256,
            },
        ),
        BookReplay(
            market_slug=config.market.slug,
            token_index=config.down_token_index,
            start_time=config.start_time,
            end_time=book_end_time,
            metadata={
                **shared_replay_metadata,
                "expected_records_sha256": config.down_records_sha256,
            },
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
                entry_start_seconds=config.maker.entry_start_seconds,
                entry_end_seconds=config.maker.entry_end_seconds,
                confirmation_signals=config.maker.confirmation_signals,
                signal_cadence_seconds=config.maker.signal_cadence_seconds,
                signal_cadence_tolerance_seconds=config.maker.signal_cadence_tolerance_seconds,
                max_work_seconds=config.maker.max_work_seconds,
                stale_after_seconds=config.maker.stale_after_seconds,
                cancel_probability_drop=config.maker.cancel_probability_drop,
                max_visible_depth_fraction=config.maker.max_visible_depth_fraction,
                price_level_tick_offsets=config.maker.price_level_tick_offsets,
            )
        )
        return created_strategy

    backtest = PredictionMarketBacktest(
        name=name,
        data=data,
        replays=replays,
        joint_strategy_factory=joint_strategy_factory,
        auxiliary_data_factory=lambda loaded_sims: (
            *config.signals,
            BtcReplayBoundary(
                market_slug=config.market.slug,
                ts_event=int(config.end_time.timestamp() * 1_000_000_000),
                ts_init=int(config.end_time.timestamp() * 1_000_000_000),
            ),
        ),
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
