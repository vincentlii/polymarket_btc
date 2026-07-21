from __future__ import annotations

from collections.abc import Sequence
from math import inf, nan

import httpx
import pytest
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.exceptions import PolyApiException

from btc_short_horizon.live.gateway import (
    GatewayCancelResult,
    GatewayCancellationUnknownError,
    GatewayHeartbeatError,
    GatewayMarketPrewarm,
    GatewayOrderResponse,
    GatewaySubmissionUnknownError,
    LiveOrderRequest,
    PyClobV2Gateway,
)


pytest.importorskip("py_clob_client_v2")


RULES_SHA256 = "a" * 64
CONDITION_A = "0x" + ("a" * 64)
TOKEN_A = "1"
ORDER_0 = "0x" + ("1" * 64)
ORDER_1 = "0x" + ("2" * 64)


class _CurrentV2Client:
    def __init__(self) -> None:
        self.retry_on_error = False
        self.order_cancel_payload = None
        self.market_cancel_payload = None
        self.cancel_all_calls = 0
        self.created_orders: list[object] = []
        self.posted_order = None
        self.posted_orders = None
        self.heartbeat_ids: list[str] = []
        self.reads: list[tuple[str, str]] = []
        self.order_responses: list[dict[str, object]] = [
            {
                "success": True,
                "errorMsg": "",
                "orderID": ORDER_0,
                "status": "live",
            }
        ]

    def get_version(self) -> int:
        self.reads.append(("version", ""))
        return 2

    def get_tick_size(self, token_id: str) -> str:
        self.reads.append(("tick", token_id))
        return "0.01"

    def get_neg_risk(self, token_id: str) -> bool:
        self.reads.append(("neg_risk", token_id))
        return False

    def create_order(self, order_args, options):  # type: ignore[no-untyped-def]
        self.order_args = order_args
        self.order_options = options
        signed = ORDER_0 if order_args.price == 0.5 else ORDER_1
        self.created_orders.append(signed)
        return signed

    def post_order(self, order, order_type, post_only=False):  # type: ignore[no-untyped-def]
        self.posted_order = (order, order_type, post_only)
        return self.order_responses[0]

    def post_orders(self, args: Sequence[object], post_only=False):  # type: ignore[no-untyped-def]
        self.posted_orders = (tuple(args), post_only)
        return self.order_responses

    def cancel_order(self, payload):  # type: ignore[no-untyped-def]
        self.order_cancel_payload = payload
        return {"canceled": [payload.orderID], "not_canceled": {}}

    def cancel_market_orders(self, payload):  # type: ignore[no-untyped-def]
        self.market_cancel_payload = payload
        return {"canceled": ["market-order"], "not_canceled": {}}

    def cancel_all(self):  # type: ignore[no-untyped-def]
        self.cancel_all_calls += 1
        return {"canceled": ["venue-order"], "not_canceled": {}}

    def post_heartbeat(self, heartbeat_id: str) -> dict[str, str]:
        self.heartbeat_ids.append(heartbeat_id)
        return {"heartbeat_id": heartbeat_id or "heartbeat-1"}


def _request(**overrides: object) -> LiveOrderRequest:
    values: dict[str, object] = {
        "condition_id": CONDITION_A,
        "token_id": TOKEN_A,
        "price": 0.5,
        "size": 1.0,
        "tick_size": "0.01",
        "neg_risk": False,
        "minimum_order_size": 1.0,
        "placement_id": "condition-10-up",
        "layer_index": 0,
        "rules_observed_at_ns": 1,
        "rules_sha256": RULES_SHA256,
        "maker_fee_rate_bps": 0,
    }
    values.update(overrides)
    return LiveOrderRequest(**values)  # type: ignore[arg-type]


def _gateway(client: _CurrentV2Client) -> PyClobV2Gateway:
    return PyClobV2Gateway(
        client,
        order_id_resolver=lambda signed_order, neg_risk: str(signed_order),
    )


