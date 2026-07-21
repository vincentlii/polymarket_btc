"""Read-only local HTTP dashboard for persisted BTC runtime state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlparse

from btc_short_horizon.live.dashboard_page import dashboard_html
from btc_short_horizon.live.dashboard_state import BotDashboardSnapshot, DashboardSnapshotStore
from btc_short_horizon.live.runtime import (
    RuntimeControl,
    RuntimeHealth,
    RuntimeStatus,
    RuntimeStatusStore,
    check_runtime_health,
)


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    runtime_root: Path
    health_service: str = "forward_collector"
    max_age_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.health_service.strip():
            raise ValueError("health_service is required")
        if self.max_age_seconds <= 0.0:
            raise ValueError("max_age_seconds must be > 0")


def build_dashboard_payload(
    config: DashboardConfig,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Aggregate validated runtime and performance projections without mutating either."""

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    errors: list[str] = []
    try:
        statuses = RuntimeStatusStore(config.runtime_root).all()
        stop_request = RuntimeControl(config.runtime_root).stop_request()
    except ValueError as exc:
        statuses = ()
        stop_request = None
        errors.append(str(exc))

    try:
        snapshot = DashboardSnapshotStore(config.runtime_root).read()
    except ValueError as exc:
        snapshot = None
        errors.append(str(exc))

    by_service = {status.service: status for status in statuses}
    primary = check_runtime_health(
        by_service.get(config.health_service),
        now=current_time,
        max_age_seconds=config.max_age_seconds,
    )
    if not statuses and errors:
        primary = RuntimeHealth(False, "invalid_runtime_status", primary.age_seconds)
    shadow = _shadow_projection(by_service.get("opening_shadow"), errors)
    snapshot_health = _snapshot_health(
        snapshot,
        now=current_time,
        max_age_seconds=config.max_age_seconds,
    )
    return {
        "generated_at": current_time.isoformat(),
        "health_service": config.health_service,
        "health": {
            "healthy": primary.healthy,
            "reason": primary.reason,
            "age_seconds": primary.age_seconds,
        },
        "statuses": [
            _status_payload(status, current_time, config.max_age_seconds) for status in statuses
        ],
        "snapshot": None if snapshot is None else snapshot.to_json(),
        "snapshot_health": snapshot_health,
        "shadow": shadow,
        "stop_request": (
            None
            if stop_request is None
            else {
                "reason": stop_request.reason,
                "requested_at": stop_request.requested_at.isoformat(),
            }
        ),
        "errors": errors,
    }


def _snapshot_health(
    snapshot: BotDashboardSnapshot | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, object]:
    if snapshot is None:
        return {"healthy": False, "reason": "missing_snapshot", "age_seconds": None}
    generated_at = snapshot.generated_at
    age_seconds = (now - generated_at).total_seconds()
    if age_seconds < -5.0:
        return {
            "healthy": False,
            "reason": "snapshot_timestamp_in_future",
            "age_seconds": age_seconds,
        }
    if age_seconds > max_age_seconds:
        return {"healthy": False, "reason": "stale_snapshot", "age_seconds": age_seconds}
    return {"healthy": True, "reason": "ok", "age_seconds": age_seconds}


def _shadow_projection(
    status: RuntimeStatus | None,
    errors: list[str],
) -> dict[str, object] | None:
    if status is None:
        return None
    value = status.details.get("last_shadow")
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append("opening_shadow last_shadow is not a JSON object")
        return None
    if value.get("orders_submitted") != 0:
        errors.append("opening_shadow must report zero submitted orders")
        return None
    return dict(value)


def create_dashboard_server(
    config: DashboardConfig,
    *,
    host: str,
    port: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ThreadingHTTPServer:
    if not host.strip():
        raise ValueError("host is required")
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                self._send_html(dashboard_html())
                return
            payload = build_dashboard_payload(config, now=now())
            if path == "/api/status":
                self._send_json(payload, status=HTTPStatus.OK)
                return
            if path == "/healthz":
                status = (
                    HTTPStatus.OK
                    if bool(payload["health"]["healthy"])
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self._send_json(payload["health"], status=status)
                return
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            self._send_json({"error": "read_only"}, status=HTTPStatus.METHOD_NOT_ALLOWED)

        def log_message(self, _format: str, *_args: object) -> None:
            """Avoid per-poll request logs; persisted status is the audit surface."""

        def _send_json(self, value: object, *, status: HTTPStatus) -> None:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._send_common_headers("application/json; charset=utf-8", len(encoded))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_html(self, html: str) -> None:
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self._send_common_headers("text/html; charset=utf-8", len(encoded))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(encoded)

        def _send_common_headers(self, content_type: str, content_length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(content_length))

    return ThreadingHTTPServer((host, port), DashboardHandler)


def serve_dashboard(config: DashboardConfig, *, host: str, port: int) -> None:
    server = create_dashboard_server(config, host=host, port=port)
    print(f"BTC runtime dashboard listening on http://{host}:{port}")
    with server:
        server.serve_forever(poll_interval=0.5)


def _status_payload(
    status: RuntimeStatus, now: datetime, max_age_seconds: float
) -> dict[str, object]:
    health = check_runtime_health(status, now=now, max_age_seconds=max_age_seconds)
    return {
        "service": status.service,
        "mode": status.mode,
        "state": status.state,
        "healthy": status.healthy,
        "started_at": status.started_at.isoformat(),
        "updated_at": status.updated_at.isoformat(),
        "details": dict(status.details),
        "health": {
            "healthy": health.healthy,
            "reason": health.reason,
            "age_seconds": health.age_seconds,
        },
    }
