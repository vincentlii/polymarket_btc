"""Real-time, credential-free Research Paper decision and portfolio projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from math import isfinite
import os
from pathlib import Path
from uuid import uuid4

import numpy as np

from btc_short_horizon.data import MarketOutcome, MarketWindow
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.data.polymarket import PolymarketL2Normalizer
from btc_short_horizon.live.dashboard_state import (
    BotDashboardSnapshot,
    EquityPoint,
    GateState,
    HealthIndicator,
    HealthState,
    PerformanceSnapshot,
    StrategyCycle,
    StrategyStage,
    TradePerformance,
)
from btc_short_horizon.live.gateway import PaperOrderGateway
from btc_short_horizon.live.paper_execution import (
    PaperExecutionConfig,
    PaperExecutionSimulator,
    PaperMarketRules,
    PaperPlacement,
)
from btc_short_horizon.models import OpeningMispricingPrediction
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_evidence import OpeningMarketObservation
from btc_short_horizon.strategy import (
    ConsecutiveSignalConfirmation,
    MakerStrategyConfig,
    OutcomeBooks,
    SideBook,
    TokenSide,
    VisibleBookLevel,
    evaluate_cancellation,
    plan_opening_mispricing_orders,
)


type PaperPredictor = Callable[
    [MarketWindow, BinanceKlineHistory | None, OpeningMarketObservation],
    OpeningMispricingPrediction,
]


@dataclass(slots=True)
class PaperTradeRecord:
    placement_id: str
    market_slug: str
    token_id: str
    side: str
    placed_at_ns: int
    shares: float
    filled_shares: float
    filled_notional: float
    planned_notional: float
    p_fair: float
    market_price: float
    status: str
    cancel_race_filled_shares: float = 0.0
    outcome: str | None = None
    settled_at_ns: int | None = None
    realized_pnl: float | None = None

    @property
    def entry_price(self) -> float | None:
        return None if self.filled_shares <= 0.0 else self.filled_notional / self.filled_shares

    def to_json(self) -> dict[str, object]:
        return {
            "placement_id": self.placement_id,
            "market_slug": self.market_slug,
            "token_id": self.token_id,
            "side": self.side,
            "placed_at_ns": self.placed_at_ns,
            "shares": self.shares,
            "filled_shares": self.filled_shares,
            "filled_notional": self.filled_notional,
            "planned_notional": self.planned_notional,
            "p_fair": self.p_fair,
            "market_price": self.market_price,
            "status": self.status,
            "cancel_race_filled_shares": self.cancel_race_filled_shares,
            "outcome": self.outcome,
            "settled_at_ns": self.settled_at_ns,
            "realized_pnl": self.realized_pnl,
        }

    @classmethod
    def from_json(cls, raw: object) -> PaperTradeRecord:
        if not isinstance(raw, Mapping):
            raise ValueError("paper trade record must be a JSON object")
        return cls(
            placement_id=_text(raw.get("placement_id"), "placement_id"),
            market_slug=_text(raw.get("market_slug"), "market_slug"),
            token_id=_text(raw.get("token_id"), "token_id"),
            side=_text(raw.get("side"), "side"),
            placed_at_ns=_integer(raw.get("placed_at_ns"), "placed_at_ns"),
            shares=_number(raw.get("shares"), "shares"),
            filled_shares=_number(raw.get("filled_shares"), "filled_shares"),
            filled_notional=_number(raw.get("filled_notional"), "filled_notional"),
            planned_notional=_number(raw.get("planned_notional"), "planned_notional"),
            p_fair=_number(raw.get("p_fair"), "p_fair"),
            market_price=_number(raw.get("market_price"), "market_price"),
            status=_text(raw.get("status"), "status"),
            cancel_race_filled_shares=_number(
                raw.get("cancel_race_filled_shares", 0.0),
                "cancel_race_filled_shares",
            ),
            outcome=_optional_text(raw.get("outcome"), "outcome"),
            settled_at_ns=_optional_integer(raw.get("settled_at_ns"), "settled_at_ns"),
            realized_pnl=_optional_number(raw.get("realized_pnl"), "realized_pnl"),
        )


@dataclass(slots=True)
class PaperLedgerSnapshot:
    starting_balance: float
    records: list[PaperTradeRecord]


class PaperLedgerStore:
    """Atomic Paper-only ledger; it never impersonates the authenticated account ledger."""

    def __init__(self, runtime_root: Path) -> None:
        self.path = runtime_root / "paper" / "ledger.json"

    def write(self, snapshot: PaperLedgerSnapshot) -> Path:
        payload = {
            "schema_version": 2,
            "starting_balance": snapshot.starting_balance,
            "records": [record.to_json() for record in snapshot.records],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.path

    def read(self) -> PaperLedgerSnapshot | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise ValueError("invalid Research Paper ledger JSON") from exc
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 2:
            raise ValueError("unsupported Research Paper ledger schema")
        records = raw.get("records")
        if not isinstance(records, list):
            raise ValueError("Research Paper ledger records must be an array")
        starting_balance = _number(raw.get("starting_balance"), "starting_balance")
        if starting_balance <= 0.0:
            raise ValueError("starting_balance must be > 0")
        return PaperLedgerSnapshot(
            starting_balance=starting_balance,
            records=[PaperTradeRecord.from_json(item) for item in records],
        )


class ResearchPaperEngine:
    """One shared decision state machine over collector-admitted public events."""

    def __init__(
        self,
        *,
        predictor: PaperPredictor,
        model_id: str,
        maker_config: MakerStrategyConfig,
        execution_config: PaperExecutionConfig,
        ledger_store: PaperLedgerStore,
        starting_balance: float,
        kline_history: BinanceKlineHistory | None = None,
    ) -> None:
        if not callable(predictor) or not model_id:
            raise ValueError("predictor and model_id are required")
        if not isfinite(starting_balance) or starting_balance <= 0.0:
            raise ValueError("starting_balance must be finite and > 0")
        self.predictor = predictor
        self.model_id = model_id
        self.maker_config = maker_config
        self.ledger_store = ledger_store
        self.kline_history = kline_history
        restored = ledger_store.read()
        if restored is not None and abs(restored.starting_balance - starting_balance) > 1e-9:
            raise ValueError("configured Paper starting balance differs from persisted ledger")
        self.records = [] if restored is None else restored.records
        changed = False
        for record in self.records:
            if record.status in {"insert_pending", "working", "cancel_pending"}:
                record.status = "recovery_canceled"
                changed = True
        self.simulator = PaperExecutionSimulator(
            gateway=PaperOrderGateway(),
            config=execution_config,
        )
        self.starting_balance = starting_balance
        self.market: MarketWindow | None = None
        self.rules: dict[str, PaperMarketRules] = {}
        self._normalizers: dict[str, PolymarketL2Normalizer] = {}
        self._last_books: dict[str, tuple[int, int]] = {}
        self._gapped_tokens: set[str] = set()
        self._tick_changed_tokens: set[str] = set()
        self._binance_gap = False
        self._confirmation = ConsecutiveSignalConfirmation(
            required_signals=maker_config.confirmation_signals,
            cadence_seconds=maker_config.signal_cadence_seconds,
            tolerance_seconds=maker_config.signal_cadence_tolerance_seconds,
        )
        self._active_placement: PaperPlacement | None = None
        self._active_record: PaperTradeRecord | None = None
        if changed:
            self._persist()

    @property
    def active_placement(self) -> PaperPlacement | None:
        return self._active_placement

    def activate_market(
        self,
        market: MarketWindow,
        *,
        rules: Mapping[str, PaperMarketRules],
    ) -> None:
        expected = {market.up_token_id, market.down_token_id}
        if set(rules) != expected:
            raise ValueError("Paper rules must cover exactly the Up and Down tokens")
        if any(
            item.condition_id.casefold() != market.condition_id.casefold()
            for item in rules.values()
        ):
            raise ValueError("Paper rules condition ID does not match the market")
        self.market = market
        self.rules = dict(rules)
        self._normalizers = {token: PolymarketL2Normalizer(token_id=token) for token in expected}
        self._last_books.clear()
        self._gapped_tokens.clear()
        self._tick_changed_tokens.clear()
        self._confirmation.reset()
        self._active_placement = None
        self._active_record = None

    def on_event(self, event: RawCollectorEvent) -> None:
        if event.event_type == "continuity_gap":
            if event.timing.instrument in self._normalizers:
                self._gapped_tokens.add(event.timing.instrument)
                self._normalizers[event.timing.instrument].reset()
                self._last_books.pop(event.timing.instrument, None)
            if event.timing.source == "binance_spot" and event.timing.instrument == "BTCUSDT":
                self._binance_gap = True
            return
        if (
            event.timing.source == "binance_spot"
            and event.timing.instrument == "BTCUSDT"
            and event.event_type == "kline_1s"
        ):
            self._append_binance_kline(event.payload)
            return
        normalizer = self._normalizers.get(event.timing.instrument)
        if normalizer is None or event.timing.source != "polymarket_clob":
            return
        receive_ts = event.timing.collector_receive_ts or event.timing.available_ts
        result = normalizer.apply(event.payload, collector_receive_ts=receive_ts)
        if result.book_top is not None and result.timing is not None:
            self._last_books[event.timing.instrument] = (
                int(result.timing.available_ts.timestamp() * 1_000_000_000),
                event.epoch_id,
            )
            if event.event_type == "book":
                self._gapped_tokens.discard(event.timing.instrument)
        if result.tick_size_changed:
            expected_tick = float(self.rules[event.timing.instrument].tick_size)
            if result.tick_size is None or abs(result.tick_size - expected_tick) > 1e-12:
                self._tick_changed_tokens.add(event.timing.instrument)
        if result.trade is not None:
            self.on_trade(
                token_id=event.timing.instrument,
                aggressor_side=result.trade.aggressor_side,
                price=result.trade.price,
                size=result.trade.quantity,
                available_ts_ns=result.trade.available_ts_ns,
            )

    def on_trade(
        self,
        *,
        token_id: str,
        aggressor_side: str,
        price: float,
        size: float,
        available_ts_ns: int,
    ) -> None:
        self.simulator.on_trade(
            token_id=token_id,
            aggressor_side=aggressor_side,  # type: ignore[arg-type]
            price=price,
            size=size,
            available_ts_ns=available_ts_ns,
        )
        self._sync_active_record()

    def advance(self, *, now_ts_ns: int) -> None:
        self.simulator.advance(now_ts_ns=now_ts_ns, books=self._books_by_token())
        self._sync_active_record()

    def decide(self, *, now_ts_ns: int) -> str:
        if self.market is None:
            return "market_unavailable"
        self.advance(now_ts_ns=now_ts_ns)
        books = self._outcome_books()
        if any(record.market_slug == self.market.slug for record in self.records):
            placement = self._active_placement
            if placement is None or placement.status not in {"insert_pending", "working"}:
                return (
                    "cancel_pending"
                    if placement is not None and placement.status == "cancel_pending"
                    else "placement_cycle_used"
                )
            if books is None:
                self.simulator.request_cancel(placement, now_ts_ns=now_ts_ns)
                self._sync_active_record()
                return "book_unavailable"
            prediction = self._predict_current(books=books, now_ts_ns=now_ts_ns)
            return self.reevaluate(prediction, now_ts_ns=now_ts_ns)
        if books is None:
            self._confirmation.reset()
            return "book_unavailable"
        elapsed = (now_ts_ns - int(self.market.t0.timestamp() * 1_000_000_000)) / 1e9
        if (
            not self.maker_config.entry_start_seconds
            <= elapsed
            <= self.maker_config.entry_end_seconds
        ):
            self._confirmation.reset()
            return "outside_entry_window"
        prediction = self._predict_current(books=books, now_ts_ns=now_ts_ns)
        if (
            prediction.has_data_gap
            or not prediction.structure_valid
            or not prediction.tick_unchanged
            or not prediction.fee_unchanged
            or not prediction.latency_healthy
            or prediction.data_age_seconds > self.maker_config.stale_after_seconds
        ):
            self._confirmation.reset()
            return "unsafe_prediction"
        decision = plan_opening_mispricing_orders(
            market_slug=self.market.slug,
            p_boundary_up=prediction.p_boundary_up,
            p_up=prediction.p_up,
            books=books,
            decision_ts_ns=now_ts_ns,
            elapsed_seconds=elapsed,
            config=self.maker_config,
        )
        if decision.plan is None:
            self._confirmation.reset()
            return decision.reason
        if not self._confirmation.observe(decision.plan.side, signal_ts_ns=now_ts_ns):
            return "confirmation_pending"
        if decision.plan.total_notional > self._available_balance() + 1e-12:
            self._confirmation.reset()
            return "insufficient_virtual_balance"
        side_book = books.for_side(decision.plan.side)
        placement = self.simulator.submit(
            plan=decision.plan,
            rules=self.rules[decision.plan.token_id],
            book=side_book,
            now_ts_ns=now_ts_ns,
        )
        self._active_placement = placement
        self._active_record = PaperTradeRecord(
            placement_id=placement.placement_id,
            market_slug=decision.plan.market_slug,
            token_id=decision.plan.token_id,
            side=decision.plan.side.value,
            placed_at_ns=now_ts_ns,
            shares=decision.plan.total_size,
            filled_shares=0.0,
            filled_notional=0.0,
            planned_notional=decision.plan.total_notional,
            p_fair=decision.plan.p_fair,
            market_price=decision.plan.p_market,
            status=placement.status,
        )
        self.records.append(self._active_record)
        self._persist()
        return "submitted"

    def reevaluate(self, prediction: OpeningMispricingPrediction, *, now_ts_ns: int) -> str:
        placement = self._active_placement
        if placement is None or placement.status not in {"working", "insert_pending"}:
            return "no_working_order"
        selected = prediction.p_up if placement.plan.side is TokenSide.UP else 1.0 - prediction.p_up
        assessment = evaluate_cancellation(
            plan=placement.plan,
            now_ts_ns=now_ts_ns,
            selected_probability=selected,
            data_age_seconds=prediction.data_age_seconds,
            has_data_gap=prediction.has_data_gap,
            structure_valid=prediction.structure_valid,
            tick_unchanged=prediction.tick_unchanged,
            fee_unchanged=prediction.fee_unchanged,
            latency_healthy=prediction.latency_healthy,
            config=self.maker_config,
        )
        if assessment.should_cancel:
            self.simulator.request_cancel(placement, now_ts_ns=now_ts_ns)
            self._sync_active_record()
        return assessment.reason

    def settle(
        self,
        *,
        market_slug: str,
        outcome: MarketOutcome,
        label_available_ts_ns: int,
    ) -> None:
        if outcome is MarketOutcome.VOID:
            payout_by_side = {"up": None, "down": None}
        else:
            payout_by_side = {
                "up": 1.0 if outcome is MarketOutcome.UP else 0.0,
                "down": 1.0 if outcome is MarketOutcome.DOWN else 0.0,
            }
        changed = False
        for record in self.records:
            if record.market_slug != market_slug or record.realized_pnl is not None:
                continue
            payout = payout_by_side[record.side]
            record.realized_pnl = (
                0.0 if payout is None else payout * record.filled_shares - record.filled_notional
            )
            record.outcome = outcome.value
            record.settled_at_ns = label_available_ts_ns
            record.status = "simulated_resolved"
            changed = True
        if changed:
            self._persist()

    def dashboard_snapshot(self, *, now: datetime) -> BotDashboardSnapshot:
        now = now.astimezone(UTC)
        performance = self._performance(now)
        return BotDashboardSnapshot(
            generated_at=now,
            run_mode="research_paper",
            strategy=StrategyCycle(
                stage=StrategyStage.SHADOW,
                gate_state=GateState.RUNNING,
                next_action="继续积累实时模拟成交；正式 Maker Go 仍需悲观 BookReplay 与 Canary。",
                model_id=self.model_id,
                progress_label="已完成模拟市场",
                progress_current=float(sum(item.realized_pnl is not None for item in self.records)),
                progress_target=300.0,
            ),
            performance=performance,
            health=(
                HealthIndicator(
                    key="paper-execution",
                    label="Research Paper 撮合",
                    state=HealthState.WARNING,
                    detail="模拟：P99 latency、完整可见 queue、50% trade volume；不是实盘成交证明。",
                    updated_at=now,
                ),
                HealthIndicator(
                    key="paper-model",
                    label="方向模型",
                    state=HealthState.WARNING,
                    detail="Research Proxy；方向 Gate 尚未 Go。",
                    updated_at=now,
                ),
            ),
        )

    def _sync_active_record(self) -> None:
        if self._active_placement is None or self._active_record is None:
            return
        placement = self._active_placement
        record = self._active_record
        before = (
            record.status,
            record.filled_shares,
            record.filled_notional,
            record.cancel_race_filled_shares,
        )
        record.status = placement.status
        record.filled_shares = placement.filled_size
        record.filled_notional = placement.filled_notional
        record.cancel_race_filled_shares = placement.cancel_race_filled_size
        after = (
            record.status,
            record.filled_shares,
            record.filled_notional,
            record.cancel_race_filled_shares,
        )
        if after != before:
            self._persist()

    def _outcome_books(self) -> OutcomeBooks | None:
        if self.market is None:
            return None
        books = self._books_by_token()
        up = books.get(self.market.up_token_id)
        down = books.get(self.market.down_token_id)
        if up is None or down is None:
            return None
        try:
            return OutcomeBooks(up=up, down=down)
        except ValueError:
            return None

    def _predict_current(
        self,
        *,
        books: OutcomeBooks,
        now_ts_ns: int,
    ) -> OpeningMispricingPrediction:
        assert self.market is not None
        up_meta = self._last_books[self.market.up_token_id]
        down_meta = self._last_books[self.market.down_token_id]
        data_age = max(now_ts_ns - up_meta[0], now_ts_ns - down_meta[0]) / 1e9
        observation = OpeningMarketObservation(
            market_slug=self.market.slug,
            decision_ts_ns=now_ts_ns,
            p_market_mid_up=books.implied_up_midpoint,
            data_age_seconds=max(0.0, data_age),
            up_available_ts_ns=up_meta[0],
            down_available_ts_ns=down_meta[0],
            up_epoch_id=up_meta[1],
            down_epoch_id=down_meta[1],
            has_data_gap=bool(self._gapped_tokens) or self._binance_gap,
            structure_valid=True,
            tick_unchanged=not self._tick_changed_tokens,
        )
        return self.predictor(self.market, self.kline_history, observation)

    def _books_by_token(self) -> dict[str, SideBook]:
        books: dict[str, SideBook] = {}
        for token, normalizer in self._normalizers.items():
            bids, asks = normalizer.book_levels()
            if not bids or not asks or token not in self.rules:
                continue
            rules = self.rules[token]
            try:
                books[token] = SideBook(
                    token_id=token,
                    bids=tuple(VisibleBookLevel(price=price, size=size) for price, size in bids),
                    asks=tuple(VisibleBookLevel(price=price, size=size) for price, size in asks),
                    tick_size=float(rules.tick_size),
                    minimum_order_size=rules.minimum_order_size,
                )
            except ValueError:
                continue
        return books

    def _performance(self, now: datetime) -> PerformanceSnapshot:
        realized = sum(item.realized_pnl or 0.0 for item in self.records)
        open_records = tuple(item for item in self.records if item.realized_pnl is None)
        open_cost = sum(item.filled_notional for item in open_records)
        cash, working_notional = self._cash_and_working_notional()
        open_value = open_cost
        equity = cash + open_value
        settled = tuple(item for item in self.records if item.realized_pnl is not None)
        settled_fills = tuple(item for item in settled if item.filled_shares > 0.0)
        points = [
            EquityPoint(
                timestamp=(
                    datetime.fromtimestamp(self.records[0].placed_at_ns / 1e9, tz=UTC)
                    if self.records
                    else now
                ),
                equity=self.starting_balance,
            )
        ]
        cumulative = 0.0
        for item in sorted(settled, key=lambda value: value.settled_at_ns or 0):
            cumulative += item.realized_pnl or 0.0
            assert item.settled_at_ns is not None
            points.append(
                EquityPoint(
                    timestamp=datetime.fromtimestamp(item.settled_at_ns / 1e9, tz=UTC),
                    equity=self.starting_balance + cumulative,
                )
            )
        if points[-1].timestamp < now:
            points.append(EquityPoint(timestamp=now, equity=equity))
        peak = points[0].equity
        max_drawdown = 0.0
        for point in points:
            peak = max(peak, point.equity)
            max_drawdown = max(max_drawdown, peak - point.equity)
        recent = tuple(
            TradePerformance(
                order_id=item.placement_id,
                market_slug=item.market_slug,
                side=item.side,
                status=item.status,
                placed_at=datetime.fromtimestamp(item.placed_at_ns / 1e9, tz=UTC),
                shares=item.shares,
                filled_shares=item.filled_shares,
                entry_price=item.entry_price,
                p_fair=item.p_fair,
                market_price=item.market_price,
                realized_pnl=item.realized_pnl,
                unrealized_pnl=(0.0 if item.realized_pnl is None and item.filled_shares else None),
                order_latency_ms=self.simulator.config.insert_latency_ms,
            )
            for item in sorted(self.records, key=lambda value: value.placed_at_ns, reverse=True)[
                :10
            ]
        )
        today_pnl = sum(
            item.realized_pnl or 0.0
            for item in settled
            if item.settled_at_ns is not None
            and datetime.fromtimestamp(item.settled_at_ns / 1e9, tz=UTC).date() == now.date()
        )
        return PerformanceSnapshot(
            starting_balance=self.starting_balance,
            equity=equity,
            available_balance=max(0.0, cash - working_notional),
            open_exposure=open_cost + working_notional,
            realized_pnl=realized,
            unrealized_pnl=0.0,
            today_pnl=today_pnl,
            max_drawdown=max_drawdown,
            win_rate=(
                None
                if not settled_fills
                else sum((item.realized_pnl or 0.0) > 0.0 for item in settled_fills)
                / len(settled_fills)
            ),
            order_count=len(self.records),
            fill_count=sum(item.filled_shares > 0.0 for item in self.records),
            equity_curve=tuple(points),
            recent_trades=recent,
        )

    def _available_balance(self) -> float:
        cash, working_notional = self._cash_and_working_notional()
        return max(0.0, cash - working_notional)

    def _cash_and_working_notional(self) -> tuple[float, float]:
        filled_cost = sum(item.filled_notional for item in self.records)
        settled_payout = sum(
            item.filled_notional + (item.realized_pnl or 0.0)
            for item in self.records
            if item.realized_pnl is not None
        )
        working_notional = sum(
            max(0.0, item.planned_notional - item.filled_notional)
            for item in self.records
            if item.realized_pnl is None
            and item.status in {"insert_pending", "working", "cancel_pending"}
        )
        return self.starting_balance - filled_cost + settled_payout, working_notional

    def _append_binance_kline(self, payload: Mapping[str, object]) -> None:
        data = payload.get("data")
        message = data if isinstance(data, Mapping) else payload
        kline = message.get("k")
        if not isinstance(kline, Mapping) or not bool(kline.get("x")):
            return
        values = (
            _integer(kline.get("t"), "kline open time") * 1_000_000,
            _number(kline.get("c"), "kline close"),
            _number(kline.get("v"), "kline volume"),
            _number(kline.get("q"), "kline quote volume"),
            _number(kline.get("V"), "kline taker buy volume"),
        )
        if self.kline_history is None:
            return
        previous = int(self.kline_history.open_ts_ns[-1])
        if values[0] <= previous:
            return
        if values[0] != previous + 1_000_000_000:
            self._binance_gap = True
        limit = 4_000
        self.kline_history = BinanceKlineHistory(
            open_ts_ns=np.append(self.kline_history.open_ts_ns, values[0])[-limit:],
            close=np.append(self.kline_history.close, values[1])[-limit:],
            volume=np.append(self.kline_history.volume, values[2])[-limit:],
            quote_volume=np.append(self.kline_history.quote_volume, values[3])[-limit:],
            taker_buy_volume=np.append(self.kline_history.taker_buy_volume, values[4])[-limit:],
            interval_seconds=1,
        )
        recent = self.kline_history.open_ts_ns[-3_602:]
        if len(recent) >= 3_601 and np.all(np.diff(recent) == 1_000_000_000):
            self._binance_gap = False

    def _persist(self) -> None:
        self.ledger_store.write(
            PaperLedgerSnapshot(
                starting_balance=self.starting_balance,
                records=self.records,
            )
        )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


__all__ = [
    "PaperLedgerSnapshot",
    "PaperLedgerStore",
    "PaperPredictor",
    "PaperTradeRecord",
    "ResearchPaperEngine",
]
