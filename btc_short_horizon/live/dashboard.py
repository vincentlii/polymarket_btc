"""Read-only local HTTP dashboard for persisted BTC runtime state."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import isfinite
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from btc_short_horizon.live.dashboard_page import dashboard_html
from btc_short_horizon.live.dashboard_state import BotDashboardSnapshot, DashboardSnapshotStore
from btc_short_horizon.live.append_only_ledger import AppendOnlyLedgerRepository
from btc_short_horizon.live.runtime import (
    RuntimeControl,
    RuntimeHealth,
    RuntimeStatus,
    RuntimeStatusStore,
    check_runtime_health,
)
from btc_short_horizon.data.readiness import current_protocol_receipts


_ORDER_HISTORY_DEFAULT_LIMIT = 50
_ORDER_HISTORY_MAX_LIMIT = 100
_ORDER_HISTORY_ERROR_MESSAGE = "订单历史账本无效，已拒绝返回不完整结果。"


class _OrderHistoryDataError(ValueError):
    """Ledger data is unsafe or invalid; details must not cross the HTTP boundary."""


class _OrderHistoryRequestError(ValueError):
    """The caller supplied an invalid pagination request."""


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    runtime_root: Path
    health_service: str = "forward_collector"
    active_services: tuple[str, ...] = ()
    max_age_seconds: float = 30.0
    status_interval_grace_factor: float = 1.5

    def __post_init__(self) -> None:
        if not self.health_service.strip():
            raise ValueError("health_service is required")
        if len(set(self.active_services)) != len(self.active_services) or any(
            not service.strip() for service in self.active_services
        ):
            raise ValueError("active_services must contain unique non-empty service names")
        if self.active_services and self.health_service not in self.active_services:
            raise ValueError("health_service must be included in active_services")
        if self.max_age_seconds <= 0.0:
            raise ValueError("max_age_seconds must be > 0")
        if (
            not isfinite(self.status_interval_grace_factor)
            or self.status_interval_grace_factor < 1.0
        ):
            raise ValueError("status_interval_grace_factor must be finite and >= 1")


def build_dashboard_payload(
    config: DashboardConfig,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Aggregate validated runtime and performance projections without mutating either."""

    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    errors: list[str] = []
    try:
        statuses = RuntimeStatusStore(config.runtime_root).all(config.active_services or None)
        stop_request = RuntimeControl(config.runtime_root).stop_request()
    except ValueError as exc:
        statuses = ()
        stop_request = None
        errors.append(str(exc))

    try:
        snapshot = DashboardSnapshotStore(config.runtime_root).read()
    except ValueError as exc:
        snapshot = None
        errors.append(str(exc))

    by_service = {status.service: status for status in statuses}
    missing_active_services = tuple(
        service for service in config.active_services if service not in by_service
    )
    for service in missing_active_services:
        errors.append(f"active runtime status missing: {service}")
    primary_status = by_service.get(config.health_service)
    primary = check_runtime_health(
        primary_status,
        now=current_time,
        max_age_seconds=_status_max_age_seconds(primary_status, config=config),
    )
    if not statuses and errors:
        primary = RuntimeHealth(False, "invalid_runtime_status", primary.age_seconds)
    shadow = _shadow_projection(
        (
            by_service.get("opening_shadow")
            if not config.active_services or "opening_shadow" in config.active_services
            else None
        ),
        errors,
    )
    if missing_active_services:
        primary = RuntimeHealth(False, "missing_active_status", primary.age_seconds)
    snapshot_health = _snapshot_health(
        snapshot,
        now=current_time,
        max_age_seconds=config.max_age_seconds,
    )
    return {
        "generated_at": current_time.isoformat(),
        "health_service": config.health_service,
        "health": {
            "healthy": primary.healthy,
            "reason": primary.reason,
            "age_seconds": primary.age_seconds,
        },
        "statuses": [
            {
                **_status_payload(
                    status,
                    current_time,
                    _status_max_age_seconds(status, config=config),
                ),
                "active": not config.active_services or status.service in config.active_services,
            }
            for status in statuses
        ],
        "snapshot": None if snapshot is None else snapshot.to_json(),
        "snapshot_health": snapshot_health,
        "shadow": shadow,
        "training_readiness": _readiness_status(config.runtime_root),
        "stop_request": (
            None
            if stop_request is None
            else {
                "reason": stop_request.reason,
                "requested_at": stop_request.requested_at.isoformat(),
            }
        ),
        "errors": errors,
    }


