from __future__ import annotations

from datetime import UTC, datetime

import pytest

from btc_short_horizon.data import (
    BTC_15M_MARKET_FAMILY,
    MarketCatalog,
    MarketOutcome,
    MarketValidationError,
    MarketWindow,
    market_catalog_payload,
    read_market_catalog,
    write_market_catalog,
)


def _window() -> MarketWindow:
    t0 = datetime(2026, 4, 13, tzinfo=UTC)
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id="condition",
        up_token_id="up-token",
        down_token_id="down-token",
        t0=t0,
        t1=t0 + BTC_15M_MARKET_FAMILY.window_seconds_as_timedelta,
        rule_epoch="chainlink-btc-usd-v1",
        rule_hash="a" * 64,
        resolution=MarketOutcome.UP,
        label_available_ts=datetime(2026, 4, 13, 0, 16, tzinfo=UTC),
    )


def test_market_catalog_round_trip_preserves_validated_token_mapping(tmp_path) -> None:  # type: ignore[no-untyped-def]
    catalog = MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(_window(),))
    path = tmp_path / "btc-15m.json"

    write_market_catalog(path=path, catalog=catalog, collected_at=datetime(2026, 4, 13, tzinfo=UTC))

    restored = read_market_catalog(path)
    market = restored.require(_window().slug)
    assert market.up_token_id == "up-token"
    assert market.down_token_id == "down-token"
    assert market.resolution is MarketOutcome.UP


def test_market_catalog_payload_rejects_timestamp_without_timezone() -> None:
    catalog = MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(_window(),))

    with pytest.raises(MarketValidationError, match="timezone"):
        market_catalog_payload(catalog=catalog, collected_at=datetime(2026, 4, 13))


def test_market_catalog_reader_rejects_unknown_market_family(tmp_path) -> None:  # type: ignore[no-untyped-def]
    catalog = MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(_window(),))
    path = tmp_path / "btc-15m.json"
    write_market_catalog(path=path, catalog=catalog, collected_at=datetime(2026, 4, 13, tzinfo=UTC))
    content = path.read_text(encoding="utf-8").replace(
        '"family": "btc_updown_15m"', '"family": "other"'
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(MarketValidationError, match="unknown family"):
        read_market_catalog(path)
