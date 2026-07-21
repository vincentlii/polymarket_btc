from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from btc_short_horizon.live import (
    AccountSnapshot,
    GatewayCancelResult,
    GatewayHeartbeatError,
    GatewayMarketPrewarm,
    GatewayOrderResponse,
    GatewayCancellationUnknownError,
    GatewaySubmissionUnknownError,
    JsonlWriteAheadLog,
    LiveExecutionConfig,
    LiveExecutionService,
    LiveMode,
    LiveOrder,
    LiveOrderRequest,
    LiveOrderStatus,
    LiveTradeStatus,
    PaperOrderGateway,
    PreparedPostOnlyOrder,
    RecoveredOrderBinding,
    RecoveredTerminalOrder,
    StartupReconciliationEvidence,
    TerminalVenueOrderStatus,
    TradingSafetyConfig,
    account_snapshot_sha256,
)


RULES_SHA256 = "a" * 64
BASE_TS_NS = 1_000_000_000
CONDITION_A = "0x" + ("a" * 64)
CONDITION_B = "0x" + ("b" * 64)
TOKEN_A = "1"
TOKEN_B = "2"


class _Gateway:
    def __init__(self) -> None:
        self.prewarmed: list[LiveOrderRequest] = []
        self.prepared: list[LiveOrderRequest] = []
        self.submitted_batches: list[tuple[LiveOrderRequest, ...]] = []
        self.cancelled: list[str] = []
        self.cancel_all_calls = 0
        self.heartbeat_ids: list[str] = []
        self.submit_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.cancel_all_error: Exception | None = None
        self.heartbeat_error: Exception | None = None
        self.responses: list[GatewayOrderResponse] = []
        self.cancel_result: GatewayCancelResult | None = None
        self.cancel_all_result: GatewayCancelResult | None = None

    def prewarm_market(self, request: LiveOrderRequest) -> GatewayMarketPrewarm:
        self.prewarmed.append(request)
        return GatewayMarketPrewarm(request.token_id, 2, request.tick_size, request.neg_risk)

    def prepare_post_only_buy(self, request: LiveOrderRequest) -> PreparedPostOnlyOrder:
        self.prepared.append(request)
        return PreparedPostOnlyOrder(
            request,
            object(),
            10,
            f"venue-{request.layer_index}",
        )

    def submit_prepared_post_only_buy(
        self, prepared: PreparedPostOnlyOrder
    ) -> GatewayOrderResponse:
        return self.submit_prepared_post_only_buys((prepared,))[0]

    def submit_prepared_post_only_buys(
        self, prepared: tuple[PreparedPostOnlyOrder, ...]
    ) -> tuple[GatewayOrderResponse, ...]:
        self.submitted_batches.append(tuple(item.request for item in prepared))
        if self.submit_error is not None:
            raise self.submit_error
        if self.responses:
            return tuple(self.responses)
        return tuple(
            GatewayOrderResponse(
                accepted=True,
                status="live",
                venue_order_id=f"venue-{item.request.layer_index}",
                order_build_ns=item.order_build_ns,
                submit_round_trip_ns=20,
            )
            for item in prepared
        )

    def cancel_order(self, venue_order_id: str) -> GatewayCancelResult:
        self.cancelled.append(venue_order_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_result or GatewayCancelResult((venue_order_id,), {})

    def cancel_market(self, condition_id: str, token_id: str | None = None) -> GatewayCancelResult:
        del condition_id, token_id
        return GatewayCancelResult((), {})

    def cancel_all(self) -> GatewayCancelResult:
        self.cancel_all_calls += 1
        if self.cancel_all_error is not None:
            raise self.cancel_all_error
        return self.cancel_all_result or GatewayCancelResult((), {})

    def send_heartbeat(self, heartbeat_id: str = "") -> str:
        self.heartbeat_ids.append(heartbeat_id)
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return heartbeat_id or "heartbeat-1"


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
        "rules_observed_at_ns": BASE_TS_NS,
        "rules_sha256": RULES_SHA256,
        "maker_fee_rate_bps": 0,
    }
    values.update(overrides)
    return LiveOrderRequest(**values)  # type: ignore[arg-type]


def _account(**overrides: object) -> AccountSnapshot:
    values: dict[str, object] = {
        "observed_at_ns": BASE_TS_NS,
        "collateral_balance": 100.0,
        "collateral_allowance": 100.0,
        "account_equity": 100.0,
        "unresolved_position_cost": 0.0,
        "open_order_notional": 0.0,
        "daily_realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_pnl_day": date(1970, 1, 1),
        "working_market_ids": frozenset(),
        "open_orders": 0,
        "feeds_healthy": True,
        "market_channel_healthy": True,
        "user_channel_healthy": True,
        "heartbeat_healthy": True,
        "clock_healthy": True,
        "geo_eligible": True,
        "account_reconciled": True,
    }
    values.update(overrides)
    return AccountSnapshot(**values)  # type: ignore[arg-type]


