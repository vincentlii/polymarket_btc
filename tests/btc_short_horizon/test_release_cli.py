import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.btc_release import release


def _previous_compose(tmp_path: Path) -> Path:
    path = tmp_path / "compose.previous.yaml"
    path.write_text("services: {}\n", encoding="utf-8")
    return path


def test_release_direct_entrypoint_help_works_from_repo_root() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/btc_release.py", "--help"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


class Runner:
    def __init__(
        self,
        sha: str,
        *,
        fail_api: bool = False,
        fail_preflight: bool = False,
        fail_label: bool = False,
        missing_readiness: bool = False,
        fail_compose_up: bool = False,
        paper_health_failures: int = 0,
    ) -> None:
        self.sha, self.fail_api, self.fail_preflight = sha, fail_api, fail_preflight
        self.fail_label, self.missing_readiness, self.calls = fail_label, missing_readiness, []
        self.fail_compose_up = fail_compose_up
        self.paper_health_failures = paper_health_failures

    def run(self, command, *, env=None):  # type: ignore[no-untyped-def]
        self.calls.append((command, env))
        joined = " ".join(command)
        if command == ("git", "status", "--porcelain"):
            return ""
        if command == ("git", "rev-parse", "HEAD"):
            return self.sha
        if "images -q" in joined:
            return "sha256:previous"
        if "image inspect" in joined:
            return (
                ("bad" if self.fail_label else self.sha)
                if "org.opencontainers.image.revision" in joined
                else "sha256:new"
            )
        if "printenv BTC_CODE_REVISION" in joined:
            return env["BTC_CODE_REVISION"] if env else self.sha
        if "btc_vps_preflight.py" in joined:
            if self.fail_preflight:
                raise RuntimeError("preflight")
            return json.dumps({"passed": True, "report_path": "preflight.json"})
        if "btc_runtime_healthcheck.py" in joined and "--service research_paper" in joined:
            if self.paper_health_failures:
                self.paper_health_failures -= 1
                raise RuntimeError("paper warming")
            return "healthy"
        if (
            "up -d --wait" in joined
            and self.fail_compose_up
            and env.get("BTC_IMAGE") != "sha256:previous"
        ):
            raise RuntimeError("compose unhealthy")
        if "api/status" in joined:
            statuses = [
                {
                    "service": "forward_collector",
                    "details": {
                        "identity": {"code_revision": "bad" if self.fail_api else self.sha}
                    },
                },
                {
                    "service": "research_paper",
                    "details": {
                        "identity": {"code_revision": "bad" if self.fail_api else self.sha}
                    },
                },
                {
                    "service": "training_readiness",
                    "healthy": True,
                    "details": {
                        "identity": {"code_revision": "bad" if self.fail_api else self.sha}
                    },
                },
            ]
            if self.missing_readiness:
                statuses = [
                    item for item in statuses if item.get("service") != "training_readiness"
                ]
            return json.dumps({"statuses": statuses})
        return "ok"


def test_release_writes_receipt_only_after_four_way_sha_verification(tmp_path) -> None:
    sha = "a" * 40
    env_file = tmp_path / ".env"
    env_file.write_text(f"BTC_CODE_REVISION={'b' * 40}\nBTC_IMAGE=old\nSECRET=keep\n")
    path = release(
        runner=Runner(sha),
        release_sha=sha,
        env_file=env_file,
        previous_compose_file=_previous_compose(tmp_path),
        receipt_root=tmp_path / "receipts",
        rule_epoch="chainlink-btc-usd-twap-60s-v1",
        data_root=tmp_path / "data",
        output_root=tmp_path / "output",
        runtime_root=tmp_path / "runtime",
    )
    payload = json.loads(path.read_text())
    assert payload["release_sha"] == payload["container_revision"] == payload["api_revision"]
    assert "SECRET=keep" in env_file.read_text()
    assert f"BTC_CODE_REVISION={sha}" in env_file.read_text()


def test_release_rolls_back_previous_image_on_api_mismatch(tmp_path) -> None:
    sha = "a" * 40
    runner = Runner(sha, fail_api=True)
    env_file = tmp_path / ".env"
    original = f"BTC_CODE_REVISION={'b' * 40}\nBTC_IMAGE=old\nSECRET=keep\n"
    env_file.write_text(original)
    with pytest.raises(ValueError, match="full SHA mismatch"):
        release(
            runner=runner,
            release_sha=sha,
            env_file=env_file,
            previous_compose_file=_previous_compose(tmp_path),
            receipt_root=tmp_path / "receipts",
            rule_epoch="chainlink-btc-usd-twap-60s-v1",
            data_root=tmp_path / "data",
            output_root=tmp_path / "output",
            runtime_root=tmp_path / "runtime",
        )
    assert any(env and env.get("BTC_IMAGE") == "sha256:previous" for _, env in runner.calls)
    assert not (tmp_path / "receipts").exists()
    assert env_file.read_text() == original


