from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btc_short_horizon.data import (
    BTC_15M_MARKET_FAMILY,
    BTC_5M_MARKET_FAMILY,
    BtcMarketFamily,
    MarketCatalog,
    MarketCollectionMode,
    MarketOutcome,
    MarketValidationError,
    MarketWindow,
    TimedMarketEvent,
)

T0 = datetime(2026, 4, 13, 0, 0, tzinfo=UTC)
RULE_HASH = "a" * 64


def make_window(
    *,
    family: BtcMarketFamily = BTC_15M_MARKET_FAMILY,
    t0: datetime = T0,
    resolution: MarketOutcome | None = MarketOutcome.UP,
    label_available_ts: datetime | None = None,
    rule_epoch: str = "chainlink-v1",
    rule_hash: str = RULE_HASH,
    up_token_id: str = "up-token",
    down_token_id: str = "down-token",
) -> MarketWindow:
    t1 = t0 + timedelta(seconds=family.window_seconds)
    if resolution is not None and label_available_ts is None:
        label_available_ts = t1
    return MarketWindow(
        family=family,
        slug=family.slug_for(t0),
        condition_id=f"condition-{int(t0.timestamp())}",
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        t0=t0,
        t1=t1,
        rule_epoch=rule_epoch,
        rule_hash=rule_hash,
        resolution=resolution,
        label_available_ts=label_available_ts,
    )


def test_default_market_families_encode_15m_complete_and_5m_collection_only() -> None:
    assert BTC_15M_MARKET_FAMILY.collection_mode is MarketCollectionMode.COMPLETE
    assert not BTC_15M_MARKET_FAMILY.is_collection_only
    assert BTC_5M_MARKET_FAMILY.collection_mode is MarketCollectionMode.COLLECTION_ONLY
    assert BTC_5M_MARKET_FAMILY.is_collection_only


@pytest.mark.parametrize(
    ("family", "expected_suffix"),
    (
        (BTC_15M_MARKET_FAMILY, "15m"),
        (BTC_5M_MARKET_FAMILY, "5m"),
    ),
)
def test_epoch_slug_round_trip_for_configured_families(
    family: BtcMarketFamily,
    expected_suffix: str,
) -> None:
    slug = family.slug_for(T0)

    assert slug == f"btc-updown-{expected_suffix}-{int(T0.timestamp())}"
    assert family.parse_slug(slug) == T0


@pytest.mark.parametrize(
    "slug",
    (
        "btc-updown-10m-1776038400",
        "btc-updown-15m-1776038401",
        "btc-updown-15m-01776038400",
        "BTC-updown-15m-1776038400",
        "btc-updown-15m-1776038400 ",
        "eth-updown-15m-1776038400",
    ),
)
def test_invalid_15m_slugs_are_rejected(slug: str) -> None:
    with pytest.raises(MarketValidationError):
        BTC_15M_MARKET_FAMILY.parse_slug(slug)


def test_custom_market_family_is_configurable_but_requires_minute_alignment() -> None:
    family = BtcMarketFamily(
        name="btc_updown_30m",
        slug_prefix="btc-updown",
        window_seconds=30 * 60,
        collection_mode="complete",
    )

    assert family.slug_for(T0) == f"btc-updown-30m-{int(T0.timestamp())}"
    with pytest.raises(MarketValidationError, match="align"):
        family.slug_for(T0 + timedelta(minutes=15))


def test_market_window_accepts_two_tokens_rule_version_and_available_label() -> None:
    window = make_window()

    assert window.slug == BTC_15M_MARKET_FAMILY.slug_for(T0)
    assert window.t1 - window.t0 == timedelta(minutes=15)
    assert window.rule_epoch == "chainlink-v1"
    assert window.rule_hash == RULE_HASH
    assert window.winning_token_id == "up-token"
    assert window.label_available_ts == window.t1


@pytest.mark.parametrize(
    "kwargs",
    (
        {"up_token_id": "same", "down_token_id": "same"},
        {"rule_epoch": "  "},
        {"rule_hash": "not-a-sha256"},
    ),
)
def test_market_window_rejects_invalid_token_or_rule_contract(kwargs: dict[str, str]) -> None:
    with pytest.raises(MarketValidationError):
        make_window(**kwargs)


