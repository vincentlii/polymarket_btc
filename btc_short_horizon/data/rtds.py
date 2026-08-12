"""Polymarket RTDS Chainlink BTC/USD reference-price normalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from math import isfinite
from typing import Mapping

from btc_short_horizon.data.contracts import TimedMarketEvent
from btc_short_horizon.features.events import BtcReferencePrice


_E18 = Decimal(10) ** 18
_DISPLAY_VALUE_TOLERANCE = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class RtdsReferenceRecord:
    timing: TimedMarketEvent
    reference: BtcReferencePrice


@dataclass(frozen=True, slots=True)
class RtdsTwapRecord:
    """Raw official 60-second Chainlink TWAP evidence; it is not a model feature."""

    timing: TimedMarketEvent
    publication_ts: datetime
    value: Decimal
    full_accuracy_value: str
    display_value: Decimal | None
    window_seconds: int


def normalize_chainlink_btc_usd(
    message: Mapping[str, object], *, collector_receive_ts: datetime
) -> RtdsReferenceRecord:
    """Normalize the documented RTDS ``crypto_prices_chainlink`` update format."""

    if message.get("topic") != "crypto_prices_chainlink":
        raise ValueError("RTDS message must come from crypto_prices_chainlink")
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("RTDS message payload must be an object")
    symbol = _text(payload.get("symbol"), "payload.symbol").casefold()
    if symbol != "btc/usd":
        raise ValueError("RTDS reference must be btc/usd")
    source_ts = _timestamp(payload.get("timestamp"), "payload.timestamp")
    receive_ts = _as_utc(collector_receive_ts)
    available_ts = max(source_ts, receive_ts)
    value = _positive_float(payload.get("value"), "payload.value")
    sequence = sha256(
        json.dumps(message, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    timing = TimedMarketEvent(
        source_ts=source_ts,
        collector_receive_ts=receive_ts,
        available_ts=available_ts,
        sequence_or_hash=sequence,
        source="polymarket_rtds_chainlink",
        instrument=symbol,
        schema_version="polymarket-rtds-v1",
        ingest_version="btc-short-horizon-v1",
    )
    return RtdsReferenceRecord(
        timing=timing,
        reference=BtcReferencePrice(
            source_ts_ns=_datetime_to_ns(source_ts),
            available_ts_ns=_datetime_to_ns(available_ts),
            price=value,
            source="chainlink",
            instrument=symbol,
        ),
    )


def normalize_chainlink_btc_usd_twap_60s(
    message: Mapping[str, object], *, collector_receive_ts: datetime
) -> RtdsTwapRecord:
    """Normalize one official RTDS 60-second TWAP update without reconstruction."""

    if message.get("topic") != "crypto_prices_twap_sixty" or message.get("type") != "update":
        raise ValueError("RTDS message must be a crypto_prices_twap_sixty update")
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("RTDS message payload must be an object")
    symbol = _text(payload.get("symbol"), "payload.symbol").casefold()
    if symbol != "btc/usd":
        raise ValueError("RTDS TWAP reference must be btc/usd")
    if payload.get("window_s") != 60:
        raise ValueError("RTDS TWAP reference must have window_s=60")
    source_ts = _timestamp(payload.get("timestamp"), "payload.timestamp")
    publication_ts = _timestamp(message.get("timestamp"), "timestamp")
    receive_ts = _as_utc(collector_receive_ts)
    full_accuracy_value = payload.get("full_accuracy_value")
    if (
        not isinstance(full_accuracy_value, str)
        or not full_accuracy_value
        or not full_accuracy_value.lstrip("-").isdigit()
    ):
        raise ValueError("payload.full_accuracy_value must be a signed E18 integer string")
    value = Decimal(int(full_accuracy_value)) / _E18
    if value <= 0:
        raise ValueError("payload.full_accuracy_value must represent a value > 0")
    display_value = None
    if payload.get("value") is not None:
        display_value = _decimal(payload.get("value"), "payload.value")
        if abs(display_value - value) > _DISPLAY_VALUE_TOLERANCE:
            raise ValueError("payload display value disagrees with full_accuracy_value")
    sequence = sha256(
        json.dumps(message, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    timing = TimedMarketEvent(
        source_ts=source_ts,
        collector_receive_ts=receive_ts,
        available_ts=max(source_ts, receive_ts),
        sequence_or_hash=sequence,
        source="polymarket_rtds_chainlink_twap_60s",
        instrument=symbol,
        schema_version="polymarket-rtds-chainlink-twap-v1",
        ingest_version="btc-short-horizon-v1",
    )
    return RtdsTwapRecord(
        timing=timing,
        publication_ts=publication_ts,
        value=value,
        full_accuracy_value=full_accuracy_value,
        display_value=display_value,
        window_seconds=60,
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _timestamp(value: object, name: str) -> datetime:
    try:
        millis = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be epoch milliseconds") from exc
    if millis < 0:
        raise ValueError(f"{name} must be non-negative")
    return datetime.fromtimestamp(millis / 1_000, UTC)


def _positive_float(value: object, name: str) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(numeric) or numeric <= 0.0:
        raise ValueError(f"{name} must be finite and > 0")
    return numeric


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal")
    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not numeric.is_finite() or numeric <= 0:
        raise ValueError(f"{name} must be finite and > 0")
    return numeric


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("collector_receive_ts must be timezone-aware")
    return value.astimezone(UTC)


def _datetime_to_ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)
