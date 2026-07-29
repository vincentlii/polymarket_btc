"""Bounded stream-quality validation and explicit data epoch boundaries."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from btc_short_horizon.data.contracts import TimedMarketEvent


@dataclass(frozen=True, slots=True)
class DataQualityDecision:
    accepted: bool
    epoch_id: int
    reason: str
    stale: bool = False


@dataclass(frozen=True, slots=True)
class DataQualityStats:
    total_events: int
    accepted_events: int
    duplicate_events: int
    out_of_order_events: int
    stale_events: int
    gap_events: int
    epochs: int


@dataclass(frozen=True, slots=True)
class PreparedQualityEvent:
    """One O(1) validation decision that can be committed after capacity admission."""

    event: TimedMarketEvent
    identifier: tuple[str, str, str, str]
    decision: DataQualityDecision
    validator_revision: int


class EventQualityValidator:
    """Rejects unsafe ordering/duplicates and starts a new epoch at every gap."""

    def __init__(
        self,
        *,
        max_seen_identifiers: int = 100_000,
        max_transport_delay: timedelta = timedelta(seconds=1),
    ) -> None:
        if max_seen_identifiers < 1:
            raise ValueError("max_seen_identifiers must be >= 1")
        if max_transport_delay < timedelta(0):
            raise ValueError("max_transport_delay must be non-negative")
        self.max_seen_identifiers = max_seen_identifiers
        self.max_transport_delay = max_transport_delay
        self._seen_identifiers: set[tuple[str, str, str, str]] = set()
        self._seen_order: deque[tuple[str, str, str, str]] = deque()
        self._last_available_ts = None
        self._epoch_id = 0
        self._total_events = 0
        self._accepted_events = 0
        self._duplicate_events = 0
        self._out_of_order_events = 0
        self._stale_events = 0
        self._gap_events = 0
        self._revision = 0

    @property
    def epoch_id(self) -> int:
        return self._epoch_id

    @property
    def last_available_ts(self) -> datetime | None:
        """Return the causal high-water mark used for ordering decisions."""

        return self._last_available_ts

    @property
    def stats(self) -> DataQualityStats:
        return DataQualityStats(
            total_events=self._total_events,
            accepted_events=self._accepted_events,
            duplicate_events=self._duplicate_events,
            out_of_order_events=self._out_of_order_events,
            stale_events=self._stale_events,
            gap_events=self._gap_events,
            epochs=self._epoch_id + 1,
        )

    def mark_gap(self, *, reason: str) -> DataQualityDecision:
        """Create a new epoch before accepting data after a detected continuity gap."""

        if not reason:
            raise ValueError("gap reason is required")
        self._revision += 1
        return self._start_gap(reason=reason)

    def prepare(self, event: TimedMarketEvent) -> PreparedQualityEvent:
        """Classify without mutation so callers can enforce capacity first."""

        identifier = (
            event.source,
            event.instrument,
            event.schema_version,
            event.sequence_or_hash,
        )
        if identifier in self._seen_identifiers:
            decision = DataQualityDecision(False, self._epoch_id, "duplicate")
        elif self._last_available_ts is not None and event.available_ts < self._last_available_ts:
            decision = DataQualityDecision(False, self._epoch_id + 1, "out_of_order")
        else:
            stale = False
            if event.collector_receive_ts is not None:
                transport_delay = event.collector_receive_ts - event.source_ts
                stale = (
                    transport_delay > self.max_transport_delay
                    or transport_delay < -self.max_transport_delay
                )
            decision = DataQualityDecision(True, self._epoch_id, "accepted", stale=stale)
        return PreparedQualityEvent(
            event=event,
            identifier=identifier,
            decision=decision,
            validator_revision=self._revision,
        )

    def commit(self, prepared: PreparedQualityEvent) -> DataQualityDecision:
        """Apply exactly one prior decision, rejecting stale transactional plans."""

        if prepared.validator_revision != self._revision:
            raise RuntimeError("quality decision is stale")
        self._revision += 1
        self._total_events += 1
        decision = prepared.decision
        if decision.reason == "duplicate":
            self._duplicate_events += 1
            return decision
        if decision.reason == "out_of_order":
            self._out_of_order_events += 1
            self._start_gap(reason="available_time_regression")
            return decision
        if not decision.accepted:
            raise RuntimeError(f"unsupported quality decision: {decision.reason}")
        if decision.stale:
            self._stale_events += 1
        self._remember(prepared.identifier)
        self._last_available_ts = prepared.event.available_ts
        self._accepted_events += 1
        return decision

    def _start_gap(self, *, reason: str) -> DataQualityDecision:
        self._gap_events += 1
        self._epoch_id += 1
        self._last_available_ts = None
        self._seen_identifiers.clear()
        self._seen_order.clear()
        return DataQualityDecision(False, self._epoch_id, f"gap:{reason}")

    def observe(self, event: TimedMarketEvent) -> DataQualityDecision:
        """Assess one normalized event; caller stores only accepted events in the epoch."""

        prepared = self.prepare(event)
        return self.commit(prepared)

    def _remember(self, identifier: tuple[str, str, str, str]) -> None:
        self._seen_identifiers.add(identifier)
        self._seen_order.append(identifier)
        if len(self._seen_order) > self.max_seen_identifiers:
            expired = self._seen_order.popleft()
            self._seen_identifiers.discard(expired)
