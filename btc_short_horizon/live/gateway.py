"""Thin, version-specific gateway over the official py-clob-client-v2 SDK."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter_ns
from typing import Any, Protocol

import httpx


@dataclass(frozen=True, slots=True)
class LiveOrderRequest:
    condition_id: str
    token_id: str
    price: float
    size: float
    tick_size: str
    neg_risk: bool

    def __post_init__(self) -> None:
        if not self.condition_id or not self.token_id:
            raise ValueError("condition_id and token_id are required")
        if not 0.0 < self.price < 1.0:
            raise ValueError("price must be in (0, 1)")
        if self.size <= 0.0:
            raise ValueError("size must be > 0")
        if not self.tick_size:
            raise ValueError("tick_size is required")


@dataclass(frozen=True, slots=True)
class GatewayOrderResponse:
    venue_order_id: str
    status: str
    order_build_ns: int | None = None
    submit_round_trip_ns: int | None = None


class GatewaySubmissionUnknownError(RuntimeError):
    """The venue may have accepted the request, so automatic retry is unsafe."""


class GatewayCancellationUnknownError(RuntimeError):
    """The venue may have applied the cancellation despite a transport failure."""


@dataclass(frozen=True, slots=True)
class PreparedPostOnlyOrder:
    request: LiveOrderRequest
    signed_order: object
    order_build_ns: int


class LiveOrderGateway(Protocol):
    def submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse: ...

    def cancel_order(self, venue_order_id: str) -> None: ...

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> None: ...

    def cancel_all(self) -> None: ...

    def send_heartbeat(self, heartbeat_id: str = "") -> str: ...


class PaperOrderGateway:
    """In-memory order gateway used exclusively by the safe paper mode."""

    def __init__(self) -> None:
        self._next_order_number = 1
        self._open_orders: dict[str, LiveOrderRequest] = {}

    @property
    def open_orders(self) -> dict[str, LiveOrderRequest]:
        return dict(self._open_orders)

    def submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse:
        venue_order_id = f"paper-{self._next_order_number}"
        self._next_order_number += 1
        self._open_orders[venue_order_id] = request
        return GatewayOrderResponse(venue_order_id=venue_order_id, status="paper_live")

    def cancel_order(self, venue_order_id: str) -> None:
        self._open_orders.pop(venue_order_id, None)

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> None:
        self._open_orders = {
            venue_order_id: request
            for venue_order_id, request in self._open_orders.items()
            if request.condition_id != condition_id
            or (token_id is not None and request.token_id != token_id)
        }

    def cancel_all(self) -> None:
        self._open_orders.clear()

    def send_heartbeat(self, heartbeat_id: str = "") -> str:
        return heartbeat_id or "paper-heartbeat"


class PyClobV2Gateway:
    """Uses only current V2 order creation and post-only submission semantics."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse:
        prepared = self.prepare_post_only_buy(request)
        return self.submit_prepared_post_only_buy(prepared)

    def prepare_post_only_buy(self, request: LiveOrderRequest) -> PreparedPostOnlyOrder:
        try:
            from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError(
                "Install the project's live extra to use py-clob-client-v2."
            ) from exc
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
        return PreparedPostOnlyOrder(
            request=request,
            signed_order=signed_order,
            order_build_ns=perf_counter_ns() - started_ns,
        )

    def submit_prepared_post_only_buy(
        self, prepared: PreparedPostOnlyOrder
    ) -> GatewayOrderResponse:
        try:
            from py_clob_client_v2 import OrderType
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError(
                "Install the project's live extra to use py-clob-client-v2."
            ) from exc
        started_ns = perf_counter_ns()
        try:
            response = self.client.post_order(
                prepared.signed_order,
                OrderType.GTC,
                post_only=True,
            )
        except Exception as exc:
            if not _is_ambiguous_request_error(exc):
                raise
            raise GatewaySubmissionUnknownError(
                "CLOB order submission ended without a definitive venue response"
            ) from exc
        submit_ns = perf_counter_ns() - started_ns
        if not isinstance(response, dict) or not response.get("orderID"):
            raise RuntimeError(f"CLOB V2 rejected post-only order: {response!r}")
        return GatewayOrderResponse(
            venue_order_id=str(response["orderID"]),
            status=str(response.get("status", "unknown")),
            order_build_ns=prepared.order_build_ns,
            submit_round_trip_ns=submit_ns,
        )

    def cancel_order(self, venue_order_id: str) -> None:
        try:
            from py_clob_client_v2 import OrderPayload
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError(
                "Install the project's live extra to use py-clob-client-v2."
            ) from exc
        try:
            self.client.cancel_order(OrderPayload(orderID=venue_order_id))
        except Exception as exc:
            if not _is_ambiguous_request_error(exc):
                raise
            raise GatewayCancellationUnknownError(
                "CLOB order cancellation ended without a definitive venue response"
            ) from exc

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> None:
        try:
            from py_clob_client_v2 import OrderMarketCancelParams
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError(
                "Install the project's live extra to use py-clob-client-v2."
            ) from exc
        try:
            self.client.cancel_market_orders(
                OrderMarketCancelParams(market=condition_id, asset_id=token_id)
            )
        except Exception as exc:
            if not _is_ambiguous_request_error(exc):
                raise
            raise GatewayCancellationUnknownError(
                "CLOB market cancellation ended without a definitive venue response"
            ) from exc

    def cancel_all(self) -> None:
        try:
            self.client.cancel_all()
        except Exception as exc:
            if not _is_ambiguous_request_error(exc):
                raise
            raise GatewayCancellationUnknownError(
                "CLOB cancel-all ended without a definitive venue response"
            ) from exc

    def send_heartbeat(self, heartbeat_id: str = "") -> str:
        response = self.client.post_heartbeat(heartbeat_id)
        if not isinstance(response, dict):
            raise RuntimeError(f"invalid CLOB heartbeat response: {response!r}")
        next_id = response.get("heartbeat_id") or response.get("heartbeatId")
        if not isinstance(next_id, str) or not next_id:
            raise RuntimeError(f"CLOB heartbeat response has no heartbeat ID: {response!r}")
        return next_id


def _is_ambiguous_request_error(exc: Exception) -> bool:
    """Return true only when the venue outcome cannot be determined safely."""

    if isinstance(exc, httpx.TransportError):
        return True
    if exc.__class__.__name__ != "PolyApiException":
        return False
    status_code = getattr(exc, "status_code", None)
    return status_code is None or status_code >= 500
