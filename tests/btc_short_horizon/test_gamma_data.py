from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import httpx

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketValidationError
from btc_short_horizon.data.gamma import GammaMarketClient, gamma_market_to_window, gamma_rule_hash
from btc_short_horizon.data.rule_contract import (
    CHAINLINK_BTC_USD_POINT_V1,
    CHAINLINK_BTC_USD_TWAP_60S_V1,
    classify_btc_15m_rule_epoch,
)


T0 = datetime(2026, 4, 13, tzinfo=UTC)


def _payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "slug": BTC_15M_MARKET_FAMILY.slug_for(T0),
        "conditionId": "condition",
        "outcomes": '["Down", "Up"]',
        "clobTokenIds": '["down-token", "up-token"]',
        "closed": True,
        "outcomePrices": '["0", "1"]',
        "updatedAt": "2026-04-13T00:16:00Z",
        "question": "Bitcoin Up or Down?",
        "description": "Bitcoin price at the end of the time range determines the outcome.",
        "resolutionSource": "https://data.chain.link/streams/btc-usd",
    }
    values.update(overrides)
    return values


def test_gamma_market_maps_outcomes_not_token_position_and_isolates_label_time() -> None:
    window = gamma_market_to_window(
        _payload(), family=BTC_15M_MARKET_FAMILY, rule_epoch=CHAINLINK_BTC_USD_POINT_V1
    )

    assert window.up_token_id == "up-token"
    assert window.down_token_id == "down-token"
    assert window.resolution is MarketOutcome.UP
    assert window.label_available_ts == T0 + timedelta(minutes=16)
    assert len(window.rule_hash) == 64


def test_gamma_rule_hash_changes_for_rule_text_not_for_prices() -> None:
    base = _payload()
    changed_price = _payload(outcomePrices='["1", "0"]')
    changed_rule = _payload(description="A changed resolution rule.")

    assert gamma_rule_hash(base) == gamma_rule_hash(changed_price)
    assert gamma_rule_hash(base) != gamma_rule_hash(changed_rule)


def test_gamma_rule_contract_classifies_adjacent_point_and_twap_metadata() -> None:
    point = _payload()
    twap = _payload(
        slug=BTC_15M_MARKET_FAMILY.slug_for(T0 + timedelta(minutes=15)),
        description="Resolves using the official Chainlink BTC/USD 60-second TWAP.",
        resolutionSource="https://data.chain.link/streams/btc-usd-twap-60s-streams",
    )

    assert classify_btc_15m_rule_epoch(point) == CHAINLINK_BTC_USD_POINT_V1
    assert classify_btc_15m_rule_epoch(twap) == CHAINLINK_BTC_USD_TWAP_60S_V1


def test_gamma_rule_contract_rejects_wrong_or_ambiguous_expected_epoch() -> None:
    with pytest.raises(MarketValidationError, match="rule epoch mismatch"):
        gamma_market_to_window(
            _payload(),
            family=BTC_15M_MARKET_FAMILY,
            rule_epoch=CHAINLINK_BTC_USD_TWAP_60S_V1,
        )
    with pytest.raises(MarketValidationError, match="cannot classify"):
        classify_btc_15m_rule_epoch(
            _payload(
                description="Uses a stated BTC reference.",
                resolutionSource="https://data.chain.link/streams/unknown",
            )
        )


def test_gamma_rule_contract_accepts_official_point_source_and_rejects_conflicts() -> None:
    official_point = _payload(
        description=(
            "This market will resolve to 'Up' if the Bitcoin price at the end of the time "
            "range is greater than or equal to the Bitcoin price at the start of the time "
            "range. Otherwise, this market will resolve to 'Down'."
        ),
        resolutionSource="https://data.chain.link/streams/btc-usd",
    )

    assert classify_btc_15m_rule_epoch(official_point) == CHAINLINK_BTC_USD_POINT_V1
    with pytest.raises(MarketValidationError, match="conflicting"):
        classify_btc_15m_rule_epoch(
            _payload(
                description="The official Chainlink BTC/USD 60-second TWAP determines this market.",
                resolutionSource="https://data.chain.link/streams/btc-usd",
            )
        )
    with pytest.raises(MarketValidationError, match="conflicting"):
        classify_btc_15m_rule_epoch(
            _payload(
                description=official_point["description"],
                resolutionSource="https://data.chain.link/streams/btc-usd-twap-60s-streams",
            )
        )


def test_gamma_parser_rejects_non_up_down_markets() -> None:
    with pytest.raises(MarketValidationError, match="include Up and Down"):
        gamma_market_to_window(
            _payload(outcomes='["Yes", "No"]'),
            family=BTC_15M_MARKET_FAMILY,
            rule_epoch=CHAINLINK_BTC_USD_POINT_V1,
        )


@pytest.mark.asyncio
async def test_gamma_discovery_uses_keyset_cursors_for_open_and_closed_markets() -> None:
    captured: list[dict[str, str]] = []
    second_open_slug = BTC_15M_MARKET_FAMILY.slug_for(T0 + timedelta(minutes=15))
    closed_slug = BTC_15M_MARKET_FAMILY.slug_for(T0 + timedelta(minutes=30))

    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        captured.append(query)
        if query == {"limit": "100", "closed": "false"}:
            return httpx.Response(
                200,
                json={
                    "markets": [_payload(closed=False)],
                    "next_cursor": "open-page-2",
                },
            )
        if query == {
            "limit": "100",
            "closed": "false",
            "after_cursor": "open-page-2",
        }:
            return httpx.Response(
                200, json={"markets": [_payload(slug=second_open_slug, closed=False)]}
            )
        if query == {"limit": "100", "closed": "true"}:
            return httpx.Response(200, json={"markets": [_payload(slug=closed_slug)]})
        raise AssertionError(f"unexpected request query {query}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = await GammaMarketClient().discover_catalog(
            family=BTC_15M_MARKET_FAMILY,
            rule_epoch=CHAINLINK_BTC_USD_POINT_V1,
            closed=None,
            client=client,
        )

    assert len(catalog) == 3
    assert catalog.require(second_open_slug).resolution is None
    assert catalog.require(closed_slug).resolution is MarketOutcome.UP
    assert captured == [
        {"limit": "100", "closed": "false"},
        {"limit": "100", "closed": "false", "after_cursor": "open-page-2"},
        {"limit": "100", "closed": "true"},
    ]


@pytest.mark.asyncio
async def test_gamma_discovery_uses_exact_slug_filter() -> None:
    requested_slug = BTC_15M_MARKET_FAMILY.slug_for(T0)
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return httpx.Response(200, json={"markets": [_payload(closed=False)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        catalog = await GammaMarketClient().discover_catalog(
            family=BTC_15M_MARKET_FAMILY,
            rule_epoch=CHAINLINK_BTC_USD_POINT_V1,
            closed=False,
            slugs=(requested_slug,),
            client=client,
        )

    assert catalog.require(requested_slug).resolution is None
    assert captured == [{"limit": "100", "closed": "false", "slug": requested_slug}]


@pytest.mark.asyncio
async def test_gamma_discovery_rejects_page_sizes_above_gamma_limit() -> None:
    with pytest.raises(ValueError, match="\\[1, 100\\]"):
        await GammaMarketClient().discover_catalog(
            family=BTC_15M_MARKET_FAMILY,
            rule_epoch=CHAINLINK_BTC_USD_POINT_V1,
            page_size=101,
        )
