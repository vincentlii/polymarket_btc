from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btc_short_horizon.live.runtime import (
    RuntimeControl,
    RuntimeStatus,
    RuntimeStatusStore,
    build_runtime_identity,
    check_runtime_health,
    filesystem_usage,
)


def _timestamp() -> datetime:
    return datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _status(*, updated_at: datetime | None = None, **changes: object) -> RuntimeStatus:
    status_updated_at = updated_at or _timestamp()
    values: dict[str, object] = {
        "service": "forward_collector",
        "mode": "shadow",
        "state": "running",
        "healthy": True,
        "started_at": min(_timestamp(), status_updated_at),
        "updated_at": status_updated_at,
        "details": {"market_slug": "btc-updown-15m-1", "events_written": 12},
    }
    values.update(changes)
    return RuntimeStatus(**values)  # type: ignore[arg-type]


def test_status_store_round_trips_sorted_statuses(tmp_path) -> None:
    store = RuntimeStatusStore(tmp_path)
    written = store.write(_status(service="forward_collector"))
    store.write(_status(service="shadow_scheduler"))

    assert written == tmp_path / "status" / "forward_collector.json"
    assert store.read("forward_collector") == _status(service="forward_collector")
    assert [item.service for item in store.all()] == ["forward_collector", "shadow_scheduler"]


def test_status_store_rejects_malformed_status(tmp_path) -> None:
    path = tmp_path / "status" / "forward_collector.json"
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid runtime JSON"):
        RuntimeStatusStore(tmp_path).read("forward_collector")


def test_runtime_health_fails_closed_for_stale_failed_and_future_status() -> None:
    now = _timestamp()

    assert check_runtime_health(None, now=now, max_age_seconds=30).reason == "missing_status"
    assert (
        check_runtime_health(
            _status(updated_at=now - timedelta(seconds=31)), now=now, max_age_seconds=30
        ).reason
        == "stale_status"
    )
    assert (
        check_runtime_health(
            _status(state="failed", healthy=False), now=now, max_age_seconds=30
        ).reason
        == "state_failed"
    )
    assert (
        check_runtime_health(
            _status(updated_at=now + timedelta(seconds=6)), now=now, max_age_seconds=30
        ).reason
        == "status_timestamp_in_future"
    )
    assert check_runtime_health(
        _status(updated_at=now - timedelta(seconds=5)), now=now, max_age_seconds=30
    ).healthy


def test_runtime_control_persists_and_clears_stop_request(tmp_path) -> None:
    control = RuntimeControl(tmp_path)

    requested = control.request_stop(reason="operator_request", requested_at=_timestamp())

    assert control.stop_request() == requested
    assert control.clear_stop()
    assert control.stop_request() is None
    assert not control.clear_stop()


def test_runtime_identity_and_filesystem_usage_are_reproducible(tmp_path, monkeypatch) -> None:
    config = tmp_path / "baseline.toml"
    config.write_text("[project]\nname='btc'\n", encoding="utf-8")
    monkeypatch.setenv("BTC_CODE_REVISION", "abc123")

    identity = build_runtime_identity(
        config_path=config,
        ingest_version="ingest-v6",
        model_sha256="a" * 64,
    )
    usage = filesystem_usage(tmp_path)

    assert identity["code_revision"] == "abc123"
    assert len(identity["config_sha256"]) == 64
    assert identity["ingest_version"] == "ingest-v6"
    assert identity["model_sha256"] == "a" * 64
    assert usage["total_bytes"] >= usage["free_bytes"] > 0
    assert 0.0 <= usage["used_percent"] <= 100.0


@pytest.mark.parametrize("service", ("", "../escape", "nested/name"))
def test_status_path_rejects_unsafe_service_name(tmp_path, service: str) -> None:
    with pytest.raises(ValueError):
        RuntimeStatusStore(tmp_path).status_path(service)
