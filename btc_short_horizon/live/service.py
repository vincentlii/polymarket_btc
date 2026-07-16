"""Shadow-first order service, canary gates, WAL, and user-channel reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import isfinite
from typing import Mapping
from uuid import uuid4

from btc_short_horizon.live.gateway import (
    GatewayCancellationUnknownError,
    GatewaySubmissionUnknownError,
    LiveOrderGateway,
    LiveOrderRequest,
    PaperOrderGateway,
)
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
        if config.mode is LiveMode.PAPER and not isinstance(gateway, PaperOrderGateway):
            raise ValueError("paper mode requires PaperOrderGateway")
        self.config = config
        self.wal = wal
        self.gateway = gateway
        self.orders: dict[str, LiveOrder] = {}
        self.trades: dict[tuple[str, str], LiveTrade] = {}
        self._client_order_by_venue: dict[str, str] = {}
        self._progress = CanaryProgress()
        self._last_heartbeat_ts_ns: int | None = None
        self._heartbeat_cancel_requested = False
        self._heartbeat_id = ""
        self._ambiguous_markets: set[str] = set()
        self._halted = False

    @property
    def canary_progress(self) -> CanaryProgress:
        return self._progress

    @property
    def halted(self) -> bool:
        return self._halted

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
        if self._halted:
            self._block_order(order, ts_ns=ts_ns, reason="kill_switch_active")
            return SubmitResult(False, client_order_id, "kill_switch_active")
        if request.condition_id in self._ambiguous_markets:
            self._block_order(order, ts_ns=ts_ns, reason="ambiguous_submission_pending")
            return SubmitResult(False, client_order_id, "ambiguous_submission_pending")
        if self.config.mode is LiveMode.SHADOW:
            self._write("shadow_order", ts_ns, {"order": order})
            return SubmitResult(False, client_order_id, "shadow_mode")
        if self.config.mode is LiveMode.CANARY and request.size > self.config.canary_max_shares:
            self._block_order(order, ts_ns=ts_ns, reason="canary_size_limit")
            return SubmitResult(False, client_order_id, "canary_size_limit")
        risk_config = self.config.risk
        if self.config.mode is LiveMode.PAPER:
            risk_config = replace(
                risk_config,
                trading_enabled=True,
                require_geo_eligible=False,
            )
        risk = evaluate_order_risk(
            config=risk_config,
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
        except GatewaySubmissionUnknownError as exc:
            unknown = submitted.transition(LiveOrderStatus.UNKNOWN)
            self.orders[client_order_id] = unknown
            self._ambiguous_markets.add(request.condition_id)
            self._write("order_submission_unknown", ts_ns, {"order": unknown, "error": str(exc)})
            return SubmitResult(False, client_order_id, "gateway_submission_unknown")
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
        try:
            self.gateway.cancel_order(order.venue_order_id)
        except GatewayCancellationUnknownError as exc:
            unknown = requested.transition(LiveOrderStatus.UNKNOWN)
            self.orders[client_order_id] = unknown
            self._ambiguous_markets.add(order.market_id)
            self._write("cancel_unknown", ts_ns, {"order": unknown, "error": str(exc)})

    def cancel_all(self, *, ts_ns: int, reason: str) -> None:
        if not reason:
            raise ValueError("cancel reason is required")
        if self.config.mode is LiveMode.SHADOW:
            self._write("shadow_cancel_all", ts_ns, {"reason": reason})
            return
        assert self.gateway is not None
        self._write("cancel_all_requested", ts_ns, {"reason": reason})
        self.gateway.cancel_all()

    def emergency_stop(self, *, ts_ns: int, reason: str) -> bool:
        """Freeze new submissions before attempting cancellation of venue orders."""

        if ts_ns < 0:
            raise ValueError("ts_ns must be non-negative")
        if not reason:
            raise ValueError("kill-switch reason is required")
        if self._halted:
            return False
        self._halted = True
        self._write("kill_switch_activated", ts_ns, {"reason": reason})
        try:
            self.cancel_all(ts_ns=ts_ns, reason=reason)
        except Exception as exc:
            self._write("kill_switch_cancel_error", ts_ns, {"error": str(exc)})
        return True

    def record_heartbeat(self, *, ts_ns: int) -> None:
        if ts_ns < 0:
            raise ValueError("ts_ns must be non-negative")
        self._last_heartbeat_ts_ns = ts_ns
        self._heartbeat_cancel_requested = False
        self._write("heartbeat", ts_ns, {})

    def send_venue_heartbeat(self, *, ts_ns: int) -> str:
        if ts_ns < 0:
            raise ValueError("ts_ns must be non-negative")
        if self.config.mode is LiveMode.SHADOW:
            self.record_heartbeat(ts_ns=ts_ns)
            return ""
        assert self.gateway is not None
        self._heartbeat_id = self.gateway.send_heartbeat(self._heartbeat_id)
        self.record_heartbeat(ts_ns=ts_ns)
        self._write("venue_heartbeat", ts_ns, {"heartbeat_id": self._heartbeat_id})
        return self._heartbeat_id

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
        self.emergency_stop(ts_ns=now_ts_ns, reason="heartbeat_timeout")
        self._heartbeat_cancel_requested = True
        return True

    def reconcile_user_event(self, event: Mapping[str, object], *, ts_ns: int) -> None:
        event_type = str(event.get("event_type") or event.get("type") or "").casefold()
        if event_type == "order":
            self._reconcile_order(event, ts_ns=ts_ns)
        elif event_type == "trade":
            self._reconcile_trade(event, ts_ns=ts_ns)

    def restore_from_wal(self) -> int:
        """Restore the latest durable lifecycle snapshots before venue reconciliation."""

        if self.orders or self.trades:
            raise ValueError("restore_from_wal requires an empty service state")
        restored = 0
        for record in self.wal.read():
            event_type = str(record.get("event_type") or "")
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                continue
            raw_order = payload.get("order")
            if isinstance(raw_order, Mapping):
                order = _live_order_from_json(raw_order)
                self.orders[order.client_order_id] = order
                if order.venue_order_id:
                    self._client_order_by_venue[order.venue_order_id] = order.client_order_id
                if order.status is LiveOrderStatus.UNKNOWN:
                    self._ambiguous_markets.add(order.market_id)
                restored += 1
            raw_trade = payload.get("trade")
            if isinstance(raw_trade, Mapping):
                trade = _live_trade_from_json(raw_trade)
                self.trades[(trade.trade_id, trade.client_order_id)] = trade
            if event_type == "kill_switch_activated":
                self._halted = True
            elif event_type == "heartbeat":
                self._last_heartbeat_ts_ns = int(record.get("ts_ns", 0))
            elif event_type == "venue_heartbeat":
                heartbeat_id = payload.get("heartbeat_id")
                if isinstance(heartbeat_id, str):
                    self._heartbeat_id = heartbeat_id
        submitted_orders = sum(order.venue_order_id is not None for order in self.orders.values())
        self._progress = CanaryProgress(submitted_orders=submitted_orders, fills=len(self.trades))
        return restored

    def resolve_submission_unknown(
        self,
        *,
        client_order_id: str,
        ts_ns: int,
        venue_order_id: str | None = None,
        confirmed_absent: bool = False,
    ) -> LiveOrder:
        """Apply an explicit venue reconciliation result; never guess or auto-retry."""

        if ts_ns < 0:
            raise ValueError("ts_ns must be non-negative")
        if bool(venue_order_id) == confirmed_absent:
            raise ValueError("provide exactly one of venue_order_id or confirmed_absent=True")
        order = self.orders[client_order_id]
        if order.status is not LiveOrderStatus.UNKNOWN:
            raise ValueError("only an unknown submission can be resolved")
        if venue_order_id:
            resolved = order.transition(LiveOrderStatus.LIVE, venue_order_id=venue_order_id)
            self._client_order_by_venue[venue_order_id] = client_order_id
            event_type = "order_submission_found"
        else:
            resolved = order.transition(LiveOrderStatus.REJECTED)
            event_type = "order_submission_confirmed_absent"
        self.orders[client_order_id] = resolved
        self._ambiguous_markets.discard(order.market_id)
        self._write(event_type, ts_ns, {"order": resolved})
        return resolved

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
                LiveOrderStatus.UNKNOWN,
            }:
                order = order.transition(LiveOrderStatus.CANCELED)
        elif kind in {"PLACEMENT", "UPDATE"} and order.status is LiveOrderStatus.SUBMITTED:
            order = order.transition(LiveOrderStatus.LIVE)
        self.orders[order.client_order_id] = order
        if order.status is not LiveOrderStatus.UNKNOWN:
            self._ambiguous_markets.discard(order.market_id)
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


def _live_order_from_json(value: Mapping[str, object]) -> LiveOrder:
    return LiveOrder(
        client_order_id=str(value["client_order_id"]),
        market_id=str(value["market_id"]),
        token_id=str(value["token_id"]),
        price=float(value["price"]),
        size=float(value["size"]),
        status=LiveOrderStatus(str(value["status"])),
        venue_order_id=(
            None if value.get("venue_order_id") is None else str(value["venue_order_id"])
        ),
        matched_size=float(value.get("matched_size", 0.0)),
    )


def _live_trade_from_json(value: Mapping[str, object]) -> LiveTrade:
    return LiveTrade(
        trade_id=str(value["trade_id"]),
        client_order_id=str(value["client_order_id"]),
        price=float(value["price"]),
        size=float(value["size"]),
        status=LiveTradeStatus(str(value["status"])),
        transaction_hash=(
            None if value.get("transaction_hash") is None else str(value["transaction_hash"])
        ),
    )
