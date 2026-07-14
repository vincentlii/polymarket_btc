from __future__ import annotations

from btc_short_horizon.backtest.audit import OrderAuditTrail


def test_order_audit_trail_preserves_ordered_json_ready_events() -> None:
    trail = OrderAuditTrail()
    trail.record(
        event_type="submit",
        ts_ns=100,
        client_order_id="client-1",
        price=0.48,
        size=2.0,
    )
    trail.record(
        event_type="cancel_request",
        ts_ns=110,
        client_order_id="client-1",
        reason="probability_drop",
    )

    assert trail.records == (
        {
            "event_type": "submit",
            "ts_ns": 100,
            "client_order_id": "client-1",
            "price": 0.48,
            "size": 2.0,
        },
        {
            "event_type": "cancel_request",
            "ts_ns": 110,
            "client_order_id": "client-1",
            "reason": "probability_drop",
        },
    )
