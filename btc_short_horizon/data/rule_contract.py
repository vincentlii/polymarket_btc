"""Fail-closed BTC 15m resolution-rule epoch classification."""

from __future__ import annotations

from collections.abc import Mapping
import re

from btc_short_horizon.data.contracts import MarketValidationError


CHAINLINK_BTC_USD_POINT_V1 = "chainlink-btc-usd-point-v1"
CHAINLINK_BTC_USD_TWAP_60S_V1 = "chainlink-btc-usd-twap-60s-v1"

_POINT_SOURCE = "https://data.chain.link/streams/btc-usd"
_TWAP_60S_SOURCE = "https://data.chain.link/streams/btc-usd-twap-60s-streams"
_TEXT_FIELDS = (
    "question",
    "description",
    "rules",
    "resolutionDescription",
    "cryptoMarketConfigId",
)
_BTC_USD = re.compile(r"\bbtc\s*/\s*usd\b|\bbitcoin\b", re.IGNORECASE)
_TWAP = re.compile(r"\btwap\b|time[- ]weighted average", re.IGNORECASE)
_SIXTY_SECONDS = re.compile(r"\b60(?:[- ]?second|s)\b", re.IGNORECASE)


def classify_btc_15m_rule_epoch(payload: Mapping[str, object]) -> str:
    """Classify only explicit Gamma rule text; never infer the epoch from time."""

    source = _resolution_source(payload)
    rule_text = _rule_text(payload)
    if not _BTC_USD.search(rule_text):
        raise MarketValidationError("cannot classify BTC 15m rule epoch from Gamma metadata")
    is_twap = bool(_TWAP.search(rule_text))
    has_sixty_second_window = bool(_SIXTY_SECONDS.search(rule_text))
    if source == _POINT_SOURCE:
        if is_twap or has_sixty_second_window:
            raise MarketValidationError("conflicting BTC 15m point rule metadata")
        return CHAINLINK_BTC_USD_POINT_V1
    if source == _TWAP_60S_SOURCE:
        if not is_twap or not has_sixty_second_window:
            raise MarketValidationError("conflicting BTC 15m 60-second TWAP rule metadata")
        return CHAINLINK_BTC_USD_TWAP_60S_V1
    raise MarketValidationError("cannot classify BTC 15m rule epoch from Gamma metadata")


def require_btc_15m_rule_epoch(payload: Mapping[str, object], *, expected_epoch: str) -> str:
    """Verify the caller's expected epoch against immutable rule-bearing metadata."""

    observed = classify_btc_15m_rule_epoch(payload)
    if observed != expected_epoch:
        raise MarketValidationError(
            f"BTC 15m rule epoch mismatch: expected {expected_epoch!r}, observed {observed!r}"
        )
    return observed


def _rule_text(payload: Mapping[str, object]) -> str:
    values: list[str] = []
    for field in _TEXT_FIELDS:
        value = payload.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            values.append(value)
    return "\n".join(values)


def _resolution_source(payload: Mapping[str, object]) -> str:
    values = {
        value.strip()
        for field in ("resolutionSource", "resolution_source")
        if isinstance((value := payload.get(field)), str) and value.strip()
    }
    if len(values) != 1:
        raise MarketValidationError("cannot classify BTC 15m rule epoch from Gamma metadata")
    return values.pop()
