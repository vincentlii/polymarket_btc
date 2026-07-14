from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from btc_short_horizon.data import (
    BTC_15M_MARKET_FAMILY,
    MarketCatalog,
    MarketWindow,
    write_market_catalog,
)
from scripts.btc_forward_collector import (
    build_collector,
    collect_current_market_windows,
    current_market_slug,
    parse_args,
)


def test_forward_collector_cli_builds_btc_only_collector_from_explicit_token_ids() -> None:
    args = parse_args(
        [
            "--config",
            "configs/btc_short_horizon/baseline.toml",
            "--token-id",
            "up-token",
            "--token-id",
            "down-token",
            "--binance-stream",
            "btcusdt@trade",
            "--flush-size",
            "25",
        ]
    )

    collector = build_collector(args)

    assert collector.token_ids == ("up-token", "down-token")
    assert collector.flush_size == 25
    assert collector.flush_interval_seconds == 60.0
    assert args.binance_stream == ["btcusdt@trade"]


def test_forward_collector_cli_rejects_incomplete_explicit_token_pair() -> None:
    args = parse_args(["--token-id", "up-token"])

    with pytest.raises(ValueError, match="exactly twice"):
        build_collector(args)


def test_forward_collector_cli_resolves_tokens_from_validated_catalog(tmp_path) -> None:  # type: ignore[no-untyped-def]
    t0 = datetime(2026, 4, 13, tzinfo=UTC)
    market = MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id="condition",
        up_token_id="up-token",
        down_token_id="down-token",
        t0=t0,
        t1=t0 + BTC_15M_MARKET_FAMILY.window_seconds_as_timedelta,
        rule_epoch="chainlink-btc-usd-v1",
        rule_hash="a" * 64,
    )
    catalog_path = tmp_path / "catalog.json"
    write_market_catalog(
        path=catalog_path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
        collected_at=t0,
    )
    args = parse_args(
        [
            "--market-catalog",
            str(catalog_path),
            "--market-slug",
            market.slug,
        ]
    )

    collector = build_collector(args)

    assert collector.token_ids == ("up-token", "down-token")


def test_follow_current_rotates_from_exact_gamma_catalog_and_persists_metadata(tmp_path) -> None:  # type: ignore[no-untyped-def]
    t0 = datetime(2026, 4, 13, tzinfo=UTC)
    market = MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id="condition",
        up_token_id="up-token",
        down_token_id="down-token",
        t0=t0,
        t1=t0 + BTC_15M_MARKET_FAMILY.window_seconds_as_timedelta,
        rule_epoch="chainlink-btc-usd-v1",
        rule_hash="a" * 64,
    )
    catalog = MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,))
    gamma = _FakeGammaClient(catalog)
    started = asyncio.Event()
    outer_stop = asyncio.Event()
    factory_calls: list[tuple[Path, tuple[str, str], int, float]] = []

    def collector_factory(
        raw_data_root: Path,
        token_ids: tuple[str, str],
        flush_size: int,
        flush_interval_seconds: float,
    ) -> _FakeWindowCollector:
        factory_calls.append((raw_data_root, token_ids, flush_size, flush_interval_seconds))
        return _FakeWindowCollector(started)

    async def run() -> None:
        async def stop_after_start() -> None:
            await started.wait()
            outer_stop.set()

        stopper = asyncio.create_task(stop_after_start())
        await collect_current_market_windows(
            family=BTC_15M_MARKET_FAMILY,
            rule_epoch="chainlink-btc-usd-v1",
            raw_data_root=tmp_path / "raw",
            catalog_directory=tmp_path / "metadata",
            flush_size=25,
            flush_interval_seconds=60.0,
            binance_streams=("btcusdt@trade",),
            rotation_poll_seconds=1.0,
            stop_event=outer_stop,
            gamma_client=gamma,
            collector_factory=collector_factory,
            now=lambda: t0,
        )
        await stopper

    asyncio.run(run())

    assert current_market_slug(BTC_15M_MARKET_FAMILY, t0) == market.slug
    assert gamma.calls == [
        {
            "family": BTC_15M_MARKET_FAMILY,
            "rule_epoch": "chainlink-btc-usd-v1",
            "closed": False,
            "slugs": (market.slug,),
        }
    ]
    assert factory_calls == [(tmp_path / "raw", ("up-token", "down-token"), 25, 60.0)]
    assert (tmp_path / "metadata" / f"{market.slug}-{market.rule_hash}.json").exists()


class _FakeGammaClient:
    def __init__(self, catalog: MarketCatalog) -> None:
        self.catalog = catalog
        self.calls: list[dict[str, object]] = []

    async def discover_catalog(self, **kwargs: object) -> MarketCatalog:
        self.calls.append(dict(kwargs))
        return self.catalog


class _FakeWindowCollector:
    def __init__(self, started: asyncio.Event) -> None:
        self.started = started

    async def collect_forever(
        self, *, stop_event: asyncio.Event, binance_streams: tuple[str, ...]
    ) -> None:
        assert binance_streams == ("btcusdt@trade",)
        self.started.set()
        await stop_event.wait()