def _readiness_status(runtime_root: Path) -> dict[str, object]:
    root = runtime_root / "readiness" / "receipts"
    paths = sorted(root.glob("*.json"), key=lambda item: item.name)[-96:] if root.is_dir() else []
    try:
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    except (OSError, json.JSONDecodeError):
        return {"healthy": False, "reason": "invalid_receipt", "receipt_count": len(paths)}
    receipts = current_protocol_receipts(payloads)
    errors = [
        item for item in payloads if item.get("schema_version") == "btc-training-readiness-error-v1"
    ]
    invalid = [item.get("market_slug") for item in receipts if item.get("ready") is not True]
    return {
        "healthy": bool(receipts) and not invalid and not errors,
        "reason": "ok" if receipts and not invalid and not errors else "missing_or_failed_receipt",
        "receipt_count": len(receipts),
        "error_count": len(errors),
        "ready_count": sum(item.get("ready") is True for item in receipts),
        "invalid_markets": invalid,
    }


def build_order_history_payload(
    config: DashboardConfig,
    *,
    limit: int = _ORDER_HISTORY_DEFAULT_LIMIT,
    cursor: str | None = None,
    variant: str | None = None,
    epoch: str | None = None,
) -> dict[str, object]:
    """Read a deterministic, credential-free page from all Research Paper ledgers."""

    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _ORDER_HISTORY_MAX_LIMIT
    ):
        raise _OrderHistoryRequestError(
            f"limit 必须是 1 到 {_ORDER_HISTORY_MAX_LIMIT} 之间的整数。"
        )
    if variant is not None:
        _validate_variant_id(variant)
    if epoch is not None:
        _validate_variant_id(epoch)

    records, available_variants, available_epochs = _read_order_history(config.runtime_root)
    filtered = [
        item
        for item in records
        if (variant is None or item[0][2] == variant) and (epoch is None or item[0][1] == epoch)
    ]
    start = 0
    if cursor is not None:
        cursor_key = _decode_order_cursor(cursor)
        try:
            start = next(index for index, item in enumerate(filtered) if item[0] == cursor_key) + 1
        except StopIteration as exc:
            raise _OrderHistoryRequestError("cursor 已失效或不属于当前筛选结果。") from exc

    page = filtered[start : start + limit]
    has_more = start + len(page) < len(filtered)
    return {
        "items": [item[1] for item in page],
        "next_cursor": _encode_order_cursor(page[-1][0]) if page and has_more else None,
        "has_more": has_more,
        "limit": limit,
        "variant": variant,
        "epoch": epoch,
        "available_variants": available_variants,
        "available_epochs": available_epochs,
        "total_records": len(filtered),
        "total_realized_pnl": (
            None
            if variant is None
            else sum(float(item[1]["realized_pnl"] or 0.0) for item in filtered)
        ),
        "execution_epoch_count": len({item[0][1] for item in filtered}),
    }


