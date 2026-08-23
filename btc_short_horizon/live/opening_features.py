"""Bounded live adapter for the causal opening-feature module."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from btc_short_horizon.data.binance import normalize_binance_trade
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.data.okx import OkxBookSynchronizer, normalize_okx_trade
from btc_short_horizon.data.polymarket import PolymarketL2Normalizer
from btc_short_horizon.data.rtds import normalize_chainlink_btc_usd
from btc_short_horizon.features.events import BtcBookTop
from btc_short_horizon.features.opening import OpeningFeatureObservation, OpeningFeatureState


class LiveOpeningFeatureAdapter:
    """Consume admitted events once and expose the shared causal snapshot interface."""

    def __init__(
        self,
        *,
        up_token_id: str,
        down_token_id: str,
        required_venue_sources: tuple[str, ...],
    ) -> None:
        self._state = OpeningFeatureState(
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            required_venue_sources=required_venue_sources,
        )
        self._polymarket = {
            token_id: PolymarketL2Normalizer(token_id=token_id)
            for token_id in (up_token_id, down_token_id)
        }
        self._okx = {
            "okx_spot": OkxBookSynchronizer(instrument="BTC-USDT", source="okx_spot"),
            "okx_swap": OkxBookSynchronizer(instrument="BTC-USDT-SWAP", source="okx_swap"),
        }
        self._seen_events: set[int] = set()

    def on_event(self, event: RawCollectorEvent) -> None:
        source = event.timing.source
        instrument = event.timing.instrument
        if not (
            source in self._state.required_venue_sources
            or source == "polymarket_rtds_chainlink"
            or (source == "polymarket_clob" and instrument in self._polymarket)
        ):
            return
        identity = hash(
            (
                event.collector_session_id,
                event.admission_sequence,
                event.timing.source,
                event.timing.instrument,
                event.event_type,
                event.timing.sequence_or_hash,
            )
        )
        if identity in self._seen_events:
            return
        self._seen_events.add(identity)
        if event.event_type == "continuity_gap":
            self._mark_gap(source=source, instrument=instrument)
            return
        receive_ts = event.timing.collector_receive_ts or event.timing.available_ts
        if source in {"binance_spot", "binance_perp"}:
            self._on_binance(event, receive_ts=receive_ts)
        elif source in self._okx:
            self._on_okx(event, receive_ts=receive_ts)
        elif source == "polymarket_rtds_chainlink":
            if event.event_type == "crypto_prices_chainlink":
                self._state.update(
                    normalize_chainlink_btc_usd(
                        event.payload,
                        collector_receive_ts=receive_ts,
                    ).reference
                )
        elif source == "polymarket_clob" and instrument in self._polymarket:
            result = self._polymarket[instrument].apply(
                event.payload,
                collector_receive_ts=receive_ts,
            )
            if result.book_top is not None:
                self._state.update(result.book_top)

    def snapshot(
        self,
        *,
        decision_ts_ns: int,
        market_window_start_ns: int,
    ) -> OpeningFeatureObservation:
        return self._state.snapshot(
            decision_ts_ns=decision_ts_ns,
            market_window_start_ns=market_window_start_ns,
        )

    def _mark_gap(self, *, source: str, instrument: str) -> None:
        if source == "polymarket_rtds_chainlink":
            self._state.mark_gap(source="chainlink", instrument="btc/usd")
        elif source in {"binance_spot", "binance_perp", "okx_spot", "okx_swap"}:
            self._state.mark_gap(source=source, instrument=instrument)
            synchronizer = self._okx.get(source)
            if synchronizer is not None:
                synchronizer.reset()
        elif source == "polymarket_clob" and instrument in self._polymarket:
            self._polymarket[instrument].reset()
            self._state.mark_gap(source=source, instrument=instrument)

    def _on_binance(self, event: RawCollectorEvent, *, receive_ts: datetime) -> None:
        payload = event.payload
        nested = payload.get("data")
        message = nested if isinstance(nested, Mapping) else payload
        if event.event_type in {"trade", "aggtrade"}:
            self._state.update(
                normalize_binance_trade(
                    message,
                    collector_receive_ts=receive_ts,
                    source=event.timing.source,
                ).trade
            )
            return
        if event.event_type != "partial_depth_snapshot":
            return
        bids = message.get("bids", message.get("b"))
        asks = message.get("asks", message.get("a"))
        if not isinstance(bids, (list, tuple)) or not bids:
            raise ValueError("Binance partial depth requires bids")
        if not isinstance(asks, (list, tuple)) or not asks:
            raise ValueError("Binance partial depth requires asks")
        bid, ask = bids[0], asks[0]
        if not isinstance(bid, (list, tuple)) or len(bid) < 2:
            raise ValueError("Binance partial depth bid is malformed")
        if not isinstance(ask, (list, tuple)) or len(ask) < 2:
            raise ValueError("Binance partial depth ask is malformed")
        available_ts_ns = int(event.timing.available_ts.timestamp() * 1_000_000_000)
        source_ts_ns = int(event.timing.source_ts.timestamp() * 1_000_000_000)
        self._state.update(
            BtcBookTop(
                source_ts_ns=source_ts_ns,
                available_ts_ns=available_ts_ns,
                bid=float(bid[0]),
                ask=float(ask[0]),
                bid_size=float(bid[1]),
                ask_size=float(ask[1]),
                source=event.timing.source,
                instrument=event.timing.instrument,
            )
        )

    def _on_okx(self, event: RawCollectorEvent, *, receive_ts: datetime) -> None:
        data = event.payload.get("data")
        if not isinstance(data, (list, tuple)) or len(data) != 1:
            raise ValueError("OKX admitted payload requires exactly one data item")
        item = data[0]
        if not isinstance(item, Mapping):
            raise ValueError("OKX admitted data item must be an object")
        if event.event_type == "trade":
            self._state.update(
                normalize_okx_trade(
                    item,
                    collector_receive_ts=receive_ts,
                    source=event.timing.source,
                ).trade
            )
            return
        if not event.event_type.startswith("books_"):
            return
        action = event.event_type.removeprefix("books_")
        normalized = dict(item)
        if action == "snapshot" and "prevSeqId" not in normalized:
            normalized["prevSeqId"] = -1
        applied = self._okx[event.timing.source].apply(
            action=action,
            payload=normalized,
            collector_receive_ts=receive_ts,
        )
        if applied.status.value == "gap":
            self._state.mark_gap(
                source=event.timing.source,
                instrument=event.timing.instrument,
            )
        elif applied.book_top is not None:
            self._state.update(applied.book_top)


__all__ = ["LiveOpeningFeatureAdapter"]