def _order_event(
    *,
    kind: str = "UPDATE",
    size_matched: str = "0.5",
    venue_order_id: str = "venue-0",
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "event_type": "order",
        "id": venue_order_id,
        "market": CONDITION_A,
        "asset_id": TOKEN_A,
        "side": "BUY",
        "original_size": "1",
        "size_matched": size_matched,
        "price": "0.5",
        "type": kind,
        "status": "LIVE" if kind != "CANCELLATION" else "CANCELED",
    }
    values.update(overrides)
    return values


def _trade_event(
    *,
    status: str = "MATCHED",
    trade_id: str = "trade-1",
    matched_amount: str = "0.5",
    venue_order_id: str = "venue-0",
    **overrides: object,
) -> dict[str, object]:
    values: dict[str, object] = {
        "event_type": "trade",
        "id": trade_id,
        "market": CONDITION_A,
        "asset_id": TOKEN_A,
        "status": status,
        "last_update": "1",
        "match_time": "1",
        "price": "0.5",
        "side": "BUY",
        "size": matched_amount,
        "timestamp": "1",
        "type": "TRADE",
        "maker_orders": [
            {
                "order_id": venue_order_id,
                "matched_amount": matched_amount,
                "price": "0.5",
                "asset_id": TOKEN_A,
            }
        ],
    }
    values.update(overrides)
    return values


def _evidence(
    account: AccountSnapshot,
    *,
    venue_open_order_ids: frozenset[str] = frozenset(),
    recovered_orders: tuple[RecoveredOrderBinding, ...] = (),
    terminal_orders: tuple[RecoveredTerminalOrder, ...] = (),
    unmatched_client_order_ids: frozenset[str] = frozenset(),
) -> StartupReconciliationEvidence:
    return StartupReconciliationEvidence(
        source_started_at_ns=account.observed_at_ns,
        observed_at_ns=account.observed_at_ns,
        expected_open_order_ids=venue_open_order_ids,
        venue_open_order_ids=venue_open_order_ids,
        missing_open_order_ids=frozenset(),
        unexpected_open_order_ids=frozenset(),
        recovered_orders=recovered_orders,
        terminal_orders=terminal_orders,
        unmatched_client_order_ids=unmatched_client_order_ids,
        pending_trade_ids=frozenset(),
        ledger_sha256="b" * 64,
        account_snapshot_sha256=account_snapshot_sha256(account),
        country="KR",
        region="11",
    )


def _config(*, mode: LiveMode = LiveMode.CANARY, **overrides: object) -> LiveExecutionConfig:
    values: dict[str, object] = {
        "mode": mode,
        "risk": TradingSafetyConfig(trading_enabled=True),
        "heartbeat_timeout_seconds": 5.0,
        "canary_max_shares": 1.0,
        "max_rule_age_seconds": 30.0,
        "max_layers_per_cycle": 3,
        "allowed_maker_fee_rate_bps": 0,
    }
    values.update(overrides)
    return LiveExecutionConfig(**values)  # type: ignore[arg-type]


def _service(
    tmp_path: Path,
    *,
    gateway: _Gateway | PaperOrderGateway | None = None,
    mode: LiveMode = LiveMode.CANARY,
    reconcile: bool = True,
    config: LiveExecutionConfig | None = None,
    filename: str = "wal.jsonl",
) -> tuple[LiveExecutionService, _Gateway | PaperOrderGateway]:
    selected_gateway = gateway or _Gateway()
    service = LiveExecutionService(
        config=config or _config(mode=mode),
        wal=JsonlWriteAheadLog(tmp_path / filename),
        gateway=selected_gateway,
    )
    if reconcile and mode is LiveMode.CANARY:
        account = _account()
        service.complete_startup_reconciliation(
            account=account,
            evidence=_evidence(account),
            ts_ns=BASE_TS_NS,
        )
    if mode is not LiveMode.SHADOW:
        service.prewarm_market(_request(), ts_ns=BASE_TS_NS)
    return service, selected_gateway


def test_shadow_mode_never_calls_gateway_and_records_hash_chained_wal(tmp_path: Path) -> None:
    gateway = _Gateway()
    service, _ = _service(tmp_path, gateway=gateway, mode=LiveMode.SHADOW)

    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)

    assert not result.submitted
    assert result.reason == "shadow_mode"
    assert not gateway.prepared
    records = service.wal.read()
    assert records[-1]["event_type"] == "shadow_order"
    assert records[-1]["sequence"] == len(records)


