"""Causal multi-venue features for the first three minutes of a BTC 15m market."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import erf, log, sqrt
from statistics import median
from typing import Iterable

from btc_short_horizon.features.events import BtcBookTop, BtcReferencePrice, BtcTrade
from btc_short_horizon.features.schema import FeatureSchema


_NANOS_PER_SECOND = 1_000_000_000
_VENUE_SOURCES = ("binance_spot", "binance_perp", "okx_spot", "okx_swap")
_WINDOWS = (1, 5, 15, 30, 60, 300)


def opening_feature_schema() -> FeatureSchema:
    names = [
        "elapsed_seconds",
        "remaining_seconds",
        "boundary_log_return",
        "remaining_sigma",
        "boundary_z",
        "p_boundary_up",
        "consensus_dispersion_bps",
        "chainlink_consensus_basis_bps",
    ]
    for source in _VENUE_SOURCES:
        prefix = source
        for seconds in _WINDOWS:
            names.extend(
                (
                    f"{prefix}_return_{seconds}s",
                    f"{prefix}_rv_{seconds}s",
                    f"{prefix}_flow_{seconds}s",
                )
            )
        names.extend(
            (
                f"{prefix}_book_imbalance",
                f"{prefix}_microprice_distance",
                f"{prefix}_spread_bps",
                f"{prefix}_trade_age_seconds",
                f"{prefix}_book_age_seconds",
            )
        )
    names.extend(("data_age_seconds", "has_data_gap"))
    return FeatureSchema(version="btc-opening-mispricing-v1", names=tuple(names))


@dataclass(frozen=True, slots=True)
class OpeningFeatureObservation:
    market_window_start_ns: int
    ts_event: int
    ts_init: int
    feature_schema_hash: str
    values: tuple[float, ...]
    p_boundary_up: float
    p_market_mid_up: float
    quality_flags: frozenset[str] = frozenset()

    @property
    def eligible(self) -> bool:
        return not self.quality_flags


@dataclass(slots=True)
class _VenueState:
    trades: deque[BtcTrade] = field(default_factory=deque)
    book: BtcBookTop | None = None
    last_available_ts_ns: int = 0
    gap: bool = False


@dataclass(frozen=True, slots=True)
class OpeningVenueSnapshot:
    """Bounded venue-only component used by the readiness materializer."""

    values: tuple[tuple[str, float], ...]
    latest_price: float | None
    rv_300: float
    flags: frozenset[str]
    ages: tuple[float, ...]
    gap: bool


@dataclass(frozen=True, slots=True)
class OpeningReferenceSnapshot:
    """Bounded Chainlink component used by the readiness materializer."""

    opening: BtcReferencePrice | None
    latest: BtcReferencePrice | None
    latest_age_seconds: float | None
    gap: bool


def market_probability_from_books(
    *,
    up: BtcBookTop | None,
    down: BtcBookTop | None,
    decision_ts_ns: int,
    stale_seconds: float = 1.0,
) -> tuple[float, frozenset[str]]:
    """Compute the PM complement probability from two bounded BBO snapshots."""

    if up is None or down is None:
        return 0.5, frozenset({"polymarket_book_unavailable"})
    up_age = (decision_ts_ns - up.available_ts_ns) / _NANOS_PER_SECOND
    down_age = (decision_ts_ns - down.available_ts_ns) / _NANOS_PER_SECOND
    flags: set[str] = set()
    if up_age > stale_seconds or down_age > stale_seconds:
        flags.add("polymarket_book_stale")
    up_mid = (up.bid + up.ask) / 2.0
    down_implied_up = 1.0 - ((down.bid + down.ask) / 2.0)
    if abs(up_mid - down_implied_up) > max(up.ask - up.bid, down.ask - down.bid):
        flags.add("polymarket_complement_mismatch")
    return (
        max(1e-6, min(1.0 - 1e-6, (up_mid + down_implied_up) / 2.0)),
        frozenset(flags),
    )


class OpeningFeatureState:
    """Per-stream state; no venue can regress or contaminate another venue's epoch."""

    def __init__(
        self,
        *,
        up_token_id: str,
        down_token_id: str,
        schema: FeatureSchema | None = None,
        required_venue_sources: tuple[str, ...] = _VENUE_SOURCES,
        venue_stale_seconds: float = 1.0,
        chainlink_stale_seconds: float = 10.0,
    ) -> None:
        if not up_token_id or not down_token_id or up_token_id == down_token_id:
            raise ValueError("distinct up_token_id and down_token_id are required")
        if not required_venue_sources or any(
            source not in _VENUE_SOURCES for source in required_venue_sources
        ):
            raise ValueError(
                "required_venue_sources must be a non-empty subset of supported venues"
            )
        if venue_stale_seconds <= 0.0 or chainlink_stale_seconds <= 0.0:
            raise ValueError("staleness limits must be > 0")
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.schema = schema or opening_feature_schema()
        self.required_venue_sources = required_venue_sources
        self.venue_stale_seconds = venue_stale_seconds
        self.chainlink_stale_seconds = chainlink_stale_seconds
        self._venues: dict[str, _VenueState] = {source: _VenueState() for source in _VENUE_SOURCES}
        self._polymarket: dict[str, _VenueState] = {
            up_token_id: _VenueState(),
            down_token_id: _VenueState(),
        }
        self._references: deque[BtcReferencePrice] = deque()
        self._reference_last_available_ts_ns = 0
        self._reference_gap = False

    def mark_gap(self, *, source: str, instrument: str) -> None:
        if source == "chainlink":
            if instrument != "btc/usd":
                raise ValueError(f"unknown Chainlink instrument {instrument!r}")
            self._references.clear()
            self._reference_last_available_ts_ns = 0
            self._reference_gap = True
            return
        state = self._state(source=source, instrument=instrument)
        state.trades.clear()
        state.book = None
        state.last_available_ts_ns = 0
        state.gap = True

    def venue_snapshot(
        self, *, source: str, decision_ts_ns: int
    ) -> OpeningVenueSnapshot:
        values, flags, latest_price, rv_300, ages = self._venue_values(
            source=source,
            decision_ts_ns=decision_ts_ns,
        )
        return OpeningVenueSnapshot(
            values=tuple(sorted(values.items())),
            latest_price=latest_price,
            rv_300=rv_300,
            flags=frozenset(flags),
            ages=tuple(ages),
            gap=self._venues[source].gap,
        )

    def reference_snapshot(
        self, *, decision_ts_ns: int, market_window_start_ns: int
    ) -> OpeningReferenceSnapshot:
        opening, latest = self._reference_prices(
            decision_ts_ns=decision_ts_ns,
            market_window_start_ns=market_window_start_ns,
        )
        latest_age = (
            None
            if latest is None
            else (decision_ts_ns - latest.available_ts_ns) / _NANOS_PER_SECOND
        )
        return OpeningReferenceSnapshot(
            opening=opening,
            latest=latest,
            latest_age_seconds=latest_age,
            gap=self._reference_gap,
        )

    def update(self, event: BtcTrade | BtcBookTop | BtcReferencePrice) -> None:
        if isinstance(event, BtcReferencePrice):
            if event.available_ts_ns < self._reference_last_available_ts_ns:
                raise ValueError("Chainlink events must be ordered by available_ts_ns")
            self._reference_last_available_ts_ns = event.available_ts_ns
            self._references.append(event)
            self._prune_references(event.available_ts_ns)
            return
        state = self._state(source=event.source, instrument=event.instrument)
        if event.available_ts_ns < state.last_available_ts_ns:
            raise ValueError("stream events must be ordered by available_ts_ns")
        state.last_available_ts_ns = event.available_ts_ns
        if isinstance(event, BtcTrade):
            state.trades.append(event)
            self._prune_trades(state, event.available_ts_ns)
        else:
            state.book = event

    def snapshot(
        self, *, decision_ts_ns: int, market_window_start_ns: int
    ) -> OpeningFeatureObservation:
        if decision_ts_ns < market_window_start_ns:
            raise ValueError("decision_ts_ns must not precede market_window_start_ns")
        elapsed_seconds = (decision_ts_ns - market_window_start_ns) / _NANOS_PER_SECOND
        remaining_seconds = max(0.0, 900.0 - elapsed_seconds)
        flags: set[str] = set()
        venue_values: dict[str, float] = {}
        venue_prices: list[float] = []
        ages: list[float] = []
        primary_rv_300 = 0.0
        for source in _VENUE_SOURCES:
            values, source_flags, price, rv_300, source_ages = self._venue_values(
                source=source,
                decision_ts_ns=decision_ts_ns,
            )
            venue_values.update(values)
            if source in self.required_venue_sources:
                flags.update(source_flags)
                ages.extend(source_ages)
            if price is not None:
                venue_prices.append(price)
            if source == "binance_spot":
                primary_rv_300 = rv_300

        reference_open, reference_latest = self._reference_prices(
            decision_ts_ns=decision_ts_ns,
            market_window_start_ns=market_window_start_ns,
        )
        if reference_open is None or reference_latest is None:
            flags.add("reference_unavailable")
        else:
            reference_age = (decision_ts_ns - reference_latest.available_ts_ns) / _NANOS_PER_SECOND
            ages.append(reference_age)
            if reference_age > self.chainlink_stale_seconds:
                flags.add("reference_stale")

        consensus = median(venue_prices) if venue_prices else None
        if consensus is None:
            flags.add("consensus_unavailable")
        p_boundary_up, boundary_return, remaining_sigma, boundary_z = self._boundary_probability(
            consensus=consensus,
            opening_reference=reference_open.price if reference_open is not None else None,
            rv_300=primary_rv_300,
            remaining_seconds=remaining_seconds,
        )
        if remaining_sigma <= 0.0:
            flags.add("volatility_unavailable")

        p_market_mid_up, market_flags = self._market_probability(decision_ts_ns=decision_ts_ns)
        flags.update(market_flags)
        dispersion_bps = _dispersion_bps(venue_prices, consensus)
        latest_reference_price = reference_latest.price if reference_latest is not None else None
        chainlink_basis_bps = (
            ((consensus / latest_reference_price) - 1.0) * 10_000.0
            if consensus is not None and latest_reference_price is not None
            else 0.0
        )
        values = {
            "elapsed_seconds": elapsed_seconds,
            "remaining_seconds": remaining_seconds,
            "boundary_log_return": boundary_return,
            "remaining_sigma": remaining_sigma,
            "boundary_z": boundary_z,
            "p_boundary_up": p_boundary_up,
            "consensus_dispersion_bps": dispersion_bps,
            "chainlink_consensus_basis_bps": chainlink_basis_bps,
            **venue_values,
            "data_age_seconds": max(ages) if ages else 86_400.0,
            "has_data_gap": 1.0 if any(state.gap for state in self._venues.values()) else 0.0,
        }
        if any(state.gap for state in self._venues.values()) or self._reference_gap:
            flags.add("gap")
        return OpeningFeatureObservation(
            market_window_start_ns=market_window_start_ns,
            ts_event=decision_ts_ns,
            ts_init=decision_ts_ns,
            feature_schema_hash=self.schema.hash,
            values=self.schema.vector_from(values),
            p_boundary_up=p_boundary_up,
            p_market_mid_up=p_market_mid_up,
            quality_flags=frozenset(flags),
        )

    def _state(self, *, source: str, instrument: str) -> _VenueState:
        if source == "polymarket_clob":
            try:
                return self._polymarket[instrument]
            except KeyError as exc:
                raise ValueError(f"unknown Polymarket token {instrument!r}") from exc
        try:
            return self._venues[source]
        except KeyError as exc:
            raise ValueError(f"unsupported venue source {source!r}") from exc

    def _venue_values(
        self, *, source: str, decision_ts_ns: int
    ) -> tuple[dict[str, float], set[str], float | None, float, list[float]]:
        state = self._venues[source]
        prefix = source
        values: dict[str, float] = {}
        flags: set[str] = set()
        ages: list[float] = []
        latest_price = state.trades[-1].price if state.trades else None
        trade_age = (
            86_400.0
            if not state.trades
            else (decision_ts_ns - state.trades[-1].available_ts_ns) / _NANOS_PER_SECOND
        )
        book_age = (
            86_400.0
            if state.book is None
            else (decision_ts_ns - state.book.available_ts_ns) / _NANOS_PER_SECOND
        )
        ages.extend((trade_age, book_age))
        if source in self.required_venue_sources:
            if latest_price is None:
                flags.add(f"{source}_trade_unavailable")
            elif trade_age > self.venue_stale_seconds:
                flags.add(f"{source}_trade_stale")
            if state.book is None:
                flags.add(f"{source}_book_unavailable")
            elif book_age > self.venue_stale_seconds:
                flags.add(f"{source}_book_stale")
            if state.gap:
                flags.add(f"{source}_gap")
        rv_300 = 0.0
        for seconds in _WINDOWS:
            window = _trades_since(state.trades, decision_ts_ns - seconds * _NANOS_PER_SECOND)
            ret, rv, flow = _trade_window_values(window, latest_price)
            values[f"{prefix}_return_{seconds}s"] = ret
            values[f"{prefix}_rv_{seconds}s"] = rv
            values[f"{prefix}_flow_{seconds}s"] = flow
            if seconds == 300:
                rv_300 = rv
        imbalance, microprice_distance, spread_bps = _book_values(state.book)
        values.update(
            {
                f"{prefix}_book_imbalance": imbalance,
                f"{prefix}_microprice_distance": microprice_distance,
                f"{prefix}_spread_bps": spread_bps,
                f"{prefix}_trade_age_seconds": min(trade_age, 86_400.0),
                f"{prefix}_book_age_seconds": min(book_age, 86_400.0),
            }
        )
        return values, flags, latest_price, rv_300, ages

    def _reference_prices(
        self, *, decision_ts_ns: int, market_window_start_ns: int
    ) -> tuple[BtcReferencePrice | None, BtcReferencePrice | None]:
        available = tuple(
            item for item in self._references if item.available_ts_ns <= decision_ts_ns
        )
        if not available:
            return None, None
        opening = [item for item in available if item.source_ts_ns <= market_window_start_ns]
        return (opening[-1] if opening else None), available[-1]

    def _market_probability(self, *, decision_ts_ns: int) -> tuple[float, set[str]]:
        up = self._polymarket[self.up_token_id].book
        down = self._polymarket[self.down_token_id].book
        flags: set[str] = set()
        if up is None or down is None:
            return 0.5, {"polymarket_book_unavailable"}
        up_age = (decision_ts_ns - up.available_ts_ns) / _NANOS_PER_SECOND
        down_age = (decision_ts_ns - down.available_ts_ns) / _NANOS_PER_SECOND
        if up_age > self.venue_stale_seconds or down_age > self.venue_stale_seconds:
            flags.add("polymarket_book_stale")
        up_mid = (up.bid + up.ask) / 2.0
        down_implied_up = 1.0 - ((down.bid + down.ask) / 2.0)
        if abs(up_mid - down_implied_up) > max(up.ask - up.bid, down.ask - down.bid):
            flags.add("polymarket_complement_mismatch")
        return max(1e-6, min(1.0 - 1e-6, (up_mid + down_implied_up) / 2.0)), flags

    @staticmethod
    def _boundary_probability(
        *,
        consensus: float | None,
        opening_reference: float | None,
        rv_300: float,
        remaining_seconds: float,
    ) -> tuple[float, float, float, float]:
        if (
            consensus is None
            or opening_reference is None
            or rv_300 <= 0.0
            or remaining_seconds <= 0.0
        ):
            return 0.5, 0.0, 0.0, 0.0
        boundary_return = log(consensus / opening_reference)
        remaining_sigma = rv_300 * sqrt(remaining_seconds / 300.0)
        if remaining_sigma <= 0.0:
            return 0.5, boundary_return, 0.0, 0.0
        z = boundary_return / remaining_sigma
        p_up = 0.5 * (1.0 + erf(z / sqrt(2.0)))
        return max(1e-6, min(1.0 - 1e-6, p_up)), boundary_return, remaining_sigma, z

    @staticmethod
    def _prune_trades(state: _VenueState, now_ns: int) -> None:
        cutoff = now_ns - 300 * _NANOS_PER_SECOND
        while state.trades and state.trades[0].available_ts_ns < cutoff:
            state.trades.popleft()

    def _prune_references(self, now_ns: int) -> None:
        cutoff = now_ns - 1_800 * _NANOS_PER_SECOND
        while self._references and self._references[0].available_ts_ns < cutoff:
            self._references.popleft()


