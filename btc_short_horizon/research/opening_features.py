"""Causal feature reconstruction from versioned BTC forward raw evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from btc_short_horizon.data import MarketWindow
from btc_short_horizon.data.binance import (
    normalize_binance_book_ticker,
    normalize_binance_trade,
)
from btc_short_horizon.data.rtds import normalize_chainlink_btc_usd
from btc_short_horizon.features import (
    BtcBookTop,
    BtcReferencePrice,
    BtcTrade,
    OpeningFeatureObservation,
    OpeningFeatureState,
)
from btc_short_horizon.research.opening_evidence import (
    ForwardRawEvent,
    RawPayloadError,
    load_forward_polymarket_book_events,
    load_forward_raw_events,
)


_NANOS_PER_SECOND = 1_000_000_000
_SUPPORTED_VENUE_SOURCES = ("binance_spot", "binance_perp")
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
    epoch_id: int
    value: _FeatureValue | None = None
    gap_before: bool = False
    tick_size_changed: bool = False

    def __post_init__(self) -> None:
        if not self.raw_source or not self.state_source or not self.state_instrument:
            raise ValueError("raw/state source and state instrument are required")
        if min(self.source_ts_ns, self.available_ts_ns, self.epoch_id) < 0:
            raise ValueError("feature state event timestamps and epoch must be non-negative")
        if self.collector_receive_ts_ns is not None and self.collector_receive_ts_ns < 0:
            raise ValueError("collector_receive_ts_ns must be non-negative when provided")
        if not self.sequence_or_hash:
            raise ValueError("sequence_or_hash is required")
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


def build_forward_opening_feature_observations(
    *,
    raw_data_root: Path,
    market: MarketWindow,
    start_time: datetime,
    end_time: datetime,
    decision_ts_ns: Sequence[int],
    ingest_version: str,
    required_venue_sources: tuple[str, ...] = _SUPPORTED_VENUE_SOURCES,
) -> ForwardOpeningFeatureBuild:
    """Build causal opening features from CLOB, Chainlink, and core Binance evidence."""

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

    clob_events, clob_summary = _load_clob_feature_events(
        raw_data_root=raw_data_root,
        market=market,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
    )
    chainlink_events, chainlink_summary = _load_chainlink_feature_events(
        raw_data_root=raw_data_root,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
    )
    venue_results = tuple(
        _load_binance_feature_events(
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


def _load_clob_feature_events(
    *,
    raw_data_root: Path,
    market: MarketWindow,
    start_time: datetime,
    end_time: datetime,
    ingest_version: str,
) -> tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary]:
    up = load_forward_polymarket_book_events(
        raw_data_root=raw_data_root,
        token_id=market.up_token_id,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
    )
    down = load_forward_polymarket_book_events(
        raw_data_root=raw_data_root,
        token_id=market.down_token_id,
        start_time=start_time,
        end_time=end_time,
        ingest_version=ingest_version,
    )
    events = tuple(
        ForwardFeatureStateEvent(
            raw_source="polymarket_clob",
            state_source="polymarket_clob",
            state_instrument=state_event.token_id,
            source_ts_ns=state_event.source_ts_ns,
            collector_receive_ts_ns=state_event.collector_receive_ts_ns,
            available_ts_ns=state_event.available_ts_ns,
            sequence_or_hash=(
                f"{state_event.token_id}:{state_event.epoch_id}:{state_event.source_ts_ns}"
            ),
            epoch_id=state_event.epoch_id,
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


def _chainlink_state_events(
    raw_events: Sequence[ForwardRawEvent],
) -> tuple[tuple[ForwardFeatureStateEvent, ...], int]:
    events: list[ForwardFeatureStateEvent] = []
    current_epoch: int | None = None
    gap_count = 0
    for raw in raw_events:
        gap_before = _epoch_gap(raw=raw, current_epoch=current_epoch)
        if gap_before:
            gap_count += 1
            events.append(
                _gap_event(
                    raw,
                    state_source=_CHAINLINK_STATE_SOURCE,
                    state_instrument=_CHAINLINK_INSTRUMENT,
                )
            )
        current_epoch = raw.epoch_id
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
        events.append(
            _value_event(
                raw,
                state_source=_CHAINLINK_STATE_SOURCE,
                state_instrument=_CHAINLINK_INSTRUMENT,
                value=reference,
            )
        )
    return tuple(events), gap_count


def _binance_state_events(
    raw_events: Sequence[ForwardRawEvent], *, source: str
) -> tuple[tuple[ForwardFeatureStateEvent, ...], int]:
    events: list[ForwardFeatureStateEvent] = []
    current_epochs: dict[str, int] = {}
    gap_count = 0
    for raw in raw_events:
        stream_id = _binance_feature_stream_id(raw.event_type)
        if stream_id is None:
            continue
        gap_before = _epoch_gap(
            raw=raw,
            current_epoch=current_epochs.get(stream_id),
        )
        if gap_before:
            gap_count += 1
            events.append(_gap_event(raw, state_source=source, state_instrument=_BTCUSDT))
        current_epochs[stream_id] = raw.epoch_id
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
            events.append(
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
            events.append(
                _value_event(
                    raw,
                    state_source=source,
                    state_instrument=_BTCUSDT,
                    value=book,
                )
            )
    return tuple(events), gap_count


def _binance_feature_stream_id(event_type: str) -> str | None:
    if event_type in {"trade", "aggtrade"}:
        return "trade"
    if event_type == "book_ticker":
        return "book_ticker"
    return None


def _epoch_gap(*, raw: ForwardRawEvent, current_epoch: int | None) -> bool:
    if current_epoch is not None and raw.epoch_id < current_epoch:
        raise RawPayloadError(
            "raw epoch regressed in causal feature order at "
            f"{raw.path}:{raw.row_index}: {raw.epoch_id} < {current_epoch}"
        )
    return current_epoch is not None and raw.epoch_id != current_epoch


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
        epoch_id=raw.epoch_id,
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
        epoch_id=raw.epoch_id,
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


def _feature_event_sort_key(event: ForwardFeatureStateEvent) -> tuple[int, int, int, int, str, str]:
    return (
        event.available_ts_ns,
        event.collector_receive_ts_ns or event.available_ts_ns,
        event.source_ts_ns,
        0 if event.gap_before else 1,
        event.raw_source,
        event.sequence_or_hash,
    )


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
    "build_forward_opening_feature_observations",
]
