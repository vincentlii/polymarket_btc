from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
import json
from threading import Thread

from btc_short_horizon.live.dashboard import (
    DashboardConfig,
    build_dashboard_payload,
    create_dashboard_server,
)
from btc_short_horizon.live.dashboard_page import dashboard_html
from btc_short_horizon.live.dashboard_state import (
    BotDashboardSnapshot,
    DashboardSnapshotStore,
    GateState,
    HealthIndicator,
    HealthState,
    PerformanceSnapshot,
    StrategyCycle,
    StrategyStage,
)
from btc_short_horizon.live.runtime import RuntimeControl, RuntimeStatus, RuntimeStatusStore


def _now() -> datetime:
    return datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _dashboard_snapshot() -> BotDashboardSnapshot:
    return BotDashboardSnapshot(
        generated_at=_now(),
        run_mode="paper",
        strategy=StrategyCycle(
            stage=StrategyStage.CHALLENGE,
            gate_state=GateState.RUNNING,
            next_action="完成挑战窗口后人工评审。",
            progress_label="已结算市场",
            progress_current=1_152,
            progress_target=2_500,
        ),
        performance=PerformanceSnapshot(equity=1_024.5),
        health=(
            HealthIndicator(
                key="clob-market-ws",
                label="Polymarket Market WS",
                state=HealthState.OK,
                detail="订单簿更新正常",
                updated_at=_now(),
                latency_ms=41.0,
            ),
            HealthIndicator(
                key="order-api",
                label="Order API P95",
                state=HealthState.WARNING,
                detail="延迟高于近期基线",
                updated_at=_now(),
                latency_ms=182.0,
            ),
        ),
    )


def _paper_record(
    placement_id: str,
    placed_at_ns: int,
    *,
    schema_version: int,
    variant_id: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "placement_id": placement_id,
        "market_slug": f"btc-updown-15m-{placed_at_ns}",
        "token_id": "private-token-id-must-not-leak",
        "side": "up",
        "placed_at_ns": placed_at_ns,
        "shares": 4.0,
        "filled_shares": 2.0,
        "filled_notional": 0.84,
        "planned_notional": 1.68,
        "p_fair": 0.61,
        "market_price": 0.42,
        "cancel_race_filled_shares": 0.0,
        "outcome": None,
        "settled_at_ns": None,
        "realized_pnl": None,
    }
    if schema_version == 2:
        record["status"] = "working"
        return record
    record.update(
        {
            "variant_id": variant_id,
            "execution_status": "working",
            "settlement_status": "pending",
            "terminal_reason": None,
            "execution_route": "maker",
            "maker_filled_shares": 2.0,
            "taker_filled_shares": 0.0,
            "maker_filled_notional": 0.84,
            "taker_filled_notional": 0.0,
            "taker_fees": 0.0,
            "initial_queue_ahead": 5.0,
            "remaining_queue_ahead": 2.0,
            "raw_eligible_sell_volume": 8.0,
            "stressed_eligible_sell_volume": 6.0,
            "active_at_ns": placed_at_ns + 50_000_000,
            "cancel_requested_at_ns": None,
            "cancel_ack_at_ns": None,
            "terminal_at_ns": None,
            "fak_limit_price": None,
            "fak_net_edge_per_share": None,
        }
    )
    if schema_version == 4:
        record.update(
            {
                "opportunity_id": f"opportunity-{placement_id}",
                "entry_regime": "3-30s",
                "price_bucket": "0.40-0.50",
                "go_eligible": True,
                "decision_best_ask": 0.43,
                "signal_observations": [
                    {
                        "signal_number": 1,
                        "observed_at_ns": placed_at_ns - 1_000_000,
                        "p_fair": 0.61,
                        "maker_price": 0.42,
                        "executable_vwap": 0.43,
                        "taker_fee_per_share": 0.001,
                        "taker_net_edge": 0.179,
                        "private_note": "must-not-cross-dashboard-boundary",
                    }
                ],
            }
        )
    return record


