"""Shadow-first maker execution coordinator with durable fail-closed state."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
import re
from threading import RLock

from btc_short_horizon.live.gateway import (
    GatewayCancellationUnknownError,
    GatewayHeartbeatError,
    GatewayMarketPrewarm,
    GatewayOrderResponse,
    GatewaySubmissionUnknownError,
    LiveOrderGateway,
    LiveOrderRequest,
    PaperOrderGateway,
    PreparedPostOnlyOrder,
)
from btc_short_horizon.live.risk import (
    AccountSnapshot,
    RiskReservations,
    TradingSafetyConfig,
    evaluate_order_risk,
)
from btc_short_horizon.live.reconciliation import (
    LocalOrderExpectation,
    RecoveredOrderBinding,
    RecoveredTerminalOrder,
    StartupReconciliationEvidence,
    TerminalVenueOrderStatus,
    account_snapshot_sha256,
)
from btc_short_horizon.live.state import (
    LiveOrder,
    LiveOrderStatus,
    LiveTrade,
    LiveTradeStatus,
)
from btc_short_horizon.live.wal import JsonlWriteAheadLog


_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TOKEN_ID = re.compile(r"^[0-9]{1,78}$")


class LiveMode(StrEnum):
    SHADOW = "shadow"
    PAPER = "paper"
    CANARY = "canary"


@dataclass(frozen=True, slots=True)
class LiveExecutionConfig:
    mode: LiveMode = LiveMode.SHADOW
    risk: TradingSafetyConfig = field(default_factory=TradingSafetyConfig)
    heartbeat_timeout_seconds: float = 10.0
    canary_max_shares: float = 1.0
    max_rule_age_seconds: float = 30.0
    max_layers_per_cycle: int = 3
    allowed_maker_fee_rate_bps: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.mode, LiveMode):
            object.__setattr__(self, "mode", LiveMode(self.mode))
        for name in (
            "heartbeat_timeout_seconds",
            "canary_max_shares",
            "max_rule_age_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"{name} must be finite and > 0")
        if (
            isinstance(self.max_layers_per_cycle, bool)
            or not isinstance(self.max_layers_per_cycle, int)
            or not 1 <= self.max_layers_per_cycle <= 15
        ):
            raise ValueError("max_layers_per_cycle must be an integer in [1, 15]")
        if (
            isinstance(self.allowed_maker_fee_rate_bps, bool)
            or not isinstance(self.allowed_maker_fee_rate_bps, int)
            or self.allowed_maker_fee_rate_bps < 0
        ):
            raise ValueError("allowed_maker_fee_rate_bps must be a non-negative integer")


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
    """Owns the local lifecycle, reservation, idempotency, and recovery invariants."""

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
        self._placement_order: dict[tuple[str, int], str] = {}
        self._placement_by_client: dict[str, tuple[str, int]] = {}
        self._market_cycles: set[str] = set()
        self._prewarmed: dict[str, tuple[str, GatewayMarketPrewarm]] = {}
        self._reserved_by_client: dict[str, float] = {}
        self._progress = CanaryProgress()
        self._last_heartbeat_ts_ns: int | None = None
        self._heartbeat_cancel_requested = False
        self._heartbeat_id = ""
        self._ambiguous_markets: set[str] = set()
        self._startup_reconciled = config.mode is not LiveMode.CANARY
        self._recovery_required = False
        self._halted = False
        self._lock = RLock()
        self._wal_buffer: list[tuple[str, int, object]] | None = None

    @property
    def canary_progress(self) -> CanaryProgress:
        return self._progress

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    @property
    def startup_reconciled(self) -> bool:
        return self._startup_reconciled

    @property
    def reserved_notional(self) -> float:
        with self._lock:
            return sum(self._reserved_by_client.values())

    @property
    def startup_order_expectations(self) -> tuple[LocalOrderExpectation, ...]:
        """Durable identities an authenticated startup reconciliation must prove."""

        expected_statuses = {
            LiveOrderStatus.SUBMITTED,
            LiveOrderStatus.LIVE,
            LiveOrderStatus.UNKNOWN,
            LiveOrderStatus.CANCEL_REQUESTED,
            LiveOrderStatus.NOT_CANCELED,
        }
        with self._lock:
            return tuple(
                LocalOrderExpectation(
                    client_order_id=order.client_order_id,
                    venue_order_id=order.venue_order_id or order.expected_venue_order_id,
                    market_id=order.market_id,
                    token_id=order.token_id,
                    price=order.price,
                    original_size=order.size,
                )
                for order in sorted(self.orders.values(), key=lambda item: item.client_order_id)
                if order.status in expected_statuses
            )

    def prewarm_market(self, request: LiveOrderRequest, *, ts_ns: int) -> GatewayMarketPrewarm:
        _require_ts(ts_ns)
        with self._lock:
            if self.config.mode is LiveMode.SHADOW:
                result = GatewayMarketPrewarm(
                    request.token_id, 2, request.tick_size, request.neg_risk
                )
            else:
                assert self.gateway is not None
                result = self.gateway.prewarm_market(request)
            self._prewarmed[request.token_id] = (request.rules_sha256, result)
            self._write(
                "market_prewarmed",
                ts_ns,
                {
                    "condition_id": request.condition_id,
                    "token_id": request.token_id,
                    "rules_sha256": request.rules_sha256,
                    "tick_size": result.tick_size,
                    "neg_risk": result.neg_risk,
                    "clob_version": result.clob_version,
                },
            )
            return result

    def complete_startup_reconciliation(
        self,
        *,
        account: AccountSnapshot,
        evidence: StartupReconciliationEvidence,
        ts_ns: int,
    ) -> None:
        _require_ts(ts_ns)
        with self._lock:
            if not account.account_reconciled:
                raise ValueError("startup account snapshot is not reconciled")
            if not isinstance(evidence, StartupReconciliationEvidence):
                raise TypeError("startup reconciliation evidence is required")
            if not evidence.reconciled:
                raise ValueError("startup reconciliation evidence is not complete")
            if evidence.observed_at_ns != account.observed_at_ns:
                raise ValueError("startup evidence observation does not match account snapshot")
            if evidence.account_snapshot_sha256 != account_snapshot_sha256(account):
                raise ValueError("startup evidence does not match the account snapshot")
            age_ns = ts_ns - account.observed_at_ns
            if age_ns < -int(self.config.risk.max_future_clock_skew_seconds * 1_000_000_000):
                raise ValueError("startup account snapshot is future-dated")
            if age_ns > int(self.config.risk.max_account_snapshot_age_seconds * 1_000_000_000):
                raise ValueError("startup account snapshot is stale")
            prospective_orders = dict(self.orders)
            prospective_venue_map = dict(self._client_order_by_venue)
            recovery_events: list[tuple[str, LiveOrder]] = []

            for client_order_id, order in tuple(prospective_orders.items()):
                if order.status in {LiveOrderStatus.CREATED, LiveOrderStatus.SIGNED}:
                    rejected = order.transition(LiveOrderStatus.REJECTED)
                    prospective_orders[client_order_id] = rejected
                    recovery_events.append(("startup_pre_network_order_rejected", rejected))

            for binding in evidence.recovered_orders:
                recovered = self._apply_startup_binding(
                    prospective_orders,
                    prospective_venue_map,
                    binding,
                )
                recovery_events.append(("startup_order_recovered", recovered))

            for terminal in evidence.terminal_orders:
                recovered = self._apply_startup_terminal_order(
                    prospective_orders,
                    prospective_venue_map,
                    terminal,
                )
                recovery_events.append(("startup_terminal_order_recovered", recovered))

            for client_order_id, order in tuple(prospective_orders.items()):
                expected_venue_order_id = order.venue_order_id or order.expected_venue_order_id
                if expected_venue_order_id not in evidence.expected_open_order_ids:
                    continue
                if order.status in {LiveOrderStatus.SUBMITTED, LiveOrderStatus.UNKNOWN}:
                    recovered = order.transition(
                        LiveOrderStatus.LIVE,
                        venue_order_id=expected_venue_order_id,
                    )
                    prospective_orders[client_order_id] = recovered
                    assert recovered.venue_order_id is not None
                    existing = prospective_venue_map.get(recovered.venue_order_id)
                    if existing not in {None, recovered.client_order_id}:
                        raise ValueError("startup venue order ID is already mapped")
                    prospective_venue_map[recovered.venue_order_id] = recovered.client_order_id
                    recovery_events.append(("startup_order_recovered", recovered))
                elif order.status is LiveOrderStatus.CANCEL_REQUESTED:
                    not_canceled = order.transition(LiveOrderStatus.NOT_CANCELED)
                    prospective_orders[client_order_id] = not_canceled
                    recovery_events.append(("startup_cancel_still_open", not_canceled))

            unresolved = {
                LiveOrderStatus.SUBMITTED,
                LiveOrderStatus.UNKNOWN,
                LiveOrderStatus.CANCEL_REQUESTED,
            }
            if any(order.status in unresolved for order in prospective_orders.values()):
                raise ValueError("startup reconciliation has unresolved local orders")
            local_open_orders = tuple(
                order
                for order in prospective_orders.values()
                if order.status in {LiveOrderStatus.LIVE, LiveOrderStatus.NOT_CANCELED}
            )
            local_venue_order_ids = frozenset(
                order.venue_order_id for order in local_open_orders if order.venue_order_id
            )
            if (
                evidence.expected_open_order_ids != local_venue_order_ids
                or evidence.venue_open_order_ids != local_venue_order_ids
            ):
                raise ValueError("startup venue orders do not match local live orders")
            if account.open_orders != len(local_open_orders):
                raise ValueError("startup account open-order count does not match local state")
            local_open_market_ids = {order.market_id for order in local_open_orders}
            if not local_open_market_ids <= account.working_market_ids:
                raise ValueError("startup account working markets omit a local open order")
            local_open_notional = sum(
                order.price * max(order.size - order.matched_size, 0.0)
                for order in local_open_orders
            )
            if abs(account.open_order_notional - local_open_notional) > 1e-9:
                raise ValueError("startup account open-order notional does not match local state")

            self.orders = prospective_orders
            self._client_order_by_venue = prospective_venue_map
            self._reserved_by_client.clear()
            self._startup_reconciled = True
            self._ambiguous_markets = {
                order.market_id
                for order in self.orders.values()
                if order.status is LiveOrderStatus.NOT_CANCELED
            }
            if not self._ambiguous_markets and not self._halted:
                self._recovery_required = False
            with self._batch_wal():
                for event_type, recovered_order in recovery_events:
                    self._write_order(event_type, ts_ns, recovered_order)
                self._write(
                    "startup_reconciled",
                    ts_ns,
                    {
                        "order_count": len(self.orders),
                        "venue_open_order_count": len(local_venue_order_ids),
                        "recovered_order_count": len(evidence.recovered_orders),
                        "terminal_order_count": len(evidence.terminal_orders),
                        "ledger_sha256": evidence.ledger_sha256,
                        "account_snapshot_sha256": evidence.account_snapshot_sha256,
                    },
                )

    @staticmethod
    def _apply_startup_binding(
        orders: dict[str, LiveOrder],
        venue_map: dict[str, str],
        binding: RecoveredOrderBinding,
    ) -> LiveOrder:
        order = orders.get(binding.client_order_id)
        if order is None:
            raise ValueError("startup recovery references an unknown local order")
        if order.status not in {LiveOrderStatus.SUBMITTED, LiveOrderStatus.UNKNOWN}:
            raise ValueError("startup recovery references a non-recoverable local order")
        if order.venue_order_id is not None:
            raise ValueError("startup recovery cannot replace an existing venue order ID")
        if (
            order.expected_venue_order_id is not None
            and order.expected_venue_order_id.casefold() != binding.venue_order_id.casefold()
        ):
            raise ValueError("startup recovery does not match the signed order hash")
        if (
            order.market_id != binding.market_id
            or order.token_id != binding.token_id
            or abs(order.price - binding.price) > 1e-12
            or abs(order.size - binding.original_size) > 1e-9
        ):
            raise ValueError("startup recovery identity does not match durable local state")
        existing = venue_map.get(binding.venue_order_id)
        if existing not in {None, order.client_order_id}:
            raise ValueError("startup recovery venue order ID is already mapped")
        recovered = order.transition(
            LiveOrderStatus.LIVE,
            venue_order_id=binding.venue_order_id,
        )
        orders[order.client_order_id] = recovered
        venue_map[binding.venue_order_id] = order.client_order_id
        return recovered

    @staticmethod
    def _apply_startup_terminal_order(
        orders: dict[str, LiveOrder],
        venue_map: dict[str, str],
        terminal: RecoveredTerminalOrder,
    ) -> LiveOrder:
        order = orders.get(terminal.client_order_id)
        if order is None:
            raise ValueError("terminal startup proof references an unknown local order")
        expected_id = order.venue_order_id or order.expected_venue_order_id
        if expected_id is None or expected_id.casefold() != terminal.venue_order_id.casefold():
            raise ValueError("terminal startup proof does not match the signed order hash")
        if (
            order.market_id != terminal.market_id
            or order.token_id != terminal.token_id
            or abs(order.price - terminal.price) > 1e-12
            or abs(order.size - terminal.original_size) > 1e-9
        ):
            raise ValueError("terminal startup proof identity does not match local state")
        existing = venue_map.get(terminal.venue_order_id)
        if existing not in {None, order.client_order_id}:
            raise ValueError("terminal startup venue order ID is already mapped")
        recovered = order.expect_venue_order_id(terminal.venue_order_id)
        if recovered.venue_order_id is None:
            recovered = replace(recovered, venue_order_id=terminal.venue_order_id)
        recovered = recovered.record_order_cumulative_match(terminal.matched_size)
        if terminal.status is TerminalVenueOrderStatus.MATCHED:
            if recovered.status is not LiveOrderStatus.FILLED:
                raise ValueError("matched terminal proof did not produce a filled order")
        elif terminal.status in {
            TerminalVenueOrderStatus.CANCELED,
            TerminalVenueOrderStatus.CANCELED_MARKET_RESOLVED,
        }:
            if recovered.status is not LiveOrderStatus.FILLED:
                recovered = recovered.transition(LiveOrderStatus.CANCELED)
        elif terminal.status is TerminalVenueOrderStatus.INVALID:
            if recovered.status not in {LiveOrderStatus.SUBMITTED, LiveOrderStatus.UNKNOWN}:
                raise ValueError("invalid terminal proof conflicts with a previously live order")
            recovered = recovered.transition(LiveOrderStatus.REJECTED)
        else:  # pragma: no cover - exhaustive enum defense
            raise ValueError("unsupported terminal startup order status")
        orders[order.client_order_id] = recovered
        venue_map[terminal.venue_order_id] = order.client_order_id
        return recovered

    def submit(
        self,
        *,
        request: LiveOrderRequest,
        account: AccountSnapshot,
        ts_ns: int,
    ) -> SubmitResult:
        return self.submit_many(requests=(request,), account=account, ts_ns=ts_ns)[0]

    def submit_many(
        self,
        *,
        requests: Sequence[LiveOrderRequest],
        account: AccountSnapshot,
        ts_ns: int,
    ) -> tuple[SubmitResult, ...]:
        _require_ts(ts_ns)
        items = tuple(requests)
        self._validate_batch(items)
        with self._lock:
            keys = tuple((request.placement_id, request.layer_index) for request in items)
            existing = tuple(self._placement_order.get(key) for key in keys)
            if any(client_order_id is not None for client_order_id in existing):
                if not all(client_order_id is not None for client_order_id in existing):
                    raise ValueError("batch contains a partial duplicate placement")
                return tuple(
                    SubmitResult(
                        False,
                        client_order_id,
                        "duplicate_placement",
                        self.orders[client_order_id].venue_order_id,
                    )
                    for client_order_id in existing
                    if client_order_id is not None
                )
            return self._submit_new_orders(items, account=account, ts_ns=ts_ns)

    def request_cancel(self, *, client_order_id: str, ts_ns: int) -> None:
        _require_ts(ts_ns)
        with self._lock:
            order = self.orders[client_order_id]
            if self.config.mode is LiveMode.SHADOW:
                self._write_order("shadow_cancel", ts_ns, order)
                return
            if order.status not in {LiveOrderStatus.LIVE, LiveOrderStatus.NOT_CANCELED}:
                raise ValueError("only live or not-canceled orders can be cancelled")
            if order.venue_order_id is None:
                raise ValueError("live order has no venue_order_id")
            assert self.gateway is not None
            requested = order.transition(LiveOrderStatus.CANCEL_REQUESTED)
            self.orders[client_order_id] = requested
            self._write_order("cancel_requested", ts_ns, requested)
            try:
                receipt = self.gateway.cancel_order(order.venue_order_id)
            except GatewayCancellationUnknownError as exc:
                self._record_cancel_unknown(
                    requested,
                    ts_ns=ts_ns,
                    event_type="cancel_unknown",
                    error_type=type(exc).__name__,
                )
                return
            except Exception as exc:
                self._record_cancel_unknown(
                    requested,
                    ts_ns=ts_ns,
                    event_type="cancel_unexpected_error",
                    error_type=type(exc).__name__,
                )
                self.emergency_stop(ts_ns=ts_ns, reason="unexpected_gateway_cancel_error")
                return
            if order.venue_order_id in receipt.canceled:
                canceled = requested.transition(LiveOrderStatus.CANCELED)
                self.orders[client_order_id] = canceled
                self._sync_reservation(canceled)
                self._write_order("cancel_ack", ts_ns, canceled)
            else:
                not_canceled = requested.transition(LiveOrderStatus.NOT_CANCELED)
                self.orders[client_order_id] = not_canceled
                self._mark_ambiguous(not_canceled.market_id)
                self._write_order(
                    "cancel_not_applied",
                    ts_ns,
                    not_canceled,
                    reason=receipt.not_canceled.get(order.venue_order_id, "unknown"),
                )

    def cancel_all(self, *, ts_ns: int, reason: str) -> None:
        _require_ts(ts_ns)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancel reason is required")
        with self._lock:
            if self.config.mode is LiveMode.SHADOW:
                self._write("shadow_cancel_all", ts_ns, {"reason": reason})
                return
            assert self.gateway is not None
            targeted = {
                (order.venue_order_id or order.expected_venue_order_id): order.client_order_id
                for order in self.orders.values()
                if (order.venue_order_id or order.expected_venue_order_id)
                and order.status
                in {
                    LiveOrderStatus.LIVE,
                    LiveOrderStatus.CANCEL_REQUESTED,
                    LiveOrderStatus.NOT_CANCELED,
                    LiveOrderStatus.UNKNOWN,
                }
            }
            self._write(
                "cancel_all_requested",
                ts_ns,
                {"reason": reason, "target_count": len(targeted)},
            )
            try:
                receipt = self.gateway.cancel_all()
            except Exception as exc:
                for client_order_id in targeted.values():
                    self._record_cancel_unknown(
                        self.orders[client_order_id],
                        ts_ns=ts_ns,
                        event_type="cancel_all_unknown",
                        error_type=type(exc).__name__,
                    )
                self._halt_without_cancel(ts_ns=ts_ns, reason="cancel_all_gateway_error")
                raise

            canceled_ids = set(receipt.canceled)
            not_canceled_ids = set(receipt.not_canceled)
            receipt_ids = canceled_ids | not_canceled_ids
            unexpected_ids = receipt_ids - set(targeted)
            if unexpected_ids:
                self._recovery_required = True
                self._write(
                    "cancel_all_unmapped_receipt",
                    ts_ns,
                    {"order_count": len(unexpected_ids)},
                )

            for venue_order_id, client_order_id in targeted.items():
                order = self.orders[client_order_id]
                if venue_order_id in canceled_ids:
                    canceled = order.transition(LiveOrderStatus.CANCELED)
                    self.orders[client_order_id] = canceled
                    self._sync_reservation(canceled)
                    self._write_order("cancel_all_ack", ts_ns, canceled)
                elif venue_order_id in not_canceled_ids:
                    not_canceled = (
                        order
                        if order.status is LiveOrderStatus.NOT_CANCELED
                        else order.transition(LiveOrderStatus.NOT_CANCELED)
                        if order.status is not LiveOrderStatus.UNKNOWN
                        else order
                    )
                    self.orders[client_order_id] = not_canceled
                    self._mark_ambiguous(not_canceled.market_id)
                    self._write_order(
                        "cancel_all_not_applied",
                        ts_ns,
                        not_canceled,
                        reason=receipt.not_canceled[venue_order_id],
                    )
                else:
                    self._record_cancel_unknown(
                        order,
                        ts_ns=ts_ns,
                        event_type="cancel_all_omitted",
                        error_type="IncompleteCancelReceipt",
                    )

    def emergency_stop(self, *, ts_ns: int, reason: str) -> bool:
        _require_ts(ts_ns)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("kill-switch reason is required")
        with self._lock:
            if not self._halt_without_cancel(ts_ns=ts_ns, reason=reason):
                return False
            try:
                self.cancel_all(ts_ns=ts_ns, reason=reason)
            except Exception as exc:
                self._write(
                    "kill_switch_cancel_error",
                    ts_ns,
                    {"error_type": type(exc).__name__},
                )
            return True

    def record_heartbeat(self, *, ts_ns: int) -> None:
        _require_ts(ts_ns)
        with self._lock:
            self._last_heartbeat_ts_ns = ts_ns
            self._heartbeat_cancel_requested = False

    def send_venue_heartbeat(self, *, ts_ns: int) -> str:
        _require_ts(ts_ns)
        with self._lock:
            if self.config.mode is LiveMode.SHADOW:
                self.record_heartbeat(ts_ns=ts_ns)
                return ""
            assert self.gateway is not None
            try:
                next_id = self.gateway.send_heartbeat(self._heartbeat_id)
            except GatewayHeartbeatError:
                self.emergency_stop(ts_ns=ts_ns, reason="venue_heartbeat_failed")
                raise
            self._heartbeat_id = next_id
            self.record_heartbeat(ts_ns=ts_ns)
            return next_id

    def enforce_heartbeat_timeout(self, *, now_ts_ns: int) -> bool:
        _require_ts(now_ts_ns, name="now_ts_ns")
        with self._lock:
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

    def reconcile_user_event(self, event: Mapping[str, object], *, ts_ns: int) -> bool:
        _require_ts(ts_ns)
        with self._lock:
            try:
                event_type = str(event.get("event_type") or "").casefold()
                if event_type == "order":
                    return self._reconcile_order(event, ts_ns=ts_ns)
                if event_type == "trade":
                    return self._reconcile_trade(event, ts_ns=ts_ns)
                raise ValueError("unsupported user event type")
            except ValueError as exc:
                self._write(
                    "user_event_invalid",
                    ts_ns,
                    {
                        "event_kind": str(event.get("event_type") or "unknown")[:32],
                        "error_type": type(exc).__name__,
                    },
                )
                self.emergency_stop(ts_ns=ts_ns, reason="invalid_user_channel_event")
                return False

    def restore_from_wal(self) -> int:
        with self._lock:
            if self.orders or self.trades:
                raise ValueError("restore_from_wal requires an empty service state")
            restored = 0
            for record in self.wal.read():
                event_type = str(record["event_type"])
                payload = record["payload"]
                if not isinstance(payload, Mapping):
                    continue
                raw_order = payload.get("order")
                if isinstance(raw_order, Mapping):
                    order = _live_order_from_json(raw_order)
                    self.orders[order.client_order_id] = order
                    if order.venue_order_id:
                        existing = self._client_order_by_venue.get(order.venue_order_id)
                        if existing not in {None, order.client_order_id}:
                            raise ValueError("WAL maps one venue order to multiple local orders")
                        self._client_order_by_venue[order.venue_order_id] = order.client_order_id
                    placement_id = payload.get("placement_id")
                    layer_index = payload.get("layer_index")
                    if isinstance(placement_id, str) and isinstance(layer_index, int):
                        key = (placement_id, layer_index)
                        existing = self._placement_order.get(key)
                        if existing not in {None, order.client_order_id}:
                            raise ValueError("WAL maps one placement layer to multiple orders")
                        self._placement_order[key] = order.client_order_id
                        self._placement_by_client[order.client_order_id] = key
                    if order.status in {LiveOrderStatus.UNKNOWN, LiveOrderStatus.NOT_CANCELED}:
                        self._ambiguous_markets.add(order.market_id)
                    if event_type in {
                        "order_submit_started",
                        "order_live",
                        "order_venue_rejected",
                        "order_submission_unknown",
                        "startup_order_recovered",
                        "startup_terminal_order_recovered",
                    }:
                        self._market_cycles.add(order.market_id)
                    restored += 1
                raw_trade = payload.get("trade")
                if isinstance(raw_trade, Mapping):
                    trade = _live_trade_from_json(raw_trade)
                    self.trades[(trade.trade_id, trade.client_order_id)] = trade
                if event_type == "kill_switch_activated":
                    self._halted = True
                    self._recovery_required = True
            for order in self.orders.values():
                self._sync_reservation(order)
            self._progress = CanaryProgress(
                submitted_orders=sum(
                    order.venue_order_id is not None for order in self.orders.values()
                ),
                fills=sum(
                    trade.status is LiveTradeStatus.CONFIRMED for trade in self.trades.values()
                ),
            )
            self._startup_reconciled = self.config.mode is not LiveMode.CANARY
            self._recovery_required = self._recovery_required or bool(self._ambiguous_markets)
            self._heartbeat_id = ""
            self._last_heartbeat_ts_ns = None
            return restored

    def _submit_new_orders(
        self,
        items: tuple[LiveOrderRequest, ...],
        *,
        account: AccountSnapshot,
        ts_ns: int,
    ) -> tuple[SubmitResult, ...]:
        with self._batch_wal():
            orders = tuple(self._create_order(request, ts_ns=ts_ns) for request in items)
            reason = self._admission_reason(items, account=account, ts_ns=ts_ns)
            if reason is not None:
                return self._block_orders(orders, ts_ns=ts_ns, reason=reason)
            if self.config.mode is LiveMode.SHADOW:
                for order in orders:
                    self._write_order("shadow_order", ts_ns, order)
                return tuple(
                    SubmitResult(False, order.client_order_id, "shadow_mode") for order in orders
                )

            risk_config = self.config.risk
            if self.config.mode is LiveMode.PAPER:
                risk_config = replace(
                    risk_config,
                    trading_enabled=True,
                    require_healthy_feeds=False,
                    require_market_channel=False,
                    require_user_channel=False,
                    require_heartbeat=False,
                    require_healthy_clock=False,
                    require_geo_eligible=False,
                    require_account_reconciled=False,
                )
            provisional = self._risk_reservations()
            for request in items:
                decision = evaluate_order_risk(
                    config=risk_config,
                    account=account,
                    reservations=provisional,
                    order_notional=float(request.notional),
                    market_id=request.condition_id,
                    now_ts_ns=ts_ns,
                )
                if not decision.allowed:
                    return self._block_orders(orders, ts_ns=ts_ns, reason=decision.reason)
                provisional = RiskReservations(
                    notional=provisional.notional + float(request.notional),
                    order_count=provisional.order_count + 1,
                    market_ids=provisional.market_ids | {request.condition_id},
                )

            assert self.gateway is not None
            prepared: list[PreparedPostOnlyOrder] = []
            try:
                for order, request in zip(orders, items, strict=True):
                    signed = order.transition(LiveOrderStatus.SIGNED)
                    self.orders[order.client_order_id] = signed
                    prepared_order = self.gateway.prepare_post_only_buy(request)
                    signed = signed.expect_venue_order_id(prepared_order.expected_venue_order_id)
                    self.orders[order.client_order_id] = signed
                    prepared.append(prepared_order)
                    self._write_order("order_signed", ts_ns, signed)
            except Exception as exc:
                return self._reject_before_network(
                    orders,
                    ts_ns=ts_ns,
                    reason="order_prepare_failed",
                    error_type=type(exc).__name__,
                )

            for order, request in zip(orders, items, strict=True):
                signed = self.orders[order.client_order_id]
                submitted = signed.transition(LiveOrderStatus.SUBMITTED)
                self.orders[order.client_order_id] = submitted
                self._reserved_by_client[order.client_order_id] = float(request.notional)
                self._write_order("order_submit_started", ts_ns, submitted)
            self._market_cycles.add(items[0].condition_id)

        try:
            responses = (
                (self.gateway.submit_prepared_post_only_buy(prepared[0]),)
                if len(prepared) == 1
                else self.gateway.submit_prepared_post_only_buys(tuple(prepared))
            )
        except GatewaySubmissionUnknownError as exc:
            with self._batch_wal():
                results = self._submission_unknown(
                    orders,
                    ts_ns=ts_ns,
                    error_type=type(exc).__name__,
                )
            self.emergency_stop(ts_ns=ts_ns, reason="gateway_submission_unknown")
            return results
        except Exception as exc:
            with self._batch_wal():
                results = self._submission_unknown(
                    orders,
                    ts_ns=ts_ns,
                    error_type=type(exc).__name__,
                )
            self.emergency_stop(ts_ns=ts_ns, reason="unexpected_gateway_submission_error")
            return results
        if len(responses) != len(orders):
            with self._batch_wal():
                return self._submission_unknown(
                    orders,
                    ts_ns=ts_ns,
                    error_type="GatewayResponseCountMismatch",
                )
        if not self._submission_response_ids_are_valid(orders, responses):
            with self._batch_wal():
                results = self._submission_unknown(
                    orders,
                    ts_ns=ts_ns,
                    error_type="GatewayResponseIdentityMismatch",
                )
            self.emergency_stop(ts_ns=ts_ns, reason="invalid_gateway_submission_response")
            return results
        with self._batch_wal():
            return tuple(
                self._apply_submission_response(order, response, ts_ns=ts_ns)
                for order, response in zip(orders, responses, strict=True)
            )

    def _submission_response_ids_are_valid(
        self,
        orders: Sequence[LiveOrder],
        responses: Sequence[GatewayOrderResponse],
    ) -> bool:
        venue_order_ids = tuple(
            response.venue_order_id for response in responses if response.venue_order_id is not None
        )
        if len(set(venue_order_ids)) != len(venue_order_ids) or not all(
            venue_order_id not in self._client_order_by_venue for venue_order_id in venue_order_ids
        ):
            return False
        return all(
            not response.accepted
            or (
                response.venue_order_id is not None
                and self.orders[order.client_order_id].expected_venue_order_id is not None
                and response.venue_order_id.casefold()
                == self.orders[order.client_order_id].expected_venue_order_id.casefold()
            )
            for order, response in zip(orders, responses, strict=True)
        )

    def _validate_batch(self, items: tuple[LiveOrderRequest, ...]) -> None:
        if not 1 <= len(items) <= self.config.max_layers_per_cycle:
            raise ValueError("placement batch has an invalid layer count")
        if len({item.layer_index for item in items}) != len(items):
            raise ValueError("placement batch has duplicate layer indexes")
        if len({item.placement_id for item in items}) != 1:
            raise ValueError("placement batch must share one placement_id")
        if len({item.condition_id for item in items}) != 1:
            raise ValueError("placement batch must share one market")
        if len({item.token_id for item in items}) != 1:
            raise ValueError("placement batch must share one outcome token")
        if len({str(item.price) for item in items}) != len(items):
            raise ValueError("placement batch layers must use unique prices")

    def _create_order(self, request: LiveOrderRequest, *, ts_ns: int) -> LiveOrder:
        client_order_id = _client_order_id(request)
        order = LiveOrder(
            client_order_id=client_order_id,
            market_id=request.condition_id,
            token_id=request.token_id,
            price=request.price,
            size=request.size,
        )
        key = (request.placement_id, request.layer_index)
        self.orders[client_order_id] = order
        self._placement_order[key] = client_order_id
        self._placement_by_client[client_order_id] = key
        self._write_order("order_decision", ts_ns, order, mode=self.config.mode)
        return order

    def _admission_reason(
        self,
        items: tuple[LiveOrderRequest, ...],
        *,
        account: AccountSnapshot,
        ts_ns: int,
    ) -> str | None:
        if self._halted:
            return "kill_switch_active"
        if not self._startup_reconciled:
            return "startup_reconciliation_required"
        if self._recovery_required:
            return "recovery_required"
        market_id = items[0].condition_id
        if market_id in self._ambiguous_markets:
            return "ambiguous_submission_pending"
        if market_id in self._market_cycles:
            return "market_cycle_already_used"
        if (
            self.config.mode is LiveMode.CANARY
            and sum(request.size for request in items) > self.config.canary_max_shares + 1e-12
        ):
            return "canary_cycle_size_limit"
        for request in items:
            age_ns = ts_ns - request.rules_observed_at_ns
            if age_ns < -int(self.config.risk.max_future_clock_skew_seconds * 1_000_000_000):
                return "market_rules_in_future"
            if age_ns > int(self.config.max_rule_age_seconds * 1_000_000_000):
                return "market_rules_stale"
            if request.maker_fee_rate_bps != self.config.allowed_maker_fee_rate_bps:
                return "maker_fee_changed"
            if self.config.mode is LiveMode.SHADOW:
                continue
            prewarmed = self._prewarmed.get(request.token_id)
            if prewarmed is None or prewarmed[0] != request.rules_sha256:
                return "market_not_prewarmed"
            _, rules = prewarmed
            if rules.tick_size != request.tick_size or rules.neg_risk is not request.neg_risk:
                return "market_prewarm_mismatch"
        return None

    def _risk_reservations(self) -> RiskReservations:
        market_ids = frozenset(
            self.orders[client_order_id].market_id for client_order_id in self._reserved_by_client
        )
        return RiskReservations(
            notional=self.reserved_notional,
            order_count=len(self._reserved_by_client),
            market_ids=market_ids,
        )

    def _block_orders(
        self, orders: Sequence[LiveOrder], *, ts_ns: int, reason: str
    ) -> tuple[SubmitResult, ...]:
        results: list[SubmitResult] = []
        for order in orders:
            current = self.orders[order.client_order_id]
            rejected = current.transition(LiveOrderStatus.REJECTED)
            self.orders[order.client_order_id] = rejected
            self._write_order("order_blocked", ts_ns, rejected, reason=reason)
            results.append(SubmitResult(False, order.client_order_id, reason))
        return tuple(results)

    def _reject_before_network(
        self,
        orders: Sequence[LiveOrder],
        *,
        ts_ns: int,
        reason: str,
        error_type: str,
    ) -> tuple[SubmitResult, ...]:
        results: list[SubmitResult] = []
        for order in orders:
            current = self.orders[order.client_order_id]
            if current.status in {LiveOrderStatus.CREATED, LiveOrderStatus.SIGNED}:
                rejected = current.transition(LiveOrderStatus.REJECTED)
                self.orders[order.client_order_id] = rejected
                self._write_order(
                    "order_prepare_rejected",
                    ts_ns,
                    rejected,
                    reason=reason,
                    error_type=error_type,
                )
            results.append(SubmitResult(False, order.client_order_id, reason))
        return tuple(results)

    def _submission_unknown(
        self, orders: Sequence[LiveOrder], *, ts_ns: int, error_type: str
    ) -> tuple[SubmitResult, ...]:
        results: list[SubmitResult] = []
        for order in orders:
            current = self.orders[order.client_order_id]
            unknown = current.transition(LiveOrderStatus.UNKNOWN)
            self.orders[order.client_order_id] = unknown
            self._mark_ambiguous(order.market_id)
            self._write_order(
                "order_submission_unknown",
                ts_ns,
                unknown,
                error_type=error_type,
            )
            results.append(SubmitResult(False, order.client_order_id, "gateway_submission_unknown"))
        return tuple(results)

    def _apply_submission_response(
        self,
        original: LiveOrder,
        response: GatewayOrderResponse,
        *,
        ts_ns: int,
    ) -> SubmitResult:
        current = self.orders[original.client_order_id]
        if response.accepted:
            assert response.venue_order_id is not None
            live = current.transition(
                LiveOrderStatus.LIVE,
                venue_order_id=response.venue_order_id,
            )
            self.orders[original.client_order_id] = live
            self._client_order_by_venue[response.venue_order_id] = original.client_order_id
            self._progress = replace(
                self._progress,
                submitted_orders=self._progress.submitted_orders + 1,
            )
            self._write_order("order_live", ts_ns, live, response=response)
            return SubmitResult(
                True,
                original.client_order_id,
                "submitted",
                response.venue_order_id,
            )
        rejected = current.transition(LiveOrderStatus.REJECTED)
        self.orders[original.client_order_id] = rejected
        self._sync_reservation(rejected)
        reason = response.rejection_reason or "venue_rejected"
        self._write_order("order_venue_rejected", ts_ns, rejected, reason=reason)
        return SubmitResult(False, original.client_order_id, reason)

    def _reconcile_order(self, event: Mapping[str, object], *, ts_ns: int) -> bool:
        venue_order_id = _required_event_text(event.get("id"), "order.id")
        market_id, token_id, price, original_size = _order_event_identity(event)
        client_order_id = self._client_order_by_venue.get(venue_order_id)
        if client_order_id is None:
            candidates = tuple(
                order
                for order in self.orders.values()
                if order.venue_order_id is None
                and order.status in {LiveOrderStatus.SUBMITTED, LiveOrderStatus.UNKNOWN}
                and order.market_id == market_id
                and order.token_id == token_id
                and abs(order.price - price) <= 1e-12
                and abs(order.size - original_size) <= 1e-12
            )
            if len(candidates) != 1:
                raise ValueError("user order event is not uniquely mapped to a local order")
            before = candidates[0]
            if (
                before.expected_venue_order_id is not None
                and before.expected_venue_order_id.casefold() != venue_order_id.casefold()
            ):
                raise ValueError("user order ID does not match the signed order hash")
            order = before.transition(LiveOrderStatus.LIVE, venue_order_id=venue_order_id)
            client_order_id = order.client_order_id
            self.orders[client_order_id] = order
            self._client_order_by_venue[venue_order_id] = client_order_id
        else:
            order = self.orders[client_order_id]
            before = order
            if (
                order.market_id != market_id
                or order.token_id != token_id
                or abs(order.price - price) > 1e-12
                or abs(order.size - original_size) > 1e-12
            ):
                raise ValueError("order event identity does not match the local order")
        kind = _required_event_text(event.get("type"), "order.type").upper()
        if kind not in {"PLACEMENT", "UPDATE", "CANCELLATION"}:
            raise ValueError("unsupported user order event")
        cumulative = _nonnegative_float(event.get("size_matched", 0), "size_matched")
        if cumulative < order.order_channel_matched_size - 1e-12:
            self._write_order("user_order_stale", ts_ns, order, venue_event_type=kind)
            return False
        if kind in {"PLACEMENT", "UPDATE"} and order.status in {
            LiveOrderStatus.SUBMITTED,
            LiveOrderStatus.UNKNOWN,
        }:
            order = order.transition(LiveOrderStatus.LIVE)
        order = order.record_order_cumulative_match(cumulative)
        if kind == "CANCELLATION" and order.status is not LiveOrderStatus.FILLED:
            if order.status in {
                LiveOrderStatus.LIVE,
                LiveOrderStatus.CANCEL_REQUESTED,
                LiveOrderStatus.NOT_CANCELED,
                LiveOrderStatus.UNKNOWN,
            }:
                order = order.transition(LiveOrderStatus.CANCELED)
        self.orders[client_order_id] = order
        self._sync_reservation(order)
        self._clear_market_ambiguity_if_resolved(order.market_id)
        if order == before:
            return False
        self._write_order(
            "user_order",
            ts_ns,
            order,
            venue_event_type=kind,
            cumulative_matched_size=cumulative,
        )
        return True

    def _reconcile_trade(self, event: Mapping[str, object], *, ts_ns: int) -> bool:
        trade_id = _required_event_text(event.get("id"), "trade.id")
        if _required_event_text(event.get("type"), "trade.type").upper() != "TRADE":
            raise ValueError("unsupported user trade event type")
        status_text = _required_event_text(event.get("status"), "trade.status").casefold()
        try:
            desired_status = LiveTradeStatus(status_text)
        except ValueError as exc:
            raise ValueError("unsupported user trade status") from exc
        event_market = _event_condition_id(event.get("market"), "trade.market")
        event_asset_id = _event_token_id(event.get("asset_id"), "trade.asset_id")
        if _required_event_text(event.get("side"), "trade.side").upper() not in {"BUY", "SELL"}:
            raise ValueError("trade side must be BUY or SELL")
        _probability_float(event.get("price"), "trade.price")
        _positive_float(event.get("size"), "trade.size")
        match_time_ns = _event_unix_seconds_ns(event.get("match_time"), "trade.match_time")
        last_update_ns = _event_unix_seconds_ns(event.get("last_update"), "trade.last_update")
        message_timestamp_ns = _event_unix_seconds_ns(event.get("timestamp"), "trade.timestamp")
        if last_update_ns < match_time_ns or message_timestamp_ns < match_time_ns:
            raise ValueError("trade update timestamp precedes its match time")
        future_tolerance_ns = int(self.config.risk.max_future_clock_skew_seconds * 1_000_000_000)
        if max(last_update_ns, message_timestamp_ns) > ts_ns + future_tolerance_ns:
            raise ValueError("trade event is future-dated")

        recognized: list[tuple[str, LiveOrder, float, float]] = []
        seen_venue_order_ids: set[str] = set()
        for maker in _mapping_sequence(event.get("maker_orders")):
            venue_order_id = _required_event_text(maker.get("order_id"), "maker.order_id")
            if venue_order_id in seen_venue_order_ids:
                raise ValueError("trade event contains a duplicate maker order")
            seen_venue_order_ids.add(venue_order_id)
            client_order_id = self._client_order_by_venue.get(venue_order_id)
            if client_order_id is None:
                continue
            order = self.orders[client_order_id]
            if event_market != order.market_id:
                raise ValueError("trade market does not match the local order")
            if event_asset_id != order.token_id:
                raise ValueError("trade asset does not match the local order")
            maker_asset_id = _event_token_id(maker.get("asset_id"), "maker.asset_id")
            if maker_asset_id != order.token_id:
                raise ValueError("maker asset does not match the local order")
            matched = _positive_float(maker.get("matched_amount"), "matched_amount")
            price = _probability_float(maker.get("price"), "price")
            if abs(price - order.price) > 1e-12:
                raise ValueError("maker price does not match the local order")
            recognized.append((client_order_id, order, matched, price))
        if not recognized:
            raise ValueError("user trade event is not mapped to a local order")

        applied = False
        transaction_hash = _optional_text(event.get("transaction_hash"))
        for client_order_id, order, matched, price in recognized:
            maker_applied = False
            key = (trade_id, client_order_id)
            trade = self.trades.get(key)
            if trade is None:
                trade = LiveTrade(
                    trade_id=trade_id,
                    client_order_id=client_order_id,
                    price=price,
                    size=matched,
                    status=LiveTradeStatus.MATCHED,
                    match_time_ns=match_time_ns,
                    last_update_ns=last_update_ns,
                )
                self.trades[key] = trade
                cumulative = sum(
                    item.size
                    for (item_trade_id, item_client_id), item in self.trades.items()
                    if item_client_id == client_order_id and item_trade_id
                )
                order = order.record_trade_cumulative_match(cumulative)
                self.orders[client_order_id] = order
                maker_applied = True
            elif abs(trade.size - matched) > 1e-12 or abs(trade.price - price) > 1e-12:
                raise ValueError("duplicate trade changed price or matched amount")
            elif trade.match_time_ns != match_time_ns:
                raise ValueError("duplicate trade changed match time")
            elif trade.last_update_ns is not None and last_update_ns < trade.last_update_ns:
                continue
            previous_status = trade.status
            if desired_status is not trade.status:
                if _trade_status_is_stale(trade.status, desired_status):
                    continue
            updated_trade = trade.transition(
                desired_status,
                transaction_hash=transaction_hash,
                last_update_ns=last_update_ns,
            )
            if updated_trade != trade:
                trade = updated_trade
                self.trades[key] = trade
                maker_applied = True
            if (
                previous_status is not LiveTradeStatus.CONFIRMED
                and trade.status is LiveTradeStatus.CONFIRMED
            ):
                self._progress = replace(self._progress, fills=self._progress.fills + 1)
            self._sync_reservation(self.orders[client_order_id])
            if maker_applied:
                self._write_order(
                    "user_trade",
                    ts_ns,
                    self.orders[client_order_id],
                    trade=trade,
                )
                applied = True
        if desired_status is LiveTradeStatus.FAILED and applied:
            for _, order, _, _ in recognized:
                self._mark_ambiguous(order.market_id)
            self.emergency_stop(ts_ns=ts_ns, reason="venue_trade_failed")
        return applied

    def _record_cancel_unknown(
        self,
        order: LiveOrder,
        *,
        ts_ns: int,
        event_type: str,
        error_type: str,
    ) -> None:
        unknown = (
            order
            if order.status is LiveOrderStatus.UNKNOWN
            else order.transition(LiveOrderStatus.UNKNOWN)
        )
        self.orders[order.client_order_id] = unknown
        self._mark_ambiguous(unknown.market_id)
        self._write_order(event_type, ts_ns, unknown, error_type=error_type)

    def _halt_without_cancel(self, *, ts_ns: int, reason: str) -> bool:
        if self._halted:
            return False
        self._halted = True
        self._recovery_required = True
        self._write("kill_switch_activated", ts_ns, {"reason": reason})
        return True

    def _sync_reservation(self, order: LiveOrder) -> None:
        if order.status is LiveOrderStatus.REJECTED:
            self._reserved_by_client.pop(order.client_order_id, None)
            return
        if order.status is LiveOrderStatus.CANCELED and order.matched_size <= 1e-12:
            self._reserved_by_client.pop(order.client_order_id, None)
            return
        if order.status in {
            LiveOrderStatus.CANCELED,
            LiveOrderStatus.FILLED,
        }:
            self._reserved_by_client[order.client_order_id] = order.price * order.matched_size

    def _mark_ambiguous(self, market_id: str) -> None:
        self._ambiguous_markets.add(market_id)
        self._recovery_required = True

    def _clear_market_ambiguity_if_resolved(self, market_id: str) -> None:
        if not any(
            order.market_id == market_id
            and order.status in {LiveOrderStatus.UNKNOWN, LiveOrderStatus.NOT_CANCELED}
            for order in self.orders.values()
        ):
            self._ambiguous_markets.discard(market_id)

    def _write_order(
        self,
        event_type: str,
        ts_ns: int,
        order: LiveOrder,
        **extra: object,
    ) -> None:
        placement_id, layer_index = self._placement_by_client.get(
            order.client_order_id, ("unknown", -1)
        )
        self._write(
            event_type,
            ts_ns,
            {
                "order": order,
                "placement_id": placement_id,
                "layer_index": layer_index,
                **extra,
            },
        )

    @contextmanager
    def _batch_wal(self) -> Iterator[None]:
        if self._wal_buffer is not None:
            raise RuntimeError("nested WAL batches are not supported")
        self._wal_buffer = []
        try:
            yield
        finally:
            pending = self._wal_buffer
            self._wal_buffer = None
            if pending:
                self.wal.append_many(tuple(pending))

    def _write(self, event_type: str, ts_ns: int, payload: Mapping[str, object]) -> None:
        if self._wal_buffer is not None:
            self._wal_buffer.append((event_type, ts_ns, payload))
        else:
            self.wal.append(event_type=event_type, ts_ns=ts_ns, payload=payload)


def _client_order_id(request: LiveOrderRequest) -> str:
    body = {
        "condition_id": request.condition_id,
        "token_id": request.token_id,
        "price": str(request.price),
        "size": str(request.size),
        "tick_size": request.tick_size,
        "neg_risk": request.neg_risk,
        "placement_id": request.placement_id,
        "layer_index": request.layer_index,
        "rules_sha256": request.rules_sha256,
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()[:32]


def _trade_status_is_stale(current: LiveTradeStatus, desired: LiveTradeStatus) -> bool:
    if current in {LiveTradeStatus.CONFIRMED, LiveTradeStatus.FAILED}:
        return desired not in {LiveTradeStatus.CONFIRMED, LiveTradeStatus.FAILED}
    return desired is LiveTradeStatus.MATCHED and current in {
        LiveTradeStatus.MINED,
        LiveTradeStatus.RETRYING,
    }


def _order_event_identity(
    event: Mapping[str, object],
) -> tuple[str, str, float, float]:
    market_id = _event_condition_id(event.get("market"), "order.market")
    token_id = _event_token_id(event.get("asset_id"), "order.asset_id")
    if _required_event_text(event.get("side"), "order.side").upper() != "BUY":
        raise ValueError("order event side does not match the local buy order")
    price = _probability_float(event.get("price"), "order.price")
    original_size = _positive_float(event.get("original_size"), "order.original_size")
    return market_id, token_id, price, original_size


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list | tuple):
        raise ValueError("maker_orders must be an array")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError("maker_orders must contain only objects")
    return tuple(value)


def _nonnegative_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
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


def _probability_float(value: object, name: str) -> float:
    result = _positive_float(value, name)
    if result >= 1.0:
        raise ValueError(f"{name} must be in (0, 1)")
    return result


def _event_unix_seconds_ns(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, str) or not value.isdigit():
        raise ValueError(f"{name} must be non-negative Unix seconds")
    return int(value) * 1_000_000_000


def _required_event_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _event_condition_id(value: object, name: str) -> str:
    text = _required_event_text(value, name)
    if not _CONDITION_ID.fullmatch(text):
        raise ValueError(f"{name} must be a condition ID")
    return text.casefold()


def _event_token_id(value: object, name: str) -> str:
    text = _required_event_text(value, name)
    if not _TOKEN_ID.fullmatch(text) or not 0 < int(text) < 2**256:
        raise ValueError(f"{name} must be a positive uint256 token ID")
    return str(int(text))


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _require_ts(value: object, *, name: str = "ts_ns") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


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
        expected_venue_order_id=(
            None
            if value.get("expected_venue_order_id") is None
            else str(value["expected_venue_order_id"])
        ),
        order_channel_matched_size=float(value.get("order_channel_matched_size", 0.0)),
        trade_matched_size=float(value.get("trade_matched_size", 0.0)),
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
        match_time_ns=(None if value.get("match_time_ns") is None else int(value["match_time_ns"])),
        last_update_ns=(
            None if value.get("last_update_ns") is None else int(value["last_update_ns"])
        ),
    )
