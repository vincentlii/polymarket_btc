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
        assert service["environment"]["BTC_CODE_REVISION"] == "${BTC_CODE_REVISION}"

    collector = compose["services"]["forward_collector"]
    assert collector["restart"] == "on-failure:5"
    assert collector["stop_signal"] == "SIGINT"
    assert collector["stop_grace_period"] == "45s"
    assert collector["build"]["args"]["BTC_CODE_REVISION"].startswith("${BTC_CODE_REVISION:?")


def test_docker_context_keeps_btc_data_source_package() -> None:
    patterns = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "/data" in patterns
    assert "data" not in patterns