def _write_ledger(path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_dashboard_payload_reports_service_health_and_stop_request(tmp_path) -> None:
    config = DashboardConfig(runtime_root=tmp_path, max_age_seconds=30)
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="shadow",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=1),
            updated_at=_now() - timedelta(seconds=2),
            details={"current_market": "btc-updown-15m-1"},
        )
    )
    RuntimeControl(tmp_path).request_stop(reason="operator_request", requested_at=_now())

    payload = build_dashboard_payload(config, now=_now())

    assert payload["health"] == {"healthy": True, "reason": "ok", "age_seconds": 2.0}
    assert payload["stop_request"] == {
        "reason": "operator_request",
        "requested_at": _now().isoformat(),
    }
    assert payload["statuses"] == [
        {
            "service": "forward_collector",
            "mode": "shadow",
            "state": "running",
            "healthy": True,
            "started_at": (_now() - timedelta(minutes=1)).isoformat(),
            "updated_at": (_now() - timedelta(seconds=2)).isoformat(),
            "details": {"current_market": "btc-updown-15m-1"},
            "health": {"healthy": True, "reason": "ok", "age_seconds": 2.0},
        }
    ]


def test_dashboard_html_labels_research_paper_as_simulated_not_account_truth() -> None:
    html = dashboard_html()

    assert "Research Paper 模拟账本新鲜" in html
    assert "Simulated ledger" in html
    assert "三种成交策略对比" in html
    assert "variant_summaries" in html
    assert "recent_orders" in html
    assert "recent_trades" not in html
    assert "research_paper:'Research Paper'" in html


def test_dashboard_page_explains_recent_limit_and_loads_paginated_history() -> None:
    html = dashboard_html()

    assert "仅展示最近 15 条" in html
    assert "查看完整历史" in html
    assert "主策略本进程决策漏斗" in html
    assert "EV / 机会" in html
    assert "decision_funnel" in html
    assert "/api/orders" in html
    assert "history-variant" in html


def test_dashboard_payload_fails_closed_when_status_is_stale(tmp_path) -> None:
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="shadow",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=1),
            updated_at=_now() - timedelta(seconds=31),
        )
    )

    payload = build_dashboard_payload(DashboardConfig(runtime_root=tmp_path), now=_now())

    assert payload["health"] == {"healthy": False, "reason": "stale_status", "age_seconds": 31.0}


def test_dashboard_uses_service_declared_publish_interval_for_freshness(tmp_path) -> None:
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="forward_collection",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=2),
            updated_at=_now(),
        )
    )
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="opening_shadow",
            mode="post_window_shadow",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=2),
            updated_at=_now() - timedelta(seconds=61),
            details={"expected_status_interval_seconds": 60.0},
        )
    )

    payload = build_dashboard_payload(DashboardConfig(runtime_root=tmp_path), now=_now())
    by_service = {item["service"]: item for item in payload["statuses"]}

    assert by_service["opening_shadow"]["health"] == {
        "healthy": True,
        "reason": "ok",
        "age_seconds": 61.0,
    }


def test_dashboard_payload_includes_validated_performance_and_lifecycle_snapshot(tmp_path) -> None:
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="forward_collection",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=1),
            updated_at=_now(),
        )
    )
    DashboardSnapshotStore(tmp_path).write(_dashboard_snapshot())

    payload = build_dashboard_payload(DashboardConfig(runtime_root=tmp_path), now=_now())

    assert payload["snapshot"]["performance"]["equity"] == 1_024.5
    assert payload["snapshot"]["strategy"]["stage"] == "challenge"
    assert payload["snapshot"]["health"][1]["latency_ms"] == 182.0
    assert payload["snapshot_health"] == {
        "healthy": True,
        "reason": "ok",
        "age_seconds": 0.0,
    }


def test_dashboard_marks_stale_performance_projection_without_hiding_values(tmp_path) -> None:
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="forward_collection",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=2),
            updated_at=_now(),
        )
    )
    DashboardSnapshotStore(tmp_path).write(
        replace(_dashboard_snapshot(), generated_at=_now() - timedelta(seconds=31))
    )

    payload = build_dashboard_payload(DashboardConfig(runtime_root=tmp_path), now=_now())

    assert payload["snapshot"] is not None
    assert payload["snapshot_health"] == {
        "healthy": False,
        "reason": "stale_snapshot",
        "age_seconds": 31.0,
    }


