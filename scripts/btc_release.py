"""Build, deploy, verify, receipt, and automatically roll back one immutable release."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from typing import Protocol

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data.storage import write_atomic_json  # noqa: E402


class Runner(Protocol):
    def run(self, command: tuple[str, ...], *, env: dict[str, str] | None = None) -> str: ...


class SubprocessRunner:
    def run(self, command: tuple[str, ...], *, env: dict[str, str] | None = None) -> str:
        result = subprocess.run(command, check=True, text=True, capture_output=True, env=env)
        return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class ReleaseEvidence:
    release_sha: str
    previous_image: str
    image: str
    image_id: str
    image_revision: str
    preflight_sha256: str
    health_sha256: str
    container_revision: str
    api_revision: str
    preflight_receipt: str
    previous_compose_sha256: str


def release(
    *,
    runner: Runner,
    release_sha: str,
    env_file: Path,
    previous_compose_file: Path,
    receipt_root: Path,
    rule_epoch: str,
    data_root: Path,
    output_root: Path,
    runtime_root: Path,
) -> Path:
    if len(release_sha) != 40 or any(ch not in "0123456789abcdef" for ch in release_sha):
        raise ValueError("release_sha must be a full lowercase Git SHA")
    if runner.run(("git", "status", "--porcelain")):
        raise ValueError("release worktree must be clean, including untracked files")
    if runner.run(("git", "rev-parse", "HEAD")) != release_sha:
        raise ValueError("release_sha does not match clean HEAD")
    if not previous_compose_file.is_file():
        raise ValueError("previous_compose_file must identify the deployed release topology")
    previous_compose_sha256 = sha256(previous_compose_file.read_bytes()).hexdigest()
    compose = ("docker", "compose", "--env-file", str(env_file), "-f", "deploy/compose.yaml")
    previous_compose = (
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(previous_compose_file),
    )
    previous_image = runner.run((*previous_compose, "images", "-q", "forward_collector"))
    if not previous_image:
        raise ValueError("previous release image is required for safe rollback")
    original_env = env_file.read_bytes()
    previous_revision = _env_value(original_env.decode("utf-8"), "BTC_CODE_REVISION")
    image = f"polymarket-btc-forward:{release_sha}"
    env = {**os.environ, "BTC_IMAGE": image, "BTC_CODE_REVISION": release_sha}
    deployed = False
    try:
        _write_release_env(env_file, original_env.decode("utf-8"), release_sha, image)
        preflight = runner.run(
            (
                "uv",
                "run",
                "python",
                "scripts/btc_vps_preflight.py",
                "--code-revision",
                release_sha,
                "--rule-epoch",
                rule_epoch,
                "--compose-env-file",
                str(env_file),
                "--data-root",
                str(data_root),
                "--output-root",
                str(output_root),
                "--runtime-root",
                str(runtime_root),
            ),
            env=env,
        )
        preflight_payload = json.loads(preflight)
        if preflight_payload.get("passed") is not True or not preflight_payload.get("report_path"):
            raise ValueError("deployment preflight did not produce a passing receipt")
        runner.run(
            (
                "docker",
                "build",
                "--build-arg",
                f"BTC_CODE_REVISION={release_sha}",
                "--label",
                f"org.opencontainers.image.revision={release_sha}",
                "-t",
                image,
                "-f",
                "deploy/Dockerfile",
                ".",
            ),
            env=env,
        )
        image_id = runner.run(("docker", "image", "inspect", image, "--format", "{{.Id}}"))
        image_revision = runner.run(
            (
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            )
        )
        if image_revision != release_sha:
            raise ValueError("built image OCI revision mismatch")
        deployed = True
        runner.run((*compose, "up", "-d", "--wait"), env=env)
        health_outputs = []
        for service, age in (
            ("forward_collector", "30"),
            ("research_paper", "30"),
            ("training_readiness", "150"),
        ):
            health_outputs.append(
                runner.run(
                    (
                        "uv",
                        "run",
                        "python",
                        "scripts/btc_runtime_healthcheck.py",
                        "--runtime-root",
                        str(runtime_root),
                        "--service",
                        service,
                        "--max-age-seconds",
                        age,
                    ),
                    env=env,
                )
            )
        health = "\n".join(health_outputs)
        container_revision = runner.run(
            (*compose, "exec", "-T", "forward_collector", "printenv", "BTC_CODE_REVISION"),
            env=env,
        )
        api_revision = runner.run(("curl", "-fsS", "http://127.0.0.1:8080/api/status"), env=env)
        api_payload = json.loads(api_revision)
        statuses = {
            item.get("service"): item
            for item in api_payload.get("statuses", [])
            if isinstance(item, dict)
        }
        revisions = {
            statuses[service].get("details", {}).get("identity", {}).get("code_revision")
            for service in ("forward_collector", "research_paper")
            if service in statuses
        }
        readiness_ok = (
            statuses.get("training_readiness", {}).get("healthy") is True
            and statuses.get("training_readiness", {})
            .get("details", {})
            .get("identity", {})
            .get("code_revision")
            == release_sha
        )
        api_sha = release_sha if revisions == {release_sha} else None
        if container_revision != release_sha or api_sha != release_sha or not readiness_ok:
            raise ValueError("deployed container/API full SHA mismatch")
    except Exception:
        _write_atomic_bytes(env_file, original_env)
        rollback_env = {
            **os.environ,
            "BTC_IMAGE": previous_image,
            "BTC_CODE_REVISION": previous_revision,
        }
        if deployed:
            runner.run(
                (*previous_compose, "up", "-d", "--wait", "--remove-orphans"),
                env=rollback_env,
            )
            for service in ("forward_collector", "research_paper"):
                runner.run(
                    (
                        "uv",
                        "run",
                        "python",
                        "scripts/btc_runtime_healthcheck.py",
                        "--runtime-root",
                        str(runtime_root),
                        "--service",
                        service,
                        "--max-age-seconds",
                        "30",
                    ),
                    env=rollback_env,
                )
            rolled_back = runner.run(
                (
                    *previous_compose,
                    "exec",
                    "-T",
                    "forward_collector",
                    "printenv",
                    "BTC_CODE_REVISION",
                ),
                env=rollback_env,
            )
            if rolled_back != previous_revision:
                raise RuntimeError("rollback revision verification failed")
        raise
    evidence = ReleaseEvidence(
        release_sha,
        previous_image,
        image,
        image_id,
        image_revision,
        sha256(preflight.encode()).hexdigest(),
        sha256(health.encode()).hexdigest(),
        container_revision,
        str(api_sha),
        str(preflight_payload["report_path"]),
        previous_compose_sha256,
    )
    payload = asdict(evidence)
    payload["receipt_sha256"] = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    receipt_root.mkdir(parents=True, exist_ok=True)
    path = receipt_root / f"release-{payload['receipt_sha256']}.json"
    write_atomic_json(path, payload)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--previous-compose-file", type=Path, required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--rule-epoch", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    print(
        release(
            runner=SubprocessRunner(),
            release_sha=args.release_sha,
            env_file=args.env_file,
            previous_compose_file=args.previous_compose_file,
            receipt_root=args.receipt_root,
            rule_epoch=args.rule_epoch,
            data_root=args.data_root,
            output_root=args.output_root,
            runtime_root=args.runtime_root,
        )
    )
    return 0


def _env_value(content: str, key: str) -> str:
    values = [
        line.partition("=")[2].strip()
        for line in content.splitlines()
        if line.partition("=")[0].strip() == key
    ]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"release env must define {key} exactly once")
    return values[0].strip("'\"")


def _write_release_env(path: Path, content: str, revision: str, image: str) -> None:
    replacements = {"BTC_CODE_REVISION": revision, "BTC_IMAGE": image}
    seen: set[str] = set()
    lines = []
    for line in content.splitlines():
        key, separator, _ = line.partition("=")
        normalized = key.strip()
        if separator and normalized in replacements:
            lines.append(f"{normalized}={replacements[normalized]}")
            seen.add(normalized)
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in replacements.items() if key not in seen)
    _write_atomic_bytes(path, ("\n".join(lines) + "\n").encode())


def _write_atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
