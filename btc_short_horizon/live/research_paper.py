"""Real-time, credential-free Research Paper decision and portfolio projection."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
from math import isfinite
import os
from pathlib import Path
from uuid import uuid4

import numpy as np

from btc_short_horizon.config import PaperExecutionVariantConfig
from btc_short_horizon.data import MarketOutcome, MarketWindow
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.data.polymarket import PolymarketL2Normalizer
from btc_short_horizon.live.dashboard_state import (
    BotDashboardSnapshot,
    DecisionFunnelSnapshot,
    EquityPoint,
    ExecutionSegmentPerformance,
    ExecutionVariantPerformance,
    GateState,
    HealthIndicator,
    HealthState,
    OrderPerformance,
    PerformanceSnapshot,
    StrategyCycle,
    StrategyStage,
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
from btc_short_horizon.research.opening_proxy import opening_regime_for_elapsed_seconds
from btc_short_horizon.strategy import (
    ConsecutiveSignalConfirmation,
    MakerStrategyConfig,
    OrderPlan,
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


@dataclass(frozen=True, slots=True)
class PaperSignalObservation:
    signal_number: int
    observed_at_ns: int
    p_fair: float
    maker_price: float
    executable_vwap: float | None
    taker_fee_per_share: float | None
    taker_net_edge: float | None

    def __post_init__(self) -> None:
        if self.signal_number not in {1, 2, 3}:
            raise ValueError("signal_number must be 1, 2, or 3")
        if self.observed_at_ns < 0:
            raise ValueError("observed_at_ns must be non-negative")
        for name in ("p_fair", "maker_price"):
            value = getattr(self, name)
            if not isfinite(value) or not 0.0 < value < 1.0:
                raise ValueError(f"{name} must be in (0, 1)")
        for name in ("executable_vwap", "taker_fee_per_share", "taker_net_edge"):
            value = getattr(self, name)
            if value is not None and not isfinite(value):
                raise ValueError(f"{name} must be finite when provided")

    def to_json(self) -> dict[str, object]:
        return {
            "signal_number": self.signal_number,
            "observed_at_ns": self.observed_at_ns,
            "p_fair": self.p_fair,
            "maker_price": self.maker_price,
            "executable_vwap": self.executable_vwap,
            "taker_fee_per_share": self.taker_fee_per_share,
            "taker_net_edge": self.taker_net_edge,
        }

    @classmethod
    def from_json(cls, raw: object) -> PaperSignalObservation:
        value = _mapping_value(raw, "paper signal observation")
        return cls(
            signal_number=_integer(value.get("signal_number"), "signal_number"),
            observed_at_ns=_integer(value.get("observed_at_ns"), "observed_at_ns"),
            p_fair=_number(value.get("p_fair"), "p_fair"),
            maker_price=_number(value.get("maker_price"), "maker_price"),
            executable_vwap=_optional_number(value.get("executable_vwap"), "executable_vwap"),
            taker_fee_per_share=_optional_number(
                value.get("taker_fee_per_share"), "taker_fee_per_share"
            ),
            taker_net_edge=_optional_number(value.get("taker_net_edge"), "taker_net_edge"),
        )


@dataclass(slots=True)
class PaperTradeRecord:
    variant_id: str
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
    execution_status: str
    opportunity_id: str
    entry_regime: str
    price_bucket: str
    go_eligible: bool
    decision_best_ask: float
    signal_observations: list[PaperSignalObservation] = field(default_factory=list)
    settlement_status: str = "pending"
    terminal_reason: str | None = None
    execution_route: str = "maker"
    cancel_race_filled_shares: float = 0.0
    maker_filled_shares: float = 0.0
    taker_filled_shares: float = 0.0
    maker_filled_notional: float = 0.0
    taker_filled_notional: float = 0.0
    taker_fees: float = 0.0
    initial_queue_ahead: float = 0.0
    remaining_queue_ahead: float = 0.0
    raw_eligible_sell_volume: float = 0.0
    stressed_eligible_sell_volume: float = 0.0
    active_at_ns: int | None = None
    cancel_requested_at_ns: int | None = None
    cancel_ack_at_ns: int | None = None
    terminal_at_ns: int | None = None
    fak_limit_price: float | None = None
    fak_net_edge_per_share: float | None = None
    outcome: str | None = None
    settled_at_ns: int | None = None
    realized_pnl: float | None = None

    @property
    def entry_price(self) -> float | None:
        return None if self.filled_shares <= 0.0 else self.filled_notional / self.filled_shares

    def to_json(self) -> dict[str, object]:
        return {
            "variant_id": self.variant_id,
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
            "execution_status": self.execution_status,
            "opportunity_id": self.opportunity_id,
            "entry_regime": self.entry_regime,
            "price_bucket": self.price_bucket,
            "go_eligible": self.go_eligible,
            "decision_best_ask": self.decision_best_ask,
            "signal_observations": [item.to_json() for item in self.signal_observations],
            "settlement_status": self.settlement_status,
            "terminal_reason": self.terminal_reason,
            "execution_route": self.execution_route,
            "cancel_race_filled_shares": self.cancel_race_filled_shares,
            "maker_filled_shares": self.maker_filled_shares,
            "taker_filled_shares": self.taker_filled_shares,
            "maker_filled_notional": self.maker_filled_notional,
            "taker_filled_notional": self.taker_filled_notional,
            "taker_fees": self.taker_fees,
            "initial_queue_ahead": self.initial_queue_ahead,
            "remaining_queue_ahead": self.remaining_queue_ahead,
            "raw_eligible_sell_volume": self.raw_eligible_sell_volume,
            "stressed_eligible_sell_volume": self.stressed_eligible_sell_volume,
            "active_at_ns": self.active_at_ns,
            "cancel_requested_at_ns": self.cancel_requested_at_ns,
            "cancel_ack_at_ns": self.cancel_ack_at_ns,
            "terminal_at_ns": self.terminal_at_ns,
            "fak_limit_price": self.fak_limit_price,
            "fak_net_edge_per_share": self.fak_net_edge_per_share,
            "outcome": self.outcome,
            "settled_at_ns": self.settled_at_ns,
            "realized_pnl": self.realized_pnl,
        }

    @classmethod
    def from_json(cls, raw: object) -> PaperTradeRecord:
        if not isinstance(raw, Mapping):
            raise ValueError("paper trade record must be a JSON object")
        return cls(
            variant_id=_text(raw.get("variant_id"), "variant_id"),
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
            execution_status=_text(raw.get("execution_status"), "execution_status"),
            opportunity_id=_text(raw.get("opportunity_id"), "opportunity_id"),
            entry_regime=_text(raw.get("entry_regime"), "entry_regime"),
            price_bucket=_text(raw.get("price_bucket"), "price_bucket"),
            go_eligible=_boolean(raw.get("go_eligible"), "go_eligible"),
            decision_best_ask=_number(raw.get("decision_best_ask"), "decision_best_ask"),
            signal_observations=[
                PaperSignalObservation.from_json(item)
                for item in _sequence_value(raw.get("signal_observations"), "signal_observations")
            ],
            settlement_status=_text(raw.get("settlement_status"), "settlement_status"),
            terminal_reason=_optional_text(raw.get("terminal_reason"), "terminal_reason"),
            execution_route=_text(raw.get("execution_route"), "execution_route"),
            cancel_race_filled_shares=_number(
                raw.get("cancel_race_filled_shares", 0.0),
                "cancel_race_filled_shares",
            ),
            maker_filled_shares=_number(raw.get("maker_filled_shares", 0.0), "maker_filled_shares"),
            taker_filled_shares=_number(raw.get("taker_filled_shares", 0.0), "taker_filled_shares"),
            maker_filled_notional=_number(
                raw.get("maker_filled_notional", 0.0), "maker_filled_notional"
            ),
            taker_filled_notional=_number(
                raw.get("taker_filled_notional", 0.0), "taker_filled_notional"
            ),
            taker_fees=_number(raw.get("taker_fees", 0.0), "taker_fees"),
            initial_queue_ahead=_number(raw.get("initial_queue_ahead", 0.0), "initial_queue_ahead"),
            remaining_queue_ahead=_number(
                raw.get("remaining_queue_ahead", 0.0), "remaining_queue_ahead"
            ),
            raw_eligible_sell_volume=_number(
                raw.get("raw_eligible_sell_volume", 0.0), "raw_eligible_sell_volume"
            ),
            stressed_eligible_sell_volume=_number(
                raw.get("stressed_eligible_sell_volume", 0.0),
                "stressed_eligible_sell_volume",
            ),
            active_at_ns=_optional_integer(raw.get("active_at_ns"), "active_at_ns"),
            cancel_requested_at_ns=_optional_integer(
                raw.get("cancel_requested_at_ns"), "cancel_requested_at_ns"
            ),
            cancel_ack_at_ns=_optional_integer(raw.get("cancel_ack_at_ns"), "cancel_ack_at_ns"),
            terminal_at_ns=_optional_integer(raw.get("terminal_at_ns"), "terminal_at_ns"),
            fak_limit_price=_optional_number(raw.get("fak_limit_price"), "fak_limit_price"),
            fak_net_edge_per_share=_optional_number(
                raw.get("fak_net_edge_per_share"), "fak_net_edge_per_share"
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

    def __init__(self, runtime_root: Path, execution_epoch: str, variant_id: str) -> None:
        _identifier(execution_epoch, "execution_epoch")
        _identifier(variant_id, "variant_id")
        self.runtime_root = runtime_root
        self.execution_epoch = execution_epoch
        self.variant_id = variant_id
        self.path = (
            runtime_root
            / "paper"
            / "epochs"
            / execution_epoch
            / "variants"
            / variant_id
            / "ledger.json"
        )

    def write(self, snapshot: PaperLedgerSnapshot) -> Path:
        payload = {
            "schema_version": 4,
            "execution_epoch": self.execution_epoch,
            "variant_id": self.variant_id,
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
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 4:
            raise ValueError("unsupported Research Paper ledger schema")
        if raw.get("execution_epoch") != self.execution_epoch:
            raise ValueError("Research Paper ledger epoch does not match its path")
        if raw.get("variant_id") != self.variant_id:
            raise ValueError("Research Paper ledger variant does not match its path")
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


class PaperRuleSnapshotStore:
    """Frozen public CLOB rules required to reproduce one Paper execution epoch."""

    def __init__(self, runtime_root: Path, execution_epoch: str) -> None:
        _identifier(execution_epoch, "execution_epoch")
        self.root = runtime_root / "paper" / "epochs" / execution_epoch / "rules"
        self.execution_epoch = execution_epoch

    def write(
        self,
        market: MarketWindow,
        rules: Mapping[str, PaperMarketRules],
    ) -> Path:
        self._validate_rules(market, rules)
        path = self._path(market.slug)
        payload = {
            "schema_version": 1,
            "execution_epoch": self.execution_epoch,
            "market": {
                "slug": market.slug,
                "condition_id": market.condition_id,
                "up_token_id": market.up_token_id,
                "down_token_id": market.down_token_id,
                "t0_ns": int(market.t0.timestamp() * 1_000_000_000),
                "t1_ns": int(market.t1.timestamp() * 1_000_000_000),
                "rule_epoch": market.rule_epoch,
                "rule_hash": market.rule_hash,
            },
            "rules": [rules[token_id].to_json() for token_id in sorted(rules)],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != encoded:
                    raise ValueError("immutable rule snapshot conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def read(self, market: MarketWindow) -> dict[str, PaperMarketRules]:
        path = self._path(market.slug)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid Research Paper rule snapshot JSON") from exc
        if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
            raise ValueError("unsupported Research Paper rule snapshot schema")
        if raw.get("execution_epoch") != self.execution_epoch:
            raise ValueError("Research Paper rule snapshot epoch mismatch")
        identity = _mapping_value(raw.get("market"), "rule snapshot market")
        expected_identity = {
            "slug": market.slug,
            "condition_id": market.condition_id,
            "up_token_id": market.up_token_id,
            "down_token_id": market.down_token_id,
            "t0_ns": int(market.t0.timestamp() * 1_000_000_000),
            "t1_ns": int(market.t1.timestamp() * 1_000_000_000),
            "rule_epoch": market.rule_epoch,
            "rule_hash": market.rule_hash,
        }
        if dict(identity) != expected_identity:
            raise ValueError("Research Paper rule snapshot market identity mismatch")
        values = _sequence_value(raw.get("rules"), "rule snapshot rules")
        rules = {item.token_id: item for item in map(PaperMarketRules.from_json, values)}
        if len(values) != len(rules):
            raise ValueError("Research Paper rule snapshot repeats a token")
        self._validate_rules(market, rules)
        return rules

    def _path(self, market_slug: str) -> Path:
        return self.root / f"{_identifier(market_slug, 'market_slug', maximum=320)}.json"

    @staticmethod
    def _validate_rules(
        market: MarketWindow,
        rules: Mapping[str, PaperMarketRules],
    ) -> None:
        expected = {market.up_token_id, market.down_token_id}
        if set(rules) != expected:
            raise ValueError("Paper rule snapshot must cover exactly the Up and Down tokens")
        if any(
            item.token_id != token_id
            or item.condition_id.casefold() != market.condition_id.casefold()
            for token_id, item in rules.items()
        ):
            raise ValueError("Paper rule snapshot does not match the market")


class ResearchPaperEngine:
    """One shared decision state machine over collector-admitted public events."""

    def __init__(
        self,
        *,
        predictor: PaperPredictor,
        model_id: str,
        maker_config: MakerStrategyConfig,
        execution_config: PaperExecutionConfig,
        variant: PaperExecutionVariantConfig,
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
        self.variant = variant
        self.ledger_store = ledger_store
        self.kline_history = kline_history
        restored = ledger_store.read()
        if restored is not None and abs(restored.starting_balance - starting_balance) > 1e-9:
            raise ValueError("configured Paper starting balance differs from persisted ledger")
        self.records = [] if restored is None else restored.records
        changed = False
        for record in self.records:
            if record.execution_status in {
                "insert_pending",
                "working",
                "cancel_pending",
                "fak_pending",
            }:
                record.execution_status = "recovery_canceled"
                record.terminal_reason = "process_restart"
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
        self._latest_prediction: OpeningMispricingPrediction | None = None
        self._signal_observations: list[PaperSignalObservation] = []
        if changed:
            self._persist()

    @property
    def active_placement(self) -> PaperPlacement | None:
        return self._active_placement

    def predict_current(self, *, now_ts_ns: int) -> OpeningMispricingPrediction | None:
        if self.market is None:
            return None
        books = self._outcome_books()
        if books is None:
            return None
        return self._predict_current(books=books, now_ts_ns=now_ts_ns)

    def needs_prediction(self, *, now_ts_ns: int) -> bool:
        if self.market is None:
            return False
        placement = self._active_placement
        if placement is not None and placement.status in {"insert_pending", "working"}:
            return True
        if any(record.market_slug == self.market.slug for record in self.records):
            return False
        elapsed = (now_ts_ns - int(self.market.t0.timestamp() * 1_000_000_000)) / 1e9
        return (
            self.maker_config.entry_start_seconds <= elapsed <= self.maker_config.entry_end_seconds
        )

    def set_kline_history(self, history: BinanceKlineHistory) -> None:
        self.kline_history = history

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
        if any(
            placement.status in {"insert_pending", "working", "cancel_pending", "fak_pending"}
            for placement in self.simulator.placements
        ):
            raise RuntimeError("cannot activate a market with an unfinished paper placement")
        PaperRuleSnapshotStore(
            self.ledger_store.runtime_root,
            self.ledger_store.execution_epoch,
        ).write(market, rules)
        self.simulator.retire_terminal_placements()
        self.market = market
        self.rules = dict(rules)
        self._normalizers = {token: PolymarketL2Normalizer(token_id=token) for token in expected}
        self._last_books.clear()
        self._gapped_tokens.clear()
        self._tick_changed_tokens.clear()
        self._confirmation.reset()
        self._active_placement = None
        self._active_record = None
        self._latest_prediction = None
        self._signal_observations.clear()

    def on_event(self, event: RawCollectorEvent) -> None:
        event_ts_ns = int(event.timing.available_ts.timestamp() * 1_000_000_000)
        self._advance_execution_before(event_ts_ns)
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

    def _advance_execution_before(self, event_ts_ns: int) -> None:
        """Apply due latency transitions against the last causally known book.

        An event observed after an insert/cancel/FAK deadline must not reprice a
        transition which happened before that event became available. Events at
        the exact deadline are deliberately applied first as the conservative
        same-timestamp tie break.
        """

        while self._active_placement is not None:
            placement = self._active_placement
            if placement.status == "insert_pending":
                due_ts_ns = placement.active_ts_ns
            elif placement.status == "working":
                due_ts_ns = placement.plan.expires_ts_ns
            elif placement.status == "cancel_pending":
                due_ts_ns = placement.cancel_ack_ts_ns
            elif placement.status == "fak_pending":
                due_ts_ns = placement.fak_active_ts_ns
            else:
                return
            if due_ts_ns is None or due_ts_ns >= event_ts_ns:
                return
            previous_status = placement.status
            self.advance(now_ts_ns=due_ts_ns)
            if placement.status == previous_status:
                return

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
        unsafe_reason = self._unsafe_execution_reason(now_ts_ns=now_ts_ns)
        if self._active_placement is not None and unsafe_reason is not None:
            if self._active_placement.status == "fak_pending":
                self.simulator.abort_fak(
                    self._active_placement,
                    now_ts_ns=now_ts_ns,
                    reason=f"fak_{unsafe_reason}",
                )
            else:
                self.simulator.request_cancel(
                    self._active_placement,
                    now_ts_ns=now_ts_ns,
                    reason=unsafe_reason,
                )
        self.simulator.advance(now_ts_ns=now_ts_ns, books=self._books_by_token())
        self._maybe_request_fak(now_ts_ns=now_ts_ns)
        self._sync_active_record()

    def _unsafe_execution_reason(self, *, now_ts_ns: int) -> str | None:
        placement = self._active_placement
        if placement is None or placement.status not in {
            "insert_pending",
            "working",
            "cancel_pending",
            "fak_pending",
        }:
            return None
        if self._gapped_tokens or self._binance_gap:
            return "data_gap"
        if self._tick_changed_tokens:
            return "tick_changed"
        if self.market is None:
            return "book_stale_connection_unobserved"
        stale_after_ns = round(self.maker_config.stale_after_seconds * 1_000_000_000)
        for token_id in (self.market.up_token_id, self.market.down_token_id):
            metadata = self._last_books.get(token_id)
            if metadata is None or now_ts_ns - metadata[0] > stale_after_ns:
                return "book_stale_connection_unobserved"
        return None

    def decide(
        self,
        *,
        now_ts_ns: int,
        prediction: OpeningMispricingPrediction | None = None,
    ) -> str:
        if self.market is None:
            return "market_unavailable"
        self.advance(now_ts_ns=now_ts_ns)
        books = self._outcome_books()
        if any(record.market_slug == self.market.slug for record in self.records):
            placement = self._active_placement
            if books is not None and prediction is not None:
                self._validate_shared_prediction(prediction, now_ts_ns=now_ts_ns)
                self._latest_prediction = prediction
                self._maybe_record_third_signal(
                    prediction=prediction,
                    books=books,
                    now_ts_ns=now_ts_ns,
                )
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
            prediction = prediction or self._predict_current(books=books, now_ts_ns=now_ts_ns)
            self._validate_shared_prediction(prediction, now_ts_ns=now_ts_ns)
            self._latest_prediction = prediction
            self._maybe_record_third_signal(
                prediction=prediction,
                books=books,
                now_ts_ns=now_ts_ns,
            )
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
        prediction = prediction or self._predict_current(books=books, now_ts_ns=now_ts_ns)
        self._validate_shared_prediction(prediction, now_ts_ns=now_ts_ns)
        self._latest_prediction = prediction
        safety_reason = self._prediction_safety_reason(prediction)
        if safety_reason is not None:
            self._confirmation.reset()
            self._signal_observations.clear()
            return safety_reason
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
            self._signal_observations.clear()
            return decision.reason
        confirmed = self._confirmation.observe(decision.plan.side, signal_ts_ns=now_ts_ns)
        if self._confirmation.count == 1:
            self._signal_observations.clear()
        side_book = books.for_side(decision.plan.side)
        self._signal_observations.append(
            self._signal_observation(
                plan=decision.plan,
                book=side_book,
                now_ts_ns=now_ts_ns,
                signal_number=self._confirmation.count,
            )
        )
        if not confirmed:
            return "confirmation_pending"
        selected_probability = decision.plan.p_fair
        planned_notional = (
            decision.plan.total_size * selected_probability
            if self.variant.mode == "immediate_fak"
            else decision.plan.total_notional
        )
        opportunity_id = f"{decision.plan.market_slug}:{decision.plan.created_ts_ns}"
        try:
            regime = _opening_regime_for_live_decision(
                elapsed_seconds=elapsed,
                cadence_seconds=self.maker_config.signal_cadence_seconds,
                tolerance_seconds=self.maker_config.signal_cadence_tolerance_seconds,
            )
        except ValueError:
            self._confirmation.reset()
            self._signal_observations.clear()
            return "outside_frozen_regime"
        price_bucket = _price_bucket(side_book.best_ask)
        if planned_notional > self._available_balance() + 1e-12:
            self._active_record = PaperTradeRecord(
                variant_id=self.variant.variant_id,
                placement_id=opportunity_id,
                market_slug=decision.plan.market_slug,
                token_id=decision.plan.token_id,
                side=decision.plan.side.value,
                placed_at_ns=now_ts_ns,
                shares=decision.plan.total_size,
                filled_shares=0.0,
                filled_notional=0.0,
                planned_notional=planned_notional,
                p_fair=decision.plan.p_fair,
                market_price=decision.plan.p_market,
                execution_status="rejected",
                opportunity_id=opportunity_id,
                entry_regime=regime,
                price_bucket=price_bucket,
                go_eligible=price_bucket == "core",
                decision_best_ask=side_book.best_ask,
                signal_observations=list(self._signal_observations),
                terminal_reason="insufficient_virtual_balance",
                execution_route=("direct_fak" if self.variant.mode == "immediate_fak" else "maker"),
                terminal_at_ns=now_ts_ns,
            )
            self.records.append(self._active_record)
            self._persist()
            self._confirmation.reset()
            self._signal_observations.clear()
            return "insufficient_virtual_balance"
        if self.variant.mode == "immediate_fak":
            placement = self.simulator.submit_direct_fak(
                plan=decision.plan,
                rules=self.rules[decision.plan.token_id],
                book=side_book,
                now_ts_ns=now_ts_ns,
                selected_probability=selected_probability,
                minimum_net_edge=self.variant.minimum_taker_net_edge,
                slippage_buffer=self.variant.slippage_buffer,
                model_uncertainty_buffer=self.variant.model_uncertainty_buffer,
            )
        else:
            placement = self.simulator.submit(
                plan=decision.plan,
                rules=self.rules[decision.plan.token_id],
                book=side_book,
                now_ts_ns=now_ts_ns,
            )
        self._active_placement = placement
        self._active_record = PaperTradeRecord(
            variant_id=self.variant.variant_id,
            placement_id=placement.placement_id,
            market_slug=decision.plan.market_slug,
            token_id=decision.plan.token_id,
            side=decision.plan.side.value,
            placed_at_ns=now_ts_ns,
            shares=decision.plan.total_size,
            filled_shares=0.0,
            filled_notional=0.0,
            planned_notional=planned_notional,
            p_fair=decision.plan.p_fair,
            market_price=decision.plan.p_market,
            execution_status=placement.status,
            opportunity_id=opportunity_id,
            entry_regime=regime,
            price_bucket=price_bucket,
            go_eligible=price_bucket == "core",
            decision_best_ask=side_book.best_ask,
            signal_observations=list(self._signal_observations),
            execution_route=placement.execution_route,
            active_at_ns=placement.active_ts_ns,
            initial_queue_ahead=sum(layer.initial_queue_ahead for layer in placement.layers),
            remaining_queue_ahead=sum(layer.queue_ahead for layer in placement.layers),
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
            self.simulator.request_cancel(
                placement,
                now_ts_ns=now_ts_ns,
                reason=assessment.reason,
            )
            self._sync_active_record()
        elif (
            self.variant.mode == "maker_then_fak"
            and placement.maker_filled_size <= 0.0
            and now_ts_ns - placement.submitted_ts_ns
            >= round(self.variant.maker_work_seconds * 1_000_000_000)
        ):
            self.simulator.request_cancel(
                placement,
                now_ts_ns=now_ts_ns,
                reason="fak_upgrade",
            )
            self._sync_active_record()
            return "fak_cancel_pending"
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
            record.settlement_status = "void" if outcome is MarketOutcome.VOID else "resolved"
            changed = True
        if changed:
            self._persist()

    def dashboard_snapshot(self, *, now: datetime) -> BotDashboardSnapshot:
        now = now.astimezone(UTC)
        performance = self.performance_snapshot(now=now)
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
        before = record.to_json()
        record.execution_status = placement.status
        record.filled_shares = placement.filled_size
        record.filled_notional = placement.filled_notional
        record.cancel_race_filled_shares = placement.cancel_race_filled_size
        record.terminal_reason = placement.terminal_reason
        record.execution_route = placement.execution_route
        record.maker_filled_shares = placement.maker_filled_size
        record.taker_filled_shares = placement.taker_filled_size
        record.maker_filled_notional = placement.maker_filled_notional
        record.taker_filled_notional = placement.taker_filled_notional
        record.taker_fees = placement.taker_fees
        record.remaining_queue_ahead = sum(layer.queue_ahead for layer in placement.layers)
        record.raw_eligible_sell_volume = sum(
            layer.raw_eligible_sell_volume for layer in placement.layers
        )
        record.stressed_eligible_sell_volume = sum(
            layer.stressed_eligible_sell_volume for layer in placement.layers
        )
        record.cancel_requested_at_ns = placement.cancel_requested_ts_ns
        record.cancel_ack_at_ns = placement.cancel_ack_ts_ns
        record.terminal_at_ns = placement.terminal_ts_ns
        record.fak_limit_price = placement.fak_limit_price
        record.fak_net_edge_per_share = placement.fak_net_edge_per_share
        if record.to_json() != before:
            self._persist()

    def _maybe_request_fak(self, *, now_ts_ns: int) -> None:
        placement = self._active_placement
        prediction = self._latest_prediction
        if (
            self.variant.mode != "maker_then_fak"
            or placement is None
            or prediction is None
            or placement.status != "canceled"
            or placement.terminal_reason != "fak_upgrade"
            or placement.maker_filled_size > 0.0
        ):
            return
        prediction_age = prediction.data_age_seconds + max(
            0.0, (now_ts_ns - prediction.trigger_ts_ns) / 1_000_000_000
        )
        if (
            prediction_age > self.maker_config.stale_after_seconds
            or prediction.has_data_gap
            or not prediction.structure_valid
            or not prediction.tick_unchanged
            or not prediction.fee_unchanged
            or not prediction.latency_healthy
        ):
            placement.terminal_reason = "fak_unsafe_prediction"
            placement.terminal_ts_ns = now_ts_ns
            return
        selected = prediction.p_up if placement.plan.side is TokenSide.UP else 1.0 - prediction.p_up
        self.simulator.request_fak(
            placement,
            now_ts_ns=now_ts_ns,
            selected_probability=selected,
            minimum_net_edge=self.variant.minimum_taker_net_edge,
            slippage_buffer=self.variant.slippage_buffer,
            model_uncertainty_buffer=self.variant.model_uncertainty_buffer,
        )

    def _prediction_safety_reason(
        self,
        prediction: OpeningMispricingPrediction,
    ) -> str | None:
        if prediction.has_data_gap:
            return "data_gap"
        if prediction.data_age_seconds > self.maker_config.stale_after_seconds:
            return "book_stale_connection_unobserved"
        if not prediction.structure_valid:
            return "structure_invalid"
        if not prediction.tick_unchanged:
            return "tick_changed"
        if not prediction.fee_unchanged:
            return "fee_changed"
        if not prediction.latency_healthy:
            return "latency_unhealthy"
        return None

    def _signal_observation(
        self,
        *,
        plan: OrderPlan,
        book: SideBook,
        now_ts_ns: int,
        signal_number: int,
    ) -> PaperSignalObservation:
        uses_taker_policy = self.variant.mode != "maker"
        quote = self.simulator.preview_fak(
            token_id=plan.token_id,
            requested_size=plan.total_size,
            rules=self.rules[plan.token_id],
            book=book,
            now_ts_ns=now_ts_ns,
            selected_probability=plan.p_fair,
            minimum_net_edge=(self.variant.minimum_taker_net_edge if uses_taker_policy else 0.0),
            slippage_buffer=(self.variant.slippage_buffer if uses_taker_policy else 0.0),
            model_uncertainty_buffer=(
                self.variant.model_uncertainty_buffer if uses_taker_policy else 0.0
            ),
        )
        fee_per_share = None if quote.filled_size <= 0.0 else quote.taker_fees / quote.filled_size
        return PaperSignalObservation(
            signal_number=signal_number,
            observed_at_ns=now_ts_ns,
            p_fair=plan.p_fair,
            maker_price=plan.layers[0].price,
            executable_vwap=quote.average_price,
            taker_fee_per_share=fee_per_share,
            taker_net_edge=quote.net_edge_per_share,
        )

    def _maybe_record_third_signal(
        self,
        *,
        prediction: OpeningMispricingPrediction,
        books: OutcomeBooks,
        now_ts_ns: int,
    ) -> None:
        if (
            self.market is None
            or self._active_record is None
            or self._active_placement is None
            or len(self._active_record.signal_observations) >= 3
            or self._prediction_safety_reason(prediction) is not None
            or self._confirmation.last_signal_ts_ns is None
        ):
            return
        cadence_ns = round(self.maker_config.signal_cadence_seconds * 1_000_000_000)
        tolerance_ns = round(self.maker_config.signal_cadence_tolerance_seconds * 1_000_000_000)
        delta_ns = now_ts_ns - self._confirmation.last_signal_ts_ns
        if not cadence_ns - tolerance_ns <= delta_ns <= cadence_ns + tolerance_ns:
            return
        elapsed = (now_ts_ns - int(self.market.t0.timestamp() * 1_000_000_000)) / 1e9
        decision = plan_opening_mispricing_orders(
            market_slug=self.market.slug,
            p_boundary_up=prediction.p_boundary_up,
            p_up=prediction.p_up,
            books=books,
            decision_ts_ns=now_ts_ns,
            elapsed_seconds=elapsed,
            config=self.maker_config,
        )
        if decision.plan is None or decision.plan.side is not self._active_placement.plan.side:
            return
        observation = self._signal_observation(
            plan=decision.plan,
            book=books.for_side(decision.plan.side),
            now_ts_ns=now_ts_ns,
            signal_number=3,
        )
        self._signal_observations.append(observation)
        self._active_record.signal_observations.append(observation)
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

    def _validate_shared_prediction(
        self,
        prediction: OpeningMispricingPrediction,
        *,
        now_ts_ns: int,
    ) -> None:
        assert self.market is not None
        if prediction.market_slug != self.market.slug:
            raise ValueError("shared Paper prediction belongs to a different market")
        if prediction.trigger_ts_ns != now_ts_ns:
            raise ValueError("shared Paper prediction does not match the decision timestamp")

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

    def performance_snapshot(self, *, now: datetime) -> PerformanceSnapshot:
        realized = sum(item.realized_pnl or 0.0 for item in self.records)
        open_records = tuple(item for item in self.records if item.realized_pnl is None)
        open_value = sum(
            item.maker_filled_notional + item.taker_filled_notional for item in open_records
        )
        cash, working_notional = self._cash_and_working_notional()
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
            OrderPerformance(
                variant_id=item.variant_id,
                order_id=item.placement_id,
                market_slug=item.market_slug,
                side=item.side,
                execution_status=item.execution_status,
                settlement_status=item.settlement_status,
                placed_at=datetime.fromtimestamp(item.placed_at_ns / 1e9, tz=UTC),
                shares=item.shares,
                filled_shares=item.filled_shares,
                entry_price=item.entry_price,
                p_fair=item.p_fair,
                market_price=item.market_price,
                realized_pnl=item.realized_pnl,
                unrealized_pnl=(0.0 if item.realized_pnl is None and item.filled_shares else None),
                order_latency_ms=self.simulator.config.insert_latency_ms,
                terminal_reason=item.terminal_reason,
                execution_route=item.execution_route,
                taker_fees=item.taker_fees,
                initial_queue_ahead=item.initial_queue_ahead,
                remaining_queue_ahead=item.remaining_queue_ahead,
                opportunity_id=item.opportunity_id,
                entry_regime=item.entry_regime,
                price_bucket=item.price_bucket,
                go_eligible=item.go_eligible,
                decision_best_ask=item.decision_best_ask,
                signal_edge_decay=_signal_edge_decay(item.signal_observations),
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
            open_exposure=open_value + working_notional,
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
            primary_variant_id=(self.variant.variant_id if self.variant.primary else None),
            variant_summaries=((self.variant_performance(),) if self.variant.primary else ()),
            recent_orders=recent,
            paper_execution_epoch=self.ledger_store.execution_epoch,
        )

    def variant_performance(self) -> ExecutionVariantPerformance:
        equity, realized = self._performance_totals()
        resolved = tuple(item for item in self.records if item.realized_pnl is not None)
        core_resolved = tuple(item for item in resolved if item.go_eligible)
        tail_resolved = tuple(item for item in resolved if not item.go_eligible)
        core_realized = sum(item.realized_pnl or 0.0 for item in core_resolved)
        tail_realized = sum(item.realized_pnl or 0.0 for item in tail_resolved)
        resolved_fills = tuple(item for item in resolved if item.filled_shares > 0.0)
        filled_shares = sum(item.filled_shares for item in resolved_fills)
        return ExecutionVariantPerformance(
            variant_id=self.variant.variant_id,
            label=self.variant.label,
            policy=self.variant.mode,
            primary=self.variant.primary,
            starting_balance=self.starting_balance,
            equity=equity,
            realized_pnl=realized,
            order_count=len(self.records),
            fill_count=sum(item.filled_shares > 0.0 for item in self.records),
            taker_fees=sum(item.taker_fees for item in self.records),
            resolved_opportunity_count=len(resolved),
            core_resolved_opportunity_count=len(core_resolved),
            tail_resolved_opportunity_count=len(tail_resolved),
            paired_ev_per_opportunity=(None if not resolved else realized / len(resolved)),
            core_paired_ev_per_opportunity=(
                None if not core_resolved else core_realized / len(core_resolved)
            ),
            tail_paired_ev_per_opportunity=(
                None if not tail_resolved else tail_realized / len(tail_resolved)
            ),
            conditional_ev_per_filled_share=(
                None if filled_shares <= 0.0 else realized / filled_shares
            ),
            segment_summaries=_segment_performance(self.records),
        )

    def _performance_totals(self) -> tuple[float, float]:
        realized = sum(item.realized_pnl or 0.0 for item in self.records)
        open_value = sum(
            item.maker_filled_notional + item.taker_filled_notional
            for item in self.records
            if item.realized_pnl is None
        )
        cash, _working_notional = self._cash_and_working_notional()
        return cash + open_value, realized

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
            and item.execution_status
            in {"insert_pending", "working", "cancel_pending", "fak_pending"}
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
            _wire_number(kline.get("c"), "kline close"),
            _wire_number(kline.get("v"), "kline volume"),
            _wire_number(kline.get("q"), "kline quote volume"),
            _wire_number(kline.get("V"), "kline taker buy volume"),
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


class ResearchPaperPortfolio:
    """Fan one admitted event stream into isolated execution-policy ledgers."""

    def __init__(self, engines: tuple[ResearchPaperEngine, ...]) -> None:
        if not engines:
            raise ValueError("Research Paper portfolio requires at least one engine")
        variant_ids = {engine.variant.variant_id for engine in engines}
        if len(variant_ids) != len(engines):
            raise ValueError("Research Paper portfolio variant IDs must be unique")
        primary = tuple(engine for engine in engines if engine.variant.primary)
        if len(primary) != 1:
            raise ValueError("Research Paper portfolio requires exactly one primary engine")
        model_ids = {engine.model_id for engine in engines}
        if len(model_ids) != 1:
            raise ValueError("Research Paper portfolio engines must share one model")
        self.engines = engines
        self.primary = primary[0]
        self.last_decisions: dict[str, str] = {}
        self.decision_counts: Counter[str] = Counter()
        self._primary_record_offset = len(self.primary.records)

    @property
    def model_id(self) -> str:
        return self.primary.model_id

    @property
    def market(self) -> MarketWindow | None:
        return self.primary.market

    @property
    def records(self) -> tuple[PaperTradeRecord, ...]:
        return tuple(record for engine in self.engines for record in engine.records)

    def set_kline_history(self, history: BinanceKlineHistory) -> None:
        for engine in self.engines:
            engine.kline_history = history

    def activate_market(
        self,
        market: MarketWindow,
        *,
        rules: Mapping[str, PaperMarketRules],
    ) -> None:
        for engine in self.engines:
            engine.activate_market(market, rules=rules)

    def on_event(self, event: RawCollectorEvent) -> None:
        for engine in self.engines:
            engine.on_event(event)

    def advance(self, *, now_ts_ns: int) -> None:
        for engine in self.engines:
            engine.advance(now_ts_ns=now_ts_ns)

    def decide(self, *, now_ts_ns: int) -> str:
        self.decision_counts["decision_ticks"] += 1
        prediction = (
            self.primary.predict_current(now_ts_ns=now_ts_ns)
            if any(engine.needs_prediction(now_ts_ns=now_ts_ns) for engine in self.engines)
            else None
        )
        if prediction is not None:
            self.decision_counts["predictions"] += 1
        primary_records_before = len(self.primary.records)
        primary_result = self.primary.decide(
            now_ts_ns=now_ts_ns,
            prediction=prediction,
        )
        primary_opportunity_created = len(self.primary.records) > primary_records_before
        results = {self.primary.variant.variant_id: primary_result}
        for engine in self.engines:
            if engine is self.primary:
                continue
            try:
                results[engine.variant.variant_id] = engine.decide(
                    now_ts_ns=now_ts_ns,
                    prediction=prediction,
                )
            except ValueError as exc:
                results[engine.variant.variant_id] = f"prediction_unavailable:{exc}"
        self.last_decisions = results
        if primary_result == "confirmation_pending" or primary_opportunity_created:
            self.decision_counts["eligible_signal_ticks"] += 1
        if primary_result == "confirmation_pending":
            self.decision_counts["confirmation_pending"] += 1
        if primary_opportunity_created:
            self.decision_counts["opportunities"] += 1
        return primary_result

    def settle(
        self,
        *,
        market_slug: str,
        outcome: MarketOutcome,
        label_available_ts_ns: int,
    ) -> None:
        for engine in self.engines:
            engine.settle(
                market_slug=market_slug,
                outcome=outcome,
                label_available_ts_ns=label_available_ts_ns,
            )

    def dashboard_snapshot(self, *, now: datetime) -> BotDashboardSnapshot:
        snapshot = self.primary.dashboard_snapshot(now=now)
        assert snapshot.performance is not None
        summaries = tuple(engine.variant_performance() for engine in self.engines)
        recent_orders = tuple(
            sorted(
                (
                    order
                    for engine in self.engines
                    for order in engine.performance_snapshot(now=now).recent_orders
                ),
                key=lambda item: item.placed_at,
                reverse=True,
            )[:15]
        )
        performance = replace(
            snapshot.performance,
            primary_variant_id=self.primary.variant.variant_id,
            variant_summaries=summaries,
            recent_orders=recent_orders,
            decision_funnel=self._decision_funnel(),
        )
        return replace(snapshot, performance=performance)

    def _decision_funnel(self) -> DecisionFunnelSnapshot:
        records = self.primary.records[self._primary_record_offset :]
        return DecisionFunnelSnapshot(
            scope="primary_process_session",
            decision_ticks=self.decision_counts["decision_ticks"],
            predictions=self.decision_counts["predictions"],
            eligible_signal_ticks=self.decision_counts["eligible_signal_ticks"],
            confirmation_pending=self.decision_counts["confirmation_pending"],
            opportunities=self.decision_counts["opportunities"],
            placements=sum(item.active_at_ns is not None for item in records),
            working=sum(
                item.execution_status
                in {"insert_pending", "working", "cancel_pending", "fak_pending"}
                for item in records
            ),
            rejected=sum(item.execution_status == "rejected" for item in records),
            canceled=sum(
                item.execution_status in {"canceled", "recovery_canceled"} for item in records
            ),
            fills=sum(item.filled_shares > 0.0 for item in records),
            resolved=sum(item.realized_pnl is not None for item in records),
        )


def _signal_edge_decay(observations: list[PaperSignalObservation]) -> float | None:
    edges = [item.taker_net_edge for item in observations if item.taker_net_edge is not None]
    return None if len(edges) < 2 else edges[0] - edges[-1]


def _price_bucket(executable_ask: float) -> str:
    if executable_ask < 0.20:
        return "tail_low"
    if executable_ask > 0.80:
        return "tail_high"
    return "core"


def _opening_regime_for_live_decision(
    *,
    elapsed_seconds: float,
    cadence_seconds: float,
    tolerance_seconds: float,
) -> str:
    try:
        return opening_regime_for_elapsed_seconds(elapsed_seconds).value
    except ValueError:
        scheduled_seconds = round(elapsed_seconds / cadence_seconds) * cadence_seconds
        if abs(elapsed_seconds - scheduled_seconds) > tolerance_seconds:
            raise
        return opening_regime_for_elapsed_seconds(scheduled_seconds).value


def _segment_performance(
    records: list[PaperTradeRecord],
) -> tuple[ExecutionSegmentPerformance, ...]:
    summaries: list[ExecutionSegmentPerformance] = []
    for dimension in ("entry_regime", "price_bucket"):
        for key in sorted({getattr(item, dimension) for item in records}):
            selected = tuple(item for item in records if getattr(item, dimension) == key)
            resolved = tuple(item for item in selected if item.realized_pnl is not None)
            pnl = sum(item.realized_pnl or 0.0 for item in resolved)
            summaries.append(
                ExecutionSegmentPerformance(
                    dimension=dimension,
                    key=key,
                    opportunity_count=len(selected),
                    resolved_count=len(resolved),
                    fill_count=sum(item.filled_shares > 0.0 for item in selected),
                    realized_pnl=pnl,
                    paired_ev_per_opportunity=(None if not resolved else pnl / len(resolved)),
                )
            )
    return tuple(summaries)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(value: object, name: str, *, maximum: int = 64) -> str:
    result = _text(value, name)
    if (
        len(result) > maximum
        or not result[0].isascii()
        or not result[0].isalnum()
        or any(
            not character.isascii() or not (character.isalnum() or character in {"_", "-"})
            for character in result
        )
    ):
        raise ValueError(f"{name} must be a simple ASCII identifier")
    return result


def _mapping_value(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _sequence_value(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


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


def _wire_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


__all__ = [
    "PaperLedgerSnapshot",
    "PaperLedgerStore",
    "PaperRuleSnapshotStore",
    "PaperPredictor",
    "PaperSignalObservation",
    "PaperTradeRecord",
    "ResearchPaperEngine",
    "ResearchPaperPortfolio",
]
