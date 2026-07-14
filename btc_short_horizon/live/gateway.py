"""Thin, version-specific gateway over the official py-clob-client-v2 SDK."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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


class LiveOrderGateway(Protocol):
    def submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse: ...

    def cancel_order(self, venue_order_id: str) -> None: ...

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> None: ...

    def cancel_all(self) -> None: ...


class PyClobV2Gateway:
    """Uses only current V2 order creation and post-only submission semantics."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse:
        try:
            from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError(
                "Install the project's live extra to use py-clob-client-v2."
            ) from exc
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
        response = self.client.post_order(signed_order, OrderType.GTC, post_only=True)
        if not isinstance(response, dict) or not response.get("orderID"):
            raise RuntimeError(f"CLOB V2 rejected post-only order: {response!r}")
        return GatewayOrderResponse(
            venue_order_id=str(response["orderID"]),
            status=str(response.get("status", "unknown")),
        )

    def cancel_order(self, venue_order_id: str) -> None:
        try:
            from py_clob_client_v2 import OrderPayload
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError(
                "Install the project's live extra to use py-clob-client-v2."
            ) from exc
        self.client.cancel_order(OrderPayload(orderID=venue_order_id))

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> None:
        try:
            from py_clob_client_v2 import OrderMarketCancelParams
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError(
                "Install the project's live extra to use py-clob-client-v2."
            ) from exc
        self.client.cancel_market_orders(
            OrderMarketCancelParams(market=condition_id, asset_id=token_id)
        )

    def cancel_all(self) -> None:
        self.client.cancel_all()
