"""Run and persist the fail-closed target-VPS deployment preflight."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
import json
from math import ceil, isfinite
from pathlib import Path
import re
import subprocess

import httpx

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.live.deployment import (  # noqa: E402
    DeploymentPreflightConfig,
    PreflightReportStore,
    run_deployment_preflight,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--rule-epoch", required=True)
    parser.add_argument("--compose-env-file", type=Path, default=Path("deploy/.env"))
    parser.add_argument("--data-root", type=Path, default=Path("deploy/runtime/data"))
    parser.add_argument("--output-root", type=Path, default=Path("deploy/runtime/output"))
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=Path("deploy/runtime/output/btc_short_horizon/runtime"),
    )
    parser.add_argument("--minimum-free-gib", type=float, default=10.0)
    parser.add_argument("--max-disk-used-percent", type=float, default=90.0)
    parser.add_argument("--latency-samples", type=int, default=5)
    parser.add_argument("--request-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-endpoint-p99-ms", type=float, default=2_000.0)
    parser.add_argument("--max-clob-clock-offset-ms", type=float, default=1_500.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    release_revision = _full_git_revision(args.code_revision, name="release revision")
    compose_revision = _full_git_revision(
        _compose_revision(args.compose_env_file),
        name="compose BTC_CODE_REVISION",
    )
    if compose_revision != release_revision:
        raise RuntimeError(
            "compose BTC_CODE_REVISION does not match release revision: "
            f"{compose_revision} != {args.code_revision}"
        )
    if not isfinite(args.minimum_free_gib) or args.minimum_free_gib <= 0.0:
        raise ValueError("minimum-free-gib must be finite and > 0")
    config = DeploymentPreflightConfig(
        release_revision=release_revision,
        observed_revision=_git_revision(),
        rule_epoch=args.rule_epoch,
        data_root=args.data_root,
        output_root=args.output_root,
        minimum_free_bytes=max(1, ceil(args.minimum_free_gib * 1024**3)),
        max_disk_used_percent=args.max_disk_used_percent,
        latency_samples=args.latency_samples,
        request_timeout_seconds=args.request_timeout_seconds,
        max_endpoint_p99_ms=args.max_endpoint_p99_ms,
        max_clob_clock_offset_ms=args.max_clob_clock_offset_ms,
    )
    with httpx.Client(
        http2=True,
        follow_redirects=False,
        headers={"User-Agent": "polymarket-btc-vps-preflight/1"},
    ) as client:
        report = run_deployment_preflight(
            config,
            http_client=client,
            ntp_synchronized=_host_ntp_synchronized(),
            observed_at=datetime.now(UTC),
        )
    receipt = PreflightReportStore(args.runtime_root).write(report)
    print(
        json.dumps(
            {
                **report.to_json(),
                "report_sha256": receipt.report_sha256,
                "report_path": str(receipt.immutable_path),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report.passed else 1


def _git_revision() -> str:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
            timeout=5.0,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("unable to identify the deployed Git revision") from exc
    if status.stdout.strip():
        raise RuntimeError("deployed release has tracked changes")
    return revision.stdout.strip().casefold()


def _compose_revision(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"unable to read compose environment: {path}") from exc
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == "BTC_CODE_REVISION":
            normalized = value.strip()
            if (
                len(normalized) >= 2
                and normalized[0] == normalized[-1]
                and normalized[0]
                in {
                    "'",
                    '"',
                }
            ):
                normalized = normalized[1:-1]
            values.append(normalized)
    if len(values) != 1 or not values[0]:
        raise RuntimeError("compose environment must define BTC_CODE_REVISION exactly once")
    return values[0]


def _full_git_revision(value: str, *, name: str) -> str:
    normalized = value.strip().casefold()
    if re.fullmatch(r"[0-9a-f]{40}", normalized) is None:
        raise RuntimeError(f"{name} must be a full 40-character Git SHA")
    return normalized


def _host_ntp_synchronized() -> bool:
    try:
        completed = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            check=True,
            capture_output=True,
            text=True,
            shell=False,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.stdout.strip().casefold() == "yes"


if __name__ == "__main__":
    raise SystemExit(main())
