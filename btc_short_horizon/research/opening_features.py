"""Causal feature reconstruction from versioned BTC forward raw evidence."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import gc
from pathlib import Path

import pyarrow as pa

from btc_short_horizon.data import MarketWindow
from btc_short_horizon.data.binance import (
    normalize_binance_book_ticker,
    normalize_binance_trade,
)
from btc_short_horizon.data.rtds import normalize_chainlink_btc_usd
from btc_short_horizon.data.okx import OkxBookSynchronizer, normalize_okx_trade
from btc_short_horizon.features import (
    BtcBookTop,
    BtcReferencePrice,
    BtcTrade,
    OpeningFeatureObservation,
    OpeningFeatureState,
)
from btc_short_horizon.research.opening_evidence import (
    ForwardBookEventLoad,
    ForwardRawEvent,
    RawPayloadError,
    TokenBookStateEvent,
    fold_forward_polymarket_book_events,
    load_forward_polymarket_book_events,
    load_forward_raw_events,
    stream_forward_raw_events,
)


_NANOS_PER_SECOND = 1_000_000_000
_SUPPORTED_VENUE_SOURCES = ("binance_spot", "binance_perp", "okx_spot", "okx_swap")
_BTCUSDT = "BTCUSDT"
_CHAINLINK_RAW_SOURCE = "polymarket_rtds_chainlink"
_CHAINLINK_STATE_SOURCE = "chainlink"
_CHAINLINK_INSTRUMENT = "btc/usd"

_FeatureValue = BtcTrade | BtcBookTop | BtcReferencePrice


@dataclass(frozen=True, slots=True)
class ForwardFeatureStateEvent:
    """One raw-derived state transition, including explicit epoch and tick boundaries."""

    raw_source: str
    state_source: str
    state_instrument: str
    source_ts_ns: int
    collector_receive_ts_ns: int | None
    available_ts_ns: int
    sequence_or_hash: str
    collector_session_id: str
    epoch_id: int
    admission_sequence: int = 0
    value: _FeatureValue | None = None
    gap_before: bool = False
    tick_size_changed: bool = False

    def __post_init__(self) -> None:
        if not self.raw_source or not self.state_source or not self.state_instrument:
            raise ValueError("raw/state source and state instrument are required")
        if min(self.source_ts_ns, self.available_ts_ns, self.epoch_id) < 0:
            raise ValueError("feature state event timestamps and epoch must be non-negative")
        if self.admission_sequence < 0:
            raise ValueError("admission_sequence must be non-negative")
        if self.collector_receive_ts_ns is not None and self.collector_receive_ts_ns < 0:
            raise ValueError("collector_receive_ts_ns must be non-negative when provided")
        if not self.sequence_or_hash or not self.collector_session_id:
            raise ValueError("sequence_or_hash and collector_session_id are required")
        if self.value is None and not (self.gap_before or self.tick_size_changed):
            raise ValueError("an empty feature state event must carry a gap or tick boundary")
        if self.value is not None and (
            self.value.source != self.state_source or self.value.instrument != self.state_instrument
        ):
            raise ValueError("feature value identity must match the state stream")


@dataclass(frozen=True, slots=True)
class ForwardFeatureSourceSummary:
    raw_source: str
    raw_part_count: int
    raw_row_count: int
    state_event_count: int
    gap_event_count: int


@dataclass(frozen=True, slots=True)
class ForwardOpeningFeatureBuild:
    observations: tuple[OpeningFeatureObservation, ...]
    input_events: tuple[ForwardFeatureStateEvent, ...]
    source_summaries: tuple[ForwardFeatureSourceSummary, ...]

    def source_event_count(self, raw_source: str) -> int:
        return sum(
            summary.state_event_count
            for summary in self.source_summaries
            if summary.raw_source == raw_source
        )


@dataclass(frozen=True, slots=True)
class ForwardOpeningReadinessBuild:
    """Bounded readiness result with no retained raw-derived event list.

    This is intentionally a separate result type.  The general feature build
    exposes ``input_events`` for downstream research materializers; returning
    an empty tuple from that API would make memory reduction indistinguishable
    from a valid empty dataset and could silently corrupt callers.
    """

    observations: tuple[OpeningFeatureObservation, ...]
    source_summaries: tuple[ForwardFeatureSourceSummary, ...]
    polymarket_exit_evidence: tuple[ForwardBookEventLoad, ...] = ()

    def source_event_count(self, raw_source: str) -> int:
        return sum(
            summary.state_event_count
            for summary in self.source_summaries
            if summary.raw_source == raw_source
        )


def build_forward_opening_feature_observations(
    *,
    raw_data_root: Path,
    market: MarketWindow,
    start_time: datetime,
    end_time: datetime,
    decision_ts_ns: Sequence[int],
    ingest_version: str,
    polymarket_source_timestamp_regression_tolerance_seconds: float | None = None,
    required_venue_sources: tuple[str, ...] = _SUPPORTED_VENUE_SOURCES,
) -> ForwardOpeningFeatureBuild:
    """Build causal opening features from CLOB, Chainlink, and core Binance evidence."""

    decisions, market_start_ns = _validate_opening_feature_request(
        market=market,
        decision_ts_ns=decision_ts_ns,
        ingest_version=ingest_version,
        required_venue_sources=required_venue_sources,
    )

    clob_events, clob_summary = _load_clob_feature_events(
        raw_data_root=raw_data_root,
        market=market,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
        polymarket_source_timestamp_regression_tolerance_seconds=(
            polymarket_source_timestamp_regression_tolerance_seconds
        ),
    )
    chainlink_events, chainlink_summary = _load_chainlink_feature_events(
        raw_data_root=raw_data_root,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
    )
    venue_results = tuple(
        (_load_okx_feature_events if source.startswith("okx_") else _load_binance_feature_events)(
            raw_data_root=raw_data_root,
            source=source,
            start_time=start_time,
            end_time=end_time,
            ingest_version=ingest_version,
        )
        for source in required_venue_sources
    )
    input_events = tuple(
        sorted(
            (
                *clob_events,
                *chainlink_events,
                *(event for events, _ in venue_results for event in events),
            ),
            key=_feature_event_sort_key,
        )
    )
    state = OpeningFeatureState(
        up_token_id=market.up_token_id,
        down_token_id=market.down_token_id,
        required_venue_sources=required_venue_sources,
    )
    event_index = 0
    tick_size_changed = False
    observations: list[OpeningFeatureObservation] = []
    for decision in decisions:
        while (
            event_index < len(input_events)
            and input_events[event_index].available_ts_ns <= decision
        ):
            event = input_events[event_index]
            event_index += 1
            if event.gap_before:
                state.mark_gap(source=event.state_source, instrument=event.state_instrument)
            if event.value is not None:
                state.update(event.value)
            tick_size_changed = tick_size_changed or event.tick_size_changed
        observation = state.snapshot(
            decision_ts_ns=decision,
            market_window_start_ns=market_start_ns,
        )
        if tick_size_changed:
            observation = replace(
                observation,
                quality_flags=frozenset((*observation.quality_flags, "tick_changed")),
            )
        observations.append(observation)
    return ForwardOpeningFeatureBuild(
        observations=tuple(observations),
        input_events=input_events,
        source_summaries=(
            clob_summary,
            chainlink_summary,
            *(summary for _, summary in venue_results),
        ),
    )


def build_forward_opening_readiness_observations(
    *,
    raw_data_root: Path,
    market: MarketWindow,
    start_time: datetime,
    end_time: datetime,
    decision_ts_ns: Sequence[int],
    ingest_version: str,
    polymarket_terminal_end_time: datetime | None = None,
    polymarket_source_timestamp_regression_tolerance_seconds: float | None = None,
    required_venue_sources: tuple[str, ...] = _SUPPORTED_VENUE_SOURCES,
) -> ForwardOpeningReadinessBuild:
    """Build readiness observations while keeping only one source in memory."""

    decisions, _market_start_ns = _validate_opening_feature_request(
        market=market,
        decision_ts_ns=decision_ts_ns,
        ingest_version=ingest_version,
        required_venue_sources=required_venue_sources,
    )
    return _build_bounded_forward_opening_feature_observations(
        raw_data_root=raw_data_root,
        market=market,
        start_time=start_time,
        end_time=end_time,
        decisions=decisions,
        ingest_version=ingest_version,
        polymarket_terminal_end_time=polymarket_terminal_end_time,
        polymarket_source_timestamp_regression_tolerance_seconds=(
            polymarket_source_timestamp_regression_tolerance_seconds
        ),
        required_venue_sources=required_venue_sources,
    )


def _build_bounded_forward_opening_feature_observations(
    *,
    raw_data_root: Path,
    market: MarketWindow,
    start_time: datetime,
    end_time: datetime,
    decisions: tuple[int, ...],
    ingest_version: str,
    polymarket_terminal_end_time: datetime | None,
    polymarket_source_timestamp_regression_tolerance_seconds: float | None,
    required_venue_sources: tuple[str, ...],
) -> ForwardOpeningReadinessBuild:
    """Build identical decision snapshots while retaining one raw source at a time."""

    states = tuple(
        OpeningFeatureState(
            up_token_id=market.up_token_id,
            down_token_id=market.down_token_id,
            required_venue_sources=required_venue_sources,
        )
        for _ in decisions
    )
    if polymarket_terminal_end_time is not None:
        if polymarket_terminal_end_time < end_time:
            raise ValueError("polymarket_terminal_end_time cannot precede feature end_time")
        clob_end_time = polymarket_terminal_end_time
    else:
        clob_end_time = end_time
    tick_size_changed = [False] * len(decisions)
    summaries: list[ForwardFeatureSourceSummary] = []

    def apply_event(event: ForwardFeatureStateEvent) -> None:
        first_decision = bisect_left(decisions, event.available_ts_ns)
        for index in range(first_decision, len(decisions)):
            state = states[index]
            if event.gap_before:
                state.mark_gap(
                    source=event.state_source,
                    instrument=event.state_instrument,
                )
            if event.value is not None:
                state.update(event.value)
            tick_size_changed[index] = tick_size_changed[index] or event.tick_size_changed

    def apply_source(
        result: tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary],
    ) -> None:
        events, summary = result
        summaries.append(summary)
        for event in sorted(events, key=_feature_event_sort_key):
            apply_event(event)

    def apply_stream(
        *,
        source: str,
        instrument: str,
        decoder: Callable[..., tuple[tuple[ForwardFeatureStateEvent, ...], int]],
    ) -> None:
        stream = stream_forward_raw_events(
            raw_data_root=raw_data_root,
            source=source,
            instrument=instrument,
            start_time=start_time,
            end_time=end_time,
            ingest_version=ingest_version,
        )
        raw_row_count = 0

        def raw_events() -> Iterable[ForwardRawEvent]:
            nonlocal raw_row_count
            for raw in stream.events:
                raw_row_count += 1
                yield raw

        state_event_count = 0

        def consume(event: ForwardFeatureStateEvent) -> None:
            nonlocal state_event_count
            state_event_count += 1
            apply_event(event)

        _unused_events, gap_count = decoder(raw_events(), on_event=consume)
        summaries.append(
            ForwardFeatureSourceSummary(
                raw_source=source,
                raw_part_count=stream.raw_part_count,
                raw_row_count=raw_row_count,
                state_event_count=state_event_count,
                gap_event_count=gap_count,
            )
        )

    clob_gap_count = 0

    def consume_clob(state_event: TokenBookStateEvent) -> None:
        nonlocal clob_gap_count
        event = ForwardFeatureStateEvent(
            raw_source="polymarket_clob",
            state_source="polymarket_clob",
            state_instrument=state_event.token_id,
            source_ts_ns=state_event.source_ts_ns,
            collector_receive_ts_ns=state_event.collector_receive_ts_ns,
            available_ts_ns=state_event.available_ts_ns,
            sequence_or_hash=(
                f"{state_event.token_id}:{state_event.collector_session_id}:"
                f"{state_event.epoch_id}:{state_event.source_ts_ns}"
            ),
            collector_session_id=state_event.collector_session_id,
            epoch_id=state_event.epoch_id,
            admission_sequence=state_event.admission_sequence,
            value=state_event.book,
            gap_before=state_event.reset_book,
            tick_size_changed=state_event.tick_size_changed,
        )
        clob_gap_count += event.gap_before
        apply_event(event)

    clob_loads_list = []
    for token_id in (market.up_token_id, market.down_token_id):
        clob_loads_list.append(
            fold_forward_polymarket_book_events(
                raw_data_root=raw_data_root,
                token_id=token_id,
                start_time=start_time,
                end_time=clob_end_time,
                ingest_version=ingest_version,
                expected_source_timestamp_regression_tolerance_seconds=(
                    polymarket_source_timestamp_regression_tolerance_seconds
                ),
                on_event=consume_clob,
            )
        )
        _release_source_memory()
    clob_loads = tuple(clob_loads_list)
    if (
        clob_loads[0].polymarket_source_timestamp_regression_tolerance_seconds
        != clob_loads[1].polymarket_source_timestamp_regression_tolerance_seconds
    ):
        raise RawPayloadError("Up/Down raw manifests use different Polymarket timestamp tolerances")
    summaries.append(
        ForwardFeatureSourceSummary(
            raw_source="polymarket_clob",
            raw_part_count=sum(item.raw_part_count for item in clob_loads),
            raw_row_count=sum(item.raw_row_count for item in clob_loads),
            state_event_count=sum(item.state_event_count or 0 for item in clob_loads),
            gap_event_count=clob_gap_count,
        )
    )
    apply_stream(
        source=_CHAINLINK_RAW_SOURCE,
        instrument=_CHAINLINK_INSTRUMENT,
        decoder=_chainlink_state_events,
    )
    _release_source_memory()
    for source in required_venue_sources:
        instrument = "BTC-USDT" if source == "okx_spot" else (
            "BTC-USDT-SWAP" if source == "okx_swap" else _BTCUSDT
        )
        decoder = (
            (lambda events, on_event=None, _source=source: _okx_state_events(
                events,
                source=_source,
                instrument=instrument,
                on_event=on_event,
            ))
            if source.startswith("okx_")
            else (lambda events, on_event=None, _source=source: _binance_state_events(
                events,
                source=_source,
                on_event=on_event,
            ))
        )
        apply_stream(source=source, instrument=instrument, decoder=decoder)
        _release_source_memory()

    observations: list[OpeningFeatureObservation] = []
    market_start_ns = _datetime_to_ns(market.t0)
    for index, (decision, state) in enumerate(zip(decisions, states, strict=True)):
        observation = state.snapshot(
            decision_ts_ns=decision,
            market_window_start_ns=market_start_ns,
        )
        if tick_size_changed[index]:
            observation = replace(
                observation,
                quality_flags=frozenset((*observation.quality_flags, "tick_changed")),
            )
        observations.append(observation)
    return ForwardOpeningReadinessBuild(
        observations=tuple(observations),
        source_summaries=tuple(summaries),
        polymarket_exit_evidence=clob_loads,
    )


def _release_source_memory() -> None:
    """Return temporary Arrow buffers between isolated readiness source folds."""

    gc.collect()
    pa.default_memory_pool().release_unused()


def _load_clob_feature_events(
    *,
    raw_data_root: Path,
    market: MarketWindow,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str,
    polymarket_source_timestamp_regression_tolerance_seconds: float | None,
) -> tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary]:
    up = load_forward_polymarket_book_events(
        raw_data_root=raw_data_root,
        token_id=market.up_token_id,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
        expected_source_timestamp_regression_tolerance_seconds=(
            polymarket_source_timestamp_regression_tolerance_seconds
        ),
    )
    down = load_forward_polymarket_book_events(
        raw_data_root=raw_data_root,
        token_id=market.down_token_id,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
        expected_source_timestamp_regression_tolerance_seconds=(
            polymarket_source_timestamp_regression_tolerance_seconds
        ),
    )
    if (
        up.polymarket_source_timestamp_regression_tolerance_seconds
        != down.polymarket_source_timestamp_regression_tolerance_seconds
    ):
        raise RawPayloadError("Up/Down raw manifests use different Polymarket timestamp tolerances")
    events = tuple(
        ForwardFeatureStateEvent(
            raw_source="polymarket_clob",
            state_source="polymarket_clob",
            state_instrument=state_event.token_id,
            source_ts_ns=state_event.source_ts_ns,
            collector_receive_ts_ns=state_event.collector_receive_ts_ns,
            available_ts_ns=state_event.available_ts_ns,
            sequence_or_hash=(
                f"{state_event.token_id}:{state_event.collector_session_id}:"
                f"{state_event.epoch_id}:{state_event.source_ts_ns}"
            ),
            collector_session_id=state_event.collector_session_id,
            epoch_id=state_event.epoch_id,
            admission_sequence=state_event.admission_sequence,
            value=state_event.book,
            gap_before=state_event.reset_book,
            tick_size_changed=state_event.tick_size_changed,
        )
        for state_event in (*up.events, *down.events)
    )
    return (
        events,
        ForwardFeatureSourceSummary(
            raw_source="polymarket_clob",
            raw_part_count=up.raw_part_count + down.raw_part_count,
            raw_row_count=up.raw_row_count + down.raw_row_count,
            state_event_count=len(events),
            gap_event_count=sum(event.gap_before for event in events),
        ),
    )


def _load_chainlink_feature_events(
    *,
    raw_data_root: Path,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str,
) -> tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary]:
    raw_load = load_forward_raw_events(
        raw_data_root=raw_data_root,
        source=_CHAINLINK_RAW_SOURCE,
        instrument=_CHAINLINK_INSTRUMENT,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
    )
    events, gap_count = _chainlink_state_events(raw_load.events)
    return (
        events,
        ForwardFeatureSourceSummary(
            raw_source=_CHAINLINK_RAW_SOURCE,
            raw_part_count=raw_load.raw_part_count,
            raw_row_count=raw_load.raw_row_count,
            state_event_count=len(events),
            gap_event_count=gap_count,
        ),
    )


def _load_binance_feature_events(
    *,
    raw_data_root: Path,
    source: str,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str,
) -> tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary]:
    raw_load = load_forward_raw_events(
        raw_data_root=raw_data_root,
        source=source,
        instrument=_BTCUSDT,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
    )
    events, gap_count = _binance_state_events(raw_load.events, source=source)
    return (
        events,
        ForwardFeatureSourceSummary(
            raw_source=source,
            raw_part_count=raw_load.raw_part_count,
            raw_row_count=raw_load.raw_row_count,
            state_event_count=len(events),
            gap_event_count=gap_count,
        ),
    )


def _load_okx_feature_events(
    *,
    raw_data_root: Path,
    source: str,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str,
) -> tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary]:
    instrument = "BTC-USDT" if source == "okx_spot" else "BTC-USDT-SWAP"
    raw_load = load_forward_raw_events(
        raw_data_root=raw_data_root,
        source=source,
        instrument=instrument,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
    )
    events, gap_count = _okx_state_events(raw_load.events, source=source, instrument=instrument)
    return (
        events,
        ForwardFeatureSourceSummary(
            raw_source=source,
            raw_part_count=raw_load.raw_part_count,
            raw_row_count=raw_load.raw_row_count,
            state_event_count=len(events),
            gap_event_count=gap_count,
        ),
    )


def _chainlink_state_events(
    raw_events: Iterable[ForwardRawEvent],
    *,
    on_event: Callable[[ForwardFeatureStateEvent], None] | None = None,
) -> tuple[tuple[ForwardFeatureStateEvent, ...], int]:
    events: list[ForwardFeatureStateEvent] = []
    emit = events.append if on_event is None else on_event
    current_epoch: tuple[str, int] | None = None
    completed_epochs: set[tuple[str, int]] = set()
    gap_count = 0
    for raw in raw_events:
        if raw.event_type == "continuity_gap":
            if _continuity_gap_stream_id(raw) != "price":
                continue
            _gap_before, current_epoch = _advance_epoch(
                raw=raw,
                current_epoch=current_epoch,
                completed_epochs=completed_epochs,
            )
            gap_count += 1
            emit(
                _gap_event(
                    raw,
                    state_source=_CHAINLINK_STATE_SOURCE,
                    state_instrument=_CHAINLINK_INSTRUMENT,
                )
            )
            continue
        gap_before, current_epoch = _advance_epoch(
            raw=raw,
            current_epoch=current_epoch,
            completed_epochs=completed_epochs,
        )
        if gap_before:
            gap_count += 1
            emit(
                _gap_event(
                    raw,
                    state_source=_CHAINLINK_STATE_SOURCE,
                    state_instrument=_CHAINLINK_INSTRUMENT,
                )
            )
        if raw.event_type != "crypto_prices_chainlink":
            continue
        try:
            record = normalize_chainlink_btc_usd(
                raw.payload,
                collector_receive_ts=_collector_receive_time(raw),
            )
        except ValueError as exc:
            raise _raw_normalization_error(raw, exc) from exc
        reference = replace(
            record.reference,
            source_ts_ns=raw.source_ts_ns,
            available_ts_ns=raw.available_ts_ns,
        )
        emit(
            _value_event(
                raw,
                state_source=_CHAINLINK_STATE_SOURCE,
                state_instrument=_CHAINLINK_INSTRUMENT,
                value=reference,
            )
        )
    return (() if on_event is not None else tuple(events)), gap_count


def _binance_state_events(
    raw_events: Iterable[ForwardRawEvent],
    *,
    source: str,
    on_event: Callable[[ForwardFeatureStateEvent], None] | None = None,
) -> tuple[tuple[ForwardFeatureStateEvent, ...], int]:
    events: list[ForwardFeatureStateEvent] = []
    emit = events.append if on_event is None else on_event
    current_epochs: dict[str, tuple[str, int]] = {}
    completed_epochs: dict[str, set[tuple[str, int]]] = {}
    gap_count = 0
    for raw in raw_events:
        if raw.event_type == "continuity_gap":
            stream_id = _continuity_gap_stream_id(raw)
            if stream_id not in {"trade", "book_ticker"}:
                continue
            _gap_before, current_epoch = _advance_epoch(
                raw=raw,
                current_epoch=current_epochs.get(stream_id),
                completed_epochs=completed_epochs.setdefault(stream_id, set()),
            )
            current_epochs[stream_id] = current_epoch
            gap_count += 1
            emit(_gap_event(raw, state_source=source, state_instrument=_BTCUSDT))
            continue
        stream_id = _binance_feature_stream_id(raw.event_type)
        if stream_id is None:
            continue
        gap_before, current_epoch = _advance_epoch(
            raw=raw,
            current_epoch=current_epochs.get(stream_id),
            completed_epochs=completed_epochs.setdefault(stream_id, set()),
        )
        if gap_before:
            gap_count += 1
            emit(_gap_event(raw, state_source=source, state_instrument=_BTCUSDT))
        current_epochs[stream_id] = current_epoch
        message = _binance_message(raw)
        if raw.event_type in {"trade", "aggtrade"}:
            try:
                record = normalize_binance_trade(
                    message,
                    collector_receive_ts=_collector_receive_time(raw),
                    source=source,
                )
            except ValueError as exc:
                raise _raw_normalization_error(raw, exc) from exc
            trade = replace(
                record.trade,
                source_ts_ns=raw.source_ts_ns,
                available_ts_ns=raw.available_ts_ns,
            )
            emit(
                _value_event(
                    raw,
                    state_source=source,
                    state_instrument=_BTCUSDT,
                    value=trade,
                )
            )
        elif raw.event_type == "book_ticker":
            try:
                normalize_binance_book_ticker(
                    message,
                    collector_receive_ts=_collector_receive_time(raw),
                    source=source,
                )
                bid_size = float(message["B"])
                ask_size = float(message["A"])
                # Binance permits a zero BBO quantity while its book changes.  It
                # is valid transport evidence but not a usable top-of-book state.
                if bid_size <= 0.0 or ask_size <= 0.0:
                    continue
                book = BtcBookTop(
                    source_ts_ns=raw.source_ts_ns,
                    available_ts_ns=raw.available_ts_ns,
                    bid=float(message["b"]),
                    ask=float(message["a"]),
                    bid_size=bid_size,
                    ask_size=ask_size,
                    source=source,
                    instrument=_BTCUSDT,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise _raw_normalization_error(raw, exc) from exc
            emit(
                _value_event(
                    raw,
                    state_source=source,
                    state_instrument=_BTCUSDT,
                    value=book,
                )
            )
        elif raw.event_type == "partial_depth_snapshot":
            try:
                bids = message.get("bids", message.get("b"))
                asks = message.get("asks", message.get("a"))
                if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
                    raise ValueError("partial depth requires non-empty bids and asks")
                bid = bids[0]
                ask = asks[0]
                if (
                    not isinstance(bid, list)
                    or len(bid) < 2
                    or not isinstance(ask, list)
                    or len(ask) < 2
                ):
                    raise ValueError("partial depth BBO levels are malformed")
                book = BtcBookTop(
                    source_ts_ns=raw.source_ts_ns,
                    available_ts_ns=raw.available_ts_ns,
                    bid=float(bid[0]),
                    ask=float(ask[0]),
                    bid_size=float(bid[1]),
                    ask_size=float(ask[1]),
                    source=source,
                    instrument=_BTCUSDT,
                )
            except (TypeError, ValueError) as exc:
                raise _raw_normalization_error(raw, exc) from exc
            emit(
                _value_event(
                    raw,
                    state_source=source,
                    state_instrument=_BTCUSDT,
                    value=book,
                )
            )
    return (() if on_event is not None else tuple(events)), gap_count


def _okx_state_events(
    raw_events: Iterable[ForwardRawEvent],
    *,
    source: str,
    instrument: str,
    on_event: Callable[[ForwardFeatureStateEvent], None] | None = None,
) -> tuple[tuple[ForwardFeatureStateEvent, ...], int]:
    events: list[ForwardFeatureStateEvent] = []
    emit = events.append if on_event is None else on_event
    synchronizer = OkxBookSynchronizer(instrument=instrument, source=source)
    current_epochs: dict[str, tuple[str, int]] = {}
    completed_epochs: dict[str, set[tuple[str, int]]] = {}
    gap_count = 0
    for raw in raw_events:
        if raw.event_type == "continuity_gap":
            stream = _continuity_gap_stream_id(raw)
            if stream not in {"trade", "book"}:
                continue
            synchronizer.reset()
            gap_count += 1
            emit(_gap_event(raw, state_source=source, state_instrument=instrument))
            continue
        stream = (
            "trade"
            if raw.event_type == "trade"
            else "book"
            if raw.event_type.startswith("books_")
            else None
        )
        if stream is None:
            continue
        gap_before, epoch = _advance_epoch(
            raw=raw,
            current_epoch=current_epochs.get(stream),
            completed_epochs=completed_epochs.setdefault(stream, set()),
        )
        current_epochs[stream] = epoch
        if gap_before:
            synchronizer.reset()
            gap_count += 1
            emit(_gap_event(raw, state_source=source, state_instrument=instrument))
        data = raw.payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise RawPayloadError(f"invalid OKX envelope at {raw.path}:{raw.row_index}")
        item = data[0]
        if stream == "trade":
            try:
                trade = normalize_okx_trade(
                    item,
                    collector_receive_ts=_collector_receive_time(raw),
                    source=source,
                ).trade
            except ValueError as exc:
                raise _raw_normalization_error(raw, exc) from exc
            emit(
                _value_event(
                    raw,
                    state_source=source,
                    state_instrument=instrument,
                    value=replace(
                        trade,
                        source_ts_ns=raw.source_ts_ns,
                        available_ts_ns=raw.available_ts_ns,
                    ),
                )
            )
            continue
        action = raw.event_type.removeprefix("books_")
        normalized = dict(item)
        if action == "snapshot" and "prevSeqId" not in normalized:
            normalized["prevSeqId"] = -1
        try:
            applied = synchronizer.apply(
                action=action,
                payload=normalized,
                collector_receive_ts=_collector_receive_time(raw),
            )
        except ValueError as exc:
            raise _raw_normalization_error(raw, exc) from exc
        if applied.status.value == "gap":
            gap_count += 1
            emit(_gap_event(raw, state_source=source, state_instrument=instrument))
        elif applied.book_top is not None:
            emit(
                _value_event(
                    raw,
                    state_source=source,
                    state_instrument=instrument,
                    value=replace(
                        applied.book_top,
                        source_ts_ns=raw.source_ts_ns,
                        available_ts_ns=raw.available_ts_ns,
                    ),
                )
            )
    return (() if on_event is not None else tuple(events)), gap_count


def _binance_feature_stream_id(event_type: str) -> str | None:
    if event_type in {"trade", "aggtrade"}:
        return "trade"
    if event_type == "book_ticker":
        return "book_ticker"
    if event_type == "partial_depth_snapshot":
        return "partial_depth"
    return None


def _continuity_gap_stream_id(raw: ForwardRawEvent) -> str:
    if raw.payload.get("event_type") != "continuity_gap":
        raise RawPayloadError(f"continuity gap payload mismatch at {raw.path}:{raw.row_index}")
    stream_id = raw.payload.get("stream_id")
    reason = raw.payload.get("reason")
    if (
        not isinstance(stream_id, str)
        or not stream_id.strip()
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        raise RawPayloadError(f"invalid continuity gap at {raw.path}:{raw.row_index}")
    return stream_id


def _advance_epoch(
    *,
    raw: ForwardRawEvent,
    current_epoch: tuple[str, int] | None,
    completed_epochs: set[tuple[str, int]],
) -> tuple[bool, tuple[str, int]]:
    epoch = (raw.collector_session_id, raw.epoch_id)
    if current_epoch is None or epoch == current_epoch:
        return False, epoch
    completed_epochs.add(current_epoch)
    if epoch in completed_epochs:
        raise RawPayloadError(
            f"raw epoch reappeared in causal feature order at {raw.path}:{raw.row_index}: {epoch}"
        )
    return True, epoch


def _gap_event(
    raw: ForwardRawEvent, *, state_source: str, state_instrument: str
) -> ForwardFeatureStateEvent:
    return ForwardFeatureStateEvent(
        raw_source=raw.source,
        state_source=state_source,
        state_instrument=state_instrument,
        source_ts_ns=raw.source_ts_ns,
        collector_receive_ts_ns=raw.collector_receive_ts_ns,
        available_ts_ns=raw.available_ts_ns,
        sequence_or_hash=f"{raw.sequence_or_hash}:gap",
        collector_session_id=raw.collector_session_id,
        epoch_id=raw.epoch_id,
        admission_sequence=raw.admission_sequence,
        gap_before=True,
    )


def _value_event(
    raw: ForwardRawEvent,
    *,
    state_source: str,
    state_instrument: str,
    value: _FeatureValue,
) -> ForwardFeatureStateEvent:
    return ForwardFeatureStateEvent(
        raw_source=raw.source,
        state_source=state_source,
        state_instrument=state_instrument,
        source_ts_ns=raw.source_ts_ns,
        collector_receive_ts_ns=raw.collector_receive_ts_ns,
        available_ts_ns=raw.available_ts_ns,
        sequence_or_hash=raw.sequence_or_hash,
        collector_session_id=raw.collector_session_id,
        epoch_id=raw.epoch_id,
        admission_sequence=raw.admission_sequence,
        value=value,
    )


def _binance_message(raw: ForwardRawEvent) -> dict[str, object]:
    payload_data = raw.payload.get("data")
    if isinstance(payload_data, dict):
        return dict(payload_data)
    return dict(raw.payload)


def _collector_receive_time(raw: ForwardRawEvent) -> datetime:
    return _datetime_from_ns(raw.collector_receive_ts_ns or raw.available_ts_ns)


def _raw_normalization_error(raw: ForwardRawEvent, exc: Exception) -> RawPayloadError:
    return RawPayloadError(f"invalid {raw.source} payload at {raw.path}:{raw.row_index}: {exc}")


def _feature_event_sort_key(
    event: ForwardFeatureStateEvent,
) -> tuple[int, int, str, str, int, int, int, str]:
    return (
        event.available_ts_ns,
        event.collector_receive_ts_ns or event.available_ts_ns,
        event.raw_source,
        event.collector_session_id,
        event.admission_sequence,
        0 if event.gap_before else 1,
        event.source_ts_ns,
        event.sequence_or_hash,
    )


def _validate_opening_feature_request(
    *,
    market: MarketWindow,
    decision_ts_ns: Sequence[int],
    ingest_version: str,
    required_venue_sources: tuple[str, ...],
) -> tuple[tuple[int, ...], int]:
    if market.family.is_collection_only:
        raise ValueError("collection-only markets cannot build 15m opening features")
    if not ingest_version or not ingest_version.strip():
        raise ValueError("ingest_version is required")
    if not required_venue_sources or len(set(required_venue_sources)) != len(
        required_venue_sources
    ):
        raise ValueError("required_venue_sources must be non-empty and unique")
    if any(source not in _SUPPORTED_VENUE_SOURCES for source in required_venue_sources):
        raise ValueError("required_venue_sources contains an unsupported forward source")
    decisions = tuple(sorted(set(int(value) for value in decision_ts_ns)))
    if not decisions:
        raise ValueError("decision_ts_ns must not be empty")
    market_start_ns = _datetime_to_ns(market.t0)
    market_end_ns = _datetime_to_ns(market.t1)
    if any(value < market_start_ns or value > market_end_ns for value in decisions):
        raise ValueError("decision timestamps must lie within the market window")
    return decisions, market_start_ns


def _datetime_to_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return int(value.astimezone(UTC).timestamp() * _NANOS_PER_SECOND)


def _datetime_from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / _NANOS_PER_SECOND, tz=UTC)


__all__ = [
    "ForwardFeatureSourceSummary",
    "ForwardFeatureStateEvent",
    "ForwardOpeningFeatureBuild",
    "ForwardOpeningReadinessBuild",
    "build_forward_opening_feature_observations",
    "build_forward_opening_readiness_observations",
]
