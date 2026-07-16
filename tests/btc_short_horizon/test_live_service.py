from __future__ import annotations

from pathlib import Path

import pytest

from btc_short_horizon.live import (
    AccountSnapshot,
    GatewayOrderResponse,
    GatewayCancellationUnknownError,
    GatewaySubmissionUnknownError,
    JsonlWriteAheadLog,
    LiveExecutionConfig,
    LiveExecutionService,
    LiveMode,
    LiveOrderStatus,
    LiveOrderRequest,
    PaperOrderGateway,
    TradingSafetyConfig,
)


class _Gateway:
    def __init__(self) -> None:
        self.requests: list[LiveOrderRequest] = []
        self.cancelled: list[str] = []
        self.cancel_all_calls = 0
        self.heartbeat_ids: list[str] = []
        self.submit_error: Exception | None = None
        self.cancel_error: Exception | None = None

    def submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse:
        self.requests.append(request)
        if self.submit_error is not None:
            raise self.submit_error
        return GatewayOrderResponse(venue_order_id="venue-order", status="live")

    def cancel_order(self, venue_order_id: str) -> None:
        self.cancelled.append(venue_order_id)
        if self.cancel_error is not None:
            raise self.cancel_error

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> None:
        del condition_id, token_id

    def cancel_all(self) -> None:
        self.cancel_all_calls += 1

    def send_heartbeat(self, heartbeat_id: str = "") -> str:
        self.heartbeat_ids.append(heartbeat_id)
        return heartbeat_id or "heartbeat-1"


def _request() -> LiveOrderRequest:
    return LiveOrderRequest(
        condition_id="condition",
        token_id="token",
        price=0.5,
        size=1.0,
        tick_size="0.01",
        neg_risk=False,
    )


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        available_balance=100.0,
        unresolved_notional=0.0,
        daily_realized_pnl=0.0,
        working_markets=0,
        open_orders=0,
        feeds_healthy=True,
        geo_eligible=True,
    )


def test_shadow_mode_never_calls_gateway_and_records_wal(tmp_path: Path) -> None:
    gateway = _Gateway()
    service = LiveExecutionService(
        config=LiveExecutionConfig(mode=LiveMode.SHADOW),
        wal=JsonlWriteAheadLog(tmp_path / "wal.jsonl"),
        gateway=gateway,
    )

    result = service.submit(request=_request(), account=_account(), ts_ns=1)

    assert not result.submitted
    assert result.reason == "shadow_mode"
    assert not gateway.requests
    assert "shadow_order" in (tmp_path / "wal.jsonl").read_text()


def test_canary_requires_risk_enablement_and_submits_only_post_only_gateway_request(
    tmp_path: Path,
) -> None:
    gateway = _Gateway()
    disabled = LiveExecutionService(
        config=LiveExecutionConfig(mode=LiveMode.CANARY),
        wal=JsonlWriteAheadLog(tmp_path / "disabled.jsonl"),
        gateway=gateway,
    )
    assert (
        blocked := disabled.submit(request=_request(), account=_account(), ts_ns=1)
    ).reason == "trading_disabled"
    assert disabled.orders[blocked.client_order_id].status is LiveOrderStatus.REJECTED

    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=JsonlWriteAheadLog(tmp_path / "live.jsonl"),
        gateway=gateway,
    )
    result = service.submit(request=_request(), account=_account(), ts_ns=2)

    assert result.submitted
    assert gateway.requests == [_request()]
    assert service.orders[result.client_order_id].venue_order_id == "venue-order"


