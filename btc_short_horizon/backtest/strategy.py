"""Nautilus adapter for the BTC 15m Opening Mispricing maker strategy."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from functools import partial
from typing import Any

from nautilus_trader.model.data import DataType
from nautilus_trader.model.enums import BookType, LiquiditySide, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from prediction_market_extensions.backtesting._backtest_runtime import (
    BACKTEST_CUSTOM_DATA_CLIENT_ID,
)

from btc_short_horizon.backtest.audit import OrderAuditTrail
from btc_short_horizon.backtest.signals import (
    BtcOpeningMispricingSignal,
    BtcReplayBoundary,
    opening_signal_data_age_seconds,
    opening_signal_entry_rejection_reason,
    validate_opening_mispricing_signal,
)
from btc_short_horizon.strategy import (
    ConsecutiveSignalConfirmation,
    LayerStructure,
    MakerOrderLayer,
    MakerStrategyConfig,
    MarketExecution,
    OrderPlan,
    OutcomeBooks,
    SideBook,
    StrategyPhase,
    TokenSide,
    VisibleBookLevel,
    evaluate_cancellation,
    plan_opening_mispricing_orders,
)


def _as_float(value: object | None) -> float | None:
    if value is None:
        return None
    as_double = getattr(value, "as_double", None)
    if callable(as_double):
        return float(as_double())
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_ts_ns(event: object) -> int:
    for name in ("ts_init", "ts_event"):
        try:
            timestamp = int(getattr(event, name, None))
        except (TypeError, ValueError):
            continue
        if timestamp >= 0:
            return timestamp
    return 0


_WORK_EXPIRY_TIMER = "btc-opening-work-expiry"
_MARKOUT_HORIZONS_SECONDS = (1, 3, 10, 30, 60)


class BtcOpeningMispricingConfig(StrategyConfig, frozen=True):  # type: ignore[call-arg]
    """Serializable configuration for one dual-token 15m opening-window strategy."""

    market_slug: str
    up_instrument_id: InstrumentId
    down_instrument_id: InstrumentId
    max_shares: Decimal = Decimal("1")
    layer_structure: str = LayerStructure.SINGLE.value
    safety_buffer: float = 0.01
    minimum_edge: float = 0.0
    entry_start_seconds: float = 3.0
    entry_end_seconds: float = 180.0
    confirmation_signals: int = 2
    signal_cadence_seconds: float = 5.0
    signal_cadence_tolerance_seconds: float = 0.25
    max_work_seconds: float = 60.0
    stale_after_seconds: float = 1.0
    cancel_probability_drop: float = 0.03
    max_visible_depth_fraction: float = 0.05
    price_level_tick_offsets: tuple[int, ...] = (0,)

    def __post_init__(self) -> None:
        if not self.market_slug or not self.market_slug.strip():
            raise ValueError("market_slug is required")
        if self.up_instrument_id == self.down_instrument_id:
            raise ValueError("up_instrument_id and down_instrument_id must differ")
        if self.max_shares <= 0:
            raise ValueError("max_shares must be > 0")
        self.maker_config()

    def maker_config(self) -> MakerStrategyConfig:
        return MakerStrategyConfig(
            structure=LayerStructure(self.layer_structure),
            max_shares=float(self.max_shares),
            safety_buffer=self.safety_buffer,
            minimum_edge=self.minimum_edge,
            entry_start_seconds=self.entry_start_seconds,
            entry_end_seconds=self.entry_end_seconds,
            confirmation_signals=self.confirmation_signals,
            signal_cadence_seconds=self.signal_cadence_seconds,
            signal_cadence_tolerance_seconds=self.signal_cadence_tolerance_seconds,
            max_work_seconds=self.max_work_seconds,
            stale_after_seconds=self.stale_after_seconds,
            cancel_probability_drop=self.cancel_probability_drop,
            max_visible_depth_fraction=self.max_visible_depth_fraction,
            price_level_tick_offsets=self.price_level_tick_offsets,
        )


class BtcOpeningMispricingStrategy(Strategy):
    """One-cycle, post-only strategy driven by causal fair probability versus price."""

    def __init__(self, config: BtcOpeningMispricingConfig) -> None:
        super().__init__(config)
        self._maker_config = config.maker_config()
        self._execution = MarketExecution(market_slug=config.market_slug)
        self._instruments: dict[TokenSide, Any] = {}
        self._latest_signal: BtcOpeningMispricingSignal | None = None
        self._last_book_ts_ns: dict[InstrumentId, int] = {}
        self._active_orders: dict[str, Any] = {}
        self._confirmation = ConsecutiveSignalConfirmation(
            required_signals=self._maker_config.confirmation_signals,
            cadence_seconds=self._maker_config.signal_cadence_seconds,
            tolerance_seconds=self._maker_config.signal_cadence_tolerance_seconds,
        )
        self._seen_fill_ids: set[str] = set()
        self._any_order_rejected = False
        self._work_timer_set = False
        self._markout_timer_sequence = 0
        self._order_audit = OrderAuditTrail()

    @property
    def order_audit_events(self) -> tuple[dict[str, object], ...]:
        return self._order_audit.records

    def on_start(self) -> None:
        self._instruments = {
            TokenSide.UP: self.cache.instrument(self.config.up_instrument_id),
            TokenSide.DOWN: self.cache.instrument(self.config.down_instrument_id),
        }
        if any(instrument is None for instrument in self._instruments.values()):
            self.log.error("BTC opening-mispricing instruments are unavailable - stopping.")
            self.stop()
            return
        for instrument_id in (self.config.up_instrument_id, self.config.down_instrument_id):
            self.subscribe_order_book_deltas(instrument_id=instrument_id, book_type=BookType.L2_MBP)
        self.subscribe_data(
            DataType(BtcOpeningMispricingSignal),
            client_id=BACKTEST_CUSTOM_DATA_CLIENT_ID,
        )
        self.subscribe_data(
            DataType(BtcReplayBoundary),
            client_id=BACKTEST_CUSTOM_DATA_CLIENT_ID,
        )
        self._execution = self._execution.begin_monitoring()

    def on_data(self, data) -> None:  # type: ignore[no-untyped-def]
        if (
            not isinstance(data, BtcOpeningMispricingSignal)
            or data.market_slug != self.config.market_slug
        ):
            return
        try:
            validate_opening_mispricing_signal(data)
        except (TypeError, ValueError) as exc:
            self.log.warning(f"Ignoring invalid BTC opening-mispricing signal: {exc}")
            self._reset_candidate()
            return
        self._latest_signal = data
        self._process_signal(now_ts_ns=int(data.ts_init))

    def on_order_book_deltas(self, deltas) -> None:  # type: ignore[no-untyped-def]
        instrument_id = getattr(deltas, "instrument_id", None)
        side = self._side_for_instrument(instrument_id)
        if side is None:
            return
        now_ts_ns = _event_ts_ns(deltas)
        self._last_book_ts_ns[instrument_id] = now_ts_ns
        if self._execution.phase is StrategyPhase.ORDER_WORKING:
            rejection_reason = self._execution_book_rejection_reason(now_ts_ns=now_ts_ns)
            if rejection_reason is not None:
                self._request_cancel(rejection_reason, now_ts_ns=now_ts_ns)

    def on_order_accepted(self, event) -> None:  # type: ignore[no-untyped-def]
        client_order_id = str(getattr(event, "client_order_id", ""))
        if client_order_id not in self._active_orders:
            return
        self._order_audit.record(
            event_type="accepted",
            ts_ns=_event_ts_ns(event),
            client_order_id=client_order_id,
            venue_order_id=str(getattr(event, "venue_order_id", "")),
        )

    def on_order_filled(self, event) -> None:  # type: ignore[no-untyped-def]
        client_order_id = str(getattr(event, "client_order_id", ""))
        if client_order_id not in self._active_orders:
            return
        trade_id = str(getattr(event, "trade_id", ""))
        event_id = str(getattr(event, "id", ""))
        fill_id = event_id or (
            f"{client_order_id}:{trade_id}:{_event_ts_ns(event)}:"
            f"{getattr(event, 'last_qty', '')}:{getattr(event, 'last_px', '')}"
        )
        if fill_id in self._seen_fill_ids:
            return
        self._seen_fill_ids.add(fill_id)
        fill_size = _as_float(getattr(event, "last_qty", None))
        fill_price = _as_float(getattr(event, "last_px", None))
        if (
            fill_size is None
            or fill_size <= 0.0
            or fill_price is None
            or not 0.0 < fill_price < 1.0
        ):
            self.log.error("A BTC opening-mispricing fill has invalid price or quantity.")
            self.stop()
            return
        tracked_order = self._active_orders[client_order_id]
        plan = self._execution.plan
        if plan is None:
            self.log.error("A BTC opening-mispricing fill arrived without an active plan.")
            self.stop()
            return
        self._order_audit.record(
            event_type="fill",
            ts_ns=_event_ts_ns(event),
            client_order_id=client_order_id,
            instrument_id=str(getattr(tracked_order, "instrument_id", "")),
            side=plan.side.value,
            size=fill_size,
            price=fill_price,
            p_boundary=plan.p_boundary,
            p_fair=plan.p_fair,
            p_market=plan.p_market,
            net_edge=plan.net_edge(fill_price),
            event_id=event_id,
            trade_id=trade_id,
            venue_order_id=str(getattr(event, "venue_order_id", "")),
            liquidity_side=getattr(
                getattr(event, "liquidity_side", None),
                "name",
                str(getattr(event, "liquidity_side", "")),
            ),
            commission=str(getattr(event, "commission", "")),
        )
        self._schedule_fill_markouts(
            client_order_id=client_order_id,
            trade_id=trade_id,
            instrument_id=getattr(tracked_order, "instrument_id"),
            fill_price=fill_price,
            fill_ts_ns=_event_ts_ns(event),
        )
        try:
            self._execution = self._execution.record_fill(fill_size)
        except ValueError as exc:
            self.log.error(f"BTC opening-mispricing fill lifecycle error: {exc}")
            self.stop()
            return
        liquidity_side = getattr(event, "liquidity_side", None)
        if liquidity_side is not LiquiditySide.MAKER:
            self.log.error("A post-only BTC order received a non-maker fill - stopping.")
            self.stop()
        cached_order = self.cache.order(getattr(event, "client_order_id", None))
        is_closed = getattr(cached_order, "is_closed", False)
        if callable(is_closed):
            is_closed = is_closed()
        if is_closed:
            self._active_orders.pop(client_order_id, None)
        if self._execution.phase is StrategyPhase.FILLED:
            self._clear_work_expiry()
            return
        if self._execution.phase is StrategyPhase.ORDER_WORKING:
            self._request_cancel("partial_fill", now_ts_ns=_event_ts_ns(event))

    def on_order_canceled(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_order_event(event, terminal_event="cancel_ack", rejected=False)

    def on_order_expired(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_order_event(event, terminal_event="expired", rejected=False)

    def on_order_rejected(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_order_event(event, terminal_event="rejected", rejected=True)

    def on_order_denied(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_order_event(event, terminal_event="denied", rejected=True)

    def on_order_cancel_rejected(self, event) -> None:  # type: ignore[no-untyped-def]
        order_id = str(getattr(event, "client_order_id", ""))
        if order_id not in self._active_orders:
            return
        self._order_audit.record(
            event_type="cancel_rejected",
            ts_ns=_event_ts_ns(event),
            client_order_id=order_id,
            reason=str(getattr(event, "reason", "")),
        )
        if self._execution.phase is StrategyPhase.CANCEL_REQUESTED:
            self._execution = self._execution.reject_cancel()

    def on_stop(self) -> None:
        if self._execution.phase is StrategyPhase.ORDER_WORKING:
            self._request_cancel("strategy_stop", now_ts_ns=int(self.clock.timestamp_ns()))

    def on_reset(self) -> None:
        self._execution = MarketExecution(market_slug=self.config.market_slug)
        self._instruments.clear()
        self._latest_signal = None
        self._last_book_ts_ns.clear()
        self._active_orders.clear()
        self._reset_candidate()
        self._seen_fill_ids.clear()
        self._any_order_rejected = False
        self._clear_work_expiry()

    def _process_signal(self, *, now_ts_ns: int) -> None:
        signal = self._latest_signal
        if signal is None or now_ts_ns <= 0:
            return
        if self._execution.phase is StrategyPhase.MONITORING:
            self._submit_plan_if_actionable(signal=signal, now_ts_ns=now_ts_ns)
        elif self._execution.phase is StrategyPhase.ORDER_WORKING:
            self._evaluate_working_orders(signal=signal, now_ts_ns=now_ts_ns)

    def _submit_plan_if_actionable(
        self, *, signal: BtcOpeningMispricingSignal, now_ts_ns: int
    ) -> None:
        rejection_reason = opening_signal_entry_rejection_reason(
            signal,
            now_ts_ns=now_ts_ns,
            stale_after_seconds=self._maker_config.stale_after_seconds,
        )
        if rejection_reason is not None:
            self._reset_candidate()
            return
        books = self._outcome_books()
        if books is None:
            self._reset_candidate()
            return
        elapsed_seconds = (
            int(signal.ts_init) - int(signal.market_window_start_ts_ns)
        ) / 1_000_000_000
        decision = plan_opening_mispricing_orders(
            market_slug=self.config.market_slug,
            p_boundary_up=float(signal.p_boundary_up),
            p_up=float(signal.p_up),
            books=books,
            decision_ts_ns=max(now_ts_ns, int(signal.ts_init)),
            elapsed_seconds=elapsed_seconds,
            config=self._maker_config,
        )
        if decision.plan is None:
            self._reset_candidate()
            return
        if not self._confirm_candidate(
            side=decision.plan.side,
            signal_ts_ns=int(signal.ts_init),
        ):
            return
        materialized = self._materialize_plan(decision.plan)
        if materialized is None:
            self.log.warning("Opening-mispricing plan has no exchange-valid order layers.")
            return
        book_ages = {
            side: self._book_age_seconds(side=side, now_ts_ns=now_ts_ns)
            for side in (TokenSide.UP, TokenSide.DOWN)
        }
        if any(age is None or age < 0.0 for age in book_ages.values()):
            self.log.warning("Opening-mispricing plan lacks causal book timestamps.")
            self._reset_candidate()
            return
        if max(book_ages.values()) > self._maker_config.stale_after_seconds:
            self.log.warning("Opening-mispricing plan uses a stale execution book.")
            self._reset_candidate()
            return
        self._order_audit.record(
            event_type="plan",
            ts_ns=materialized.created_ts_ns,
            side=materialized.side.value,
            p_boundary=materialized.p_boundary,
            p_fair=materialized.p_fair,
            p_market=materialized.p_market,
            model_edge=materialized.model_edge,
            net_edge=materialized.net_edge(materialized.layers[0].price),
            layer_count=len(materialized.layers),
            confirmation_signals=self._confirmation.count,
            up_book_age_seconds=book_ages[TokenSide.UP],
            down_book_age_seconds=book_ages[TokenSide.DOWN],
            entry_book_age_seconds_max=max(book_ages.values()),
        )
        self._execution = self._execution.submit_plan(materialized)
        self._submit_orders(materialized)

    def _evaluate_working_orders(
        self, *, signal: BtcOpeningMispricingSignal, now_ts_ns: int
    ) -> None:
        plan = self._execution.plan
        if plan is None:
            return
        book_rejection_reason = self._execution_book_rejection_reason(now_ts_ns=now_ts_ns)
        if book_rejection_reason is not None:
            self._request_cancel(book_rejection_reason, now_ts_ns=now_ts_ns)
            return
        selected_probability = (
            float(signal.p_up) if plan.side is TokenSide.UP else 1.0 - float(signal.p_up)
        )
        assessment = evaluate_cancellation(
            plan=plan,
            now_ts_ns=now_ts_ns,
            selected_probability=selected_probability,
            data_age_seconds=opening_signal_data_age_seconds(signal, now_ts_ns=now_ts_ns),
            has_data_gap=bool(signal.has_data_gap),
            structure_valid=bool(signal.structure_valid),
            tick_unchanged=bool(signal.tick_unchanged),
            fee_unchanged=bool(signal.fee_unchanged),
            latency_healthy=bool(signal.latency_healthy),
            config=self._maker_config,
        )
        if assessment.should_cancel:
            self._request_cancel(assessment.reason, now_ts_ns=now_ts_ns)

    def _outcome_books(self) -> OutcomeBooks | None:
        up = self._side_book(TokenSide.UP)
        down = self._side_book(TokenSide.DOWN)
        if up is None or down is None:
            return None
        try:
            return OutcomeBooks(up=up, down=down)
        except ValueError:
            return None

    def _side_book(self, side: TokenSide) -> SideBook | None:
        book = self.cache.order_book(self._instrument_id_for(side))
        instrument = self._instruments.get(side)
        if book is None or instrument is None:
            return None
        tick_size = _as_float(getattr(instrument, "price_increment", None))
        minimum_order_size = _as_float(getattr(instrument, "min_quantity", None))
        if tick_size is None or minimum_order_size is None:
            return None
        bids = self._visible_levels(book.bids())
        asks = self._visible_levels(book.asks())
        try:
            return SideBook(
                token_id=str(self._instrument_id_for(side)),
                bids=bids,
                asks=asks,
                tick_size=tick_size,
                minimum_order_size=minimum_order_size,
            )
        except ValueError:
            return None

    @staticmethod
    def _visible_levels(levels: list[Any]) -> tuple[VisibleBookLevel, ...]:
        normalized: list[VisibleBookLevel] = []
        for level in levels:
            price = _as_float(getattr(level, "price", None))
            raw_size = getattr(level, "size", None)
            size = _as_float(raw_size() if callable(raw_size) else raw_size)
            if price is None or size is None:
                continue
            normalized.append(VisibleBookLevel(price=price, size=size))
        return tuple(normalized)

    def _materialize_plan(self, plan: OrderPlan) -> OrderPlan | None:
        instrument = self._instruments[plan.side]
        layers: list[MakerOrderLayer] = []
        for layer in plan.layers:
            try:
                quantity = instrument.make_qty(layer.size, round_down=True)
                price = instrument.make_price(layer.price)
            except ValueError:
                return None
            actual_size = _as_float(quantity)
            actual_price = _as_float(price)
            if actual_size is None or actual_price is None or actual_size <= 0.0:
                return None
            if actual_price > layer.price + 1e-12:
                self.log.warning(
                    "Skipping layer whose venue price rounding could make it aggressive."
                )
                return None
            minimum_order_size = _as_float(getattr(instrument, "min_quantity", None))
            if minimum_order_size is None or actual_size + 1e-12 < minimum_order_size:
                return None
            materialized = MakerOrderLayer(
                price=actual_price,
                size=actual_size,
                visible_size=layer.visible_size,
                queue_ahead=layer.queue_ahead,
            )
            if (
                materialized.visible_depth_fraction
                > self._maker_config.max_visible_depth_fraction + 1e-12
            ):
                return None
            layers.append(materialized)
        if len(layers) != len(plan.layers):
            return None
        return replace(plan, layers=tuple(layers))

    def _submit_orders(self, plan: OrderPlan) -> None:
        instrument = self._instruments[plan.side]
        pending_orders: list[tuple[Any, int, MakerOrderLayer]] = []
        for layer_index, layer in enumerate(plan.layers):
            try:
                order = self.order_factory.limit(
                    instrument_id=instrument.id,
                    order_side=OrderSide.BUY,
                    quantity=instrument.make_qty(layer.size, round_down=True),
                    price=instrument.make_price(layer.price),
                    time_in_force=TimeInForce.GTC,
                    post_only=True,
                    tags=[
                        "btc_opening_mispricing_maker",
                        f"market={self.config.market_slug}",
                        f"layer={layer_index}",
                        f"p_fair={plan.p_fair:.6f}",
                        f"p_market={plan.p_market:.6f}",
                        f"edge={plan.net_edge(layer.price):.6f}",
                    ],
                )
            except ValueError as exc:
                self.log.warning(f"Rejecting invalid opening-mispricing plan: {exc}")
                pending_orders.clear()
                break
            pending_orders.append((order, layer_index, layer))
        if len(pending_orders) != len(plan.layers):
            self._execution = self._execution.reject_order()
            self._clear_work_expiry()
            return
        self.clock.set_time_alert_ns(
            _WORK_EXPIRY_TIMER,
            plan.expires_ts_ns,
            callback=self._on_work_expiry,
            allow_past=False,
        )
        self._work_timer_set = True
        for order, layer_index, layer in pending_orders:
            client_order_id = str(order.client_order_id)
            self._active_orders[client_order_id] = order
            self._order_audit.record(
                event_type="submit",
                ts_ns=plan.created_ts_ns,
                client_order_id=client_order_id,
                instrument_id=str(instrument.id),
                side=plan.side.value,
                layer_index=layer_index,
                price=layer.price,
                size=layer.size,
                p_boundary=plan.p_boundary,
                p_fair=plan.p_fair,
                p_market=plan.p_market,
                net_edge=plan.net_edge(layer.price),
                visible_size=layer.visible_size,
                visible_depth_fraction=layer.visible_depth_fraction,
                initial_queue_ahead=layer.queue_ahead,
                post_only=True,
                time_in_force=TimeInForce.GTC.name,
            )
        for order, _, _ in pending_orders:
            self.submit_order(order)

    def _request_cancel(self, reason: str, *, now_ts_ns: int) -> None:
        if self._execution.phase is not StrategyPhase.ORDER_WORKING:
            return
        self.log.info(f"Canceling BTC opening-mispricing orders: {reason}")
        self._execution = self._execution.request_cancel()
        for client_order_id, order in tuple(self._active_orders.items()):
            self._order_audit.record(
                event_type="cancel_request",
                ts_ns=max(0, now_ts_ns),
                client_order_id=client_order_id,
                reason=reason,
            )
            self.cancel_order(order)

    def _close_order_event(self, event: Any, *, terminal_event: str, rejected: bool) -> None:
        order_id = str(getattr(event, "client_order_id", ""))
        if order_id not in self._active_orders:
            return
        self._order_audit.record(
            event_type=terminal_event,
            ts_ns=_event_ts_ns(event),
            client_order_id=order_id,
        )
        self._any_order_rejected = self._any_order_rejected or rejected
        self._active_orders.pop(order_id, None)
        if self._active_orders:
            return
        if self._execution.phase is StrategyPhase.CANCEL_REQUESTED:
            self._execution = self._execution.acknowledge_cancel()
        elif self._execution.phase is StrategyPhase.ORDER_WORKING:
            self._execution = (
                self._execution.reject_order()
                if self._any_order_rejected and self._execution.filled_size == 0.0
                else self._execution.complete_working_orders()
            )
        self._clear_work_expiry()

    def _confirm_candidate(self, *, side: TokenSide, signal_ts_ns: int) -> bool:
        return self._confirmation.observe(side, signal_ts_ns=signal_ts_ns)

    def _reset_candidate(self) -> None:
        self._confirmation.reset()

    def _on_work_expiry(self, event: Any) -> None:
        self._work_timer_set = False
        if self._execution.phase is StrategyPhase.ORDER_WORKING:
            self._request_cancel("max_work_age", now_ts_ns=_event_ts_ns(event))

    def _schedule_fill_markouts(
        self,
        *,
        client_order_id: str,
        trade_id: str,
        instrument_id: InstrumentId,
        fill_price: float,
        fill_ts_ns: int,
    ) -> None:
        self._markout_timer_sequence += 1
        timer_sequence = self._markout_timer_sequence
        for horizon_seconds in _MARKOUT_HORIZONS_SECONDS:
            self.clock.set_time_alert_ns(
                f"btc-opening-markout-{timer_sequence}-{horizon_seconds}",
                fill_ts_ns + horizon_seconds * 1_000_000_000,
                callback=partial(
                    self._on_fill_markout,
                    client_order_id=client_order_id,
                    trade_id=trade_id,
                    instrument_id=instrument_id,
                    fill_price=fill_price,
                    horizon_seconds=horizon_seconds,
                ),
                allow_past=False,
            )

    def _on_fill_markout(
        self,
        event: Any,
        *,
        client_order_id: str,
        trade_id: str,
        instrument_id: InstrumentId,
        fill_price: float,
        horizon_seconds: int,
    ) -> None:
        book = self.cache.order_book(instrument_id)
        markout_ts_ns = _event_ts_ns(event)
        book_ts_ns = self._last_book_ts_ns.get(instrument_id)
        midpoint = _as_float(book.midpoint()) if book is not None else None
        valid_midpoint = (
            midpoint is not None
            and 0.0 < midpoint < 1.0
            and book_ts_ns is not None
            and 0 <= book_ts_ns <= markout_ts_ns
        )
        self._order_audit.record(
            event_type="fill_markout",
            ts_ns=markout_ts_ns,
            client_order_id=client_order_id,
            trade_id=trade_id,
            instrument_id=str(instrument_id),
            horizon_seconds=horizon_seconds,
            fill_price=fill_price,
            book_ts_ns=book_ts_ns,
            book_age_seconds=(
                (markout_ts_ns - book_ts_ns) / 1_000_000_000
                if book_ts_ns is not None and book_ts_ns <= markout_ts_ns
                else None
            ),
            midpoint=midpoint if valid_midpoint else None,
            markout=midpoint - fill_price if valid_midpoint else None,
            available=valid_midpoint,
        )

    def _clear_work_expiry(self) -> None:
        if self._work_timer_set:
            self.clock.cancel_timer(_WORK_EXPIRY_TIMER)
            self._work_timer_set = False

    def _side_for_instrument(self, instrument_id: object) -> TokenSide | None:
        if instrument_id == self.config.up_instrument_id:
            return TokenSide.UP
        if instrument_id == self.config.down_instrument_id:
            return TokenSide.DOWN
        return None

    def _instrument_id_for(self, side: TokenSide) -> InstrumentId:
        return (
            self.config.up_instrument_id if side is TokenSide.UP else self.config.down_instrument_id
        )

    def _book_age_seconds(self, *, side: TokenSide, now_ts_ns: int) -> float | None:
        book_ts_ns = self._last_book_ts_ns.get(self._instrument_id_for(side))
        if book_ts_ns is None:
            return None
        return (now_ts_ns - book_ts_ns) / 1_000_000_000

    def _execution_book_rejection_reason(self, *, now_ts_ns: int) -> str | None:
        if self._outcome_books() is None:
            return "book_unavailable"
        ages = tuple(
            self._book_age_seconds(side=side, now_ts_ns=now_ts_ns)
            for side in (TokenSide.UP, TokenSide.DOWN)
        )
        if any(age is None or age < 0.0 for age in ages):
            return "book_noncausal"
        if max(ages) > self._maker_config.stale_after_seconds:
            return "book_stale"
        return None