def test_canary_requires_startup_reconciliation_then_explicit_risk_enablement(
    tmp_path: Path,
) -> None:
    gateway = _Gateway()
    unreconciled, _ = _service(tmp_path, gateway=gateway, reconcile=False)
    unreconciled.prewarm_market(_request(), ts_ns=BASE_TS_NS)

    blocked = unreconciled.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    assert blocked.reason == "startup_reconciliation_required"

    disabled = LiveExecutionService(
        config=LiveExecutionConfig(mode=LiveMode.CANARY),
        wal=JsonlWriteAheadLog(tmp_path / "disabled.jsonl"),
        gateway=gateway,
    )
    account = _account()
    disabled.complete_startup_reconciliation(
        account=account,
        evidence=_evidence(account),
        ts_ns=BASE_TS_NS,
    )
    disabled.prewarm_market(_request(), ts_ns=BASE_TS_NS)
    blocked = disabled.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    assert blocked.reason == "trading_disabled"


def test_canary_rejects_stale_rules_fee_changes_and_missing_prewarm(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)

    stale = service.submit(
        request=_request(placement_id="stale", rules_observed_at_ns=1),
        account=_account(),
        ts_ns=BASE_TS_NS + 31_000_000_000,
    )
    fee = service.submit(
        request=_request(placement_id="fee", maker_fee_rate_bps=1),
        account=_account(),
        ts_ns=BASE_TS_NS + 2,
    )
    missing = service.submit(
        request=_request(
            condition_id=CONDITION_B,
            token_id=TOKEN_B,
            placement_id="other",
        ),
        account=_account(),
        ts_ns=BASE_TS_NS + 3,
    )

    assert stale.reason == "market_rules_stale"
    assert fee.reason == "maker_fee_changed"
    assert missing.reason == "market_not_prewarmed"


def test_paper_mode_accepts_only_local_gateway_and_cannot_reach_real_gateway(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="PaperOrderGateway"):
        _service(tmp_path, gateway=_Gateway(), mode=LiveMode.PAPER)

    gateway = PaperOrderGateway()
    service, _ = _service(tmp_path, gateway=gateway, mode=LiveMode.PAPER)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)

    assert result.submitted
    assert result.venue_order_id == "paper-1"
    assert gateway.open_orders == {"paper-1": _request()}


def test_batch_submission_reserves_all_layers_and_handles_mixed_results(tmp_path: Path) -> None:
    gateway = _Gateway()
    gateway.responses = [
        GatewayOrderResponse(True, "live", venue_order_id="venue-0"),
        GatewayOrderResponse(False, "rejected", rejection_reason="post_only_cross"),
    ]
    service, _ = _service(tmp_path, gateway=gateway)
    requests = (
        _request(layer_index=0, price=0.50, size=0.5, minimum_order_size=0.5),
        _request(layer_index=1, price=0.49, size=0.5, minimum_order_size=0.5),
    )

    results = service.submit_many(
        requests=requests,
        account=_account(),
        ts_ns=BASE_TS_NS + 1,
    )

    assert [result.submitted for result in results] == [True, False]
    assert results[1].reason == "post_only_cross"
    assert gateway.submitted_batches == [requests]
    assert service.reserved_notional == pytest.approx(0.25)
    assert service.orders[results[0].client_order_id].status is LiveOrderStatus.LIVE
    assert service.orders[results[1].client_order_id].status is LiveOrderStatus.REJECTED


def test_hot_submission_batches_durable_writes_around_the_network_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _service(tmp_path)
    append_many = service.wal.append_many
    batches: list[tuple[tuple[str, int, object], ...]] = []

    def capture(records: tuple[tuple[str, int, object], ...]) -> None:
        batch = tuple(records)
        batches.append(batch)
        append_many(batch)

    monkeypatch.setattr(service.wal, "append_many", capture)

    result = service.submit(
        request=_request(),
        account=_account(),
        ts_ns=BASE_TS_NS + 1,
    )

    assert result.submitted
    assert [[record[0] for record in batch] for batch in batches] == [
        ["order_decision", "order_signed", "order_submit_started"],
        ["order_live"],
    ]


def test_duplicate_venue_ids_in_batch_response_fail_closed(tmp_path: Path) -> None:
    gateway = _Gateway()
    gateway.responses = [
        GatewayOrderResponse(True, "live", venue_order_id="duplicate-venue-id"),
        GatewayOrderResponse(True, "live", venue_order_id="duplicate-venue-id"),
    ]
    service, _ = _service(tmp_path, gateway=gateway)

    results = service.submit_many(
        requests=(
            _request(layer_index=0, size=0.5, minimum_order_size=0.5),
            _request(layer_index=1, price=0.49, size=0.5, minimum_order_size=0.5),
        ),
        account=_account(),
        ts_ns=BASE_TS_NS + 1,
    )

    assert {result.reason for result in results} == {"gateway_submission_unknown"}
    assert all(
        service.orders[result.client_order_id].status is LiveOrderStatus.UNKNOWN
        for result in results
    )
    assert service.halted
    assert gateway.cancel_all_calls == 1