def test_preflight_failure_restores_env_without_restarting_old_container(tmp_path) -> None:
    sha = "a" * 40
    runner = Runner(sha, fail_preflight=True)
    env_file = tmp_path / ".env"
    original = f"BTC_CODE_REVISION={'b' * 40}\nBTC_IMAGE=old\n"
    env_file.write_text(original)
    with pytest.raises(RuntimeError, match="preflight"):
        release(
            runner=runner,
            release_sha=sha,
            env_file=env_file,
            previous_compose_file=_previous_compose(tmp_path),
            receipt_root=tmp_path / "receipts",
            rule_epoch="epoch",
            data_root=tmp_path / "data",
            output_root=tmp_path / "output",
            runtime_root=tmp_path / "runtime",
        )
    assert env_file.read_text() == original
    assert not any("up -d --wait" in " ".join(command) for command, _ in runner.calls)


def test_compose_health_failure_rolls_back_even_after_up_returns_nonzero(tmp_path) -> None:
    sha = "a" * 40
    runner = Runner(sha, fail_compose_up=True)
    env_file = tmp_path / ".env"
    original = f"BTC_CODE_REVISION={'b' * 40}\nBTC_IMAGE=old\n"
    env_file.write_text(original)
    with pytest.raises(RuntimeError, match="compose unhealthy"):
        release(
            runner=runner,
            release_sha=sha,
            env_file=env_file,
            previous_compose_file=_previous_compose(tmp_path),
            receipt_root=tmp_path / "receipts",
            rule_epoch="epoch",
            data_root=tmp_path / "data",
            output_root=tmp_path / "output",
            runtime_root=tmp_path / "runtime",
        )
    assert env_file.read_text() == original
    assert any(env and env.get("BTC_IMAGE") == "sha256:previous" for _, env in runner.calls)
    rollback_commands = [
        command
        for command, env in runner.calls
        if "up -d --wait --remove-orphans" in " ".join(command)
        and env
        and env.get("BTC_IMAGE") == "sha256:previous"
    ]
    assert len(rollback_commands) == 1
    assert str(tmp_path / "compose.previous.yaml") in rollback_commands[0]


def test_release_waits_for_paper_bootstrap_before_declaring_failure(tmp_path) -> None:
    sha = "a" * 40
    runner = Runner(sha, paper_health_failures=2)
    env_file = tmp_path / ".env"
    env_file.write_text(f"BTC_CODE_REVISION={'b' * 40}\nBTC_IMAGE=old\n")

    release(
        runner=runner,
        release_sha=sha,
        env_file=env_file,
        previous_compose_file=_previous_compose(tmp_path),
        receipt_root=tmp_path / "receipts",
        rule_epoch="epoch",
        data_root=tmp_path / "data",
        output_root=tmp_path / "output",
        runtime_root=tmp_path / "runtime",
    )

    assert sum(command == ("sleep", "10") for command, _ in runner.calls) == 2


@pytest.mark.parametrize("failure", ["label", "readiness"])
def test_release_restores_previous_state_for_label_or_readiness_failure(tmp_path, failure) -> None:  # type: ignore[no-untyped-def]
    sha = "a" * 40
    runner = Runner(sha, fail_label=failure == "label", missing_readiness=failure == "readiness")
    env_file = tmp_path / ".env"
    original = f"BTC_CODE_REVISION={'b' * 40}\nBTC_IMAGE=old\n"
    env_file.write_text(original)
    with pytest.raises(ValueError):
        release(
            runner=runner,
            release_sha=sha,
            env_file=env_file,
            previous_compose_file=_previous_compose(tmp_path),
            receipt_root=tmp_path / "receipts",
            rule_epoch="epoch",
            data_root=tmp_path / "data",
            output_root=tmp_path / "output",
            runtime_root=tmp_path / "runtime",
        )
    assert env_file.read_text() == original
    rollback_ups = [env for command, env in runner.calls if "up -d --wait" in " ".join(command)]
    assert len(rollback_ups) == (0 if failure == "label" else 2)
    if failure == "readiness":
        rollback_health = [
            command
            for command, env in runner.calls
            if "btc_runtime_healthcheck.py" in " ".join(command)
            and env
            and env.get("BTC_IMAGE") == "sha256:previous"
        ]
        assert {command[command.index("--service") + 1] for command in rollback_health} == {
            "forward_collector",
            "research_paper",
        }
