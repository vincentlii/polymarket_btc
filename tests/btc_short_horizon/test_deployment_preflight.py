from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path

from btc_short_horizon.live.deployment import (
    DeploymentPreflightConfig,
    PreflightReportStore,
    run_deployment_preflight,
)


REVISION = "a" * 40


class _Response:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


class _HttpClient:
    def __init__(self, *, blocked: bool = False) -> None:
        self.blocked = blocked
        self.urls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _Response:
        self.urls.append(url)
        if url.endswith("/api/geoblock"):
            return _Response(
                {
                    "blocked": self.blocked,
                    "country": "KR",
                    "region": "11",
                    "ip": "203.0.113.42",
                }
            )
        if "binance" in url:
            return _Response({"serverTime": 1_784_635_200_000})
        if url.endswith("/time"):
            return _Response(1_784_635_200)
        return _Response([{"id": "market"}])


def _clock() -> Callable[[], int]:
    values = iter(1_784_635_200_000_000_000 + offset * 10_000_000 for offset in range(1_000))
    return lambda: next(values)


def _config(tmp_path: Path) -> DeploymentPreflightConfig:
    data_root = tmp_path / "data"
    output_root = tmp_path / "output"
    data_root.mkdir()
    output_root.mkdir()
    return DeploymentPreflightConfig(
        release_revision=REVISION,
        observed_revision=REVISION,
        rule_epoch="btc-15m-current-v1",
        data_root=data_root,
        output_root=output_root,
        minimum_free_bytes=1,
        max_disk_used_percent=100.0,
        latency_samples=3,
        max_endpoint_p99_ms=1_000.0,
        max_clob_clock_offset_ms=1_000.0,
    )


def test_preflight_passes_only_complete_target_checks_and_redacts_ip(tmp_path: Path) -> None:
    report = run_deployment_preflight(
        _config(tmp_path),
        http_client=_HttpClient(),
        ntp_synchronized=True,
        clock_ns=_clock(),
        observed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert report.passed
    assert {item.key for item in report.checks} >= {
        "release_revision",
        "runtime_paths",
        "disk_capacity",
        "ntp_synchronized",
        "geoblock",
        "clob_clock",
        "endpoint_latency",
    }
    assert {item.name for item in report.endpoints} == {
        "clob",
        "gamma",
        "binance_spot",
    }
    payload = json.dumps(report.to_json(), sort_keys=True)
    assert "203.0.113.42" not in payload

    receipt = PreflightReportStore(tmp_path / "runtime").write(report)
    loaded = PreflightReportStore(tmp_path / "runtime").read_latest()
    assert loaded == report
    assert receipt.report_sha256 in receipt.immutable_path.name


def test_preflight_fails_closed_for_blocked_geo_revision_or_ntp(tmp_path: Path) -> None:
    values = _config(tmp_path)
    config = DeploymentPreflightConfig(
        release_revision=values.release_revision,
        observed_revision="b" * 40,
        rule_epoch=values.rule_epoch,
        data_root=values.data_root,
        output_root=values.output_root,
        minimum_free_bytes=values.minimum_free_bytes,
        max_disk_used_percent=values.max_disk_used_percent,
        latency_samples=values.latency_samples,
        max_endpoint_p99_ms=values.max_endpoint_p99_ms,
        max_clob_clock_offset_ms=values.max_clob_clock_offset_ms,
    )

    report = run_deployment_preflight(
        config,
        http_client=_HttpClient(blocked=True),
        ntp_synchronized=False,
        clock_ns=_clock(),
        observed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )

    assert not report.passed
    failures = {item.key for item in report.checks if not item.passed}
    assert {"release_revision", "ntp_synchronized", "geoblock"} <= failures


def test_preflight_store_rejects_tampered_latest_report(tmp_path: Path) -> None:
    store = PreflightReportStore(tmp_path / "runtime")
    report = run_deployment_preflight(
        _config(tmp_path),
        http_client=_HttpClient(),
        ntp_synchronized=True,
        clock_ns=_clock(),
        observed_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    receipt = store.write(report)
    receipt.immutable_path.write_text("{}\n", encoding="utf-8")

    try:
        store.read_latest()
    except ValueError as exc:
        assert "hash" in str(exc)
    else:  # pragma: no cover - assertion carries the expected failure detail.
        raise AssertionError("tampered preflight report was accepted")