def test_canary_share_limit_applies_to_the_whole_placement_cycle(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    requests = (
        _request(layer_index=0, size=0.6, minimum_order_size=0.5),
        _request(layer_index=1, size=0.6, minimum_order_size=0.5, price=0.49),
    )

    results = service.submit_many(
        requests=requests,
        account=_account(),
        ts_ns=BASE_TS_NS + 1,
    )

    assert {result.reason for result in results} == {"canary_cycle_size_limit"}
    assert not gateway.submitted_batches


def test_batch_prices_must_be_unique_for_unambiguous_recovery(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)

    with pytest.raises(ValueError, match="unique prices"):
        service.submit_many(
            requests=(
                _request(layer_index=0, size=0.5, minimum_order_size=0.5),
                _request(layer_index=1, size=0.5, minimum_order_size=0.5),
            ),
            account=_account(),
            ts_ns=BASE_TS_NS + 1,
        )


def test_placement_id_is_idempotent_and_market_allows_only_one_network_cycle(
    tmp_path: Path,
) -> None:
    service, gateway = _service(tmp_path)

    first = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    duplicate = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 2)
    second_cycle = service.submit(
        request=_request(placement_id="condition-20-up"),
        account=_account(),
        ts_ns=BASE_TS_NS + 3,
    )

    assert duplicate.client_order_id == first.client_order_id
    assert duplicate.reason == "duplicate_placement"
    assert second_cycle.reason == "market_cycle_already_used"
    assert len(gateway.submitted_batches) == 1


def test_local_reservation_blocks_second_market_before_network_call(tmp_path: Path) -> None:
    config = _config(
        risk=TradingSafetyConfig(
            trading_enabled=True,
            max_working_markets=2,
            max_unresolved_notional=0.75,
            max_balance_fraction=1.0,
        )
    )
    service, gateway = _service(tmp_path, config=config)
    other = _request(
        condition_id=CONDITION_B,
        token_id=TOKEN_B,
        placement_id="other-10-up",
    )
    service.prewarm_market(other, ts_ns=BASE_TS_NS)

    first = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    second = service.submit(request=other, account=_account(), ts_ns=BASE_TS_NS + 2)

    assert first.submitted
    assert second.reason == "unresolved_notional_limit"
    assert len(gateway.submitted_batches) == 1


def test_ambiguous_submission_is_reserved_and_never_automatically_retried(tmp_path: Path) -> None:
    gateway = _Gateway()
    gateway.submit_error = GatewaySubmissionUnknownError("timeout")
    service, _ = _service(tmp_path, gateway=gateway)

    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    duplicate = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 2)

    assert result.reason == "gateway_submission_unknown"
    assert service.orders[result.client_order_id].status is LiveOrderStatus.UNKNOWN
    assert duplicate.reason == "duplicate_placement"
    assert len(gateway.submitted_batches) == 1
    assert service.reserved_notional == pytest.approx(0.5)
    assert service.recovery_required
    assert service.halted
    assert gateway.cancel_all_calls == 1

    assert service.orders[result.client_order_id].status is LiveOrderStatus.UNKNOWN
    assert service.reserved_notional == pytest.approx(0.5)


def test_unknown_submission_binds_to_one_exact_user_placement_event(tmp_path: Path) -> None:
    gateway = _Gateway()
    gateway.submit_error = GatewaySubmissionUnknownError("timeout")
    service, _ = _service(tmp_path, gateway=gateway)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)

    applied = service.reconcile_user_event(
        _order_event(
            kind="PLACEMENT",
            venue_order_id="venue-0",
            size_matched="0",
        ),
        ts_ns=BASE_TS_NS + 2,
    )

    recovered = service.orders[result.client_order_id]
    assert applied
    assert recovered.status is LiveOrderStatus.LIVE
    assert recovered.venue_order_id == "venue-0"
    assert service.halted
    assert service.recovery_required


def test_cancel_receipt_is_applied_and_late_fill_is_preserved(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)

    service.request_cancel(client_order_id=result.client_order_id, ts_ns=BASE_TS_NS + 2)
    service.reconcile_user_event(
        _trade_event(
            owner="api-key-must-not-be-persisted",
        ),
        ts_ns=BASE_TS_NS + 3,
    )
    service.reconcile_user_event(
        _order_event(
            kind="CANCELLATION",
            order_owner="api-key-must-not-be-persisted",
        ),
        ts_ns=BASE_TS_NS + 4,
    )

    order = service.orders[result.client_order_id]
    assert order.matched_size == pytest.approx(0.5)
    assert order.status is LiveOrderStatus.CANCELED
    assert gateway.cancelled == ["venue-0"]
    assert "api-key-must-not-be-persisted" not in service.wal.path.read_text(encoding="utf-8")


