"""Conservative trade-evidence matching for credential-free Research Paper orders."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from math import isfinite
from typing import Literal

from btc_short_horizon.live.gateway import LiveOrderRequest, PaperOrderGateway
from btc_short_horizon.strategy import OrderPlan, SideBook
from nautilus_trader.model.enums import LiquiditySide
from prediction_market_extensions.adapters.polymarket.parsing import calculate_commission


@dataclass(frozen=True, slots=True)
class PaperExecutionConfig:
    """Fixed heuristic scenario; it is research evidence, never venue fill truth."""

    insert_latency_ms: float
    cancel_latency_ms: float
    taker_latency_ms: float
    trade_volume_multiplier: float

    def __post_init__(self) -> None:
        for name in ("insert_latency_ms", "cancel_latency_ms", "taker_latency_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and >= 0")
        if (
            isinstance(self.trade_volume_multiplier, bool)
            or not isfinite(self.trade_volume_multiplier)
            or not 0.0 < self.trade_volume_multiplier <= 1.0
        ):
            raise ValueError("trade_volume_multiplier must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class PaperMarketRules:
    condition_id: str
    token_id: str
    tick_size: str
    minimum_order_size: float
    neg_risk: bool
    maker_fee_rate_bps: int
    observed_at_ns: int
    taker_fee_rate: float = 0.0
    taker_fee_exponent: int = 1
    taker_only: bool = True

    def __post_init__(self) -> None:
        if not self.condition_id or not self.token_id or not self.tick_size:
            raise ValueError("condition_id, token_id, and tick_size are required")
        if not isfinite(self.minimum_order_size) or self.minimum_order_size <= 0.0:
            raise ValueError("minimum_order_size must be finite and > 0")
        if not isinstance(self.neg_risk, bool):
            raise ValueError("neg_risk must be bool")
        if (
            isinstance(self.maker_fee_rate_bps, bool)
            or not isinstance(self.maker_fee_rate_bps, int)
            or self.maker_fee_rate_bps < 0
        ):
            raise ValueError("maker_fee_rate_bps must be a non-negative integer")
        if (
            isinstance(self.observed_at_ns, bool)
            or not isinstance(self.observed_at_ns, int)
            or self.observed_at_ns < 0
        ):
            raise ValueError("observed_at_ns must be a non-negative integer")
        if (
            isinstance(self.taker_fee_rate, bool)
            or not isfinite(self.taker_fee_rate)
            or not 0.0 <= self.taker_fee_rate < 1.0
        ):
            raise ValueError("taker_fee_rate must be finite and in [0, 1)")
        if isinstance(self.taker_fee_exponent, bool) or self.taker_fee_exponent != 1:
            raise ValueError("Research Paper supports only the documented fee exponent 1")
        if self.taker_only is not True:
            raise ValueError("Research Paper requires taker-only fees")

    @property
    def rules_sha256(self) -> str:
        payload = {
            "condition_id": self.condition_id,
            "maker_fee_rate_bps": self.maker_fee_rate_bps,
            "minimum_order_size": self.minimum_order_size,
            "neg_risk": self.neg_risk,
            "tick_size": self.tick_size,
            "token_id": self.token_id,
            "taker_fee_rate": self.taker_fee_rate,
            "taker_fee_exponent": self.taker_fee_exponent,
            "taker_only": self.taker_only,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()

    def to_json(self) -> dict[str, object]:
        return {
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "tick_size": self.tick_size,
            "minimum_order_size": self.minimum_order_size,
            "neg_risk": self.neg_risk,
            "maker_fee_rate_bps": self.maker_fee_rate_bps,
            "observed_at_ns": self.observed_at_ns,
            "taker_fee_rate": self.taker_fee_rate,
            "taker_fee_exponent": self.taker_fee_exponent,
            "taker_only": self.taker_only,
            "rules_sha256": self.rules_sha256,
        }

    @classmethod
    def from_json(cls, raw: object) -> PaperMarketRules:
        if not isinstance(raw, Mapping):
            raise ValueError("paper market rules must be a JSON object")
        condition_id = raw.get("condition_id")
        token_id = raw.get("token_id")
        tick_size = raw.get("tick_size")
        minimum_order_size = raw.get("minimum_order_size")
        neg_risk = raw.get("neg_risk")
        maker_fee_rate_bps = raw.get("maker_fee_rate_bps")
        observed_at_ns = raw.get("observed_at_ns")
        taker_fee_rate = raw.get("taker_fee_rate")
        taker_fee_exponent = raw.get("taker_fee_exponent")
        taker_only = raw.get("taker_only")
        if not all(
            isinstance(value, str) and value for value in (condition_id, token_id, tick_size)
        ):
            raise ValueError("paper market rule identifiers must be non-empty strings")
        if (
            isinstance(minimum_order_size, bool)
            or not isinstance(minimum_order_size, int | float)
            or isinstance(taker_fee_rate, bool)
            or not isinstance(taker_fee_rate, int | float)
        ):
            raise ValueError("paper market rule sizes and rates must be numeric")
        if not isinstance(neg_risk, bool) or not isinstance(taker_only, bool):
            raise ValueError("paper market rule flags must be bool")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (maker_fee_rate_bps, observed_at_ns, taker_fee_exponent)
        ):
            raise ValueError("paper market rule counters must be integers")
        result = cls(
            condition_id=condition_id,
            token_id=token_id,
            tick_size=tick_size,
            minimum_order_size=float(minimum_order_size),
            neg_risk=neg_risk,
            maker_fee_rate_bps=maker_fee_rate_bps,
            observed_at_ns=observed_at_ns,
            taker_fee_rate=float(taker_fee_rate),
            taker_fee_exponent=taker_fee_exponent,
            taker_only=taker_only,
        )
        if raw.get("rules_sha256") != result.rules_sha256:
            raise ValueError("paper market rules hash mismatch")
        return result


@dataclass(slots=True)
class PaperOrderLayerState:
    order_id: str
    price: float
    size: float
    queue_ahead: float
    initial_queue_ahead: float
    filled_size: float = 0.0
    raw_eligible_sell_volume: float = 0.0
    stressed_eligible_sell_volume: float = 0.0

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.size - self.filled_size)


@dataclass(frozen=True, slots=True)
class PaperFakQuote:
    """Read-only executable FAK quote after fees and policy buffers."""

    requested_size: float
    filled_size: float
    filled_notional: float
    taker_fees: float
    limit_price: float | None
    net_edge_per_share: float | None

    def __post_init__(self) -> None:
        for name in ("requested_size", "filled_size", "filled_notional", "taker_fees"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value < 0.0
            ):
                raise ValueError(f"{name} must be finite and >= 0")
        if self.requested_size <= 0.0:
            raise ValueError("requested_size must be > 0")
        if self.filled_size > self.requested_size + 1e-12:
            raise ValueError("filled_size must not exceed requested_size")
        if self.filled_size <= 0.0:
            if (
                self.filled_notional != 0.0
                or self.taker_fees != 0.0
                or self.limit_price is not None
                or self.net_edge_per_share is not None
            ):
                raise ValueError("an empty FAK quote cannot contain execution values")
        elif (
            self.limit_price is None
            or isinstance(self.limit_price, bool)
            or not isinstance(self.limit_price, int | float)
            or not isfinite(self.limit_price)
            or not 0.0 < self.limit_price < 1.0
            or self.net_edge_per_share is None
            or isinstance(self.net_edge_per_share, bool)
            or not isinstance(self.net_edge_per_share, int | float)
            or not isfinite(self.net_edge_per_share)
        ):
            raise ValueError("a filled FAK quote requires a valid price and net edge")

    @property
    def fully_filled(self) -> bool:
        return self.filled_size >= self.requested_size - 1e-12

    @property
    def average_price(self) -> float | None:
        return None if self.filled_size <= 0.0 else self.filled_notional / self.filled_size

    @property
    def all_in_average_price(self) -> float | None:
        return (
            None
            if self.filled_size <= 0.0
            else (self.filled_notional + self.taker_fees) / self.filled_size
        )


@dataclass(slots=True)
class PaperPlacement:
    placement_id: str
    plan: OrderPlan
    rules: PaperMarketRules
    submitted_ts_ns: int
    active_ts_ns: int
    layers: tuple[PaperOrderLayerState, ...]
    status: str = "insert_pending"
    rejection_reason: str | None = None
    cancel_requested_ts_ns: int | None = None
    cancel_ack_ts_ns: int | None = None
    cancel_race_filled_size: float = 0.0
    terminal_reason: str | None = None
    terminal_ts_ns: int | None = None
    taker_filled_size: float = 0.0
    taker_filled_notional: float = 0.0
    taker_fees: float = 0.0
    execution_route: str = "maker"
    fak_requested_ts_ns: int | None = None
    fak_active_ts_ns: int | None = None
    fak_selected_probability: float | None = None
    fak_minimum_net_edge: float = 0.0
    fak_slippage_buffer: float = 0.0
    fak_model_uncertainty_buffer: float = 0.0
    fak_limit_price: float | None = None
    fak_net_edge_per_share: float | None = None

    @property
    def filled_size(self) -> float:
        return sum(layer.filled_size for layer in self.layers) + self.taker_filled_size

    @property
    def filled_notional(self) -> float:
        return (
            sum(layer.filled_size * layer.price for layer in self.layers)
            + self.taker_filled_notional
            + self.taker_fees
        )

    @property
    def maker_filled_size(self) -> float:
        return sum(layer.filled_size for layer in self.layers)

    @property
    def maker_filled_notional(self) -> float:
        return sum(layer.filled_size * layer.price for layer in self.layers)

    @property
    def average_fill_price(self) -> float | None:
        return None if self.filled_size <= 0.0 else self.filled_notional / self.filled_size


class PaperExecutionSimulator:
    """Pessimistic L2 heuristic requiring seller-initiated trade-volume evidence."""

    def __init__(self, *, gateway: PaperOrderGateway, config: PaperExecutionConfig) -> None:
        if not isinstance(gateway, PaperOrderGateway):
            raise TypeError("Research Paper requires PaperOrderGateway")
        self.gateway = gateway
        self.config = config
        self._placements: dict[str, PaperPlacement] = {}

    @property
    def placements(self) -> tuple[PaperPlacement, ...]:
        return tuple(self._placements.values())

    def retire_terminal_placements(self) -> None:
        if any(
            placement.status in {"insert_pending", "working", "cancel_pending", "fak_pending"}
            for placement in self._placements.values()
        ):
            raise RuntimeError("cannot retire an unfinished paper placement")
        self._placements.clear()

    def submit(
        self,
        *,
        plan: OrderPlan,
        rules: PaperMarketRules,
        book: SideBook,
        now_ts_ns: int,
    ) -> PaperPlacement:
        if plan.token_id != rules.token_id or book.token_id != rules.token_id:
            raise ValueError("plan, book, and rules token IDs must match")
        if abs(book.tick_size - float(rules.tick_size)) > 1e-12:
            raise ValueError("book tick size does not match observed market rules")
        if abs(book.minimum_order_size - rules.minimum_order_size) > 1e-12:
            raise ValueError("book minimum size does not match observed market rules")
        if rules.observed_at_ns > now_ts_ns:
            raise ValueError("market rules cannot be observed in the future")
        placement_id = f"{plan.market_slug}:{plan.created_ts_ns}"
        if placement_id in self._placements:
            raise ValueError("paper placement already exists")
        layer_states: list[PaperOrderLayerState] = []
        for index, layer in enumerate(plan.layers):
            if layer.queue_ahead is None:
                raise RuntimeError("validated maker layer has no queue-ahead value")
            request = LiveOrderRequest(
                condition_id=rules.condition_id,
                token_id=rules.token_id,
                price=layer.price,
                size=layer.size,
                tick_size=rules.tick_size,
                neg_risk=rules.neg_risk,
                minimum_order_size=rules.minimum_order_size,
                placement_id=placement_id,
                layer_index=index,
                rules_observed_at_ns=rules.observed_at_ns,
                rules_sha256=rules.rules_sha256,
                maker_fee_rate_bps=rules.maker_fee_rate_bps,
            )
            self.gateway.prewarm_market(request)
            prepared = self.gateway.prepare_post_only_buy(request)
            response = self.gateway.submit_prepared_post_only_buy(prepared)
            assert response.venue_order_id is not None
            layer_states.append(
                PaperOrderLayerState(
                    order_id=response.venue_order_id,
                    price=layer.price,
                    size=layer.size,
                    queue_ahead=layer.queue_ahead,
                    initial_queue_ahead=layer.queue_ahead,
                )
            )
        placement = PaperPlacement(
            placement_id=placement_id,
            plan=plan,
            rules=rules,
            submitted_ts_ns=now_ts_ns,
            active_ts_ns=now_ts_ns + round(self.config.insert_latency_ms * 1_000_000),
            layers=tuple(layer_states),
        )
        self._placements[placement_id] = placement
        return placement

    def submit_direct_fak(
        self,
        *,
        plan: OrderPlan,
        rules: PaperMarketRules,
        book: SideBook,
        now_ts_ns: int,
        selected_probability: float,
        minimum_net_edge: float,
        slippage_buffer: float,
        model_uncertainty_buffer: float,
    ) -> PaperPlacement:
        """Submit one delayed FAK attempt without a synthetic maker lifecycle.

        ``book`` validates the submission contract only. The single fill attempt
        uses the current book supplied to :meth:`advance` after taker latency.
        """

        self._validate_order_inputs(plan=plan, rules=rules, book=book, now_ts_ns=now_ts_ns)
        self._validate_fak_inputs(
            selected_probability=selected_probability,
            minimum_net_edge=minimum_net_edge,
            slippage_buffer=slippage_buffer,
            model_uncertainty_buffer=model_uncertainty_buffer,
        )
        placement_id = f"{plan.market_slug}:{plan.created_ts_ns}"
        if placement_id in self._placements:
            raise ValueError("paper placement already exists")
        active_ts_ns = now_ts_ns + round(self.config.taker_latency_ms * 1_000_000)
        placement = PaperPlacement(
            placement_id=placement_id,
            plan=plan,
            rules=rules,
            submitted_ts_ns=now_ts_ns,
            active_ts_ns=active_ts_ns,
            layers=(),
            status="fak_pending",
            execution_route="direct_fak",
            fak_requested_ts_ns=now_ts_ns,
            fak_active_ts_ns=active_ts_ns,
            fak_selected_probability=selected_probability,
            fak_minimum_net_edge=minimum_net_edge,
            fak_slippage_buffer=slippage_buffer,
            fak_model_uncertainty_buffer=model_uncertainty_buffer,
        )
        self._placements[placement_id] = placement
        return placement

    def preview_fak(
        self,
        *,
        token_id: str,
        requested_size: float,
        rules: PaperMarketRules,
        book: SideBook,
        now_ts_ns: int,
        selected_probability: float,
        minimum_net_edge: float,
        slippage_buffer: float,
        model_uncertainty_buffer: float,
    ) -> PaperFakQuote:
        """Quote one FAK against the complete current ask depth without mutating state."""

        self._validate_market_inputs(
            token_id=token_id,
            rules=rules,
            book=book,
            now_ts_ns=now_ts_ns,
        )
        if (
            isinstance(requested_size, bool)
            or not isinstance(requested_size, int | float)
            or not isfinite(requested_size)
            or requested_size + 1e-12 < rules.minimum_order_size
        ):
            raise ValueError("requested_size must be finite and at least the minimum order size")
        self._validate_fak_inputs(
            selected_probability=selected_probability,
            minimum_net_edge=minimum_net_edge,
            slippage_buffer=slippage_buffer,
            model_uncertainty_buffer=model_uncertainty_buffer,
        )
        remaining = requested_size
        filled = 0.0
        notional = 0.0
        fees = 0.0
        limit_price: float | None = None
        weighted_net_edge = 0.0
        for level in sorted(book.asks, key=lambda item: item.price):
            if remaining <= 1e-12:
                break
            quantity = min(remaining, level.size)
            if quantity <= 0.0:
                continue
            fee = calculate_commission(
                quantity=Decimal(str(quantity)),
                price=Decimal(str(level.price)),
                fee_rate=Decimal(str(rules.taker_fee_rate)),
                liquidity_side=LiquiditySide.TAKER,
            )
            fee_per_share = fee / quantity
            net_edge = (
                selected_probability
                - level.price
                - fee_per_share
                - slippage_buffer
                - model_uncertainty_buffer
            )
            if net_edge + 1e-12 < minimum_net_edge:
                break
            filled += quantity
            notional += quantity * level.price
            fees += fee
            weighted_net_edge += quantity * net_edge
            remaining -= quantity
            limit_price = level.price
        return PaperFakQuote(
            requested_size=requested_size,
            filled_size=filled,
            filled_notional=notional,
            taker_fees=fees,
            limit_price=limit_price,
            net_edge_per_share=None if filled <= 0.0 else weighted_net_edge / filled,
        )

    def advance(self, *, now_ts_ns: int, books: dict[str, SideBook]) -> None:
        for placement in self._placements.values():
            if placement.status == "insert_pending" and now_ts_ns >= placement.active_ts_ns:
                book = books.get(placement.plan.token_id)
                if book is None:
                    self._reject(placement, "book_unavailable")
                elif any(layer.price >= book.best_ask for layer in placement.layers):
                    self._reject(placement, "post_only_would_cross")
                else:
                    placement.status = "working"
            if placement.status == "working" and now_ts_ns >= placement.plan.expires_ts_ns:
                self.request_cancel(
                    placement,
                    now_ts_ns=now_ts_ns,
                    reason="max_work_age",
                )
            if (
                placement.status == "cancel_pending"
                and placement.cancel_ack_ts_ns is not None
                and now_ts_ns >= placement.cancel_ack_ts_ns
            ):
                for layer in placement.layers:
                    self.gateway.cancel_order(layer.order_id)
                placement.status = "partially_filled" if placement.filled_size else "canceled"
                placement.terminal_ts_ns = now_ts_ns
            if (
                placement.status == "fak_pending"
                and placement.fak_active_ts_ns is not None
                and now_ts_ns >= placement.fak_active_ts_ns
            ):
                book = books.get(placement.plan.token_id)
                if book is None:
                    placement.status = "canceled"
                    placement.terminal_reason = "fak_book_unavailable"
                    placement.terminal_ts_ns = now_ts_ns
                else:
                    self._execute_fak(placement, book=book, now_ts_ns=now_ts_ns)

    def request_cancel(
        self,
        placement: PaperPlacement,
        *,
        now_ts_ns: int,
        reason: str = "risk_cancel",
    ) -> None:
        if placement.status not in {"working", "insert_pending"}:
            return
        placement.status = "cancel_pending"
        placement.cancel_requested_ts_ns = now_ts_ns
        placement.cancel_ack_ts_ns = now_ts_ns + round(self.config.cancel_latency_ms * 1_000_000)
        placement.terminal_reason = reason

    def request_fak(
        self,
        placement: PaperPlacement,
        *,
        now_ts_ns: int,
        selected_probability: float,
        minimum_net_edge: float,
        slippage_buffer: float,
        model_uncertainty_buffer: float,
    ) -> bool:
        if placement.status != "canceled" or placement.maker_filled_size > 0.0:
            return False
        self._validate_fak_inputs(
            selected_probability=selected_probability,
            minimum_net_edge=minimum_net_edge,
            slippage_buffer=slippage_buffer,
            model_uncertainty_buffer=model_uncertainty_buffer,
        )
        placement.status = "fak_pending"
        placement.execution_route = "maker_then_fak"
        placement.fak_requested_ts_ns = now_ts_ns
        placement.fak_active_ts_ns = now_ts_ns + round(self.config.taker_latency_ms * 1_000_000)
        placement.fak_selected_probability = selected_probability
        placement.fak_minimum_net_edge = minimum_net_edge
        placement.fak_slippage_buffer = slippage_buffer
        placement.fak_model_uncertainty_buffer = model_uncertainty_buffer
        placement.terminal_reason = None
        placement.terminal_ts_ns = None
        return True

    def abort_fak(
        self,
        placement: PaperPlacement,
        *,
        now_ts_ns: int,
        reason: str,
    ) -> None:
        if placement.status != "fak_pending":
            return
        placement.status = "canceled"
        placement.terminal_reason = reason
        placement.terminal_ts_ns = now_ts_ns

    def on_trade(
        self,
        *,
        token_id: str,
        aggressor_side: Literal["buy", "sell", "unknown"],
        price: float,
        size: float,
        available_ts_ns: int,
    ) -> None:
        if aggressor_side != "sell" or not isfinite(size) or size <= 0.0:
            return
        for placement in self._placements.values():
            if (
                placement.plan.token_id != token_id
                or placement.status not in {"working", "cancel_pending"}
                or available_ts_ns < placement.active_ts_ns
            ):
                continue
            volume = size * self.config.trade_volume_multiplier
            was_cancel_pending = placement.status == "cancel_pending"
            for layer in sorted(placement.layers, key=lambda item: item.price, reverse=True):
                if volume <= 0.0 or price > layer.price or layer.remaining_size <= 0.0:
                    continue
                layer.raw_eligible_sell_volume += volume / self.config.trade_volume_multiplier
                layer.stressed_eligible_sell_volume += volume
                queue_consumed = min(volume, layer.queue_ahead)
                layer.queue_ahead -= queue_consumed
                volume -= queue_consumed
                filled = min(volume, layer.remaining_size)
                if filled <= 0.0:
                    continue
                layer.filled_size += filled
                volume -= filled
                if was_cancel_pending:
                    placement.cancel_race_filled_size += filled
            if placement.filled_size >= placement.plan.total_size - 1e-12:
                placement.status = "filled"
                placement.terminal_reason = "filled"
                placement.terminal_ts_ns = available_ts_ns
                continue
            if placement.filled_size > 0.0 and placement.status == "working":
                self.request_cancel(
                    placement,
                    now_ts_ns=available_ts_ns,
                    reason="partial_fill",
                )

    def _reject(self, placement: PaperPlacement, reason: str) -> None:
        for layer in placement.layers:
            self.gateway.cancel_order(layer.order_id)
        placement.status = "rejected"
        placement.rejection_reason = reason
        placement.terminal_reason = reason
        placement.terminal_ts_ns = placement.active_ts_ns

    @staticmethod
    def _validate_order_inputs(
        *,
        plan: OrderPlan,
        rules: PaperMarketRules,
        book: SideBook,
        now_ts_ns: int,
    ) -> None:
        if not isinstance(plan, OrderPlan):
            raise TypeError("plan must be an OrderPlan")
        PaperExecutionSimulator._validate_market_inputs(
            token_id=plan.token_id,
            rules=rules,
            book=book,
            now_ts_ns=now_ts_ns,
        )
        if isinstance(now_ts_ns, bool) or now_ts_ns < plan.created_ts_ns:
            raise ValueError("now_ts_ns must be at or after plan creation")
        if now_ts_ns >= plan.expires_ts_ns:
            raise ValueError("now_ts_ns must be before plan expiry")

    @staticmethod
    def _validate_market_inputs(
        *,
        token_id: str,
        rules: PaperMarketRules,
        book: SideBook,
        now_ts_ns: int,
    ) -> None:
        if not isinstance(rules, PaperMarketRules):
            raise TypeError("rules must be PaperMarketRules")
        if not isinstance(book, SideBook):
            raise TypeError("book must be a SideBook")
        if not isinstance(token_id, str) or not token_id:
            raise ValueError("token_id must be a non-empty string")
        if token_id != rules.token_id or book.token_id != rules.token_id:
            raise ValueError("token, book, and rules IDs must match")
        if abs(book.tick_size - float(rules.tick_size)) > 1e-12:
            raise ValueError("book tick size does not match observed market rules")
        if abs(book.minimum_order_size - rules.minimum_order_size) > 1e-12:
            raise ValueError("book minimum size does not match observed market rules")
        if isinstance(now_ts_ns, bool) or not isinstance(now_ts_ns, int) or now_ts_ns < 0:
            raise ValueError("now_ts_ns must be a non-negative integer")
        if rules.observed_at_ns > now_ts_ns:
            raise ValueError("market rules cannot be observed in the future")

    @staticmethod
    def _validate_fak_inputs(
        *,
        selected_probability: float,
        minimum_net_edge: float,
        slippage_buffer: float,
        model_uncertainty_buffer: float,
    ) -> None:
        for name, value in (
            ("selected_probability", selected_probability),
            ("minimum_net_edge", minimum_net_edge),
            ("slippage_buffer", slippage_buffer),
            ("model_uncertainty_buffer", model_uncertainty_buffer),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value < 0.0
            ):
                raise ValueError(f"{name} must be finite and >= 0")
        if not 0.0 < selected_probability < 1.0:
            raise ValueError("selected_probability must be in (0, 1)")
        if minimum_net_edge + slippage_buffer + model_uncertainty_buffer >= 1.0:
            raise ValueError("FAK edge and safety buffers must sum to less than 1")

    def _execute_fak(
        self,
        placement: PaperPlacement,
        *,
        book: SideBook,
        now_ts_ns: int,
    ) -> None:
        selected_probability = placement.fak_selected_probability
        if selected_probability is None:
            raise RuntimeError("FAK probability is unavailable")
        remaining = placement.plan.total_size - placement.maker_filled_size
        quote = self.preview_fak(
            token_id=placement.plan.token_id,
            requested_size=remaining,
            rules=placement.rules,
            book=book,
            now_ts_ns=now_ts_ns,
            selected_probability=selected_probability,
            minimum_net_edge=placement.fak_minimum_net_edge,
            slippage_buffer=placement.fak_slippage_buffer,
            model_uncertainty_buffer=placement.fak_model_uncertainty_buffer,
        )
        placement.taker_filled_size = quote.filled_size
        placement.taker_filled_notional = quote.filled_notional
        placement.taker_fees = quote.taker_fees
        placement.fak_limit_price = quote.limit_price
        placement.fak_net_edge_per_share = quote.net_edge_per_share
        placement.terminal_ts_ns = now_ts_ns
        if quote.filled_size <= 0.0:
            placement.status = "canceled"
            placement.terminal_reason = "fak_net_edge_insufficient"
        elif placement.filled_size >= placement.plan.total_size - 1e-12:
            placement.status = "filled"
            placement.terminal_reason = "fak_filled"
        else:
            placement.status = "partially_filled"
            placement.terminal_reason = "fak_partial_depth"


__all__ = [
    "PaperExecutionConfig",
    "PaperExecutionSimulator",
    "PaperFakQuote",
    "PaperMarketRules",
    "PaperOrderLayerState",
    "PaperPlacement",
]
