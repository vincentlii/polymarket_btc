"""Shadow-first order service, canary gates, WAL, and user-channel reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import isfinite
from typing import Mapping
from uuid import uuid4

from btc_short_horizon.live.gateway import LiveOrderGateway, LiveOrderRequest
from btc_short_horizon.live.risk import AccountSnapshot, TradingSafetyConfig, evaluate_order_risk
from btc_short_horizon.live.state import LiveOrder, LiveOrderStatus, LiveTrade, LiveTradeStatus
from btc_short_horizon.live.wal import JsonlWriteAheadLog


class LiveMode(StrEnum):
    SHADOW = "shadow"
    PAPER = "paper"
    CANARY = "canary"


@dataclass(frozen=True, slots=True)
class LiveExecutionConfig:
    mode: LiveMode = LiveMode.SHADOW
    risk: TradingSafetyConfig = field(default_factory=TradingSafetyConfig)
    heartbeat_timeout_seconds: float = 5.0
    canary_max_shares: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.mode, LiveMode):
            object.__setattr__(self, "mode", LiveMode(self.mode))
        if not isfinite(self.heartbeat_timeout_seconds) or self.heartbeat_timeout_seconds <= 0.0:
            raise ValueError("heartbeat_timeout_seconds must be finite and > 0")
        if not isfinite(self.canary_max_shares) or self.canary_max_shares <= 0.0:
            raise ValueError("canary_max_shares must be finite and > 0")


@dataclass(frozen=True, slots=True)
class SubmitResult:
    submitted: bool
    client_order_id: str
    reason: str
    venue_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class CanaryProgress:
    submitted_orders: int = 0
    fills: int = 0

    @property
    def ready_for_extended_canary(self) -> bool:
        return self.submitted_orders >= 200 and self.fills >= 50

    @property
    def ready_for_scale_review(self) -> bool:
        return self.submitted_orders >= 2_000 and self.fills >= 300


class LiveExecutionService:
    """Coordinates local lifecycle state; actual credentials stay inside the gateway."""

    def __init__(
        self,
        *,
        config: LiveExecutionConfig,
        wal: JsonlWriteAheadLog,
        gateway: LiveOrderGateway | None = None,
    ) -> None:
        if config.mode is not LiveMode.SHADOW and gateway is None:
            raise ValueError("paper and canary modes require a gateway")
        self.config = config
        self.wal = wal
        self.gateway = gateway
        self.orders: dict[str, LiveOrder] = {}
        self.trades: dict[tuple[str, str], LiveTrade] = {}
        self._client_order_by_venue: dict[str, str] = {}
        self._progress = CanaryProgress()
        self._last_heartbeat_ts_ns: int | None = None
        self._heartbeat_cancel_requested = False

    @property
    def canary_progress(self) -> CanaryProgress:
        return self._progress

    def submit(
        self,
        *,
        request: LiveOrderRequest,
        account: AccountSnapshot,
        ts_ns: int,
    ) -> SubmitResult:
        if ts_ns < 0:
            raise ValueError("ts_ns must be non-negative")
        client_order_id = uuid4().hex
        order = LiveOrder(
            client_order_id=client_order_id,
            market_id=request.condition_id,
            token_id=request.token_id,
            price=request.price,
            size=request.size,
        )
        self.orders[client_order_id] = order
        self._write("order_decision", ts_ns, {"order": order, "mode": self.config.mode})
        if self.config.mode is LiveMode.SHADOW:
            self._write("shadow_order", ts_ns, {"order": order})
            return SubmitResult(False, client_order_id, "shadow_mode")
        if self.config.mode is LiveMode.CANARY and request.size > self.config.canary_max_shares:
            self._block_order(order, ts_ns=ts_ns, reason="canary_size_limit")
            return SubmitResult(False, client_order_id, "canary_size_limit")
        risk = evaluate_order_risk(
            config=self.config.risk,
            account=account,
            order_notional=request.price * request.size,
        )
        if not risk.allowed:
            self._block_order(order, ts_ns=ts_ns, reason=risk.reason)
            return SubmitResult(False, client_order_id, risk.reason)
        assert self.gateway is not None
        signed = order.transition(LiveOrderStatus.SIGNED)
        submitted = signed.transition(LiveOrderStatus.SUBMITTED)
        self.orders[client_order_id] = submitted
        try:
            response = self.gateway.submit_post_only_buy(request)
        except Exception as exc:
            rejected = submitted.transition(LiveOrderStatus.REJECTED)
            self.orders[client_order_id] = rejected
            self._write("order_rejected", ts_ns, {"order": rejected, "error": str(exc)})
            return SubmitResult(False, client_order_id, "gateway_rejected")
        live = submitted.transition(LiveOrderStatus.LIVE, venue_order_id=response.venue_order_id)
        self.orders[client_order_id] = live
        self._client_order_by_venue[response.venue_order_id] = client_order_id
        self._progress = replace(
            self._progress, submitted_orders=self._progress.submitted_orders + 1
        )
        self._write("order_submitted", ts_ns, {"order": live, "response": response})
        return SubmitResult(True, client_order_id, "submitted", response.venue_order_id)

    def request_cancel(self, *, client_order_id: str, ts_ns: int) -> None:
        order = self.orders[client_order_id]
        if self.config.mode is LiveMode.SHADOW:
            self._write("shadow_cancel", ts_ns, {"order": order})
            return
        if order.status not in {LiveOrderStatus.LIVE, LiveOrderStatus.NOT_CANCELED}:
            raise ValueError("only live orders can be cancelled")
        if order.venue_order_id is None:
            raise ValueError("live order has no venue_order_id")
        assert self.gateway is not None
        requested = order.transition(LiveOrderStatus.CANCEL_REQUESTED)
        self.orders[client_order_id] = requested
        self._write("cancel_requested", ts_ns, {"order": requested})
        self.gateway.cancel_order(order.venue_order_id)

    def cancel_all(self, *, ts_ns: int, reason: str) -> None:
        if not reason:
            raise ValueError("cancel reason is required")
        if self.config.mode is LiveMode.SHADOW:
            self._write("shadow_cancel_all", ts_ns, {"reason": reason})
            return
        assert self.gateway is not None
        self._write("cancel_all_requested", ts_ns, {"reason": reason})
        self.gateway.cancel_all()

    def record_heartbeat(self, *, ts_ns: int) -> None:
        if ts_ns < 0:
            raise ValueError("ts_ns must be non-negative")
        self._last_heartbeat_ts_ns = ts_ns
        self._heartbeat_cancel_requested = False
        self._write("heartbeat", ts_ns, {})

    def enforce_heartbeat_timeout(self, *, now_ts_ns: int) -> bool:
        if now_ts_ns < 0:
            raise ValueError("now_ts_ns must be non-negative")
        if self._last_heartbeat_ts_ns is None:
            return False
        timeout_ns = int(self.config.heartbeat_timeout_seconds * 1_000_000_000)
        if now_ts_ns - self._last_heartbeat_ts_ns <= timeout_ns:
            return False
        if self._heartbeat_cancel_requested:
            return False
        self.cancel_all(ts_ns=now_ts_ns, reason="heartbeat_timeout")
        self._heartbeat_cancel_requested = True
        return True

    def reconcile_user_event(self, event: Mapping[str, object], *, ts_ns: int) -> None:
        event_type = str(event.get("event_type") or event.get("type") or "").casefold()
        if event_type == "order":
            self._reconcile_order(event, ts_ns=ts_ns)
        elif event_type == "trade":
            self._reconcile_trade(event, ts_ns=ts_ns)

    def _reconcile_order(self, event: Mapping[str, object], *, ts_ns: int) -> None:
        order = self._order_for_venue_event(event)
        if order is None:
            return
        kind = str(event.get("type") or "").upper()
        cumulative = _nonnegative_float(event.get("size_matched", 0), "size_matched")
        order = self._record_cumulative_match(order, cumulative)
        if kind == "CANCELLATION":
            if order.status in {
                LiveOrderStatus.LIVE,
                LiveOrderStatus.CANCEL_REQUESTED,
                LiveOrderStatus.NOT_CANCELED,
            }:
                order = order.transition(LiveOrderStatus.CANCELED)
        elif kind in {"PLACEMENT", "UPDATE"} and order.status is LiveOrderStatus.SUBMITTED:
            order = order.transition(LiveOrderStatus.LIVE)
        self.orders[order.client_order_id] = order
        self._write("user_order", ts_ns, {"order": order, "event": dict(event)})

    def _reconcile_trade(self, event: Mapping[str, object], *, ts_ns: int) -> None:
        trade_id = str(event.get("id") or "")
        status = str(event.get("status") or "").casefold()
        if not trade_id or status not in {item.value for item in LiveTradeStatus}:
            return
        for maker in _mapping_sequence(event.get("maker_orders")):
            venue_order_id = str(maker.get("order_id") or "")
            client_order_id = self._client_order_by_venue.get(venue_order_id)
            if client_order_id is None:
                continue
            order = self.orders[client_order_id]
            matched = _nonnegative_float(maker.get("matched_amount", 0), "matched_amount")
            trade_key = (trade_id, client_order_id)
            trade = self.trades.get(trade_key)
            if trade is None:
                if matched <= 0.0:
                    continue
                order = order.record_match(matched)
                self.orders[client_order_id] = order
                trade = LiveTrade(
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    price=_positive_float(maker.get("price"), "price"),
                    size=matched,
                    status=LiveTradeStatus.MATCHED,
                )
                self._progress = replace(self._progress, fills=self._progress.fills + 1)
            desired_status = LiveTradeStatus(status)
            if desired_status is not trade.status:
                trade = trade.transition(desired_status)
            self.trades[trade_key] = trade
            self._write("user_trade", ts_ns, {"order": order, "trade": trade, "event": dict(event)})

    def _order_for_venue_event(self, event: Mapping[str, object]) -> LiveOrder | None:
        venue_order_id = str(event.get("id") or "")
        client_order_id = self._client_order_by_venue.get(venue_order_id)
        return None if client_order_id is None else self.orders[client_order_id]

    @staticmethod
    def _record_cumulative_match(order: LiveOrder, cumulative: float) -> LiveOrder:
        if cumulative < order.matched_size:
            raise ValueError("user channel matched size cannot decrease")
        return order.record_match(cumulative - order.matched_size)

    def _write(self, event_type: str, ts_ns: int, payload: Mapping[str, object]) -> None:
        self.wal.append(event_type=event_type, ts_ns=ts_ns, payload=payload)

    def _block_order(self, order: LiveOrder, *, ts_ns: int, reason: str) -> None:
        rejected = order.transition(LiveOrderStatus.REJECTED)
        self.orders[order.client_order_id] = rejected
        self._write("order_blocked", ts_ns, {"order": rejected, "reason": reason})


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _nonnegative_float(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be finite and >= 0")
    return result


def _positive_float(value: object, name: str) -> float:
    result = _nonnegative_float(value, name)
    if result <= 0.0:
        raise ValueError(f"{name} must be > 0")
    return result