def test_order_and_trade_channels_do_not_double_count_and_stale_events_are_ignored(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    service.reconcile_user_event(
        _order_event(),
        ts_ns=BASE_TS_NS + 2,
    )
    trade = _trade_event()
    service.reconcile_user_event(trade, ts_ns=BASE_TS_NS + 3)
    service.reconcile_user_event({**trade, "status": "MINED"}, ts_ns=BASE_TS_NS + 4)
    service.reconcile_user_event({**trade, "status": "CONFIRMED"}, ts_ns=BASE_TS_NS + 5)
    stale_trade_applied = service.reconcile_user_event(trade, ts_ns=BASE_TS_NS + 6)
    stale_order_applied = service.reconcile_user_event(
        _order_event(size_matched="0.4"),
        ts_ns=BASE_TS_NS + 7,
    )

    assert service.orders[result.client_order_id].matched_size == pytest.approx(0.5)
    assert service.canary_progress.fills == 1
    assert stale_trade_applied is False
    assert stale_order_applied is False
    assert not service.halted


def test_conflicting_terminal_trade_event_fails_closed_instead_of_crashing(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    base = _trade_event()
    service.reconcile_user_event({**base, "status": "MATCHED"}, ts_ns=BASE_TS_NS + 2)
    service.reconcile_user_event({**base, "status": "MINED"}, ts_ns=BASE_TS_NS + 3)
    service.reconcile_user_event({**base, "status": "CONFIRMED"}, ts_ns=BASE_TS_NS + 4)

    applied = service.reconcile_user_event({**base, "status": "FAILED"}, ts_ns=BASE_TS_NS + 5)

    assert applied is False
    assert service.halted
    assert service.recovery_required
    assert gateway.cancel_all_calls == 1


def test_older_terminal_trade_event_is_ignored_after_confirmation(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    confirmed = _trade_event(
        status="CONFIRMED",
        match_time="0",
        last_update="1",
        timestamp="1",
    )
    service.reconcile_user_event(confirmed, ts_ns=BASE_TS_NS + 2)

    applied = service.reconcile_user_event(
        {
            **confirmed,
            "status": "FAILED",
            "last_update": "0",
            "timestamp": "0",
        },
        ts_ns=BASE_TS_NS + 3,
    )

    assert not applied
    assert not service.halted


def test_terminal_failed_trade_halts_and_requires_authoritative_recovery(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)

    applied = service.reconcile_user_event(
        _trade_event(status="FAILED"),
        ts_ns=BASE_TS_NS + 2,
    )

    assert applied
    assert service.trades[("trade-1", result.client_order_id)].status is LiveTradeStatus.FAILED
    assert service.halted
    assert service.recovery_required
    assert gateway.cancel_all_calls == 1


def test_cancel_transport_failure_becomes_unknown_and_blocks_market(tmp_path: Path) -> None:
    gateway = _Gateway()
    service, _ = _service(tmp_path, gateway=gateway)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    gateway.cancel_error = GatewayCancellationUnknownError("timeout")

    service.request_cancel(client_order_id=result.client_order_id, ts_ns=BASE_TS_NS + 2)

    assert service.orders[result.client_order_id].status is LiveOrderStatus.UNKNOWN
    assert service.recovery_required
    assert "cancel_unknown" in service.wal.path.read_text(encoding="utf-8")


def test_unexpected_cancel_exception_halts_and_marks_the_order_unknown(tmp_path: Path) -> None:
    gateway = _Gateway()
    service, _ = _service(tmp_path, gateway=gateway)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    gateway.cancel_error = RuntimeError("unexpected client bug")

    service.request_cancel(client_order_id=result.client_order_id, ts_ns=BASE_TS_NS + 2)

    assert service.orders[result.client_order_id].status is LiveOrderStatus.UNKNOWN
    assert service.halted
    assert gateway.cancel_all_calls == 1


def test_cancel_all_applies_every_receipt_row_and_marks_omissions_unknown(
    tmp_path: Path,
) -> None:
    gateway = _Gateway()
    gateway.responses = [
        GatewayOrderResponse(True, "live", venue_order_id="venue-0"),
        GatewayOrderResponse(True, "live", venue_order_id="venue-1"),
    ]
    service, _ = _service(tmp_path, gateway=gateway)
    results = service.submit_many(
        requests=(
            _request(layer_index=0, size=0.5, minimum_order_size=0.5),
            _request(layer_index=1, price=0.49, size=0.5, minimum_order_size=0.5),
        ),
        account=_account(),
        ts_ns=BASE_TS_NS + 1,
    )
    gateway.cancel_all_result = GatewayCancelResult(
        ("venue-0",),
        {"venue-1": "already matched"},
    )

    service.cancel_all(ts_ns=BASE_TS_NS + 2, reason="operator")

    assert service.orders[results[0].client_order_id].status is LiveOrderStatus.CANCELED
    assert service.orders[results[1].client_order_id].status is LiveOrderStatus.NOT_CANCELED
    assert service.recovery_required


def test_cancel_all_marks_receipt_omissions_unknown(tmp_path: Path) -> None:
    gateway = _Gateway()
    gateway.responses = [
        GatewayOrderResponse(True, "live", venue_order_id="venue-0"),
        GatewayOrderResponse(True, "live", venue_order_id="venue-1"),
    ]
    service, _ = _service(tmp_path, gateway=gateway)
    results = service.submit_many(
        requests=(
            _request(layer_index=0, size=0.5, minimum_order_size=0.5),
            _request(layer_index=1, price=0.49, size=0.5, minimum_order_size=0.5),
        ),
        account=_account(),
        ts_ns=BASE_TS_NS + 1,
    )
    gateway.cancel_all_result = GatewayCancelResult(("venue-0",), {})

    service.cancel_all(ts_ns=BASE_TS_NS + 2, reason="operator")

    assert service.orders[results[0].client_order_id].status is LiveOrderStatus.CANCELED
    assert service.orders[results[1].client_order_id].status is LiveOrderStatus.UNKNOWN
    assert service.recovery_required


def test_cancel_all_transport_failure_halts_and_marks_targets_unknown(tmp_path: Path) -> None:
    gateway = _Gateway()
    service, _ = _service(tmp_path, gateway=gateway)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    gateway.cancel_all_error = GatewayCancellationUnknownError("timeout")

    with pytest.raises(GatewayCancellationUnknownError):
        service.cancel_all(ts_ns=BASE_TS_NS + 2, reason="operator")

    assert service.orders[result.client_order_id].status is LiveOrderStatus.UNKNOWN
    assert service.halted
    assert service.recovery_required


def test_unmapped_or_wrong_identity_user_events_fail_closed(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)

    unmapped = service.reconcile_user_event(
        _order_event(venue_order_id="external-order"),
        ts_ns=BASE_TS_NS + 2,
    )

    assert not unmapped
    assert service.halted
    assert service.recovery_required
    assert gateway.cancel_all_calls == 1

    second, second_gateway = _service(tmp_path, filename="identity.jsonl")
    second.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    wrong_asset = second.reconcile_user_event(
        _trade_event(
            asset_id="wrong-token",
            maker_orders=[
                {
                    "order_id": "venue-0",
                    "matched_amount": "0.5",
                    "price": "0.5",
                    "asset_id": "wrong-token",
                    "side": "BUY",
                }
            ],
        ),
        ts_ns=BASE_TS_NS + 2,
    )
    assert not wrong_asset
    assert second.halted
    assert second_gateway.cancel_all_calls == 1


def test_trade_event_may_include_other_makers_but_must_map_one_local_order(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    event = _trade_event(status="CONFIRMED")
    event["maker_orders"] = [
        {
            "order_id": "another-makers-order",
            "matched_amount": "2",
            "price": "0.5",
            "asset_id": TOKEN_A,
            "side": "BUY",
        },
        *event["maker_orders"],  # type: ignore[misc]
    ]

    assert service.reconcile_user_event(event, ts_ns=BASE_TS_NS + 2)
    assert service.orders[result.client_order_id].matched_size == pytest.approx(0.5)
    assert service.canary_progress.fills == 1


def test_heartbeat_failure_and_timeout_both_halt_before_cancelling(tmp_path: Path) -> None:
    gateway = _Gateway()
    service, _ = _service(tmp_path, gateway=gateway)
    gateway.heartbeat_error = GatewayHeartbeatError("network")

    with pytest.raises(GatewayHeartbeatError):
        service.send_venue_heartbeat(ts_ns=BASE_TS_NS + 1)

    assert service.halted
    assert gateway.cancel_all_calls == 1

    second, second_gateway = _service(tmp_path, filename="timeout.jsonl")
    second.record_heartbeat(ts_ns=BASE_TS_NS)
    assert second.enforce_heartbeat_timeout(now_ts_ns=BASE_TS_NS + 5_000_000_001)
    assert second.halted
    assert second_gateway.cancel_all_calls == 1


def test_successful_heartbeats_do_not_grow_the_trading_wal(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    before = len(service.wal.read())

    service.send_venue_heartbeat(ts_ns=BASE_TS_NS + 1)
    service.send_venue_heartbeat(ts_ns=BASE_TS_NS + 2)

    assert len(service.wal.read()) == before


def test_service_restores_idempotency_and_requires_fresh_startup_reconciliation(
    tmp_path: Path,
) -> None:
    gateway = _Gateway()
    wal = JsonlWriteAheadLog(tmp_path / "wal.jsonl")
    service = LiveExecutionService(config=_config(), wal=wal, gateway=gateway)
    initial_account = _account()
    service.complete_startup_reconciliation(
        account=initial_account,
        evidence=_evidence(initial_account),
        ts_ns=BASE_TS_NS,
    )
    service.prewarm_market(_request(), ts_ns=BASE_TS_NS)
    result = service.submit(request=_request(), account=_account(), ts_ns=BASE_TS_NS + 1)
    assert service.send_venue_heartbeat(ts_ns=BASE_TS_NS + 2) == "heartbeat-1"

    restored = LiveExecutionService(config=_config(), wal=wal, gateway=gateway)
    assert restored.restore_from_wal() >= 1
    assert not restored.startup_reconciled
    assert restored.orders[result.client_order_id].venue_order_id == "venue-0"
    restored_account = _account(
        open_orders=1,
        open_order_notional=0.5,
        working_market_ids=frozenset({CONDITION_A}),
    )
    restored.complete_startup_reconciliation(
        account=restored_account,
        evidence=_evidence(
            restored_account,
            venue_open_order_ids=frozenset({"venue-0"}),
        ),
        ts_ns=BASE_TS_NS + 3,
    )
    restored.prewarm_market(_request(), ts_ns=BASE_TS_NS + 3)

    duplicate = restored.submit(
        request=_request(),
        account=_account(
            open_orders=1,
            open_order_notional=0.5,
            working_market_ids=frozenset({CONDITION_A}),
        ),
        ts_ns=BASE_TS_NS + 4,
    )
    assert duplicate.client_order_id == result.client_order_id
    assert duplicate.reason == "duplicate_placement"
    assert restored.send_venue_heartbeat(ts_ns=BASE_TS_NS + 5) == "heartbeat-1"
    assert gateway.heartbeat_ids == ["", ""]


def test_startup_recovers_a_posted_order_when_the_process_dies_before_response(
    tmp_path: Path,
) -> None:
    gateway = _Gateway()
    wal = JsonlWriteAheadLog(tmp_path / "response-lost.jsonl")
    service = LiveExecutionService(config=_config(), wal=wal, gateway=gateway)
    initial_account = _account()
    service.complete_startup_reconciliation(
        account=initial_account,
        evidence=_evidence(initial_account),
        ts_ns=BASE_TS_NS,
    )
    service.prewarm_market(_request(), ts_ns=BASE_TS_NS)
    gateway.submit_error = SystemExit("simulated process death")  # type: ignore[assignment]

    with pytest.raises(SystemExit, match="simulated process death"):
        service.submit(request=_request(), account=initial_account, ts_ns=BASE_TS_NS + 1)

    restored = LiveExecutionService(config=_config(), wal=wal, gateway=gateway)
    restored.restore_from_wal()
    expectations = restored.startup_order_expectations
    assert len(expectations) == 1
    assert expectations[0].venue_order_id == "venue-0"
    assert restored.orders[expectations[0].client_order_id].status is LiveOrderStatus.SUBMITTED

    account = _account(
        observed_at_ns=BASE_TS_NS + 2,
        open_orders=1,
        open_order_notional=0.5,
        working_market_ids=frozenset({CONDITION_A}),
    )
    restored.complete_startup_reconciliation(
        account=account,
        evidence=_evidence(
            account,
            venue_open_order_ids=frozenset({"venue-0"}),
        ),
        ts_ns=BASE_TS_NS + 3,
    )

    recovered = restored.orders[expectations[0].client_order_id]
    assert recovered.status is LiveOrderStatus.LIVE
    assert recovered.venue_order_id == "venue-0"
    assert restored.startup_reconciled
    assert not restored.recovery_required
    assert "startup_order_recovered" in wal.path.read_text(encoding="utf-8")

    restored.prewarm_market(_request(), ts_ns=BASE_TS_NS + 3)
    blocked = restored.submit(
        request=_request(placement_id="second-cycle"),
        account=account,
        ts_ns=BASE_TS_NS + 4,
    )
    assert blocked.reason == "market_cycle_already_used"


def test_startup_rejects_a_wal_order_proven_to_be_pre_network(tmp_path: Path) -> None:
    wal = JsonlWriteAheadLog(tmp_path / "pre-network.jsonl")
    order = LiveOrder(
        client_order_id="local-pre-network",
        market_id=CONDITION_A,
        token_id=TOKEN_A,
        price=0.5,
        size=1.0,
    )
    wal.append(
        event_type="order_decision",
        ts_ns=BASE_TS_NS,
        payload={"order": order, "placement_id": "pre-network", "layer_index": 0},
    )
    service = LiveExecutionService(config=_config(), wal=wal, gateway=_Gateway())
    service.restore_from_wal()
    account = _account()

    service.complete_startup_reconciliation(
        account=account,
        evidence=_evidence(account),
        ts_ns=BASE_TS_NS + 1,
    )

    assert service.orders[order.client_order_id].status is LiveOrderStatus.REJECTED
    assert "startup_pre_network_order_rejected" in wal.path.read_text(encoding="utf-8")


def test_startup_recovers_a_prehashed_order_that_filled_while_the_process_was_down(
    tmp_path: Path,
) -> None:
    wal = JsonlWriteAheadLog(tmp_path / "terminal-order.jsonl")
    order = LiveOrder(
        client_order_id="local-terminal",
        market_id=CONDITION_A,
        token_id=TOKEN_A,
        price=0.5,
        size=1.0,
        status=LiveOrderStatus.SUBMITTED,
        expected_venue_order_id="venue-terminal",
    )
    wal.append(
        event_type="order_submit_started",
        ts_ns=BASE_TS_NS,
        payload={"order": order, "placement_id": "terminal", "layer_index": 0},
    )
    service = LiveExecutionService(config=_config(), wal=wal, gateway=_Gateway())
    service.restore_from_wal()
    account = _account()
    terminal = RecoveredTerminalOrder(
        client_order_id=order.client_order_id,
        venue_order_id="venue-terminal",
        market_id=CONDITION_A,
        token_id=TOKEN_A,
        price=0.5,
        original_size=1.0,
        matched_size=1.0,
        status=TerminalVenueOrderStatus.MATCHED,
    )

    service.complete_startup_reconciliation(
        account=account,
        evidence=_evidence(account, terminal_orders=(terminal,)),
        ts_ns=BASE_TS_NS + 1,
    )

    recovered = service.orders[order.client_order_id]
    assert recovered.status is LiveOrderStatus.FILLED
    assert recovered.venue_order_id == "venue-terminal"
    assert recovered.matched_size == pytest.approx(1.0)
    assert service.startup_reconciled
    assert "startup_terminal_order_recovered" in wal.path.read_text(encoding="utf-8")


def test_restore_counts_only_confirmed_trade_fills(tmp_path: Path) -> None:
    gateway = _Gateway()
    wal = JsonlWriteAheadLog(tmp_path / "confirmed.jsonl")
    service = LiveExecutionService(config=_config(), wal=wal, gateway=gateway)
    account = _account()
    service.complete_startup_reconciliation(
        account=account,
        evidence=_evidence(account),
        ts_ns=BASE_TS_NS,
    )
    service.prewarm_market(_request(), ts_ns=BASE_TS_NS)
    service.submit(request=_request(), account=account, ts_ns=BASE_TS_NS + 1)
    service.reconcile_user_event(
        _trade_event(status="CONFIRMED"),
        ts_ns=BASE_TS_NS + 2,
    )

    restored = LiveExecutionService(config=_config(), wal=wal, gateway=gateway)
    restored.restore_from_wal()

    assert restored.canary_progress.fills == 1


def test_startup_reconciliation_rejects_excess_future_clock_skew(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, reconcile=False)
    account = _account(observed_at_ns=BASE_TS_NS + 300_000_000)

    with pytest.raises(ValueError, match="future-dated"):
        service.complete_startup_reconciliation(
            account=account,
            evidence=_evidence(account),
            ts_ns=BASE_TS_NS,
        )


def test_startup_reconciliation_rejects_forged_or_incomplete_evidence(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path, reconcile=False)
    account = _account()
    incomplete = replace(
        _evidence(account),
        pending_trade_ids=frozenset({"trade-not-in-ledger"}),
    )

    with pytest.raises(ValueError, match="not complete"):
        service.complete_startup_reconciliation(
            account=account,
            evidence=incomplete,
            ts_ns=BASE_TS_NS,
        )
    with pytest.raises(ValueError, match="does not match"):
        service.complete_startup_reconciliation(
            account=account,
            evidence=replace(_evidence(account), account_snapshot_sha256="c" * 64),
            ts_ns=BASE_TS_NS,
        )


def test_live_config_rejects_boolean_limits_and_impossible_layer_count() -> None:
    with pytest.raises(ValueError):
        LiveExecutionConfig(canary_max_shares=True)
    with pytest.raises(ValueError):
        LiveExecutionConfig(max_layers_per_cycle=16)
