from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from btc_short_horizon.live.runtime import RuntimeControl, RuntimeStatus, RuntimeStatusStore
from scripts.btc_forward_runtime import (
    _effective_market,
    parse_args as parse_forward_runtime_args,
)
from scripts.btc_runtime_control import main as runtime_control_main
from scripts.btc_runtime_dashboard import parse_args as parse_dashboard_args
from scripts.btc_runtime_healthcheck import main as runtime_healthcheck_main


def test_forward_runtime_cli_requires_a_verified_rule_epoch() -> None:
    with pytest.raises(SystemExit):
        parse_forward_runtime_args([])

    args = parse_forward_runtime_args(
        [
            "--rule-epoch",
            "verified-epoch",
            "--binance-futures-public-stream",
            "btcusdt@trade",
        ]
    )

    assert args.rule_epoch == "verified-epoch"
    assert args.status_interval_seconds == 5.0
    assert args.binance_futures_public_stream == ["btcusdt@trade"]


def test_forward_runtime_reports_lookahead_as_active_after_handoff() -> None:
    handoff = datetime(2026, 7, 16, 15, 15, tzinfo=UTC)
    current = SimpleNamespace(slug="current")
    lookahead = SimpleNamespace(slug="lookahead", t0=handoff)
    window = SimpleNamespace(market=current, lookahead=lookahead)

    assert _effective_market(window, now=handoff).slug == "lookahead"


def test_dashboard_cli_defaults_to_loopback_only() -> None:
    args = parse_dashboard_args([])

    assert args.host == "127.0.0.1"
    assert args.port == 8080


def test_runtime_control_cli_writes_and_clears_stop_request(tmp_path) -> None:
    root = str(tmp_path)

    assert runtime_control_main(["--runtime-root", root, "--request-stop", "operator_request"]) == 0
    assert RuntimeControl(tmp_path).stop_request() is not None
    assert runtime_control_main(["--runtime-root", root, "--clear-stop"]) == 0
    assert RuntimeControl(tmp_path).stop_request() is None


def test_runtime_healthcheck_cli_fails_closed_then_accepts_fresh_status(tmp_path) -> None:
    root = str(tmp_path)

    assert runtime_healthcheck_main(["--runtime-root", root]) == 1
    now = datetime.now(UTC)
    RuntimeStatusStore(tmp_path).write(
        RuntimeStatus(
            service="forward_collector",
            mode="forward_collection",
            state="running",
            healthy=True,
            started_at=now,
            updated_at=now,
        )
    )

    assert runtime_healthcheck_main(["--runtime-root", root]) == 0
