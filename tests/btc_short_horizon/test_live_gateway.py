from __future__ import annotations

import pytest

from btc_short_horizon.live.gateway import PyClobV2Gateway


pytest.importorskip("py_clob_client_v2")


class _CurrentV2Client:
    def __init__(self) -> None:
        self.order_cancel_payload = None
        self.market_cancel_payload = None
        self.cancel_all_calls = 0

    def cancel_order(self, payload) -> None:  # type: ignore[no-untyped-def]
        self.order_cancel_payload = payload

    def cancel_market_orders(self, payload) -> None:  # type: ignore[no-untyped-def]
        self.market_cancel_payload = payload

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1


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