@pytest.mark.parametrize("field,value", [("price", nan), ("price", inf), ("size", nan)])
def test_live_order_request_rejects_nonfinite_numbers(field: str, value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        _request(**{field: value})


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"price": True}, "price"),
        ({"size": True}, "size"),
        ({"neg_risk": 1}, "neg_risk"),
        ({"layer_index": True}, "layer_index"),
        ({"price": 0.505}, "tick_size"),
        ({"size": 1.001}, "two decimal"),
        ({"size": 0.5}, "minimum_order_size"),
        ({"rules_sha256": "operator-entered"}, "rules_sha256"),
        ({"condition_id": "condition"}, "condition_id"),
        ({"token_id": "token"}, "token_id"),
    ],
)
def test_live_order_request_rejects_ambiguous_or_exchange_invalid_values(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _request(**overrides)


def test_gateway_requires_verified_market_prewarm_before_signing() -> None:
    client = _CurrentV2Client()
    gateway = _gateway(client)

    with pytest.raises(RuntimeError, match="prewarmed"):
        gateway.prepare_post_only_buy(_request())

    prewarm = gateway.prewarm_market(_request())
    reads_after_prewarm = tuple(client.reads)
    prepared = gateway.prepare_post_only_buy(_request())

    assert prewarm.clob_version == 2
    assert prewarm.tick_size == "0.01"
    assert prewarm.neg_risk is False
    assert tuple(client.reads) == reads_after_prewarm
    assert prepared.signed_order is client.created_orders[0]


def test_gateway_derives_the_real_v2_order_hash_before_submission() -> None:
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key="0x" + ("1" * 64),
        retry_on_error=False,
    )
    client.get_version = lambda: 2  # type: ignore[method-assign]
    client.get_tick_size = lambda token_id: "0.01"  # type: ignore[method-assign]
    client.get_neg_risk = lambda token_id: False  # type: ignore[method-assign]
    gateway = PyClobV2Gateway(client)
    gateway.prewarm_market(_request())

    prepared = gateway.prepare_post_only_buy(_request())

    assert prepared.expected_venue_order_id.startswith("0x")
    assert len(prepared.expected_venue_order_id) == 66
    assert int(prepared.expected_venue_order_id[2:], 16) > 0


def test_gateway_rejects_retrying_sdk_and_mismatched_prewarm_rules() -> None:
    client = _CurrentV2Client()
    client.retry_on_error = True
    with pytest.raises(ValueError, match="retry_on_error=False"):
        _gateway(client)

    client.retry_on_error = False
    client.get_tick_size = lambda token_id: "0.001"  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="tick size changed"):
        _gateway(client).prewarm_market(_request())


def test_gateway_separates_order_build_from_submission_and_records_timings() -> None:
    client = _CurrentV2Client()
    gateway = _gateway(client)
    gateway.prewarm_market(_request())

    prepared = gateway.prepare_post_only_buy(_request())
    response = gateway.submit_prepared_post_only_buy(prepared)

    assert response.accepted
    assert response.venue_order_id == ORDER_0
    assert response.status == "live"
    assert response.order_build_ns is not None
    assert response.submit_round_trip_ns is not None
    assert client.posted_order[2] is True


def test_gateway_parses_mixed_batch_results_without_assuming_atomicity() -> None:
    client = _CurrentV2Client()
    client.order_responses = [
        {"success": True, "errorMsg": "", "orderID": ORDER_0, "status": "live"},
        {
            "success": False,
            "errorMsg": "INVALID_POST_ONLY_ORDER",
            "orderID": "",
            "status": "",
        },
    ]
    gateway = _gateway(client)
    first = _request(layer_index=0, price=0.50)
    second = _request(layer_index=1, price=0.49)
    gateway.prewarm_market(first)

    responses = gateway.submit_prepared_post_only_buys(
        (gateway.prepare_post_only_buy(first), gateway.prepare_post_only_buy(second))
    )

    assert [item.accepted for item in responses] == [True, False]
    assert responses[0].venue_order_id == ORDER_0
    assert responses[1].rejection_reason == "INVALID_POST_ONLY_ORDER"
    assert client.posted_orders[1] is True


@pytest.mark.parametrize(
    "response",
    [
        {"success": True, "errorMsg": "", "orderID": ORDER_0, "status": "matched"},
        {"success": True, "errorMsg": "", "orderID": "", "status": "live"},
        {"success": "true", "errorMsg": "", "orderID": ORDER_0, "status": "live"},
    ],
)
def test_gateway_treats_semantically_ambiguous_post_only_response_as_unknown(
    response: dict[str, object],
) -> None:
    client = _CurrentV2Client()
    client.order_responses = [response]
    gateway = _gateway(client)
    gateway.prewarm_market(_request())

    with pytest.raises(GatewaySubmissionUnknownError):
        gateway.submit_prepared_post_only_buy(gateway.prepare_post_only_buy(_request()))


def test_gateway_rejects_a_response_for_a_different_signed_order() -> None:
    client = _CurrentV2Client()
    client.order_responses = [
        {"success": True, "errorMsg": "", "orderID": ORDER_1, "status": "live"}
    ]
    gateway = _gateway(client)
    gateway.prewarm_market(_request())

    with pytest.raises(GatewaySubmissionUnknownError, match="signed order hash"):
        gateway.submit_prepared_post_only_buy(gateway.prepare_post_only_buy(_request()))


def test_gateway_treats_unhashable_success_field_as_unknown_not_a_parser_crash() -> None:
    client = _CurrentV2Client()
    client.order_responses = [
        {"success": [], "errorMsg": "bad response", "orderID": "", "status": ""}
    ]
    gateway = _gateway(client)
    gateway.prewarm_market(_request())

    with pytest.raises(GatewaySubmissionUnknownError):
        gateway.submit_prepared_post_only_buy(gateway.prepare_post_only_buy(_request()))


