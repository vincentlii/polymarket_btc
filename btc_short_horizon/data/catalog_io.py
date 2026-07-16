"""Persist validated BTC market catalogs without retaining mutable outcome data in strategy state."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

from btc_short_horizon.data.contracts import (
    BtcMarketFamily,
    MarketCollectionMode,
    MarketOutcome,
    MarketValidationError,
    MarketWindow,
)
from btc_short_horizon.data.market_catalog import MarketCatalog


CATALOG_SCHEMA_VERSION = "btc-market-catalog-v1"


def write_market_catalog(
    *, path: Path, catalog: MarketCatalog, collected_at: datetime | None = None
) -> None:
    """Atomically write a validated market catalog for later collector selection."""

    collected_at = _normalize_timestamp(collected_at or datetime.now(UTC), "collected_at")
    payload = market_catalog_payload(catalog=catalog, collected_at=collected_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def market_catalog_payload(*, catalog: MarketCatalog, collected_at: datetime) -> dict[str, object]:
    """Return the stable JSON representation of one validated market catalog."""

    normalized_collected_at = _normalize_timestamp(collected_at, "collected_at")
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "source": "gamma",
        "collected_at": normalized_collected_at.isoformat(),
        "families": [_family_record(family) for family in catalog.families],
        "markets": [_market_record(window) for window in catalog.windows()],
    }


def read_market_catalog(path: Path) -> MarketCatalog:
    """Load and validate a catalog written by :func:`write_market_catalog`."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MarketValidationError(f"market catalog JSON is invalid: {path}") from exc
    if not isinstance(payload, Mapping):
        raise MarketValidationError("market catalog must be a JSON object")
    if payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise MarketValidationError("unsupported market catalog schema_version")
    if payload.get("source") != "gamma":
        raise MarketValidationError("market catalog source must be gamma")
    _parse_timestamp(_require_value(payload, "collected_at"), "collected_at")
    families_value = _require_value(payload, "families")
    if not isinstance(families_value, list) or not families_value:
        raise MarketValidationError("market catalog families must be a non-empty array")
    families = tuple(_family_from_record(item) for item in families_value)
    markets_value = _require_value(payload, "markets")
    if not isinstance(markets_value, list):
        raise MarketValidationError("market catalog markets must be an array")
    catalog = MarketCatalog(families=families)
    families_by_name = {family.name: family for family in families}
    for item in markets_value:
        catalog.register(_market_from_record(item, families_by_name=families_by_name))
    return catalog


def _family_record(family: BtcMarketFamily) -> dict[str, object]:
    return {
        "name": family.name,
        "slug_prefix": family.slug_prefix,
        "window_seconds": family.window_seconds,
        "collection_mode": family.collection_mode.value,
    }


def _market_record(window: MarketWindow) -> dict[str, object]:
    return {
        "family": window.family.name,
        "slug": window.slug,
        "condition_id": window.condition_id,
        "up_token_id": window.up_token_id,
        "down_token_id": window.down_token_id,
        "t0": window.t0.isoformat(),
        "t1": window.t1.isoformat(),
        "rule_epoch": window.rule_epoch,
        "rule_hash": window.rule_hash,
        "resolution": None if window.resolution is None else window.resolution.value,
        "label_available_ts": (
            None if window.label_available_ts is None else window.label_available_ts.isoformat()
        ),
    }


def _family_from_record(value: object) -> BtcMarketFamily:
    if not isinstance(value, Mapping):
        raise MarketValidationError("market catalog family must be an object")
    try:
        return BtcMarketFamily(
            name=_require_text(value, "name"),
            slug_prefix=_require_text(value, "slug_prefix"),
            window_seconds=_require_int(value, "window_seconds"),
            collection_mode=MarketCollectionMode(_require_text(value, "collection_mode")),
        )
    except ValueError as exc:
        raise MarketValidationError("market catalog family is invalid") from exc


def _market_from_record(
    value: object, *, families_by_name: Mapping[str, BtcMarketFamily]
) -> MarketWindow:
    if not isinstance(value, Mapping):
        raise MarketValidationError("market catalog market must be an object")
    family_name = _require_text(value, "family")
    try:
        family = families_by_name[family_name]
    except KeyError as exc:
        raise MarketValidationError(
            f"market catalog references unknown family {family_name!r}"
        ) from exc
    resolution_value = value.get("resolution")
    label_available_value = value.get("label_available_ts")
    try:
        resolution = (
            None if resolution_value is None else MarketOutcome(_require_text(value, "resolution"))
        )
        label_available_ts = (
            None
            if label_available_value is None
            else _parse_timestamp(label_available_value, "label_available_ts")
        )
        return MarketWindow(
            family=family,
            slug=_require_text(value, "slug"),
            condition_id=_require_text(value, "condition_id"),
            up_token_id=_require_text(value, "up_token_id"),
            down_token_id=_require_text(value, "down_token_id"),
            t0=_parse_timestamp(_require_value(value, "t0"), "t0"),
            t1=_parse_timestamp(_require_value(value, "t1"), "t1"),
            rule_epoch=_require_text(value, "rule_epoch"),
            rule_hash=_require_text(value, "rule_hash"),
            resolution=resolution,
            label_available_ts=label_available_ts,
        )
    except ValueError as exc:
        raise MarketValidationError("market catalog market is invalid") from exc


def _require_value(value: Mapping[str, object], name: str) -> object:
    try:
        return value[name]
    except KeyError as exc:
        raise MarketValidationError(f"market catalog is missing {name}") from exc


def _require_text(value: Mapping[str, object], name: str) -> str:
    text = _require_value(value, name)
    if not isinstance(text, str) or not text.strip():
        raise MarketValidationError(f"market catalog {name} must be a non-empty string")
    return text.strip()


def _require_int(value: Mapping[str, object], name: str) -> int:
    integer = _require_value(value, name)
    if isinstance(integer, bool) or not isinstance(integer, int):
        raise MarketValidationError(f"market catalog {name} must be an integer")
    return integer


def _normalize_timestamp(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketValidationError(f"market catalog {name} must include a timezone")
    return value.astimezone(UTC)


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise MarketValidationError(f"market catalog {name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketValidationError(f"market catalog {name} must be ISO-8601") from exc
    return _normalize_timestamp(parsed, name)


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "market_catalog_payload",
    "read_market_catalog",
    "write_market_catalog",
]
