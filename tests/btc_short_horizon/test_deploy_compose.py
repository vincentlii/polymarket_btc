from __future__ import annotations

from pathlib import Path

import yaml


def test_vps_compose_pins_identity_and_bounds_container_logs() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text(encoding="utf-8"))

    for name in ("forward_collector", "dashboard", "opening_shadow"):
        service = compose["services"][name]
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "5"},
        }
        assert "BTC_CODE_REVISION" not in service["environment"]
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert "/tmp" in service["tmpfs"]
        assert service["pids_limit"] >= 32
        assert not any(key.startswith("POLY_") for key in service["environment"])

    collector = compose["services"]["forward_collector"]
    assert "--binance-stream" not in collector["command"]
    assert collector["restart"] == "on-failure:5"
    assert collector["stop_signal"] == "SIGINT"
    assert collector["stop_grace_period"] == "45s"
    assert collector["healthcheck"]["timeout"] == "15s"
    assert collector["build"]["args"]["BTC_CODE_REVISION"].startswith("${BTC_CODE_REVISION:?")
    dockerfile = Path("deploy/Dockerfile").read_text(encoding="utf-8")
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "BTC_CODE_REVISION=${BTC_CODE_REVISION}" in dockerfile
    assert "LABEL org.opencontainers.image.revision=${BTC_CODE_REVISION}" in dockerfile
    assert dockerfile.index(
        "RUN uv sync --locked --no-dev --no-install-project"
    ) < dockerfile.index("ARG BTC_CODE_REVISION")
    assert "apt-get install --yes --no-install-recommends libgomp1" in dockerfile
    assert all(
        mount["type"] == "bind" and mount["bind"]["create_host_path"] is False
        for mount in collector["volumes"]
    )

    dashboard = compose["services"]["dashboard"]
    assert dashboard["depends_on"]["forward_collector"]["condition"] == "service_healthy"
    assert "/healthz" in dashboard["healthcheck"]["test"][-1]

    shadow_healthcheck = compose["services"]["opening_shadow"]["healthcheck"]["test"]
    assert shadow_healthcheck[-2:] == ["--max-age-seconds", "90"]


def test_docker_context_keeps_btc_data_source_package() -> None:
    patterns = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "/data" in patterns
    assert "data" not in patterns


def test_local_deployment_secrets_and_runtime_are_never_tracked_or_built() -> None:
    git_patterns = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    docker_patterns = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "deploy/secrets/" in git_patterns
    assert "deploy/secrets" in docker_patterns
    assert "deploy/runtime/" in git_patterns
    assert "deploy/runtime" in docker_patterns


def test_readiness_worker_uses_same_raw_root_as_forward_collector() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text(encoding="utf-8"))
    command = compose["services"]["readiness_worker"]["command"]

    assert command[command.index("--candidate-root") + 1] == (
        "/app/data/btc_short_horizon/readiness/candidates"
    )
    assert command[command.index("--raw-data-root") + 1] == "/app/data/btc_short_horizon"
    assert compose["services"]["readiness_worker"]["healthcheck"]["timeout"] == "60s"
    assert compose["services"]["readiness_worker"]["mem_limit"] == "768m"
    assert compose["services"]["readiness_worker"]["environment"] == {
        "PYTHONUNBUFFERED": "1",
        "BTC_READINESS_DUCKDB_MEMORY_LIMIT": "64MB",
        "BTC_READINESS_TEMP_DIRECTORY": "/tmp",
    }
    assert "/tmp:size=256m" in compose["services"]["readiness_worker"]["tmpfs"]