def test_dashboard_projects_shadow_evidence_without_fabricating_performance(tmp_path) -> None:
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="forward_collection",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=1),
            updated_at=_now(),
        )
    )
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="opening_shadow",
            mode="post_window_shadow",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=1),
            updated_at=_now(),
            details={
                "last_shadow": {
                    "market_slug": "btc-updown-15m-1",
                    "evidence_type": "post_window_causal_shadow",
                    "orders_submitted": 0,
                    "coverage": {
                        "requested_decisions": 36,
                        "predictions": 36,
                        "quality_eligible": 34,
                    },
                }
            },
        )
    )

    payload = build_dashboard_payload(DashboardConfig(runtime_root=tmp_path), now=_now())

    assert payload["snapshot"] is None
    assert payload["shadow"] == {
        "market_slug": "btc-updown-15m-1",
        "evidence_type": "post_window_causal_shadow",
        "orders_submitted": 0,
        "coverage": {
            "requested_decisions": 36,
            "predictions": 36,
            "quality_eligible": 34,
        },
    }


def test_dashboard_payload_reports_corrupt_projection_without_hiding_runtime_health(
    tmp_path,
) -> None:
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="forward_collection",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=1),
            updated_at=_now(),
        )
    )
    projection_path = tmp_path / "dashboard" / "snapshot.json"
    projection_path.parent.mkdir(parents=True)
    projection_path.write_text("{not-json", encoding="utf-8")

    payload = build_dashboard_payload(DashboardConfig(runtime_root=tmp_path), now=_now())

    assert payload["health"]["healthy"] is True
    assert payload["snapshot"] is None
    assert payload["errors"] == [f"invalid dashboard snapshot JSON: {projection_path}"]


def test_dashboard_page_prioritizes_health_performance_and_lifecycle_without_raw_json() -> None:
    page = dashboard_html()

    assert "Bot 健康与延迟" in page
    assert "资金曲线" in page
    assert "最近订单与逐单盈亏" in page
    assert "策略生命周期" in page
    assert "performance-projection" in page
    assert "JSON.stringify(value,null,2)" not in page


def test_dashboard_http_surface_is_read_only_and_serves_health(tmp_path) -> None:
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="forward_collection",
            state="running",
            healthy=True,
            started_at=_now() - timedelta(minutes=1),
            updated_at=_now(),
        )
    )
    server = create_dashboard_server(
        DashboardConfig(runtime_root=tmp_path),
        host="127.0.0.1",
        port=0,
        now=_now,
    )
    worker = Thread(target=server.serve_forever)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"age_seconds": 0.0, "healthy": True, "reason": "ok"}

        connection.request("GET", "/")
        page_response = connection.getresponse()
        assert page_response.status == 200
        assert "BTC Bot Control Room" in page_response.read().decode("utf-8")

        connection.request("GET", "/api/status")
        status_response = connection.getresponse()
        assert status_response.status == 200
        assert json.loads(status_response.read())["health"]["healthy"] is True

        connection.request("POST", "/api/status")
        assert connection.getresponse().status == 405
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


