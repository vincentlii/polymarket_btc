"""Nautilus adapter for the BTC 15m Opening Mispricing maker strategy."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import DataType
from nautilus_trader.model.enums import BookType, OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from btc_short_horizon.backtest.audit import OrderAuditTrail
from btc_short_horizon.backtest.signals import (
    BtcOpeningMispricingSignal,
    opening_signal_data_age_seconds,
    opening_signal_entry_rejection_reason,
    validate_opening_mispricing_signal,
)
from btc_short_horizon.strategy import (
    LayerStructure,
    MakerOrderLayer,
    MakerStrategyConfig,
    MarketExecution,
    OrderPlan,
    OutcomeBooks,
    SideBook,
    StrategyPhase,
    TokenSide,
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


class BtcOpeningMispricingConfig(StrategyConfig, frozen=True):  # type: ignore[call-arg]
    """Serializable configuration for one dual-token 15m opening-window strategy."""

    market_slug: str
    up_instrument_id: InstrumentId
    down_instrument_id: InstrumentId
    max_shares: Decimal = Decimal("1")
    layer_structure: str = LayerStructure.SINGLE.value
    safety_buffer: float = 0.01
    minimum_edge: float = 0.0
    maker_fee_per_share: float = 0.0
    entry_start_seconds: float = 3.0
    entry_end_seconds: float = 180.0
    edge_persistence_seconds: float = 2.0
    max_work_seconds: float = 60.0
    stale_after_seconds: float = 1.0
    cancel_probability_drop: float = 0.03
    price_level_tick_offsets: tuple[int, ...] = (0, 1, 2)

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
            maker_fee_per_share=self.maker_fee_per_share,
            entry_start_seconds=self.entry_start_seconds,
            entry_end_seconds=self.entry_end_seconds,
            edge_persistence_seconds=self.edge_persistence_seconds,
            max_work_seconds=self.max_work_seconds,
            stale_after_seconds=self.stale_after_seconds,
            cancel_probability_drop=self.cancel_probability_drop,
            price_level_tick_offsets=self.price_level_tick_offsets,
        )


class BtcOpeningMispricingStrategy(Strategy):
    """One-cycle, post-only strategy driven by causal fair probability versus price."""

    def __init__(self, config: BtcOpeningMispricingConfig) -> None:
        super().__init__(config)
        self._maker_config = config.maker_config()
        self._execution = MarketExecution(market_slug=config.market_slug)
        self._instruments: dict[TokenSide, Any] = {}
        self._books: dict[TokenSide, OrderBook] = {}
        self._latest_signal: BtcOpeningMispricingSignal | None = None
        self._active_orders: dict[str, Any] = {}
        self._candidate_side: TokenSide | None = None
        self._candidate_since_ts_ns: int | None = None
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
        self.subscribe_data(DataType(BtcOpeningMispricingSignal))
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
            return
        self._latest_signal = data
        self._process_signal(now_ts_ns=int(data.ts_init))

    def on_order_book_deltas(self, deltas) -> None:  # type: ignore[no-untyped-def]
        side = self._side_for_instrument(getattr(deltas, "instrument_id", None))
        if side is None:
            return
        book = self._books.get(side)
        if book is None:
            book = OrderBook(self._instrument_id_for(side), book_type=BookType.L2_MBP)
            self._books[side] = book
        book.apply_deltas(deltas)
        self._process_signal(now_ts_ns=_event_ts_ns(deltas))

    def on_order_filled(self, event) -> None:  # type: ignore[no-untyped-def]
        client_order_id = str(getattr(event, "client_order_id", ""))
        if client_order_id not in self._active_orders:
            return
        fill_size = _as_float(getattr(event, "last_qty", None))
        if fill_size is None or fill_size <= 0.0:
            self.log.warning("Ignoring opening-mispricing fill with an invalid quantity.")
            return
        self._order_audit.record(
            event_type="fill",
            ts_ns=_event_ts_ns(event),
            client_order_id=client_order_id,
            size=fill_size,
            price=_as_float(getattr(event, "last_px", None)),
        )
        try:
            self._execution = self._execution.record_fill(fill_size)
        except ValueError as exc:
            self.log.error(f"BTC opening-mispricing fill lifecycle error: {exc}")
            self.stop()

    def on_order_canceled(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_order_event(event, terminal_event="cancel_ack", rejected=False)

    def on_order_expired(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_order_event(event, terminal_event="expired", rejected=False)

    def on_order_rejected(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_order_event(event, terminal_event="rejected", rejected=True)

    def on_order_denied(self, event) -> None:  # type: ignore[no-untyped-def]
        self._close_order_event(event, terminal_event="denied", rejected=True)

    def on_stop(self) -> None:
        if self._execution.phase is StrategyPhase.ORDER_WORKING:
            self._request_cancel("strategy_stop", now_ts_ns=0)

    def on_reset(self) -> None:
        self._execution = MarketExecution(market_slug=self.config.market_slug)
        self._instruments.clear()
        self._books.clear()
        self._latest_signal = None
        self._active_orders.clear()
        self._candidate_side = None
        self._candidate_since_ts_ns = None

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
            self._candidate_side = None
            self._candidate_since_ts_ns = None
            return
        books = self._outcome_books()
        if books is None:
            return
        elapsed_seconds = (
            int(signal.ts_init) - int(signal.market_window_start_ts_ns)
        ) / 1_000_000_000
        decision = plan_opening_mispricing_orders(
            market_slug=self.config.market_slug,
            p_up=float(signal.p_up),
            p_market_mid_up=float(signal.p_market_mid_up),
            books=books,
            decision_ts_ns=max(now_ts_ns, int(signal.ts_init)),
            elapsed_seconds=elapsed_seconds,
            config=self._maker_config,
        )
        if decision.plan is None:
            self._candidate_side = None
            self._candidate_since_ts_ns = None
            return
        if self._candidate_side is not decision.plan.side:
            self._candidate_side = decision.plan.side
            self._candidate_since_ts_ns = now_ts_ns
            return
        assert self._candidate_since_ts_ns is not None
        persistence_ns = round(self._maker_config.edge_persistence_seconds * 1_000_000_000)
        if now_ts_ns - self._candidate_since_ts_ns < persistence_ns:
            return
        materialized = self._materialize_plan(decision.plan)
        if materialized is None:
            self.log.warning("Opening-mispricing plan has no exchange-valid order layers.")
            return
        self._order_audit.record(
            event_type="plan",
            ts_ns=materialized.created_ts_ns,
            side=materialized.side.value,
            p_fair=materialized.p_fair,
            p_market=materialized.p_market,
            model_edge=materialized.model_edge,
            net_edge=materialized.net_edge(materialized.layers[0].price),
            layer_count=len(materialized.layers),
        )
        self._execution = self._execution.submit_plan(materialized)
        self._submit_orders(materialized)

    def _evaluate_working_orders(
        self, *, signal: BtcOpeningMispricingSignal, now_ts_ns: int
    ) -> None:
        plan = self._execution.plan
        if plan is None:
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
        book = self._books.get(side)
        instrument = self._instruments.get(side)
        if book is None or instrument is None:
            return None
        best_bid = _as_float(book.best_bid_price())
        best_ask = _as_float(book.best_ask_price())
        tick_size = _as_float(getattr(instrument, "price_increment", None))
        if best_bid is None or best_ask is None or tick_size is None:
            return None
        try:
            return SideBook(
                token_id=str(self._instrument_id_for(side)),
                best_bid=best_bid,
                best_ask=best_ask,
                tick_size=tick_size,
            )
        except ValueError:
            return None

    def _materialize_plan(self, plan: OrderPlan) -> OrderPlan | None:
        instrument = self._instruments[plan.side]
        layers: list[MakerOrderLayer] = []
        for layer in plan.layers:
            try:
                quantity = instrument.make_qty(layer.size, round_down=True)
                price = instrument.make_price(layer.price)
            except ValueError:
                continue
            actual_size = _as_float(quantity)
            actual_price = _as_float(price)
            if actual_size is None or actual_price is None or actual_size <= 0.0:
                continue
            if actual_price > layer.price + 1e-12:
                self.log.warning(
                    "Skipping layer whose venue price rounding could make it aggressive."
                )
                continue
            layers.append(MakerOrderLayer(price=actual_price, size=actual_size))
        return replace(plan, layers=tuple(layers)) if layers else None

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
                self.log.warning(f"Skipping invalid opening-mispricing layer {layer_index}: {exc}")
                continue
            pending_orders.append((order, layer_index, layer))
        if not pending_orders:
            self._execution = self._execution.reject_order()
            return
        for order, layer_index, layer in pending_orders:
            self.submit_order(order)
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
                p_fair=plan.p_fair,
                p_market=plan.p_market,
                net_edge=plan.net_edge(layer.price),
                post_only=True,
                time_in_force=TimeInForce.GTC.name,
            )

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
        self._active_orders.pop(order_id, None)
        if self._active_orders:
            return
        if self._execution.phase is StrategyPhase.CANCEL_REQUESTED:
            self._execution = self._execution.acknowledge_cancel()
        elif self._execution.phase is StrategyPhase.ORDER_WORKING:
            self._execution = (
                self._execution.reject_order()
                if rejected and self._execution.filled_size == 0.0
                else self._execution.complete_working_orders()
            )

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
