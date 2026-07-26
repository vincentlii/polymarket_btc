"""Conservative trade-evidence matching for credential-free Research Paper orders."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from typing import Literal

from btc_short_horizon.live.gateway import LiveOrderRequest, PaperOrderGateway
from btc_short_horizon.strategy import OrderPlan, SideBook


@dataclass(frozen=True, slots=True)
class PaperExecutionConfig:
    """Fixed heuristic scenario; it is research evidence, never venue fill truth."""

    insert_latency_ms: float
    cancel_latency_ms: float
    trade_volume_multiplier: float

    def __post_init__(self) -> None:
        for name in ("insert_latency_ms", "cancel_latency_ms"):
            value = getattr(self, name)
            if not isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and >= 0")
        if (
            not isfinite(self.trade_volume_multiplier)
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

    @property
    def rules_sha256(self) -> str:
        payload = {
            "condition_id": self.condition_id,
            "maker_fee_rate_bps": self.maker_fee_rate_bps,
            "minimum_order_size": self.minimum_order_size,
            "neg_risk": self.neg_risk,
            "tick_size": self.tick_size,
            "token_id": self.token_id,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return sha256(encoded).hexdigest()


@dataclass(slots=True)
class PaperOrderLayerState:
    order_id: str
    price: float
    size: float
    queue_ahead: float
    filled_size: float = 0.0

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.size - self.filled_size)


@dataclass(slots=True)
class PaperPlacement:
    placement_id: str
    plan: OrderPlan
    submitted_ts_ns: int
    active_ts_ns: int
    layers: tuple[PaperOrderLayerState, ...]
    status: str = "insert_pending"
    rejection_reason: str | None = None
    cancel_requested_ts_ns: int | None = None
    cancel_ack_ts_ns: int | None = None
    cancel_race_filled_size: float = 0.0

    @property
    def filled_size(self) -> float:
        return sum(layer.filled_size for layer in self.layers)

    @property
    def filled_notional(self) -> float:
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
                    queue_ahead=layer.visible_size,
                )
            )
        placement = PaperPlacement(
            placement_id=placement_id,
            plan=plan,
            submitted_ts_ns=now_ts_ns,
            active_ts_ns=now_ts_ns + round(self.config.insert_latency_ms * 1_000_000),
            layers=tuple(layer_states),
        )
        self._placements[placement_id] = placement
        return placement

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
                self.request_cancel(placement, now_ts_ns=now_ts_ns)
            if (
                placement.status == "cancel_pending"
                and placement.cancel_ack_ts_ns is not None
                and now_ts_ns >= placement.cancel_ack_ts_ns
            ):
                for layer in placement.layers:
                    self.gateway.cancel_order(layer.order_id)
                placement.status = "partially_filled" if placement.filled_size else "canceled"

    def request_cancel(self, placement: PaperPlacement, *, now_ts_ns: int) -> None:
        if placement.status not in {"working", "insert_pending"}:
            return
        placement.status = "cancel_pending"
        placement.cancel_requested_ts_ns = now_ts_ns
        placement.cancel_ack_ts_ns = now_ts_ns + round(self.config.cancel_latency_ms * 1_000_000)

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
                continue
            if placement.filled_size > 0.0 and placement.status == "working":
                self.request_cancel(placement, now_ts_ns=available_ts_ns)

    def _reject(self, placement: PaperPlacement, reason: str) -> None:
        for layer in placement.layers:
            self.gateway.cancel_order(layer.order_id)
        placement.status = "rejected"
        placement.rejection_reason = reason


__all__ = [
    "PaperExecutionConfig",
    "PaperExecutionSimulator",
    "PaperMarketRules",
    "PaperOrderLayerState",
    "PaperPlacement",
]
