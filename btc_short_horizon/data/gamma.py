"""Gamma market discovery and BTC Up/Down metadata normalization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
import json
import httpx

from btc_short_horizon.data.contracts import (
    BtcMarketFamily,
    MarketOutcome,
    MarketValidationError,
    MarketWindow,
)
from btc_short_horizon.data.market_catalog import MarketCatalog

_GAMMA_MARKETS_KEYSET_URL = "https://gamma-api.polymarket.com/markets/keyset"


class GammaMarketClient:
    """Small public Gamma client with explicit pagination and no hidden cache."""

    def __init__(
        self, *, base_url: str = _GAMMA_MARKETS_KEYSET_URL, timeout_seconds: float = 20.0
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Gamma base_url must use https")
        if timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be > 0")
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    async def discover_catalog(
        self,
        *,
        family: BtcMarketFamily,
        rule_epoch: str,
        closed: bool | None = None,
        page_size: int = 100,
        slugs: Sequence[str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> MarketCatalog:
        """Fetch matching Gamma markets into a validated catalog.

        ``slugs`` uses Gamma's server-side exact-slug filter. It is the normal
        live-discovery path because a current BTC window is deterministic and
        does not require paging through unrelated markets.
        """

        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be in [1, 100]")
        requested_slugs = _requested_slugs(family, slugs)
        owns_client = client is None
        active_client = client or httpx.AsyncClient(timeout=self.timeout_seconds)
        catalog = MarketCatalog(families=(family,))
        try:
            for closed_state in _closed_states(closed):
                cursor: str | None = None
                seen_cursors: set[str] = set()
                while True:
                    params: list[tuple[str, str]] = [
                        ("limit", str(page_size)),
                        ("closed", str(closed_state).lower()),
                    ]
                    params.extend(("slug", slug) for slug in requested_slugs)
                    if cursor is not None:
                        params.append(("after_cursor", cursor))
                    response = await active_client.get(self.base_url, params=params)
                    response.raise_for_status()
                    markets, next_cursor = _keyset_page(response.json())
                    for item in markets:
                        if not isinstance(item, Mapping):
                            continue
                        slug = item.get("slug")
                        if not isinstance(slug, str):
                            continue
                        try:
                            family.parse_slug(slug)
                        except MarketValidationError:
                            continue
                        window = gamma_market_to_window(item, family=family, rule_epoch=rule_epoch)
                        existing = catalog.get(window.slug)
                        if existing is None:
                            catalog.register(window)
                        elif existing != window:
                            raise MarketValidationError(
                                f"Gamma market {window.slug!r} changed during discovery; rerun it."
                            )
                    if next_cursor is None:
                        break
                    if next_cursor in seen_cursors:
                        raise ValueError("Gamma keyset pagination repeated a cursor")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
            return catalog
        finally:
            if owns_client:
                await active_client.aclose()


def gamma_market_to_window(
    payload: Mapping[str, object], *, family: BtcMarketFamily, rule_epoch: str
) -> MarketWindow:
    """Convert one Gamma response into a market window without outcome look-ahead."""

    slug = _text(payload.get("slug"), "slug")
    t0 = family.parse_slug(slug)
    outcomes = _string_list(payload.get("outcomes"), "outcomes")
    token_ids = _string_list(
        _first_present(payload, "clobTokenIds", "clob_token_ids"), "clobTokenIds"
    )
    if len(outcomes) != 2 or len(token_ids) != 2:
        raise MarketValidationError(
            "BTC Up/Down market requires exactly two outcomes and token IDs"
        )
    outcome_to_token = {
        outcome.casefold(): token for outcome, token in zip(outcomes, token_ids, strict=True)
    }
    try:
        up_token_id = outcome_to_token["up"]
        down_token_id = outcome_to_token["down"]
    except KeyError as exc:
        raise MarketValidationError("BTC market outcomes must include Up and Down") from exc
    closed = _bool(payload.get("closed", False), "closed")
    resolution = _resolution_from_payload(payload, outcomes=outcomes) if closed else None
    label_available_ts = (
        _label_available_time(payload, minimum=t0 + family.window_seconds_as_timedelta)
        if resolution is not None
        else None
    )
    return MarketWindow(
        family=family,
        slug=slug,
        condition_id=_text(_first_present(payload, "conditionId", "condition_id"), "conditionId"),
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        t0=t0,
        t1=t0 + family.window_seconds_as_timedelta,
        rule_epoch=rule_epoch,
        rule_hash=gamma_rule_hash(payload),
        resolution=resolution,
        label_available_ts=label_available_ts,
    )


def _closed_states(closed: bool | None) -> tuple[bool, ...]:
    """Query both API states when the caller deliberately requests all markets."""

    return (False, True) if closed is None else (closed,)


def _requested_slugs(family: BtcMarketFamily, slugs: Sequence[str] | None) -> tuple[str, ...]:
    if slugs is None:
        return ()
    requested = tuple(dict.fromkeys(slugs))
    for slug in requested:
        family.parse_slug(slug)
    return requested


def _keyset_page(value: object) -> tuple[Sequence[object], str | None]:
    if not isinstance(value, Mapping):
        raise ValueError("Gamma keyset endpoint must return an object")
    markets = value.get("markets")
    if not isinstance(markets, list):
        raise ValueError("Gamma keyset response markets must be an array")
    next_cursor = value.get("next_cursor")
    if next_cursor is None:
        return markets, None
    if not isinstance(next_cursor, str) or not next_cursor:
        raise ValueError("Gamma keyset response next_cursor must be a non-empty string")
    return markets, next_cursor


def gamma_rule_hash(payload: Mapping[str, object]) -> str:
    """Hash the Gamma rule-bearing fields independently of mutable prices/volume."""

    rule_fields = {
        key: payload.get(key)
        for key in (
            "question",
            "description",
            "resolutionSource",
            "resolution_source",
            "rules",
            "marketType",
            "endDate",
            "endDateIso",
        )
    }
    encoded = json.dumps(rule_fields, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(encoded).hexdigest()


def _resolution_from_payload(
    payload: Mapping[str, object], *, outcomes: Sequence[str]
) -> MarketOutcome:
    prices = _string_list(payload.get("outcomePrices"), "outcomePrices")
    if len(prices) != len(outcomes):
        raise MarketValidationError("closed market outcomePrices must map one-to-one to outcomes")
    numeric_prices = tuple(_float(price, "outcomePrices") for price in prices)
    max_index = max(range(len(numeric_prices)), key=numeric_prices.__getitem__)
    if numeric_prices[max_index] < 1.0 - 1e-9:
        return MarketOutcome.VOID
    winner = outcomes[max_index].casefold()
    if winner == "up":
        return MarketOutcome.UP
    if winner == "down":
        return MarketOutcome.DOWN
    return MarketOutcome.VOID


def _label_available_time(payload: Mapping[str, object], *, minimum: datetime) -> datetime:
    value = _first_present(
        payload, "closedTime", "closed_time", "updatedAt", "updated_at", "endDate"
    )
    if value is None:
        return minimum
    parsed = _timestamp(value)
    return max(parsed, minimum)


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MarketValidationError(f"{name} must be a JSON string array or array") from exc
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise MarketValidationError(f"{name} must be an array")
    items = tuple(_text(item, name) for item in value)
    return items


def _timestamp(value: object) -> datetime:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value) / 1_000, UTC)
    if not isinstance(value, str):
        raise MarketValidationError("timestamp must be ISO-8601 or milliseconds")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise MarketValidationError("timestamp is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketValidationError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _float(value: object, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise MarketValidationError(f"{name} must be numeric") from exc


def _bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    raise MarketValidationError(f"{name} must be bool")


def _first_present(payload: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None
