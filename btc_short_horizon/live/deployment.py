"""Fail-closed, credential-free VPS deployment preflight evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from math import ceil, isfinite
import os
from pathlib import Path
import re
import shutil
from time import monotonic_ns, time_ns
from typing import Any
from uuid import uuid4


_PREFLIGHT_SCHEMA_VERSION = "btc-deployment-preflight-v1"
_LATEST_SCHEMA_VERSION = "btc-deployment-preflight-latest-v1"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_RULE_EPOCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CLOB_TIME_URL = "https://clob.polymarket.com/time"
_GAMMA_URL = "https://gamma-api.polymarket.com/markets"
_BINANCE_TIME_URL = "https://data-api.binance.vision/api/v3/time"
_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"


@dataclass(frozen=True, slots=True)
class DeploymentPreflightConfig:
    release_revision: str
    observed_revision: str
    rule_epoch: str
    data_root: Path
    output_root: Path
    minimum_free_bytes: int = 10 * 1024**3
    max_disk_used_percent: float = 90.0
    latency_samples: int = 5
    request_timeout_seconds: float = 5.0
    max_endpoint_p99_ms: float = 2_000.0
    max_clob_clock_offset_ms: float = 1_500.0

    def __post_init__(self) -> None:
        for name in ("release_revision", "observed_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _REVISION.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase 40-character Git revision")
        if not isinstance(self.rule_epoch, str) or not _RULE_EPOCH.fullmatch(self.rule_epoch):
            raise ValueError("rule_epoch must be a safe non-empty identifier")
        for name in ("data_root", "output_root"):
            if not isinstance(getattr(self, name), Path):
                raise ValueError(f"{name} must be a Path")
        if (
            isinstance(self.minimum_free_bytes, bool)
            or not isinstance(self.minimum_free_bytes, int)
            or self.minimum_free_bytes < 1
        ):
            raise ValueError("minimum_free_bytes must be an integer >= 1")
        if (
            isinstance(self.latency_samples, bool)
            or not isinstance(self.latency_samples, int)
            or not 1 <= self.latency_samples <= 100
        ):
            raise ValueError("latency_samples must be an integer in [1, 100]")
        for name in (
            "max_disk_used_percent",
            "request_timeout_seconds",
            "max_endpoint_p99_ms",
            "max_clob_clock_offset_ms",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"{name} must be finite and > 0")
        if self.max_disk_used_percent > 100.0:
            raise ValueError("max_disk_used_percent must be <= 100")


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    key: str
    passed: bool
    detail: str

    def __post_init__(self) -> None:
        _identifier(self.key, "preflight check key")
        if not isinstance(self.passed, bool):
            raise ValueError("preflight check passed must be bool")
        _text(self.detail, "preflight check detail")

    def to_json(self) -> dict[str, object]:
        return {"key": self.key, "passed": self.passed, "detail": self.detail}

    @classmethod
    def from_json(cls, value: object) -> PreflightCheck:
        raw = _mapping(value, "preflight check")
        return cls(
            key=_text(raw.get("key"), "preflight check key"),
            passed=_bool(raw.get("passed"), "preflight check passed"),
            detail=_text(raw.get("detail"), "preflight check detail"),
        )


@dataclass(frozen=True, slots=True)
class EndpointLatency:
    name: str
    healthy: bool
    sample_count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    error_type: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.name, "endpoint name")
        if not isinstance(self.healthy, bool):
            raise ValueError("endpoint healthy must be bool")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count < 0
        ):
            raise ValueError("endpoint sample_count must be a non-negative integer")
        for name in ("p50_ms", "p95_ms", "p99_ms"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value < 0.0
            ):
                raise ValueError(f"endpoint {name} must be finite and >= 0")
        if self.healthy and (
            self.sample_count < 1
            or self.p50_ms is None
            or self.p95_ms is None
            or self.p99_ms is None
            or self.error_type is not None
        ):
            raise ValueError("healthy endpoint requires complete latency statistics")
        if not self.healthy and self.error_type is None:
            raise ValueError("failed endpoint requires an error_type")
        if self.error_type is not None:
            _identifier(self.error_type, "endpoint error_type")

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "healthy": self.healthy,
            "sample_count": self.sample_count,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "error_type": self.error_type,
        }

    @classmethod
    def from_json(cls, value: object) -> EndpointLatency:
        raw = _mapping(value, "endpoint latency")
        return cls(
            name=_text(raw.get("name"), "endpoint name"),
            healthy=_bool(raw.get("healthy"), "endpoint healthy"),
            sample_count=_integer(raw.get("sample_count"), "endpoint sample_count"),
            p50_ms=_optional_float(raw.get("p50_ms"), "endpoint p50_ms"),
            p95_ms=_optional_float(raw.get("p95_ms"), "endpoint p95_ms"),
            p99_ms=_optional_float(raw.get("p99_ms"), "endpoint p99_ms"),
            error_type=_optional_text(raw.get("error_type"), "endpoint error_type"),
        )


@dataclass(frozen=True, slots=True)
class DeploymentPreflightReport:
    observed_at: datetime
    release_revision: str
    rule_epoch: str
    checks: tuple[PreflightCheck, ...]
    endpoints: tuple[EndpointLatency, ...]

    def __post_init__(self) -> None:
        observed_at = _utc(self.observed_at, "preflight observed_at")
        if not _REVISION.fullmatch(self.release_revision):
            raise ValueError("preflight release_revision is invalid")
        if not _RULE_EPOCH.fullmatch(self.rule_epoch):
            raise ValueError("preflight rule_epoch is invalid")
        checks = tuple(self.checks)
        endpoints = tuple(self.endpoints)
        if not checks or len({item.key for item in checks}) != len(checks):
            raise ValueError("preflight checks must be non-empty and unique")
        if len({item.name for item in endpoints}) != len(endpoints):
            raise ValueError("preflight endpoint names must be unique")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "checks", checks)
        object.__setattr__(self, "endpoints", endpoints)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.checks)

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": _PREFLIGHT_SCHEMA_VERSION,
            "observed_at": self.observed_at.isoformat(),
            "release_revision": self.release_revision,
            "rule_epoch": self.rule_epoch,
            "passed": self.passed,
            "checks": [item.to_json() for item in self.checks],
            "endpoints": [item.to_json() for item in self.endpoints],
        }

    @classmethod
    def from_json(cls, value: object) -> DeploymentPreflightReport:
        raw = _mapping(value, "deployment preflight report")
        if raw.get("schema_version") != _PREFLIGHT_SCHEMA_VERSION:
            raise ValueError("unsupported deployment preflight schema")
        report = cls(
            observed_at=_timestamp(raw.get("observed_at"), "preflight observed_at"),
            release_revision=_text(raw.get("release_revision"), "release_revision"),
            rule_epoch=_text(raw.get("rule_epoch"), "rule_epoch"),
            checks=tuple(
                PreflightCheck.from_json(item)
                for item in _sequence(raw.get("checks"), "preflight checks")
            ),
            endpoints=tuple(
                EndpointLatency.from_json(item)
                for item in _sequence(raw.get("endpoints"), "preflight endpoints")
            ),
        )
        if raw.get("passed") is not report.passed:
            raise ValueError("preflight passed flag disagrees with its checks")
        return report


@dataclass(frozen=True, slots=True)
class PreflightWriteReceipt:
    report_sha256: str
    immutable_path: Path
    latest_path: Path


@dataclass(frozen=True, slots=True)
class _ProbeResult:
    public: EndpointLatency
    clock_offsets_ms: tuple[float, ...] = ()


class PreflightReportStore:
    """Content-addressed preflight reports with a validated atomic latest pointer."""

    def __init__(self, runtime_root: Path) -> None:
        self.root = runtime_root / "preflight"

    @property
    def latest_path(self) -> Path:
        return self.root / "latest.json"

    def write(self, report: DeploymentPreflightReport) -> PreflightWriteReceipt:
        encoded = _canonical_json(report.to_json()) + b"\n"
        report_sha256 = sha256(encoded).hexdigest()
        immutable_path = self.root / "reports" / f"{report_sha256}.json"
        immutable_path.parent.mkdir(parents=True, exist_ok=True)
        if immutable_path.exists():
            if immutable_path.read_bytes() != encoded:
                raise ValueError("preflight report hash collision")
        else:
            _atomic_write_bytes(immutable_path, encoded)
        pointer = {
            "schema_version": _LATEST_SCHEMA_VERSION,
            "report_sha256": report_sha256,
            "relative_path": immutable_path.relative_to(self.root).as_posix(),
        }
        _atomic_write_bytes(self.latest_path, _canonical_json(pointer) + b"\n")
        return PreflightWriteReceipt(report_sha256, immutable_path, self.latest_path)

    def read_latest(self) -> DeploymentPreflightReport | None:
        try:
            raw_pointer = _strict_json(self.latest_path.read_bytes())
        except FileNotFoundError:
            return None
        pointer = _mapping(raw_pointer, "preflight latest pointer")
        if set(pointer) != {"schema_version", "report_sha256", "relative_path"}:
            raise ValueError("preflight latest pointer has an unsupported schema")
        if pointer.get("schema_version") != _LATEST_SCHEMA_VERSION:
            raise ValueError("unsupported preflight latest schema")
        expected_hash = _sha256_text(pointer.get("report_sha256"), "report_sha256")
        relative = Path(_text(pointer.get("relative_path"), "relative_path"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("preflight latest pointer escapes its root")
        report_path = self.root / relative
        encoded = report_path.read_bytes()
        if sha256(encoded).hexdigest() != expected_hash:
            raise ValueError("preflight report hash mismatch")
        return DeploymentPreflightReport.from_json(_strict_json(encoded))


def run_deployment_preflight(
    config: DeploymentPreflightConfig,
    *,
    http_client: Any,
    ntp_synchronized: bool,
    clock_ns: Callable[[], int] = time_ns,
    latency_clock_ns: Callable[[], int] = monotonic_ns,
    observed_at: datetime | None = None,
) -> DeploymentPreflightReport:
    """Run all target-host checks; every failure remains visible in one report."""

    if not isinstance(ntp_synchronized, bool):
        raise ValueError("ntp_synchronized must be bool")
    checks: list[PreflightCheck] = [
        PreflightCheck(
            "release_revision",
            config.release_revision == config.observed_revision,
            (
                "release revision matches the tested checkout"
                if config.release_revision == config.observed_revision
                else "release revision differs from the tested checkout"
            ),
        ),
        PreflightCheck("rule_epoch", True, "verified rule epoch is explicitly configured"),
    ]
    paths_ok, paths_detail = _check_runtime_paths(config.data_root, config.output_root)
    checks.append(PreflightCheck("runtime_paths", paths_ok, paths_detail))
    disk_ok, disk_detail = _check_disk_capacity(config)
    checks.append(PreflightCheck("disk_capacity", disk_ok, disk_detail))
    checks.append(
        PreflightCheck(
            "ntp_synchronized",
            ntp_synchronized,
            "host reports NTP synchronization"
            if ntp_synchronized
            else "host does not report NTP synchronization",
        )
    )

    geo_ok, geo_detail = _check_geoblock(
        http_client,
        timeout_seconds=config.request_timeout_seconds,
    )
    checks.append(PreflightCheck("geoblock", geo_ok, geo_detail))

    probes = (
        _probe_endpoint(
            name="clob",
            url=_CLOB_TIME_URL,
            params=None,
            samples=config.latency_samples,
            timeout_seconds=config.request_timeout_seconds,
            http_client=http_client,
            validator=_clob_time_seconds,
            clock_ns=clock_ns,
            latency_clock_ns=latency_clock_ns,
        ),
        _probe_endpoint(
            name="gamma",
            url=_GAMMA_URL,
            params={"limit": 1, "active": "true", "closed": "false"},
            samples=config.latency_samples,
            timeout_seconds=config.request_timeout_seconds,
            http_client=http_client,
            validator=_gamma_payload,
            clock_ns=clock_ns,
            latency_clock_ns=latency_clock_ns,
        ),
        _probe_endpoint(
            name="binance_spot",
            url=_BINANCE_TIME_URL,
            params=None,
            samples=config.latency_samples,
            timeout_seconds=config.request_timeout_seconds,
            http_client=http_client,
            validator=_binance_time_seconds,
            clock_ns=clock_ns,
            latency_clock_ns=latency_clock_ns,
        ),
    )
    clob_offsets = probes[0].clock_offsets_ms
    clock_ok = bool(clob_offsets) and max(abs(value) for value in clob_offsets) <= (
        config.max_clob_clock_offset_ms
    )
    clock_detail = (
        f"maximum absolute CLOB clock offset {max(abs(value) for value in clob_offsets):.1f} ms"
        if clob_offsets
        else "CLOB clock offset unavailable"
    )
    checks.append(PreflightCheck("clob_clock", clock_ok, clock_detail))
    endpoint_ok = all(
        probe.public.healthy
        and probe.public.p99_ms is not None
        and probe.public.p99_ms <= config.max_endpoint_p99_ms
        for probe in probes
    )
    checks.append(
        PreflightCheck(
            "endpoint_latency",
            endpoint_ok,
            (
                "all official endpoint probes passed the configured P99 ceiling"
                if endpoint_ok
                else "an official endpoint failed or exceeded the configured P99 ceiling"
            ),
        )
    )
    return DeploymentPreflightReport(
        observed_at=_utc(observed_at or datetime.now(UTC), "preflight observed_at"),
        release_revision=config.release_revision,
        rule_epoch=config.rule_epoch,
        checks=tuple(checks),
        endpoints=tuple(probe.public for probe in probes),
    )


def _check_runtime_paths(data_root: Path, output_root: Path) -> tuple[bool, str]:
    data = data_root.resolve()
    output = output_root.resolve()
    if data == output or data in output.parents or output in data.parents:
        return False, "data and output roots must be separate sibling trees"
    for path in (data_root, output_root):
        if not path.is_dir() or path.is_symlink():
            return False, "runtime roots must be existing non-symlink directories"
        probe = path / f".preflight-write-{uuid4().hex}.tmp"
        try:
            with probe.open("xb") as handle:
                handle.write(b"preflight\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            return False, "a runtime root is not durably writable"
        finally:
            probe.unlink(missing_ok=True)
    return True, "data and output roots are separate and durably writable"


def _check_disk_capacity(config: DeploymentPreflightConfig) -> tuple[bool, str]:
    usages = tuple(shutil.disk_usage(path) for path in (config.data_root, config.output_root))
    minimum_free = min(item.free for item in usages)
    maximum_used_percent = max((item.total - item.free) / item.total * 100.0 for item in usages)
    passed = (
        minimum_free >= config.minimum_free_bytes
        and maximum_used_percent <= config.max_disk_used_percent
    )
    return (
        passed,
        f"minimum free {minimum_free} bytes; maximum used {maximum_used_percent:.3f}%",
    )


def _check_geoblock(http_client: Any, *, timeout_seconds: float) -> tuple[bool, str]:
    try:
        response = http_client.get(_GEOBLOCK_URL, timeout=timeout_seconds)
        response.raise_for_status()
        payload = _mapping(response.json(), "geoblock response")
        blocked = _bool(payload.get("blocked"), "geoblock blocked")
        country = _text(payload.get("country"), "geoblock country").upper()
        region_value = payload.get("region")
        if not isinstance(region_value, str) or len(country) != 2 or len(region_value) > 16:
            raise ValueError("geoblock location is invalid")
    except Exception as exc:
        return False, f"geoblock check failed ({type(exc).__name__})"
    return (
        not blocked,
        f"target IP is {'blocked' if blocked else 'eligible'} in {country}/{region_value.strip()}",
    )


def _probe_endpoint(
    *,
    name: str,
    url: str,
    params: Mapping[str, object] | None,
    samples: int,
    timeout_seconds: float,
    http_client: Any,
    validator: Callable[[object], float | None],
    clock_ns: Callable[[], int],
    latency_clock_ns: Callable[[], int],
) -> _ProbeResult:
    latencies: list[float] = []
    offsets: list[float] = []
    try:
        for _ in range(samples):
            wall_started_ns = clock_ns()
            latency_started_ns = latency_clock_ns()
            response = http_client.get(url, params=params, timeout=timeout_seconds)
            response.raise_for_status()
            server_seconds = validator(response.json())
            latency_finished_ns = latency_clock_ns()
            wall_finished_ns = clock_ns()
            if latency_finished_ns < latency_started_ns or wall_finished_ns < wall_started_ns:
                raise ValueError("preflight clock moved backwards")
            latencies.append((latency_finished_ns - latency_started_ns) / 1_000_000)
            if server_seconds is not None:
                midpoint_seconds = (wall_started_ns + wall_finished_ns) / 2_000_000_000
                offsets.append((server_seconds - midpoint_seconds) * 1_000.0)
    except Exception as exc:
        return _ProbeResult(
            EndpointLatency(name, False, len(latencies), None, None, None, type(exc).__name__),
            tuple(offsets),
        )
    ordered = tuple(sorted(latencies))
    return _ProbeResult(
        EndpointLatency(
            name=name,
            healthy=True,
            sample_count=len(ordered),
            p50_ms=_percentile(ordered, 0.50),
            p95_ms=_percentile(ordered, 0.95),
            p99_ms=_percentile(ordered, 0.99),
        ),
        tuple(offsets),
    )


def _clob_time_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError("CLOB time response is invalid")
    return float(value)


def _binance_time_seconds(value: object) -> None:
    raw = _mapping(value, "Binance time response").get("serverTime")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError("Binance serverTime is invalid")
    return None


def _gamma_payload(value: object) -> None:
    if not isinstance(value, list) or not value or not isinstance(value[0], Mapping):
        raise ValueError("Gamma market probe response is invalid")
    return None


def _percentile(values: tuple[float, ...], quantile: float) -> float:
    if not values:
        raise ValueError("latency sample is empty")
    return float(values[max(0, ceil(len(values) * quantile) - 1)])


def _atomic_write_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _strict_json(encoded: bytes) -> object:
    return json.loads(
        encoded.decode("utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {value}")
        ),
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{name} must be a JSON array")
    return tuple(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _identifier(value: object, name: str) -> str:
    text = _text(value, name)
    if any(character in text for character in "/\\") or text in {".", ".."}:
        raise ValueError(f"{name} must be a simple identifier")
    return text


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _optional_float(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _sha256_text(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def _timestamp(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    return _utc(parsed, name)


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "DeploymentPreflightConfig",
    "DeploymentPreflightReport",
    "EndpointLatency",
    "PreflightCheck",
    "PreflightReportStore",
    "PreflightWriteReceipt",
    "run_deployment_preflight",
]