def _read_order_history(
    runtime_root: Path,
) -> tuple[list[tuple[tuple[int, str, str, str], dict[str, object]]], list[str], list[str]]:
    records: list[tuple[tuple[int, str, str, str], dict[str, object]]] = []
    seen: set[tuple[str, str, str]] = set()
    variants: set[str] = set()
    epochs: set[str] = set()
    paper_root = runtime_root / "paper"
    try:
        ledger_paths = _paper_ledger_paths(runtime_root)
        for ledger_path in ledger_paths:
            relative_parts = ledger_path.relative_to(paper_root).parts
            if ledger_path.name == "ledger.sqlite3":
                if len(relative_parts) != 5 or relative_parts[0] != "epochs":
                    raise _OrderHistoryDataError
                execution_epoch = _data_variant_id(relative_parts[1])
                if relative_parts[2] != "variants":
                    raise _OrderHistoryDataError
                ledger_variant = _data_variant_id(relative_parts[3])
                repository = AppendOnlyLedgerRepository(
                    ledger_path,
                    execution_epoch=execution_epoch,
                    variant_id=ledger_variant,
                    read_only=True,
                )
                try:
                    raw = repository.latest()
                finally:
                    repository.close()
            else:
                raw = json.loads(
                    ledger_path.read_text(encoding="utf-8"),
                    parse_constant=_reject_nonfinite_json,
                )
            if not isinstance(raw, Mapping):
                raise _OrderHistoryDataError
            schema_version = raw.get("schema_version")
            if isinstance(schema_version, bool) or schema_version not in {2, 3, 4, 5, 6}:
                raise _OrderHistoryDataError
            raw_records = raw.get("records")
            if not isinstance(raw_records, list):
                raise _OrderHistoryDataError
            if _data_number(raw.get("starting_balance"), minimum=0.0) <= 0.0:
                raise _OrderHistoryDataError

            ledger_variant = "legacy_paper"
            execution_epoch = "legacy_schema2"
            if schema_version == 2:
                if relative_parts != ("ledger.json",):
                    raise _OrderHistoryDataError
            elif schema_version == 3:
                ledger_variant = _data_variant_id(raw.get("variant_id"))
                execution_epoch = "legacy_schema3"
                if relative_parts != ("variants", ledger_variant, "ledger.json"):
                    raise _OrderHistoryDataError
            else:
                ledger_variant = _data_variant_id(raw.get("variant_id"))
                execution_epoch = _data_variant_id(raw.get("execution_epoch"))
                ledger_filename = (
                    "ledger.sqlite3" if ledger_path.name == "ledger.sqlite3" else "ledger.json"
                )
                if relative_parts != (
                    "epochs",
                    execution_epoch,
                    "variants",
                    ledger_variant,
                    ledger_filename,
                ):
                    raise _OrderHistoryDataError
            variants.add(ledger_variant)
            epochs.add(execution_epoch)
            for raw_record in raw_records:
                record_variant = ledger_variant
                if schema_version in {3, 4, 5, 6}:
                    record_variant = _data_variant_id(
                        raw_record.get("variant_id") if isinstance(raw_record, Mapping) else None
                    )
                    if record_variant != ledger_variant:
                        raise _OrderHistoryDataError
                key, projected = _project_order_record(
                    raw_record,
                    schema_version=schema_version,
                    variant_id=record_variant,
                    execution_epoch=execution_epoch,
                )
                identity = (execution_epoch, record_variant, key[3])
                if identity in seen:
                    raise _OrderHistoryDataError
                seen.add(identity)
                records.append((key, projected))
    except _OrderHistoryDataError:
        raise
    except (
        OSError,
        OverflowError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise _OrderHistoryDataError from exc

    records.sort(key=lambda item: item[0], reverse=True)
    return records, sorted(variants), sorted(epochs)


def _paper_ledger_paths(runtime_root: Path) -> list[Path]:
    paper_root = runtime_root / "paper"
    if not paper_root.exists():
        return []
    if not paper_root.is_dir() or paper_root.is_symlink():
        raise _OrderHistoryDataError
    resolved_root = paper_root.resolve(strict=True)
    paths: list[Path] = []

    def fail_walk(_error: OSError) -> None:
        raise _OrderHistoryDataError

    for directory, directory_names, file_names in os.walk(
        paper_root,
        followlinks=False,
        onerror=fail_walk,
    ):
        current = Path(directory)
        for name in tuple(directory_names):
            if (current / name).is_symlink():
                raise _OrderHistoryDataError
        ledger_name = "ledger.sqlite3" if "ledger.sqlite3" in file_names else "ledger.json"
        if ledger_name not in file_names:
            continue
        candidate = current / ledger_name
        if candidate.is_symlink() or not candidate.is_file():
            raise _OrderHistoryDataError
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise _OrderHistoryDataError from exc
        paths.append(candidate)
    return sorted(paths, key=lambda item: item.as_posix())


def _project_order_record(
    raw: object,
    *,
    schema_version: int,
    variant_id: str,
    execution_epoch: str,
) -> tuple[tuple[int, str, str, str], dict[str, object]]:
    if not isinstance(raw, Mapping):
        raise _OrderHistoryDataError
    placement_id = _data_text(raw.get("placement_id"), maximum=160)
    market_slug = _data_text(raw.get("market_slug"), maximum=320)
    _data_text(raw.get("token_id"), maximum=320)
    side = _data_text(raw.get("side"), maximum=32)
    placed_at_ns = _data_integer(raw.get("placed_at_ns"), minimum=1)
    shares = _data_number(raw.get("shares"), minimum=0.0)
    filled_shares = _data_number(raw.get("filled_shares"), minimum=0.0)
    filled_notional = _data_number(raw.get("filled_notional"), minimum=0.0)
    _data_number(raw.get("planned_notional"), minimum=0.0)
    if filled_shares > shares + 1e-9 or filled_notional > filled_shares + 1e-9:
        raise _OrderHistoryDataError
    p_fair = _data_number(raw.get("p_fair"), minimum=0.0, maximum=1.0)
    market_price = _data_number(raw.get("market_price"), minimum=0.0, maximum=1.0)
    execution_status = _data_text(
        raw.get("execution_status") if schema_version >= 3 else raw.get("status"),
        maximum=80,
    )
    if schema_version >= 3:
        settlement_status = _data_text(raw.get("settlement_status"), maximum=80)
        execution_route = _data_text(raw.get("execution_route"), maximum=80)
    else:
        settlement_status = None
        execution_route = None
    realized_pnl = _data_optional_number(raw.get("realized_pnl"))
    terminal_reason = _data_optional_text(raw.get("terminal_reason"), maximum=160)
    active_at_ns = _data_optional_integer(raw.get("active_at_ns"), minimum=1)
    _data_optional_text(raw.get("outcome"), maximum=32)
    _data_optional_integer(raw.get("settled_at_ns"), minimum=1)
    opportunity_id: str | None = None
    entry_regime: str | None = None
    price_bucket: str | None = None
    go_eligible: bool | None = None
    decision_best_ask: float | None = None
    signal_observations: list[dict[str, object]] = []
    if schema_version == 2:
        _data_number(raw.get("cancel_race_filled_shares", 0.0), minimum=0.0)
    else:
        if schema_version >= 4:
            opportunity_id = _data_text(raw.get("opportunity_id"), maximum=160)
            entry_regime = _data_text(raw.get("entry_regime"), maximum=80)
            price_bucket = _data_text(raw.get("price_bucket"), maximum=80)
            go_eligible = _data_boolean(raw.get("go_eligible"))
            decision_best_ask = _data_number(raw.get("decision_best_ask"), minimum=0.0, maximum=1.0)
            observations = raw.get("signal_observations")
            if not isinstance(observations, list) or len(observations) > 3:
                raise _OrderHistoryDataError
            signal_observations = [
                _project_signal_observation(observation) for observation in observations
            ]
        for field in (
            "cancel_race_filled_shares",
            "maker_filled_shares",
            "taker_filled_shares",
            "maker_filled_notional",
            "taker_filled_notional",
            "initial_queue_ahead",
            "remaining_queue_ahead",
            "raw_eligible_sell_volume",
            "stressed_eligible_sell_volume",
        ):
            _data_number(raw.get(field), minimum=0.0)
        for field in (
            "cancel_requested_at_ns",
            "cancel_ack_at_ns",
            "terminal_at_ns",
        ):
            _data_optional_integer(raw.get(field), minimum=1)
        _data_optional_number(raw.get("fak_limit_price"))
        _data_optional_number(raw.get("fak_net_edge_per_share"))
    order_latency_ms = None
    if active_at_ns is not None:
        if active_at_ns < placed_at_ns:
            raise _OrderHistoryDataError
        order_latency_ms = (active_at_ns - placed_at_ns) / 1_000_000
    entry_price = None if filled_shares == 0.0 else filled_notional / filled_shares
    placed_at = datetime.fromtimestamp(placed_at_ns / 1e9, tz=UTC).isoformat()
    projected: dict[str, object] = {
        "order_id": placement_id,
        "variant_id": variant_id,
        "execution_epoch": execution_epoch,
        "market_slug": market_slug,
        "side": side,
        "placed_at": placed_at,
        "shares": shares,
        "filled_shares": filled_shares,
        "entry_price": entry_price,
        "p_fair": p_fair,
        "market_price": market_price,
        "execution_status": execution_status,
        "settlement_status": settlement_status,
        "terminal_reason": terminal_reason,
        "execution_route": execution_route,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": None,
        "order_latency_ms": order_latency_ms,
        "taker_fees": (
            _data_number(raw.get("taker_fees"), minimum=0.0) if schema_version >= 3 else None
        ),
        "opportunity_id": opportunity_id,
        "entry_regime": entry_regime,
        "price_bucket": price_bucket,
        "go_eligible": go_eligible,
        "decision_best_ask": decision_best_ask,
        "signal_observations": signal_observations,
        "model_version": (
            _data_optional_text(raw.get("model_version"), maximum=160)
            if schema_version >= 5
            else None
        ),
    }
    return (placed_at_ns, execution_epoch, variant_id, placement_id), projected


def _encode_order_cursor(key: tuple[int, str, str, str]) -> str:
    encoded = json.dumps(key, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_order_cursor(value: str) -> tuple[int, str, str, str]:
    if not isinstance(value, str) or not value or len(value) > 1_024:
        raise _OrderHistoryRequestError("cursor 格式无效。")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        raw = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise _OrderHistoryRequestError("cursor 格式无效。") from exc
    if not isinstance(raw, list) or len(raw) != 4:
        raise _OrderHistoryRequestError("cursor 格式无效。")
    placed_at_ns = _request_integer(raw[0], "cursor timestamp")
    execution_epoch = _request_variant_id(raw[1])
    variant_id = _request_variant_id(raw[2])
    placement_id = raw[3]
    if not isinstance(placement_id, str) or not placement_id or len(placement_id) > 160:
        raise _OrderHistoryRequestError("cursor 格式无效。")
    return placed_at_ns, execution_epoch, variant_id, placement_id


def _history_query(raw_query: str) -> tuple[int, str | None, str | None, str | None]:
    values = parse_qs(raw_query, keep_blank_values=True)
    if set(values) - {"limit", "cursor", "variant", "epoch"} or any(
        len(items) != 1 for items in values.values()
    ):
        raise _OrderHistoryRequestError("查询参数无效。")
    raw_limit = values.get("limit", [str(_ORDER_HISTORY_DEFAULT_LIMIT)])[0]
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise _OrderHistoryRequestError("limit 必须是整数。") from exc
    cursor = values.get("cursor", [None])[0]
    variant = values.get("variant", [None])[0]
    epoch = values.get("epoch", [None])[0]
    if cursor == "" or variant == "":
        raise _OrderHistoryRequestError("cursor 与 variant 不得为空。")
    if epoch == "":
        raise _OrderHistoryRequestError("epoch must not be empty")
    return limit, cursor, variant, epoch


def _validate_variant_id(value: str) -> None:
    _request_variant_id(value)


def _request_variant_id(value: object) -> str:
    try:
        return _data_variant_id(value)
    except _OrderHistoryDataError as exc:
        raise _OrderHistoryRequestError("variant 格式无效。") from exc


def _data_variant_id(value: object) -> str:
    text = _data_text(value, maximum=64)
    if (
        not text[0].isascii()
        or not text[0].isalnum()
        or any(
            not character.isascii() or not (character.isalnum() or character in {"_", "-"})
            for character in text
        )
    ):
        raise _OrderHistoryDataError
    return text


def _data_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _OrderHistoryDataError
    return value


def _data_optional_text(value: object, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _data_text(value, maximum=maximum)


def _data_integer(value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _OrderHistoryDataError
    return value


def _data_optional_integer(value: object, *, minimum: int) -> int | None:
    if value is None:
        return None
    return _data_integer(value, minimum=minimum)


def _data_number(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _OrderHistoryDataError
    number = float(value)
    if (
        not isfinite(number)
        or (minimum is not None and number < minimum)
        or (maximum is not None and number > maximum)
    ):
        raise _OrderHistoryDataError
    return number


def _data_optional_number(value: object) -> float | None:
    if value is None:
        return None
    return _data_number(value)


def _data_boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _OrderHistoryDataError
    return value


def _project_signal_observation(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise _OrderHistoryDataError
    signal_number = _data_integer(raw.get("signal_number"), minimum=1)
    if signal_number > 3:
        raise _OrderHistoryDataError
    observed_at_ns = _data_integer(raw.get("observed_at_ns"), minimum=0)
    p_fair = _data_number(raw.get("p_fair"), minimum=0.0, maximum=1.0)
    maker_price = _data_number(raw.get("maker_price"), minimum=0.0, maximum=1.0)
    if p_fair in {0.0, 1.0} or maker_price in {0.0, 1.0}:
        raise _OrderHistoryDataError
    executable_vwap = _data_optional_number(raw.get("executable_vwap"))
    taker_fee = _data_optional_number(raw.get("taker_fee_per_share"))
    taker_net_edge = _data_optional_number(raw.get("taker_net_edge"))
    if executable_vwap is not None and not 0.0 <= executable_vwap <= 1.0:
        raise _OrderHistoryDataError
    if taker_fee is not None and taker_fee < 0.0:
        raise _OrderHistoryDataError
    return {
        "signal_number": signal_number,
        "observed_at_ns": observed_at_ns,
        "p_fair": p_fair,
        "maker_price": maker_price,
        "executable_vwap": executable_vwap,
        "taker_fee_per_share": taker_fee,
        "taker_net_edge": taker_net_edge,
    }


def _request_integer(value: object, label: str) -> int:
    try:
        return _data_integer(value, minimum=1)
    except _OrderHistoryDataError as exc:
        raise _OrderHistoryRequestError(f"{label} 格式无效。") from exc


def _reject_nonfinite_json(_value: str) -> None:
    raise _OrderHistoryDataError


def _snapshot_health(
    snapshot: BotDashboardSnapshot | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> dict[str, object]:
    if snapshot is None:
        return {"healthy": False, "reason": "missing_snapshot", "age_seconds": None}
    generated_at = snapshot.generated_at
    age_seconds = (now - generated_at).total_seconds()
    if age_seconds < -5.0:
        return {
            "healthy": False,
            "reason": "snapshot_timestamp_in_future",
            "age_seconds": age_seconds,
        }
    if age_seconds > max_age_seconds:
        return {"healthy": False, "reason": "stale_snapshot", "age_seconds": age_seconds}
    return {"healthy": True, "reason": "ok", "age_seconds": age_seconds}


def _shadow_projection(
    status: RuntimeStatus | None,
    errors: list[str],
) -> dict[str, object] | None:
    if status is None:
        return None
    value = status.details.get("last_shadow")
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append("opening_shadow last_shadow is not a JSON object")
        return None
    if value.get("orders_submitted") != 0:
        errors.append("opening_shadow must report zero submitted orders")
        return None
    return dict(value)


def create_dashboard_server(
    config: DashboardConfig,
    *,
    host: str,
    port: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ThreadingHTTPServer:
    if not host.strip():
        raise ValueError("host is required")
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._send_html(dashboard_html())
                return
            if path == "/api/orders":
                try:
                    limit, cursor, variant, epoch = _history_query(parsed.query)
                    history = build_order_history_payload(
                        config,
                        limit=limit,
                        cursor=cursor,
                        variant=variant,
                        epoch=epoch,
                    )
                except _OrderHistoryRequestError as exc:
                    self._send_json(
                        {"error": "invalid_request", "message": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                except _OrderHistoryDataError:
                    self._send_json(
                        {
                            "error": "invalid_order_history",
                            "message": _ORDER_HISTORY_ERROR_MESSAGE,
                        },
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self._send_json(history, status=HTTPStatus.OK)
                return
            payload = build_dashboard_payload(config, now=now())
            if path == "/api/status":
                self._send_json(payload, status=HTTPStatus.OK)
                return
            if path == "/healthz":
                status = (
                    HTTPStatus.OK
                    if bool(payload["health"]["healthy"])
                    else HTTPStatus.SERVICE_UNAVAILABLE
                )
                self._send_json(payload["health"], status=status)
                return
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            self._send_json({"error": "read_only"}, status=HTTPStatus.METHOD_NOT_ALLOWED)

        def log_message(self, _format: str, *_args: object) -> None:
            """Avoid per-poll request logs; persisted status is the audit surface."""

        def _send_json(self, value: object, *, status: HTTPStatus) -> None:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._send_common_headers("application/json; charset=utf-8", len(encoded))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_html(self, html: str) -> None:
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self._send_common_headers("text/html; charset=utf-8", len(encoded))
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
            )
            self.end_headers()
            self.wfile.write(encoded)

        def _send_common_headers(self, content_type: str, content_length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(content_length))

    return ThreadingHTTPServer((host, port), DashboardHandler)


def serve_dashboard(config: DashboardConfig, *, host: str, port: int) -> None:
    server = create_dashboard_server(config, host=host, port=port)
    print(f"BTC runtime dashboard listening on http://{host}:{port}")
    with server:
        server.serve_forever(poll_interval=0.5)


def _status_payload(
    status: RuntimeStatus, now: datetime, max_age_seconds: float
) -> dict[str, object]:
    health = check_runtime_health(status, now=now, max_age_seconds=max_age_seconds)
    return {
        "service": status.service,
        "mode": status.mode,
        "state": status.state,
        "healthy": status.healthy,
        "started_at": status.started_at.isoformat(),
        "updated_at": status.updated_at.isoformat(),
        "details": dict(status.details),
        "health": {
            "healthy": health.healthy,
            "reason": health.reason,
            "age_seconds": health.age_seconds,
        },
    }


def _status_max_age_seconds(
    status: RuntimeStatus | None,
    *,
    config: DashboardConfig,
) -> float:
    if status is None:
        return config.max_age_seconds
    interval = status.details.get("expected_status_interval_seconds")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        return config.max_age_seconds
    seconds = float(interval)
    if not isfinite(seconds) or seconds <= 0.0:
        return config.max_age_seconds
    return max(config.max_age_seconds, seconds * config.status_interval_grace_factor)
