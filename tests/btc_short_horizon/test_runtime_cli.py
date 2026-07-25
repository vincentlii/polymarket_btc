from __future__ import annotations

from datetime import UTC, datetime
import time
from types import SimpleNamespace

import pytest

from btc_short_horizon.live.runtime import RuntimeControl, RuntimeStatus, RuntimeStatusStore
from scripts.btc_forward_runtime import (
    _captured_market,
    _effective_market,
    parse_args as parse_forward_runtime_args,
)
from scripts.btc_runtime_control import main as runtime_control_main
from scripts.btc_runtime_dashboard import parse_args as parse_dashboard_args
from scripts.btc_runtime_healthcheck import main as runtime_healthcheck_main
from scripts.btc_vps_preflight import main as vps_preflight_main
from scripts.btc_vps_preflight import _git_revision


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


def test_forward_runtime_only_requires_clob_during_capture_window() -> None:
    t0 = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)
    current = SimpleNamespace(slug="current", t0=t0)
    lookahead = SimpleNamespace(slug="lookahead", t0=t0.replace(minute=15))
    window = SimpleNamespace(market=current, lookahead=lookahead)

    assert (
        _captured_market(
            window,
            now=t0.replace(minute=14),
            lead_seconds=90.0,
            handoff_seconds=180.0,
        ).slug
        == "lookahead"
    )
    assert (
        _captured_market(
            window,
            now=t0.replace(minute=8),
            lead_seconds=90.0,
            handoff_seconds=180.0,
        )
        is None
    )


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


def test_vps_preflight_cli_persists_target_host_evidence(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    output_root = tmp_path / "output"
    runtime_root = tmp_path / "runtime"
    data_root.mkdir()
    output_root.mkdir()

    class Response:
        def __init__(self, payload: object) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return self.payload

    class Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, url: str, **_kwargs: object) -> Response:
            if url.endswith("/api/geoblock"):
                return Response({"blocked": False, "country": "KR", "region": "11", "ip": "secret"})
            if "binance" in url:
                return Response({"serverTime": int(time.time() * 1_000)})
            if url.endswith("/time"):
                return Response(time.time())
            return Response([{"id": "market"}])

    revision = "a" * 40
    monkeypatch.setattr("scripts.btc_vps_preflight.httpx.Client", Client)
    monkeypatch.setattr("scripts.btc_vps_preflight._git_revision", lambda: revision)
    monkeypatch.setattr("scripts.btc_vps_preflight._host_ntp_synchronized", lambda: True)

    result = vps_preflight_main(
        [
            "--code-revision",
            revision,
            "--rule-epoch",
            "btc-15m-current-v1",
            "--data-root",
            str(data_root),
            "--output-root",
            str(output_root),
            "--runtime-root",
            str(runtime_root),
            "--minimum-free-gib",
            "0.000000001",
            "--latency-samples",
            "1",
        ]
    )

    assert result == 0
    assert (runtime_root / "preflight" / "latest.json").is_file()


def test_vps_preflight_rejects_dirty_tracked_release(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(tuple(command))
        if command[1] == "rev-parse":
            return SimpleNamespace(stdout="a" * 40 + "\n")
        return SimpleNamespace(stdout=" M btc_short_horizon/live/service.py\n")

    monkeypatch.setattr("scripts.btc_vps_preflight.subprocess.run", run)

    with pytest.raises(RuntimeError, match="tracked changes"):
        _git_revision()

    assert calls == [
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain", "--untracked-files=no"),
    ]