def test_paper_mode_accepts_only_the_local_gateway_and_keeps_real_trading_disabled(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="PaperOrderGateway"):
        LiveExecutionService(
            config=LiveExecutionConfig(mode=LiveMode.PAPER),
            wal=JsonlWriteAheadLog(tmp_path / "unsafe-paper.jsonl"),
            gateway=_Gateway(),
        )

    gateway = PaperOrderGateway()
    service = LiveExecutionService(
        config=LiveExecutionConfig(mode=LiveMode.PAPER),
        wal=JsonlWriteAheadLog(tmp_path / "paper.jsonl"),
        gateway=gateway,
    )

    result = service.submit(request=_request(), account=_account(), ts_ns=1)

    assert result.submitted
    assert result.venue_order_id == "paper-1"
    assert gateway.open_orders == {"paper-1": _request()}


def test_kill_switch_blocks_new_submissions_before_attempting_cancellation(tmp_path: Path) -> None:
    gateway = _Gateway()
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=JsonlWriteAheadLog(tmp_path / "kill-switch.jsonl"),
        gateway=gateway,
    )

    assert service.emergency_stop(ts_ns=1, reason="operator_request")
    blocked = service.submit(request=_request(), account=_account(), ts_ns=2)

    assert service.halted
    assert not blocked.submitted
    assert blocked.reason == "kill_switch_active"
    assert gateway.cancel_all_calls == 1
    assert not gateway.requests
    assert not service.emergency_stop(ts_ns=3, reason="operator_request")


def test_user_channel_fill_after_cancel_request_is_preserved_until_cancellation_ack(
    tmp_path: Path,
) -> None:
    gateway = _Gateway()
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=JsonlWriteAheadLog(tmp_path / "wal.jsonl"),
        gateway=gateway,
    )
    result = service.submit(request=_request(), account=_account(), ts_ns=1)
    service.request_cancel(client_order_id=result.client_order_id, ts_ns=2)
    service.reconcile_user_event(
        {
            "event_type": "trade",
            "id": "trade-1",
            "status": "MATCHED",
            "maker_orders": [{"order_id": "venue-order", "matched_amount": "0.5", "price": "0.5"}],
        },
        ts_ns=3,
    )
    service.reconcile_user_event(
        {"event_type": "order", "id": "venue-order", "type": "CANCELLATION", "size_matched": "0.5"},
        ts_ns=4,
    )

    order = service.orders[result.client_order_id]
    assert order.matched_size == pytest.approx(0.5)
    assert order.status.value == "canceled"
    assert gateway.cancelled == ["venue-order"]


def test_user_trade_reconciliation_is_idempotent_and_adds_distinct_partial_fills(
    tmp_path: Path,
) -> None:
    gateway = _Gateway()
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=JsonlWriteAheadLog(tmp_path / "wal.jsonl"),
        gateway=gateway,
    )
    result = service.submit(request=_request(), account=_account(), ts_ns=1)
    first_trade = {
        "event_type": "trade",
        "id": "trade-1",
        "status": "MATCHED",
        "maker_orders": [{"order_id": "venue-order", "matched_amount": "0.25", "price": "0.5"}],
    }

    service.reconcile_user_event(first_trade, ts_ns=2)
    service.reconcile_user_event({**first_trade, "status": "MINED"}, ts_ns=3)
    service.reconcile_user_event(
        {
            "event_type": "trade",
            "id": "trade-2",
            "status": "MATCHED",
            "maker_orders": [{"order_id": "venue-order", "matched_amount": "0.25", "price": "0.5"}],
        },
        ts_ns=4,
    )

    assert service.orders[result.client_order_id].matched_size == pytest.approx(0.5)
    assert service.canary_progress.fills == 2


def test_canary_progress_and_heartbeat_timeout_are_explicit_gates(tmp_path: Path) -> None:
    gateway = _Gateway()
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
            heartbeat_timeout_seconds=5.0,
        ),
        wal=JsonlWriteAheadLog(tmp_path / "wal.jsonl"),
        gateway=gateway,
    )
    service._progress = type(service.canary_progress)(submitted_orders=200, fills=50)
    assert service.canary_progress.ready_for_extended_canary
    assert not service.canary_progress.ready_for_scale_review
    service.record_heartbeat(ts_ns=1)
    assert service.enforce_heartbeat_timeout(now_ts_ns=5_000_000_002)
    assert not service.enforce_heartbeat_timeout(now_ts_ns=5_000_000_003)
    assert gateway.cancel_all_calls == 1


