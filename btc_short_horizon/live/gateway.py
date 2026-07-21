"""Version-pinned, fail-closed adapter over the official CLOB V2 SDK."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
import re
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any, Protocol

import httpx


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLACEMENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TOKEN_ID = re.compile(r"^[0-9]{1,78}$")
_ORDER_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")


@dataclass(frozen=True, slots=True)
class LiveOrderRequest:
    """One immutable maker layer whose exchange rules were already observed."""

    condition_id: str
    token_id: str
    price: float
    size: float
    tick_size: str
    neg_risk: bool
    minimum_order_size: float
    placement_id: str
    layer_index: int
    rules_observed_at_ns: int
    rules_sha256: str
    maker_fee_rate_bps: int = 0

    def __post_init__(self) -> None:
        condition_id = _required_text(self.condition_id, "condition_id")
        token_id = _required_text(self.token_id, "token_id")
        if not _CONDITION_ID.fullmatch(condition_id):
            raise ValueError("condition_id must be a 0x-prefixed 32-byte condition ID")
        if not _TOKEN_ID.fullmatch(token_id) or not 0 < int(token_id) < 2**256:
            raise ValueError("token_id must be a positive uint256 decimal string")
        placement_id = _required_text(self.placement_id, "placement_id")
        if not _PLACEMENT_ID.fullmatch(placement_id):
            raise ValueError("placement_id has an invalid format")
        if isinstance(self.price, bool) or not isinstance(self.price, int | float):
            raise ValueError("price must be a finite number")
        if isinstance(self.size, bool) or not isinstance(self.size, int | float):
            raise ValueError("size must be a finite number")
        if isinstance(self.minimum_order_size, bool) or not isinstance(
            self.minimum_order_size, int | float
        ):
            raise ValueError("minimum_order_size must be a finite number")
        if not all(isfinite(value) for value in (self.price, self.size, self.minimum_order_size)):
            raise ValueError("price, size, and minimum_order_size must be finite")
        try:
            price = Decimal(str(self.price))
            size = Decimal(str(self.size))
            minimum_size = Decimal(str(self.minimum_order_size))
            tick = Decimal(self.tick_size)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("tick_size must be a positive decimal") from exc
        if tick <= 0 or tick >= 1:
            raise ValueError("tick_size must be in (0, 1)")
        if price < tick or price > Decimal(1) - tick:
            raise ValueError("price must be inside the exchange tick bounds")
        if price % tick != 0:
            raise ValueError("price must conform exactly to tick_size")
        if size <= 0 or minimum_size <= 0:
            raise ValueError("size and minimum_order_size must be > 0")
        if size < minimum_size:
            raise ValueError("size must be at least minimum_order_size")
        if size.as_tuple().exponent < -2:
            raise ValueError("size must use at most two decimal places")
        if not isinstance(self.neg_risk, bool):
            raise ValueError("neg_risk must be bool")
        if isinstance(self.layer_index, bool) or not isinstance(self.layer_index, int):
            raise ValueError("layer_index must be a non-negative integer")
        if self.layer_index < 0:
            raise ValueError("layer_index must be a non-negative integer")
        if isinstance(self.rules_observed_at_ns, bool) or not isinstance(
            self.rules_observed_at_ns, int
        ):
            raise ValueError("rules_observed_at_ns must be a non-negative integer")
        if self.rules_observed_at_ns < 0:
            raise ValueError("rules_observed_at_ns must be a non-negative integer")
        if not isinstance(self.rules_sha256, str) or not _SHA256.fullmatch(self.rules_sha256):
            raise ValueError("rules_sha256 must be a lowercase SHA-256")
        if isinstance(self.maker_fee_rate_bps, bool) or not isinstance(
            self.maker_fee_rate_bps, int
        ):
            raise ValueError("maker_fee_rate_bps must be a non-negative integer")
        if self.maker_fee_rate_bps < 0:
            raise ValueError("maker_fee_rate_bps must be a non-negative integer")
        object.__setattr__(self, "condition_id", condition_id.casefold())
        object.__setattr__(self, "token_id", str(int(token_id)))
        object.__setattr__(self, "placement_id", placement_id)

    @property
    def notional(self) -> Decimal:
        return Decimal(str(self.price)) * Decimal(str(self.size))


@dataclass(frozen=True, slots=True)
class GatewayMarketPrewarm:
    token_id: str
    clob_version: int
    tick_size: str
    neg_risk: bool

    def __post_init__(self) -> None:
        _required_text(self.token_id, "token_id")
        if isinstance(self.clob_version, bool) or self.clob_version != 2:
            raise ValueError("clob_version must be integer 2")
        try:
            tick_size = Decimal(self.tick_size)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("tick_size must be a positive decimal") from exc
        if not Decimal(0) < tick_size < Decimal(1):
            raise ValueError("tick_size must be in (0, 1)")
        if not isinstance(self.neg_risk, bool):
            raise ValueError("neg_risk must be bool")


@dataclass(frozen=True, slots=True)
class GatewayOrderResponse:
    accepted: bool
    status: str
    venue_order_id: str | None = None
    rejection_reason: str | None = None
    order_build_ns: int | None = None
    submit_round_trip_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise ValueError("accepted must be bool")
        if self.accepted:
            if self.status != "live" or not self.venue_order_id or self.rejection_reason:
                raise ValueError("accepted post-only response must be a live venue order")
        elif (
            self.status != "rejected"
            or self.venue_order_id is not None
            or not isinstance(self.rejection_reason, str)
            or not self.rejection_reason.strip()
        ):
            raise ValueError("rejected response must have only a rejection_reason")
        for name in ("order_build_ns", "submit_round_trip_ns"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class GatewayCancelResult:
    canceled: tuple[str, ...]
    not_canceled: Mapping[str, str]

    def __post_init__(self) -> None:
        if any(not isinstance(item, str) or not item for item in self.canceled):
            raise ValueError("canceled order IDs must be non-empty strings")
        if len(set(self.canceled)) != len(self.canceled):
            raise ValueError("canceled order IDs must be unique")
        normalized: dict[str, str] = {}
        for order_id, reason in self.not_canceled.items():
            if not isinstance(order_id, str) or not order_id:
                raise ValueError("not_canceled order IDs must be non-empty strings")
            if not isinstance(reason, str) or not reason:
                raise ValueError("not_canceled reasons must be non-empty strings")
            normalized[order_id] = reason
        if set(self.canceled).intersection(normalized):
            raise ValueError("an order cannot be both canceled and not_canceled")
        object.__setattr__(self, "not_canceled", MappingProxyType(normalized))


class GatewaySubmissionUnknownError(RuntimeError):
    """The venue may have accepted the request, so automatic retry is unsafe."""


class GatewayCancellationUnknownError(RuntimeError):
    """The venue cancellation outcome is not proven by a valid response."""


class GatewayHeartbeatError(RuntimeError):
    """The dead-man heartbeat did not receive a valid acknowledgement."""


@dataclass(frozen=True, slots=True)
class PreparedPostOnlyOrder:
    request: LiveOrderRequest
    signed_order: object
    order_build_ns: int
    expected_venue_order_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.request, LiveOrderRequest):
            raise ValueError("request must be a LiveOrderRequest")
        if self.signed_order is None:
            raise ValueError("signed_order is required")
        _required_text(self.expected_venue_order_id, "expected_venue_order_id")
        if (
            isinstance(self.order_build_ns, bool)
            or not isinstance(self.order_build_ns, int)
            or self.order_build_ns < 0
        ):
            raise ValueError("order_build_ns must be a non-negative integer")


class LiveOrderGateway(Protocol):
    def prewarm_market(self, request: LiveOrderRequest) -> GatewayMarketPrewarm: ...

    def prepare_post_only_buy(self, request: LiveOrderRequest) -> PreparedPostOnlyOrder: ...

    def submit_prepared_post_only_buy(
        self, prepared: PreparedPostOnlyOrder
    ) -> GatewayOrderResponse: ...

    def submit_prepared_post_only_buys(
        self, prepared: Sequence[PreparedPostOnlyOrder]
    ) -> tuple[GatewayOrderResponse, ...]: ...

    def cancel_order(self, venue_order_id: str) -> GatewayCancelResult: ...

    def cancel_market(
        self, condition_id: str, token_id: str | None = None
    ) -> GatewayCancelResult: ...

    def cancel_all(self) -> GatewayCancelResult: ...

    def send_heartbeat(self, heartbeat_id: str = "") -> str: ...


class PaperOrderGateway:
    """In-memory gateway used exclusively by the safe paper mode."""

    def __init__(self) -> None:
        self._next_order_number = 1
        self._open_orders: dict[str, LiveOrderRequest] = {}

    @property
    def open_orders(self) -> dict[str, LiveOrderRequest]:
        return dict(self._open_orders)

    def prewarm_market(self, request: LiveOrderRequest) -> GatewayMarketPrewarm:
        return GatewayMarketPrewarm(request.token_id, 2, request.tick_size, request.neg_risk)

    def prepare_post_only_buy(self, request: LiveOrderRequest) -> PreparedPostOnlyOrder:
        venue_order_id = f"paper-{self._next_order_number}"
        self._next_order_number += 1
        return PreparedPostOnlyOrder(
            request=request,
            signed_order=request,
            order_build_ns=0,
            expected_venue_order_id=venue_order_id,
        )

    def submit_prepared_post_only_buy(
        self, prepared: PreparedPostOnlyOrder
    ) -> GatewayOrderResponse:
        venue_order_id = prepared.expected_venue_order_id
        if venue_order_id in self._open_orders:
            raise GatewaySubmissionUnknownError("paper order ID was submitted more than once")
        self._open_orders[venue_order_id] = prepared.request
        return GatewayOrderResponse(
            accepted=True,
            status="live",
            venue_order_id=venue_order_id,
            order_build_ns=prepared.order_build_ns,
            submit_round_trip_ns=0,
        )

    def submit_prepared_post_only_buys(
        self, prepared: Sequence[PreparedPostOnlyOrder]
    ) -> tuple[GatewayOrderResponse, ...]:
        return tuple(self.submit_prepared_post_only_buy(item) for item in prepared)

    def cancel_order(self, venue_order_id: str) -> GatewayCancelResult:
        canceled = (venue_order_id,) if self._open_orders.pop(venue_order_id, None) else ()
        not_canceled = {} if canceled else {venue_order_id: "not_open"}
        return GatewayCancelResult(canceled, not_canceled)

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> GatewayCancelResult:
        canceled = tuple(
            order_id
            for order_id, request in self._open_orders.items()
            if request.condition_id == condition_id
            and (token_id is None or request.token_id == token_id)
        )
        for order_id in canceled:
            del self._open_orders[order_id]
        return GatewayCancelResult(canceled, {})

    def cancel_all(self) -> GatewayCancelResult:
        canceled = tuple(self._open_orders)
        self._open_orders.clear()
        return GatewayCancelResult(canceled, {})

    def send_heartbeat(self, heartbeat_id: str = "") -> str:
        return heartbeat_id or "paper-heartbeat"


class PyClobV2Gateway:
    """Deep adapter which hides pinned SDK response and cache semantics."""

    def __init__(
        self,
        client: Any,
        *,
        order_id_resolver: Callable[[object, bool], str] | None = None,
    ) -> None:
        if getattr(client, "retry_on_error", False):
            raise ValueError("the CLOB client must use retry_on_error=False")
        self.client = client
        self._order_id_resolver = order_id_resolver or (
            lambda order, neg_risk: _v2_order_id(client, order, neg_risk=neg_risk)
        )
        self._prewarmed: dict[str, GatewayMarketPrewarm] = {}

    def prewarm_market(self, request: LiveOrderRequest) -> GatewayMarketPrewarm:
        version = self.client.get_version()
        if isinstance(version, bool) or version != 2:
            raise RuntimeError(f"unsupported CLOB protocol version: {version!r}")
        tick_size = str(self.client.get_tick_size(request.token_id))
        if tick_size != request.tick_size:
            raise RuntimeError(
                f"CLOB tick size changed for token {request.token_id}; expected "
                f"{request.tick_size}, received {tick_size}"
            )
        neg_risk = self.client.get_neg_risk(request.token_id)
        if not isinstance(neg_risk, bool) or neg_risk is not request.neg_risk:
            raise RuntimeError(f"CLOB neg_risk changed for token {request.token_id}")
        # SDK 1.0.2 caches version privately but its public get_version() does not.
        # Prime that version-specific cache here so create_order performs no REST read.
        setattr(self.client, "_ClobClient__cached_version", version)
        result = GatewayMarketPrewarm(request.token_id, version, tick_size, neg_risk)
        self._prewarmed[request.token_id] = result
        return result

    def prepare_post_only_buy(self, request: LiveOrderRequest) -> PreparedPostOnlyOrder:
        prewarmed = self._prewarmed.get(request.token_id)
        if prewarmed is None:
            raise RuntimeError("market must be prewarmed before signing")
        if prewarmed.tick_size != request.tick_size or prewarmed.neg_risk is not request.neg_risk:
            raise RuntimeError("prewarmed market rules no longer match the order request")
        try:
            from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install the project's live extra to use CLOB V2.") from exc
        started_ns = perf_counter_ns()
        signed_order = self.client.create_order(
            OrderArgs(
                token_id=request.token_id,
                price=request.price,
                size=request.size,
                side=BUY,
            ),
            options=PartialCreateOrderOptions(
                tick_size=request.tick_size,
                neg_risk=request.neg_risk,
            ),
        )
        expected_venue_order_id = self._order_id_resolver(signed_order, request.neg_risk)
        if not isinstance(expected_venue_order_id, str) or not _ORDER_ID.fullmatch(
            expected_venue_order_id
        ):
            raise RuntimeError("locally computed CLOB order ID is invalid")
        return PreparedPostOnlyOrder(
            request=request,
            signed_order=signed_order,
            order_build_ns=perf_counter_ns() - started_ns,
            expected_venue_order_id=expected_venue_order_id.casefold(),
        )

    def submit_prepared_post_only_buy(
        self, prepared: PreparedPostOnlyOrder
    ) -> GatewayOrderResponse:
        try:
            from py_clob_client_v2 import OrderType
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install the project's live extra to use CLOB V2.") from exc
        started_ns = perf_counter_ns()
        try:
            raw = self.client.post_order(
                prepared.signed_order,
                OrderType.GTC,
                post_only=True,
            )
        except Exception as exc:
            rejection = _definite_rejection(exc)
            if rejection is None:
                raise GatewaySubmissionUnknownError(
                    "CLOB order submission ended without a definitive response"
                ) from exc
            return GatewayOrderResponse(
                accepted=False,
                status="rejected",
                rejection_reason=rejection,
                order_build_ns=prepared.order_build_ns,
                submit_round_trip_ns=perf_counter_ns() - started_ns,
            )
        return _verify_prepared_response(
            _parse_order_response(
                raw,
                order_build_ns=prepared.order_build_ns,
                submit_round_trip_ns=perf_counter_ns() - started_ns,
            ),
            prepared,
        )

    def submit_prepared_post_only_buys(
        self, prepared: Sequence[PreparedPostOnlyOrder]
    ) -> tuple[GatewayOrderResponse, ...]:
        items = tuple(prepared)
        if not 1 <= len(items) <= 15:
            raise ValueError("CLOB batch must contain between 1 and 15 orders")
        try:
            from py_clob_client_v2 import OrderType, PostOrdersV2Args
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install the project's live extra to use CLOB V2.") from exc
        started_ns = perf_counter_ns()
        try:
            raw = self.client.post_orders(
                [
                    PostOrdersV2Args(order=item.signed_order, orderType=OrderType.GTC)
                    for item in items
                ],
                post_only=True,
            )
        except Exception as exc:
            rejection = _definite_rejection(exc)
            if rejection is None:
                raise GatewaySubmissionUnknownError(
                    "CLOB batch submission ended without a definitive response"
                ) from exc
            elapsed = perf_counter_ns() - started_ns
            return tuple(
                GatewayOrderResponse(
                    accepted=False,
                    status="rejected",
                    rejection_reason=rejection,
                    order_build_ns=item.order_build_ns,
                    submit_round_trip_ns=elapsed,
                )
                for item in items
            )
        elapsed = perf_counter_ns() - started_ns
        if not isinstance(raw, list) or len(raw) != len(items):
            raise GatewaySubmissionUnknownError(
                "CLOB batch response did not match the submitted order count"
            )
        return tuple(
            _verify_prepared_response(
                _parse_order_response(
                    response,
                    order_build_ns=item.order_build_ns,
                    submit_round_trip_ns=elapsed,
                ),
                item,
            )
            for item, response in zip(items, raw, strict=True)
        )

    def cancel_order(self, venue_order_id: str) -> GatewayCancelResult:
        try:
            from py_clob_client_v2 import OrderPayload
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install the project's live extra to use CLOB V2.") from exc
        result = self._cancel(
            lambda: self.client.cancel_order(OrderPayload(orderID=venue_order_id))
        )
        if (venue_order_id in result.canceled) == (venue_order_id in result.not_canceled):
            raise GatewayCancellationUnknownError(
                "single-order cancel receipt did not identify exactly one outcome"
            )
        return result

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> GatewayCancelResult:
        try:
            from py_clob_client_v2 import OrderMarketCancelParams
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install the project's live extra to use CLOB V2.") from exc
        return self._cancel(
            lambda: self.client.cancel_market_orders(
                OrderMarketCancelParams(market=condition_id, asset_id=token_id)
            )
        )

    def cancel_all(self) -> GatewayCancelResult:
        return self._cancel(self.client.cancel_all)

    def _cancel(self, operation: Any) -> GatewayCancelResult:
        try:
            response = operation()
        except Exception as exc:
            raise GatewayCancellationUnknownError(
                "CLOB cancellation ended without a definitive receipt"
            ) from exc
        return _parse_cancel_response(response)

    def send_heartbeat(self, heartbeat_id: str = "") -> str:
        try:
            response = self.client.post_heartbeat(heartbeat_id)
        except Exception as exc:
            corrected = _expired_heartbeat_id(exc)
            if corrected is None:
                raise GatewayHeartbeatError("CLOB heartbeat failed") from exc
            try:
                response = self.client.post_heartbeat(corrected)
            except Exception as retry_exc:
                raise GatewayHeartbeatError("CLOB heartbeat ID recovery failed") from retry_exc
        if not isinstance(response, Mapping):
            raise GatewayHeartbeatError("invalid CLOB heartbeat response")
        next_id = response.get("heartbeat_id") or response.get("heartbeatId")
        if not isinstance(next_id, str) or not next_id:
            raise GatewayHeartbeatError("CLOB heartbeat response has no heartbeat ID")
        return next_id


def _parse_order_response(
    raw: object,
    *,
    order_build_ns: int,
    submit_round_trip_ns: int,
) -> GatewayOrderResponse:
    if not isinstance(raw, Mapping):
        raise GatewaySubmissionUnknownError("CLOB order response was not an object")
    success = raw.get("success")
    status = raw.get("status")
    venue_order_id = raw.get("orderID")
    error = raw.get("errorMsg")
    error_text = error.strip() if isinstance(error, str) else ""
    if success is True and status == "live" and isinstance(venue_order_id, str) and venue_order_id:
        if error_text:
            raise GatewaySubmissionUnknownError("CLOB live response also contained an error")
        return GatewayOrderResponse(
            accepted=True,
            status="live",
            venue_order_id=venue_order_id,
            order_build_ns=order_build_ns,
            submit_round_trip_ns=submit_round_trip_ns,
        )
    if success is False and error_text and not venue_order_id and not status:
        return GatewayOrderResponse(
            accepted=False,
            status="rejected",
            rejection_reason=error_text,
            order_build_ns=order_build_ns,
            submit_round_trip_ns=submit_round_trip_ns,
        )
    raise GatewaySubmissionUnknownError(
        "CLOB post-only response did not prove a live order or a rejection"
    )


def _verify_prepared_response(
    response: GatewayOrderResponse,
    prepared: PreparedPostOnlyOrder,
) -> GatewayOrderResponse:
    if (
        response.accepted
        and response.venue_order_id is not None
        and response.venue_order_id.casefold() != prepared.expected_venue_order_id.casefold()
    ):
        raise GatewaySubmissionUnknownError(
            "CLOB response order ID does not match the locally signed order hash"
        )
    return response


def _parse_cancel_response(raw: object) -> GatewayCancelResult:
    if not isinstance(raw, Mapping):
        raise GatewayCancellationUnknownError("CLOB cancel response was not an object")
    canceled = raw.get("canceled")
    not_canceled = raw.get("not_canceled")
    if not isinstance(canceled, list) or not isinstance(not_canceled, Mapping):
        raise GatewayCancellationUnknownError("CLOB cancel receipt has an invalid schema")
    if not all(isinstance(order_id, str) and order_id for order_id in canceled):
        raise GatewayCancellationUnknownError("CLOB cancel receipt has an invalid order ID")
    if not all(isinstance(order_id, str) and order_id for order_id in not_canceled):
        raise GatewayCancellationUnknownError("CLOB cancel receipt has an invalid order ID")
    return GatewayCancelResult(
        canceled=tuple(canceled),
        not_canceled={key: _safe_reason(value) for key, value in not_canceled.items()},
    )


def _v2_order_id(client: Any, signed_order: object, *, neg_risk: bool) -> str:
    """Derive the venue's EIP-712 order hash before the network boundary."""

    try:
        from py_clob_client_v2.config import get_contract_config
        from py_clob_client_v2.order_utils import ExchangeOrderBuilderV2
    except ImportError as exc:  # pragma: no cover - optional live dependency
        raise RuntimeError("Install the project's live extra to compute CLOB order IDs.") from exc
    signer = getattr(client, "signer", None)
    if signer is None or not callable(getattr(signer, "get_chain_id", None)):
        raise RuntimeError("CLOB client signer is unavailable for order hash derivation")
    chain_id = signer.get_chain_id()
    if isinstance(chain_id, bool) or chain_id != 137:
        raise RuntimeError("live CLOB order hash requires Polygon chain_id 137")
    contracts = get_contract_config(chain_id)
    exchange = contracts.neg_risk_exchange_v2 if neg_risk else contracts.exchange_v2
    builder = ExchangeOrderBuilderV2(exchange, chain_id, signer)
    try:
        typed_data = builder.build_order_typed_data(signed_order)
        result = builder.build_order_hash(typed_data)
    except Exception as exc:
        raise RuntimeError("failed to derive the signed CLOB order hash") from exc
    if not isinstance(result, str) or not _ORDER_ID.fullmatch(result):
        raise RuntimeError("signed CLOB order hash has an invalid format")
    return result.casefold()


def _definite_rejection(exc: Exception) -> str | None:
    if isinstance(exc, httpx.TransportError):
        return None
    if exc.__class__.__name__ != "PolyApiException":
        return None
    status = getattr(exc, "status_code", None)
    payload = getattr(exc, "error_msg", None)
    if status == 425 or isinstance(status, int) and 400 <= status < 500:
        return _safe_reason(payload, fallback=f"http_{status}")
    if status == 503:
        reason = _safe_reason(payload, fallback="")
        if "cancel-only" in reason.casefold() or "post-only mode" in reason.casefold():
            return reason
    return None


def _expired_heartbeat_id(exc: Exception) -> str | None:
    if exc.__class__.__name__ != "PolyApiException" or getattr(exc, "status_code", None) != 400:
        return None
    payload = getattr(exc, "error_msg", None)
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("heartbeat_id") or payload.get("heartbeatId")
    return value if isinstance(value, str) and value else None


def _safe_reason(value: object, *, fallback: str = "venue_rejected") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:256]
    if isinstance(value, Mapping):
        for key in ("code", "error", "errorMsg", "message"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:256]
    return fallback


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()