def test_gateway_raises_unknown_for_transport_failures() -> None:
    client = _CurrentV2Client()
    gateway = _gateway(client)
    gateway.prewarm_market(_request())
    client.post_order = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        httpx.ReadTimeout("timed out")
    )

    with pytest.raises(GatewaySubmissionUnknownError):
        gateway.submit_prepared_post_only_buy(gateway.prepare_post_only_buy(_request()))

    client.cancel_order = lambda payload: (_ for _ in ()).throw(  # type: ignore[method-assign]
        httpx.ReadTimeout("timed out")
    )
    with pytest.raises(GatewayCancellationUnknownError):
        gateway.cancel_order("order-id")


def test_gateway_classifies_definite_order_rejections_without_retrying() -> None:
    client = _CurrentV2Client()
    gateway = _gateway(client)
    gateway.prewarm_market(_request())
    response = httpx.Response(400, json={"error": "post-only order would cross"})
    client.post_order = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        PolyApiException(resp=response)
    )

    result = gateway.submit_prepared_post_only_buy(gateway.prepare_post_only_buy(_request()))

    assert not result.accepted
    assert result.rejection_reason == "post-only order would cross"


def test_gateway_recovers_expired_heartbeat_id_once() -> None:
    client = _CurrentV2Client()
    gateway = _gateway(client)
    calls = 0

    def heartbeat(heartbeat_id: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        client.heartbeat_ids.append(heartbeat_id)
        if calls == 1:
            response = httpx.Response(400, json={"heartbeat_id": "heartbeat-current"})
            raise PolyApiException(resp=response)
        return {"heartbeat_id": heartbeat_id}

    client.post_heartbeat = heartbeat  # type: ignore[method-assign]

    assert gateway.send_heartbeat("heartbeat-expired") == "heartbeat-current"
    assert client.heartbeat_ids == ["heartbeat-expired", "heartbeat-current"]


def test_gateway_accepts_the_current_documented_stateless_heartbeat_ack() -> None:
    client = _CurrentV2Client()
    client.post_heartbeat = lambda heartbeat_id: {"status": "ok"}  # type: ignore[method-assign]

    assert _gateway(client).send_heartbeat("legacy-id") == ""


def test_gateway_heartbeat_failure_is_explicit() -> None:
    client = _CurrentV2Client()
    gateway = _gateway(client)
    client.post_heartbeat = lambda heartbeat_id: (_ for _ in ()).throw(  # type: ignore[method-assign]
        httpx.ReadTimeout("timed out")
    )

    with pytest.raises(GatewayHeartbeatError):
        gateway.send_heartbeat()


def test_gateway_validates_cancel_receipts() -> None:
    client = _CurrentV2Client()
    gateway = _gateway(client)

    result = gateway.cancel_order("order-id")
    market = gateway.cancel_market("condition-id", "token-id")
    all_orders = gateway.cancel_all()

    assert result.canceled == ("order-id",)
    assert not result.not_canceled
    assert market.canceled == ("market-order",)
    assert all_orders.canceled == ("venue-order",)
    assert client.order_cancel_payload.orderID == "order-id"
    assert client.market_cancel_payload.market == "condition-id"
    assert client.market_cancel_payload.asset_id == "token-id"
    assert client.cancel_all_calls == 1


def test_gateway_rejects_malformed_cancel_receipt_as_unknown() -> None:
    client = _CurrentV2Client()
    client.cancel_order = lambda payload: {"canceled": [], "not_canceled": {}}  # type: ignore[method-assign]
    gateway = _gateway(client)

    with pytest.raises(GatewayCancellationUnknownError):
        gateway.cancel_order("order-id")

    client.cancel_order = lambda payload: {  # type: ignore[method-assign]
        "canceled": [],
        "not_canceled": {1: "invalid key"},
    }
    with pytest.raises(GatewayCancellationUnknownError):
        gateway.cancel_order("order-id")


def test_gateway_value_objects_reject_overlapping_or_invalid_receipts() -> None:
    with pytest.raises(ValueError, match="both canceled and not_canceled"):
        GatewayCancelResult(("order-id",), {"order-id": "still open"})
    with pytest.raises(ValueError, match="clob_version"):
        GatewayMarketPrewarm("token", True, "0.01", False)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="order_build_ns"):
        GatewayOrderResponse(
            True,
            "live",
            venue_order_id="order-id",
            order_build_ns=-1,
            submit_round_trip_ns=1,
        )
    with pytest.raises(ValueError, match="rejected response"):
        GatewayOrderResponse(False, "live", rejection_reason="no")