def test_ambiguous_submission_blocks_same_market_until_reconciliation(tmp_path: Path) -> None:
    gateway = _Gateway()
    gateway.submit_error = GatewaySubmissionUnknownError("timeout")
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=JsonlWriteAheadLog(tmp_path / "wal.jsonl"),
        gateway=gateway,
    )

    result = service.submit(request=_request(), account=_account(), ts_ns=1)
    blocked = service.submit(request=_request(), account=_account(), ts_ns=2)

    assert result.reason == "gateway_submission_unknown"
    assert service.orders[result.client_order_id].status is LiveOrderStatus.UNKNOWN
    assert blocked.reason == "ambiguous_submission_pending"

    gateway.submit_error = None
    resolved = service.resolve_submission_unknown(
        client_order_id=result.client_order_id,
        ts_ns=3,
        confirmed_absent=True,
    )
    retried = service.submit(request=_request(), account=_account(), ts_ns=4)

    assert resolved.status is LiveOrderStatus.REJECTED
    assert retried.submitted


def test_cancel_transport_failure_becomes_unknown_and_is_durable(tmp_path: Path) -> None:
    gateway = _Gateway()
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=JsonlWriteAheadLog(tmp_path / "wal.jsonl"),
        gateway=gateway,
    )
    result = service.submit(request=_request(), account=_account(), ts_ns=1)
    gateway.cancel_error = GatewayCancellationUnknownError("timeout")

    service.request_cancel(client_order_id=result.client_order_id, ts_ns=2)
    blocked = service.submit(request=_request(), account=_account(), ts_ns=3)

    assert service.orders[result.client_order_id].status is LiveOrderStatus.UNKNOWN
    assert blocked.reason == "ambiguous_submission_pending"
    assert "cancel_unknown" in (tmp_path / "wal.jsonl").read_text()

    service.reconcile_user_event(
        {
            "event_type": "order",
            "type": "CANCELLATION",
            "id": "venue-order",
            "size_matched": 0,
        },
        ts_ns=4,
    )
    retried = service.submit(request=_request(), account=_account(), ts_ns=5)

    assert service.orders[result.client_order_id].status is LiveOrderStatus.CANCELED
    assert retried.submitted


def test_heartbeat_timeout_halts_new_submissions(tmp_path: Path) -> None:
    gateway = _Gateway()
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=JsonlWriteAheadLog(tmp_path / "wal.jsonl"),
        gateway=gateway,
    )
    service.record_heartbeat(ts_ns=1)

    assert service.enforce_heartbeat_timeout(now_ts_ns=5_000_000_002)
    blocked = service.submit(request=_request(), account=_account(), ts_ns=5_000_000_003)

    assert service.halted
    assert blocked.reason == "kill_switch_active"
    assert gateway.cancel_all_calls == 1


def test_service_sends_real_heartbeat_and_restores_lifecycle_from_wal(tmp_path: Path) -> None:
    gateway = _Gateway()
    wal = JsonlWriteAheadLog(tmp_path / "wal.jsonl")
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=wal,
        gateway=gateway,
    )
    result = service.submit(request=_request(), account=_account(), ts_ns=1)
    assert service.send_venue_heartbeat(ts_ns=2) == "heartbeat-1"

    restored = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=True),
        ),
        wal=wal,
        gateway=gateway,
    )

    assert restored.restore_from_wal() >= 1
    assert restored.orders[result.client_order_id].venue_order_id == "venue-order"
    assert restored.canary_progress.submitted_orders == 1
    assert restored.send_venue_heartbeat(ts_ns=3) == "heartbeat-1"
    assert gateway.heartbeat_ids == ["", "heartbeat-1"]
