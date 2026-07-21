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

        connection.request("POST", "/api/status")
        assert connection.getresponse().status == 405
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()
