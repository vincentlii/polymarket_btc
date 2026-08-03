"""Deterministic causal replay for the live Research Paper state machine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

import numpy as np

from btc_short_horizon.data import MarketOutcome, MarketWindow
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.live.paper_execution import PaperMarketRules
from btc_short_horizon.live.research_paper import ResearchPaperPortfolio
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_evidence import (
    ForwardRawEvent,
    load_forward_raw_events,
)


@dataclass(frozen=True, slots=True)
class PaperReplayDecision:
    decision_ts_ns: int
    result: str


@dataclass(frozen=True, slots=True)
class PaperReplayResult:
    decisions: tuple[PaperReplayDecision, ...]
    processed_event_count: int
    replay_end_ts_ns: int


@dataclass(frozen=True, slots=True)
class PaperReplayRawStream:
    source: str
    instrument: str
    ingest_version: str | None = None

    def __post_init__(self) -> None:
        if not self.source or not self.instrument:
            raise ValueError("replay stream source and instrument are required")
        if self.ingest_version is not None and not self.ingest_version.strip():
            raise ValueError("replay stream ingest_version must be non-empty")


def load_research_paper_events(
    *,
    raw_data_root: Path,
    streams: Sequence[PaperReplayRawStream],
    start_time: datetime,
    end_time: datetime,
) -> tuple[RawCollectorEvent, ...]:
    """Load explicit local raw streams without network access or version mixing."""

    selected = tuple(streams)
    identities = {(item.source, item.instrument) for item in selected}
    if not selected or len(identities) != len(selected):
        raise ValueError("replay streams must be a non-empty unique set")
    loads = tuple(
        load_forward_raw_events(
            raw_data_root=raw_data_root,
            source=stream.source,
            instrument=stream.instrument,
            start_time=start_time,
            end_time=end_time,
            ingest_version=stream.ingest_version,
        )
        for stream in selected
    )
    if any(not loaded.events for loaded in loads):
        raise ValueError("every replay stream must contain raw events")
    versions = {loaded.ingest_version for loaded in loads}
    if len(versions) != 1 or None in versions:
        raise ValueError("replay streams must use one explicit ingest version")
    events = [
        forward_raw_event_to_collector_event(event) for loaded in loads for event in loaded.events
    ]
    return tuple(sorted(events, key=_event_sort_key))


def forward_raw_event_to_collector_event(event: ForwardRawEvent) -> RawCollectorEvent:
    """Restore the exact event contract consumed by the live Paper engine."""

    if not isinstance(event, ForwardRawEvent):
        raise TypeError("event must be a ForwardRawEvent")
    return RawCollectorEvent(
        timing=TimedMarketEvent(
            source_ts=_datetime_from_ns(event.source_ts_ns),
            collector_receive_ts=(
                None
                if event.collector_receive_ts_ns is None
                else _datetime_from_ns(event.collector_receive_ts_ns)
            ),
            available_ts=_datetime_from_ns(event.available_ts_ns),
            sequence_or_hash=event.sequence_or_hash,
            source=event.source,
            instrument=event.instrument,
            schema_version=event.schema_version,
            ingest_version=event.ingest_version,
        ),
        event_type=event.event_type,
        payload=event.payload,
        collector_session_id=event.collector_session_id,
        epoch_id=event.epoch_id,
        admission_sequence=event.admission_sequence,
    )


def build_replay_binance_history(
    events: Sequence[RawCollectorEvent],
    *,
    cutoff_ts_ns: int,
    minimum_bars: int,
) -> BinanceKlineHistory:
    """Build a strict contiguous bootstrap from admitted closed 1s klines."""

    if (
        isinstance(cutoff_ts_ns, bool)
        or not isinstance(cutoff_ts_ns, int)
        or cutoff_ts_ns < 0
        or isinstance(minimum_bars, bool)
        or not isinstance(minimum_bars, int)
        or minimum_bars <= 0
    ):
        raise ValueError("cutoff_ts_ns and minimum_bars must be positive integers")
    rows: dict[int, tuple[float, float, float, float]] = {}
    for event in events:
        if (
            event.timing.source != "binance_spot"
            or event.timing.instrument != "BTCUSDT"
            or event.event_type != "kline_1s"
            or _event_available_ts_ns(event) > cutoff_ts_ns
        ):
            continue
        data = event.payload.get("data")
        message = data if isinstance(data, Mapping) else event.payload
        kline = message.get("k")
        if not isinstance(kline, Mapping) or kline.get("x") is not True:
            continue
        open_ms = _wire_integer(kline.get("t"), "kline open time")
        open_ns = open_ms * 1_000_000
        if open_ns > cutoff_ts_ns:
            raise ValueError("Binance replay kline opens after the bootstrap cutoff")
        values = (
            _wire_number(kline.get("c"), "kline close"),
            _wire_number(kline.get("v"), "kline volume"),
            _wire_number(kline.get("q"), "kline quote volume"),
            _wire_number(kline.get("V"), "kline taker buy volume"),
        )
        if open_ns in rows and rows[open_ns] != values:
            raise ValueError("conflicting Binance replay kline")
        rows[open_ns] = values
    ordered = sorted(rows.items())
    if len(ordered) < minimum_bars:
        raise ValueError("insufficient Binance replay bootstrap bars")
    ordered = ordered[-max(minimum_bars, 4_000) :]
    timestamps = np.asarray([item[0] for item in ordered], dtype=np.int64)
    if len(timestamps) > 1 and not np.all(np.diff(timestamps) == 1_000_000_000):
        raise ValueError("Binance replay bootstrap contains a kline gap")
    if cutoff_ts_ns - int(timestamps[-1]) > 2_000_000_000:
        raise ValueError("Binance replay bootstrap is stale at the cutoff")
    values = np.asarray([item[1] for item in ordered], dtype=float)
    return BinanceKlineHistory(
        open_ts_ns=timestamps,
        close=values[:, 0],
        volume=values[:, 1],
        quote_volume=values[:, 2],
        taker_buy_volume=values[:, 3],
        interval_seconds=1,
    )


def replay_research_paper(
    *,
    portfolio: ResearchPaperPortfolio,
    market: MarketWindow,
    rules: Mapping[str, PaperMarketRules],
    events: Sequence[RawCollectorEvent],
    decision_ts_ns: Sequence[int],
    replay_end_ts_ns: int,
    outcome: MarketOutcome | None = None,
    label_available_ts_ns: int | None = None,
) -> PaperReplayResult:
    """Replay admitted events by availability time through the live portfolio.

    All events at a decision timestamp are admitted before that decision. The
    engine itself applies latency transitions against the last book known at
    the transition time, so a later event cannot reprice an earlier execution.
    """

    if not isinstance(portfolio, ResearchPaperPortfolio):
        raise TypeError("portfolio must be a ResearchPaperPortfolio")
    if not isinstance(market, MarketWindow):
        raise TypeError("market must be a MarketWindow")
    decisions = tuple(decision_ts_ns)
    if not decisions:
        raise ValueError("decision_ts_ns must contain at least one timestamp")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in decisions
    ):
        raise ValueError("decision timestamps must be non-negative integers")
    if any(right <= left for left, right in zip(decisions, decisions[1:])):
        raise ValueError("decision timestamps must be strictly increasing")
    if (
        isinstance(replay_end_ts_ns, bool)
        or not isinstance(replay_end_ts_ns, int)
        or replay_end_ts_ns < decisions[-1]
    ):
        raise ValueError("replay_end_ts_ns must be at or after the final decision")
    if (outcome is None) != (label_available_ts_ns is None):
        raise ValueError("outcome and label_available_ts_ns must be provided together")
    if label_available_ts_ns is not None:
        market_end_ts_ns = int(market.t1.timestamp() * 1_000_000_000)
        if (
            isinstance(label_available_ts_ns, bool)
            or not isinstance(label_available_ts_ns, int)
            or label_available_ts_ns < max(replay_end_ts_ns, market_end_ts_ns)
        ):
            raise ValueError("label availability must follow replay and market end")

    ordered_events = tuple(sorted(events, key=_event_sort_key))
    if any(_event_available_ts_ns(event) > replay_end_ts_ns for event in ordered_events):
        raise ValueError("events must not extend beyond replay_end_ts_ns")

    portfolio.activate_market(market, rules=rules)
    event_index = 0
    replayed_decisions: list[PaperReplayDecision] = []
    for timestamp_ns in decisions:
        while (
            event_index < len(ordered_events)
            and _event_available_ts_ns(ordered_events[event_index]) <= timestamp_ns
        ):
            portfolio.on_event(ordered_events[event_index])
            event_index += 1
        replayed_decisions.append(
            PaperReplayDecision(
                decision_ts_ns=timestamp_ns,
                result=portfolio.decide(now_ts_ns=timestamp_ns),
            )
        )
    while event_index < len(ordered_events):
        portfolio.on_event(ordered_events[event_index])
        event_index += 1
    portfolio.advance(now_ts_ns=replay_end_ts_ns)
    if outcome is not None and label_available_ts_ns is not None:
        portfolio.settle(
            market_slug=market.slug,
            outcome=outcome,
            label_available_ts_ns=label_available_ts_ns,
        )
    return PaperReplayResult(
        decisions=tuple(replayed_decisions),
        processed_event_count=event_index,
        replay_end_ts_ns=replay_end_ts_ns,
    )


def _event_available_ts_ns(event: RawCollectorEvent) -> int:
    if not isinstance(event, RawCollectorEvent):
        raise TypeError("events must contain RawCollectorEvent values")
    return int(event.timing.available_ts.timestamp() * 1_000_000_000)


def _datetime_from_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


def _wire_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _wire_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def _event_sort_key(event: RawCollectorEvent) -> tuple[int, str, int, str, str, str]:
    return (
        _event_available_ts_ns(event),
        event.collector_session_id,
        event.admission_sequence,
        event.timing.source,
        event.timing.instrument,
        event.timing.sequence_or_hash,
    )


__all__ = [
    "PaperReplayDecision",
    "PaperReplayRawStream",
    "PaperReplayResult",
    "build_replay_binance_history",
    "forward_raw_event_to_collector_event",
    "load_research_paper_events",
    "replay_research_paper",
]