def test_market_window_rejects_noncanonical_slug_and_wrong_window_length() -> None:
    valid = make_window()
    with pytest.raises(MarketValidationError, match="canonical"):
        MarketWindow(
            family=valid.family,
            slug=BTC_5M_MARKET_FAMILY.slug_for(T0),
            condition_id=valid.condition_id,
            up_token_id=valid.up_token_id,
            down_token_id=valid.down_token_id,
            t0=valid.t0,
            t1=valid.t1,
            rule_epoch=valid.rule_epoch,
            rule_hash=valid.rule_hash,
        )
    with pytest.raises(MarketValidationError, match="t1"):
        MarketWindow(
            family=valid.family,
            slug=valid.slug,
            condition_id=valid.condition_id,
            up_token_id=valid.up_token_id,
            down_token_id=valid.down_token_id,
            t0=valid.t0,
            t1=valid.t1 + timedelta(seconds=1),
            rule_epoch=valid.rule_epoch,
            rule_hash=valid.rule_hash,
        )


def test_label_availability_requires_a_final_label_after_market_close() -> None:
    with pytest.raises(MarketValidationError, match="requires a final"):
        make_window(resolution=None, label_available_ts=T0)

    unresolved = make_window(resolution=None)
    with pytest.raises(MarketValidationError, match="requires label_available"):
        MarketWindow(
            family=unresolved.family,
            slug=unresolved.slug,
            condition_id=unresolved.condition_id,
            up_token_id=unresolved.up_token_id,
            down_token_id=unresolved.down_token_id,
            t0=unresolved.t0,
            t1=unresolved.t1,
            rule_epoch=unresolved.rule_epoch,
            rule_hash=unresolved.rule_hash,
            resolution=MarketOutcome.DOWN,
        )
    with pytest.raises(MarketValidationError, match="cannot precede"):
        make_window(label_available_ts=T0 + timedelta(minutes=14, seconds=59))


def test_catalog_parses_registers_and_orders_market_windows() -> None:
    earlier = make_window(t0=T0)
    later = make_window(t0=T0 + timedelta(minutes=15), rule_epoch="chainlink-v2")
    catalog = MarketCatalog(windows=(later, earlier))

    parsed = catalog.parse_slug(earlier.slug)
    assert parsed.family is BTC_15M_MARKET_FAMILY
    assert parsed.t0 == earlier.t0
    assert catalog.require(earlier.slug) == earlier
    assert catalog.windows() == (earlier, later)
    assert catalog.windows(family_name="BTC_UPDOWN_15M") == (earlier, later)
    assert len(catalog) == 2


def test_catalog_rejects_duplicate_slug_and_incompatible_rule_family_configuration() -> None:
    window = make_window()
    catalog = MarketCatalog(windows=(window,))
    with pytest.raises(MarketValidationError, match="Duplicate market slug"):
        catalog.register(window)

    incompatible_family = BtcMarketFamily(
        name=BTC_15M_MARKET_FAMILY.name,
        slug_prefix="btc-updown-alt",
        window_seconds=15 * 60,
        collection_mode=MarketCollectionMode.COMPLETE,
    )
    incompatible = make_window(family=incompatible_family)
    with pytest.raises(MarketValidationError, match="does not match the catalog configuration"):
        catalog.register(incompatible)


def test_catalog_rejects_ambiguous_family_definitions() -> None:
    duplicate_slug_family = BtcMarketFamily(
        name="other_btc_15m",
        slug_prefix=BTC_15M_MARKET_FAMILY.slug_prefix,
        window_seconds=BTC_15M_MARKET_FAMILY.window_seconds,
        collection_mode=MarketCollectionMode.COMPLETE,
    )

    with pytest.raises(MarketValidationError, match="ambiguous"):
        MarketCatalog(families=(BTC_15M_MARKET_FAMILY, duplicate_slug_family))


def test_timed_market_event_normalizes_causal_timestamps() -> None:
    source_ts = T0
    receive_ts = T0 + timedelta(milliseconds=50)
    available_ts = T0 + timedelta(milliseconds=75)
    event = TimedMarketEvent(
        source_ts=source_ts,
        collector_receive_ts=receive_ts,
        available_ts=available_ts,
        sequence_or_hash="event-1",
        source="BINANCE",
        instrument="BTCUSDT",
        schema_version="v1",
        ingest_version="collector-v1",
    )

    assert event.source == "binance"
    assert not event.is_event_time_only
    assert event.available_ts == available_ts


def test_timed_market_event_rejects_noncausal_timestamps() -> None:
    with pytest.raises(MarketValidationError, match="source_ts"):
        TimedMarketEvent(
            source_ts=T0,
            collector_receive_ts=None,
            available_ts=T0 - timedelta(microseconds=1),
            sequence_or_hash="event-1",
            source="binance",
            instrument="BTCUSDT",
            schema_version="v1",
            ingest_version="collector-v1",
        )