def build_opening_feature_observations(
    *,
    state: OpeningFeatureState,
    events: Iterable[BtcTrade | BtcBookTop | BtcReferencePrice],
    decision_times_ns: Iterable[int],
    market_window_start_ns: int,
) -> tuple[OpeningFeatureObservation, ...]:
    ordered_events = tuple(sorted(events, key=lambda event: event.available_ts_ns))
    decisions = tuple(sorted(int(value) for value in decision_times_ns))
    observations: list[OpeningFeatureObservation] = []
    event_index = 0
    for decision_ts_ns in decisions:
        while (
            event_index < len(ordered_events)
            and ordered_events[event_index].available_ts_ns <= decision_ts_ns
        ):
            state.update(ordered_events[event_index])
            event_index += 1
        observations.append(
            state.snapshot(
                decision_ts_ns=decision_ts_ns,
                market_window_start_ns=market_window_start_ns,
            )
        )
    return tuple(observations)


def _trades_since(trades: deque[BtcTrade], cutoff_ns: int) -> tuple[BtcTrade, ...]:
    return tuple(trade for trade in trades if trade.available_ts_ns >= cutoff_ns)


def _trade_window_values(
    window: tuple[BtcTrade, ...], latest_price: float | None
) -> tuple[float, float, float]:
    if not window or latest_price is None:
        return 0.0, 0.0, 0.0
    log_returns = [
        log(current.price / previous.price) for previous, current in zip(window, window[1:])
    ]
    quote_notional = sum(trade.price * trade.quantity for trade in window)
    signed_notional = sum(
        trade.price * trade.quantity
        if trade.aggressor_side == "buy"
        else -trade.price * trade.quantity
        if trade.aggressor_side == "sell"
        else 0.0
        for trade in window
    )
    return (
        log(latest_price / window[0].price),
        sqrt(sum(value * value for value in log_returns)),
        signed_notional / quote_notional if quote_notional > 0.0 else 0.0,
    )


def _book_values(book: BtcBookTop | None) -> tuple[float, float, float]:
    if book is None:
        return 0.0, 0.0, 0.0
    total = book.bid_size + book.ask_size
    midpoint = (book.bid + book.ask) / 2.0
    microprice = (book.ask * book.bid_size + book.bid * book.ask_size) / total
    return (
        (book.bid_size - book.ask_size) / total,
        (microprice / midpoint) - 1.0,
        ((book.ask - book.bid) / midpoint) * 10_000.0,
    )


def _dispersion_bps(prices: list[float], consensus: float | None) -> float:
    if consensus is None or len(prices) < 2:
        return 0.0
    return max(abs((price / consensus) - 1.0) for price in prices) * 10_000.0