def test_dashboard_order_history_paginates_filters_and_redacts_ledgers(tmp_path) -> None:
    base_ns = int(_now().timestamp() * 1_000_000_000)
    _write_ledger(
        tmp_path / "paper" / "ledger.json",
        {
            "schema_version": 2,
            "starting_balance": 100.0,
            "records": [_paper_record("legacy-order", base_ns, schema_version=2)],
        },
    )
    _write_ledger(
        tmp_path / "paper" / "variants" / "maker_15s" / "ledger.json",
        {
            "schema_version": 3,
            "variant_id": "maker_15s",
            "starting_balance": 100.0,
            "records": [
                _paper_record(
                    "schema3-order",
                    base_ns + 1_000_000_000,
                    schema_version=3,
                    variant_id="maker_15s",
                )
            ],
        },
    )
    _write_ledger(
        tmp_path
        / "paper"
        / "epochs"
        / "paper-v2-direct-fak"
        / "variants"
        / "maker_15s"
        / "ledger.json",
        {
            "schema_version": 4,
            "execution_epoch": "paper-v2-direct-fak",
            "variant_id": "maker_15s",
            "starting_balance": 100.0,
            "records": [
                _paper_record(
                    "schema4-maker",
                    base_ns + 3_000_000_000,
                    schema_version=4,
                    variant_id="maker_15s",
                )
            ],
        },
    )
    _write_ledger(
        tmp_path
        / "paper"
        / "epochs"
        / "paper-v2-direct-fak"
        / "variants"
        / "direct_fak"
        / "ledger.json",
        {
            "schema_version": 4,
            "execution_epoch": "paper-v2-direct-fak",
            "variant_id": "direct_fak",
            "starting_balance": 100.0,
            "records": [
                _paper_record(
                    "schema4-taker",
                    base_ns + 2_000_000_000,
                    schema_version=4,
                    variant_id="direct_fak",
                )
            ],
        },
    )
    server = create_dashboard_server(
        DashboardConfig(runtime_root=tmp_path),
        host="127.0.0.1",
        port=0,
        now=_now,
    )
    worker = Thread(target=server.serve_forever)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/api/orders?limit=2")
        first_response = connection.getresponse()
        first = json.loads(first_response.read())

        assert first_response.status == 200
        assert [item["order_id"] for item in first["items"]] == [
            "schema4-maker",
            "schema4-taker",
        ]
        assert first["has_more"] is True
        assert first["total_records"] == 4
        assert first["available_variants"] == ["direct_fak", "legacy_paper", "maker_15s"]
        assert all("token_id" not in item for item in first["items"])
        assert first["items"][0]["execution_epoch"] == "paper-v2-direct-fak"
        assert first["items"][0]["opportunity_id"] == "opportunity-schema4-maker"
        assert first["items"][0]["entry_regime"] == "3-30s"
        assert first["items"][0]["price_bucket"] == "0.40-0.50"
        assert first["items"][0]["go_eligible"] is True
        assert first["items"][0]["decision_best_ask"] == 0.43
        assert first["items"][0]["signal_observations"][0] == {
            "signal_number": 1,
            "observed_at_ns": base_ns + 2_999_000_000,
            "p_fair": 0.61,
            "maker_price": 0.42,
            "executable_vwap": 0.43,
            "taker_fee_per_share": 0.001,
            "taker_net_edge": 0.179,
        }

        connection.request("GET", f"/api/orders?limit=2&cursor={first['next_cursor']}")
        second_response = connection.getresponse()
        second = json.loads(second_response.read())
        assert second_response.status == 200
        assert [item["order_id"] for item in second["items"]] == [
            "schema3-order",
            "legacy-order",
        ]
        assert second["has_more"] is False
        legacy = second["items"][1]
        assert legacy["execution_epoch"] == "legacy_schema2"
        assert legacy["settlement_status"] is None
        assert legacy["execution_route"] is None
        assert legacy["taker_fees"] is None
        assert legacy["opportunity_id"] is None
        assert legacy["signal_observations"] == []

        connection.request("GET", "/api/orders?limit=10&variant=maker_15s")
        filtered_response = connection.getresponse()
        filtered = json.loads(filtered_response.read())
        assert filtered_response.status == 200
        assert [item["order_id"] for item in filtered["items"]] == [
            "schema4-maker",
            "schema3-order",
        ]

        connection.request("GET", "/api/orders?limit=101")
        invalid_limit = connection.getresponse()
        assert invalid_limit.status == 400
        assert json.loads(invalid_limit.read())["error"] == "invalid_request"

        connection.request("GET", "/api/orders?cursor=not-base64!")
        invalid_cursor = connection.getresponse()
        assert invalid_cursor.status == 400
        assert json.loads(invalid_cursor.read())["error"] == "invalid_request"
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


def test_dashboard_order_history_fails_closed_without_exposing_paths(tmp_path) -> None:
    base_ns = int(_now().timestamp() * 1_000_000_000)
    _write_ledger(
        tmp_path / "paper" / "variants" / "maker_15s" / "ledger.json",
        {
            "schema_version": 3,
            "variant_id": "maker_15s",
            "starting_balance": 100.0,
            "records": [
                _paper_record(
                    "valid-order",
                    base_ns,
                    schema_version=3,
                    variant_id="maker_15s",
                )
            ],
        },
    )
    _write_ledger(
        tmp_path / "paper" / "epochs" / "bad-epoch" / "variants" / "bad" / "ledger.json",
        {
            "schema_version": 99,
            "starting_balance": 100.0,
            "records": [],
        },
    )
    server = create_dashboard_server(
        DashboardConfig(runtime_root=tmp_path),
        host="127.0.0.1",
        port=0,
        now=_now,
    )
    worker = Thread(target=server.serve_forever)
    worker.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/api/orders")
        response = connection.getresponse()
        raw_body = response.read().decode("utf-8")
        payload = json.loads(raw_body)

        assert response.status == 500
        assert payload == {
            "error": "invalid_order_history",
            "message": "订单历史账本无效，已拒绝返回不完整结果。",
        }
        assert str(tmp_path) not in raw_body
        assert "valid-order" not in raw_body
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()
