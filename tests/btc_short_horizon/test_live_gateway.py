from __future__ import annotations

import pytest
import httpx
from py_clob_client_v2.exceptions import PolyApiException

from btc_short_horizon.live.gateway import (
    GatewayCancellationUnknownError,
    GatewaySubmissionUnknownError,
    LiveOrderRequest,
    PyClobV2Gateway,
)


pytest.importorskip("py_clob_client_v2")


class _CurrentV2Client:
    def __init__(self) -> None:
        self.order_cancel_payload = None
        self.market_cancel_payload = None
        self.cancel_all_calls = 0
        self.created_order = object()
        self.posted_order = None
        self.heartbeat_ids: list[str] = []

    def create_order(self, order_args, options):  # type: ignore[no-untyped-def]
        self.order_args = order_args
        self.order_options = options
        return self.created_order

    def post_order(self, order, order_type, post_only=False):  # type: ignore[no-untyped-def]
        self.posted_order = (order, order_type, post_only)
        return {"orderID": "venue-order", "status": "live"}

    def cancel_order(self, payload) -> None:  # type: ignore[no-untyped-def]
        self.order_cancel_payload = payload

    def cancel_market_orders(self, payload) -> None:  # type: ignore[no-untyped-def]
        self.market_cancel_payload = payload

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1

    def post_heartbeat(self, heartbeat_id: str) -> dict[str, str]:
        self.heartbeat_ids.append(heartbeat_id)
        return {"heartbeat_id": heartbeat_id or "heartbeat-1"}


def _request() -> LiveOrderRequest:
    return LiveOrderRequest("condition", "token", 0.5, 1.0, "0.01", False)


def test_gateway_separates_order_build_from_submission_and_records_timings() -> None:
    client = _CurrentV2Client()
    gateway = PyClobV2Gateway(client)

    prepared = gateway.prepare_post_only_buy(_request())
    response = gateway.submit_prepared_post_only_buy(prepared)

    assert prepared.signed_order is client.created_order
    assert response.venue_order_id == "venue-order"
    assert response.order_build_ns is not None
    assert response.submit_round_trip_ns is not None
    assert client.posted_order[2] is True


def test_gateway_raises_unknown_for_transport_failures() -> None:
    client = _CurrentV2Client()
    gateway = PyClobV2Gateway(client)
    client.post_order = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        httpx.ReadTimeout("timed out")
    )

    with pytest.raises(GatewaySubmissionUnknownError):
        gateway.submit_post_only_buy(_request())

    client.cancel_order = lambda payload: (_ for _ in ()).throw(  # type: ignore[method-assign]
        httpx.ReadTimeout("timed out")
    )
    with pytest.raises(GatewayCancellationUnknownError):
        gateway.cancel_order("order-id")


def test_gateway_classifies_sdk_request_errors_without_retrying_definite_rejections() -> None:
    client = _CurrentV2Client()
    gateway = PyClobV2Gateway(client)
    client.post_order = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        PolyApiException(error_msg="Request exception!")
    )

    with pytest.raises(GatewaySubmissionUnknownError):
        gateway.submit_post_only_buy(_request())

    response = httpx.Response(400, json={"error": "post-only order would cross"})
    client.post_order = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        PolyApiException(resp=response)
    )
    with pytest.raises(PolyApiException):
        gateway.submit_post_only_buy(_request())


def test_gateway_carries_forward_venue_heartbeat_id() -> None:
    client = _CurrentV2Client()
    gateway = PyClobV2Gateway(client)

    assert gateway.send_heartbeat() == "heartbeat-1"
    assert gateway.send_heartbeat("heartbeat-1") == "heartbeat-1"
    assert client.heartbeat_ids == ["", "heartbeat-1"]


def test_gateway_uses_current_v2_cancel_payload_types() -> None:
    client = _CurrentV2Client()
    gateway = PyClobV2Gateway(client)

    gateway.cancel_order("order-id")
    gateway.cancel_market("condition-id", "token-id")
    gateway.cancel_all()

    assert client.order_cancel_payload.orderID == "order-id"
    assert client.market_cancel_payload.market == "condition-id"
    assert client.market_cancel_payload.asset_id == "token-id"
    assert client.cancel_all_calls == 1
