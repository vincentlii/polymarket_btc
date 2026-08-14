from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from btc_short_horizon.data import (
    BTC_15M_MARKET_FAMILY,
    MarketCatalog,
    MarketValidationError,
    MarketWindow,
    read_market_catalog,
    write_market_catalog,
)
from btc_short_horizon.data.forward import PolymarketSubscriptionWindow
from btc_short_horizon.data.rule_contract import rule_contract_sha256
from scripts.btc_forward_collector import (
    WindowCollectorSettings,
    _single_market_catalog_path,
    _write_single_market_catalog,
    build_collector,
    collect_current_market_windows,
    current_market_slug,
    parse_args,
)
import scripts.btc_forward_collector as forward_collector_module


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
            "--binance-futures-public-stream",
            "btcusdt@bookTicker",
            "--flush-size",
            "25",
        ]
    )

    collector = build_collector(args)

    assert collector.token_ids == ("up-token", "down-token")
    assert collector.flush_size == 25
    assert collector.flush_interval_seconds == 60.0
    assert collector.shutdown_flush_timeout_seconds == 30.0
    assert collector.ingest_version == "btc-short-horizon-v16"
    assert collector.polymarket_source_timestamp_regression_tolerance_seconds == 1.0
    assert collector.max_pending_events == 100_000
    assert collector.max_pending_bytes == 67_108_864
    assert args.binance_stream == ["btcusdt@trade"]
    assert args.binance_futures_public_stream == ["btcusdt@bookTicker"]


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
        rule_epoch="chainlink-btc-usd-point-v1",
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
        rule_epoch="chainlink-btc-usd-point-v1",
        rule_hash="a" * 64,
    )
    next_market = MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(market.t1),
        condition_id="next-condition",
        up_token_id="next-up-token",
        down_token_id="next-down-token",
        t0=market.t1,
        t1=market.t1 + timedelta(minutes=15),
        rule_epoch="chainlink-btc-usd-point-v1",
        rule_hash="b" * 64,
    )
    catalog = MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market, next_market))
    gamma = _FakeGammaClient(catalog)
    started = asyncio.Event()
    outer_stop = asyncio.Event()
    factory_calls: list[tuple[Path, tuple[tuple[str, ...], ...], WindowCollectorSettings]] = []

    def collector_factory(
        raw_data_root: Path,
        token_groups: tuple[tuple[str, ...], ...],
        settings: WindowCollectorSettings,
    ) -> _FakeWindowCollector:
        factory_calls.append((raw_data_root, token_groups, settings))
        return _FakeWindowCollector(started)

    async def run() -> None:
        async def stop_after_start() -> None:
            await started.wait()
            outer_stop.set()

        stopper = asyncio.create_task(stop_after_start())
        await collect_current_market_windows(
            family=BTC_15M_MARKET_FAMILY,
            rule_epoch="chainlink-btc-usd-point-v1",
            raw_data_root=tmp_path / "raw",
            catalog_directory=tmp_path / "metadata",
            flush_size=25,
            flush_interval_seconds=60.0,
            shutdown_flush_timeout_seconds=30.0,
            max_pending_events=50,
            max_pending_bytes=1_024,
            binance_spot_depth_snapshot_limit=5_000,
            binance_futures_depth_snapshot_limit=1_000,
            binance_depth_snapshot_retry_initial_seconds=0.25,
            binance_depth_snapshot_retry_max_seconds=10.0,
            polymarket_source_timestamp_regression_tolerance_seconds=1.0,
            ingest_version="test-v2",
            binance_streams=("btcusdt@trade",),
            binance_futures_market_streams=(),
            binance_futures_public_streams=("btcusdt@bookTicker",),
            okx_subscriptions=({"channel": "books5", "instId": "BTC-USDT"},),
            rotation_poll_seconds=1.0,
            polymarket_capture_lead_seconds=90.0,
            opening_handoff_delay_seconds=60.0,
            stop_event=outer_stop,
            readiness_decision_offsets_seconds=tuple(range(5, 181, 5)),
            readiness_protocol_sha256="a" * 64,
            readiness_max_feature_lookback_seconds=3600,
            readiness_required_sources=("binance_spot",),
            readiness_source_window_offsets_seconds={"binance_spot": (-3600.0, 180.0)},
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
            "rule_epoch": "chainlink-btc-usd-point-v1",
            "closed": False,
            "slugs": (market.slug, next_market.slug),
        }
    ]
    assert factory_calls == [
        (
            tmp_path / "raw",
            (
                ("up-token", "down-token"),
                ("next-up-token", "next-down-token"),
            ),
            WindowCollectorSettings(
                flush_size=25,
                flush_interval_seconds=60.0,
                shutdown_flush_timeout_seconds=30.0,
                max_pending_events=50,
                max_pending_bytes=1_024,
                binance_spot_depth_snapshot_limit=5_000,
                binance_futures_depth_snapshot_limit=1_000,
                binance_depth_snapshot_retry_initial_seconds=0.25,
                binance_depth_snapshot_retry_max_seconds=10.0,
                polymarket_source_timestamp_regression_tolerance_seconds=1.0,
                polymarket_subscription_windows=(
                    PolymarketSubscriptionWindow(
                        token_ids=("up-token", "down-token"),
                        start=t0 - timedelta(seconds=90),
                        end=t0 + timedelta(seconds=60),
                    ),
                    PolymarketSubscriptionWindow(
                        token_ids=("next-up-token", "next-down-token"),
                        start=market.t1 - timedelta(seconds=90),
                        end=market.t1 + timedelta(seconds=60),
                    ),
                ),
                ingest_version="test-v2",
                epoch_id_offset=int(t0.timestamp()),
                rule_contract_sha256=rule_contract_sha256("chainlink-btc-usd-point-v1"),
            ),
        )
    ]
    assert _single_market_catalog_path(directory=tmp_path / "metadata", market=market).exists()
    assert _single_market_catalog_path(directory=tmp_path / "metadata", market=next_market).exists()


def test_follow_catalog_preserves_first_discovery_timestamp(tmp_path: Path) -> None:
    t0 = datetime(2026, 4, 13, tzinfo=UTC)
    market = _market(t0)
    path = _single_market_catalog_path(directory=tmp_path, market=market)
    write_market_catalog(
        path=path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
        collected_at=t0 - timedelta(minutes=4),
    )
    first_contents = path.read_text(encoding="utf-8")

    returned = _write_single_market_catalog(
        directory=tmp_path,
        family=BTC_15M_MARKET_FAMILY,
        market=market,
    )

    assert returned == path
    assert path.read_text(encoding="utf-8") == first_contents
    assert read_market_catalog(path).require(market.slug) == market


def test_follow_catalog_keeps_legacy_v1_and_writes_v2_sibling(
    tmp_path: Path,
) -> None:
    market = _market(datetime(2026, 4, 13, tzinfo=UTC))
    path = _single_market_catalog_path(directory=tmp_path, market=market)
    legacy_path = path.with_name(path.name.replace("-v2.json", ".json"))
    write_market_catalog(
        path=legacy_path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
    )
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    legacy["schema_version"] = "btc-market-catalog-v1"
    legacy["markets"][0].pop("rule_contract_sha256")
    legacy_bytes = (json.dumps(legacy, indent=2, sort_keys=True) + "\n").encode()
    legacy_path.write_bytes(legacy_bytes)

    returned = _write_single_market_catalog(
        directory=tmp_path,
        family=BTC_15M_MARKET_FAMILY,
        market=market,
    )

    assert returned == path
    assert read_market_catalog(path).require(market.slug) == market
    assert legacy_path.read_bytes() == legacy_bytes


def test_follow_catalog_rejects_conflicting_v2_catalog(tmp_path: Path) -> None:
    market = _market(datetime(2026, 4, 13, tzinfo=UTC))
    path = _single_market_catalog_path(directory=tmp_path, market=market)
    write_market_catalog(
        path=path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
    )
    conflicting = json.loads(path.read_text(encoding="utf-8"))
    conflicting["markets"][0]["condition_id"] = "conflicting-condition"
    path.write_text(json.dumps(conflicting), encoding="utf-8")

    with pytest.raises(MarketValidationError, match="conflicts with Gamma metadata"):
        _write_single_market_catalog(
            directory=tmp_path,
            family=BTC_15M_MARKET_FAMILY,
            market=market,
        )


def test_follow_catalog_keeps_rule_epochs_in_separate_immutable_files(tmp_path: Path) -> None:
    market = _market(datetime(2026, 4, 13, tzinfo=UTC))
    point_market = replace(market, rule_epoch="chainlink-btc-usd-point-v1")
    twap_market = replace(market, rule_epoch="chainlink-btc-usd-twap-60s-v1")

    point_path = _write_single_market_catalog(
        directory=tmp_path,
        family=BTC_15M_MARKET_FAMILY,
        market=point_market,
    )
    twap_path = _write_single_market_catalog(
        directory=tmp_path,
        family=BTC_15M_MARKET_FAMILY,
        market=twap_market,
    )

    assert point_path != twap_path
    assert read_market_catalog(point_path).require(market.slug) == point_market
    assert read_market_catalog(twap_path).require(market.slug) == twap_market


@pytest.mark.asyncio
async def test_follow_current_rotates_storage_without_restarting_shared_feeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t0 = datetime(2026, 4, 13, tzinfo=UTC)
    markets = tuple(_unique_market(t0 + timedelta(minutes=15 * index), index) for index in range(3))
    by_slug = {market.slug: market for market in markets}
    clock = [t0]
    outer_stop = asyncio.Event()
    wait_calls = 0

    class Gamma:
        async def discover_catalog(self, **kwargs: object) -> MarketCatalog:
            slugs = kwargs["slugs"]
            assert isinstance(slugs, tuple)
            return MarketCatalog(
                families=(BTC_15M_MARKET_FAMILY,),
                windows=tuple(by_slug[slug] for slug in slugs if slug in by_slug),
            )

    class Collector:
        def __init__(self, settings: WindowCollectorSettings) -> None:
            self.windows = {
                window.token_ids: window for window in settings.polymarket_subscription_windows
            }
            self.registered: list[PolymarketSubscriptionWindow] = []
            self.rotations: list[int] = []
            self.start_count = 0
            self.stop_count = 0
            self.running = False

        def register_polymarket_subscription_window(
            self, window: PolymarketSubscriptionWindow
        ) -> bool:
            assert self.running
            existing = self.windows.get(window.token_ids)
            if existing is not None:
                assert existing == window
                return False
            self.windows[window.token_ids] = window
            self.registered.append(window)
            return True

        async def rotate_storage_session(self, *, epoch_id_offset: int) -> str:
            assert self.running
            self.rotations.append(epoch_id_offset)
            return f"session-{len(self.rotations)}"

        async def wait_polymarket_subscription_window(
            self,
            _window: PolymarketSubscriptionWindow,
            *,
            stop_event: asyncio.Event,
            timeout_seconds: float,
        ) -> bool:
            assert self.running
            assert timeout_seconds == 30.0
            return not stop_event.is_set()

        async def collect_forever(self, *, stop_event: asyncio.Event, **_kwargs: object) -> None:
            self.start_count += 1
            self.running = True
            await stop_event.wait()
            self.running = False
            self.stop_count += 1

    instances: list[Collector] = []

    def factory(
        _raw_data_root: Path,
        _token_groups: tuple[tuple[str, ...], ...],
        settings: WindowCollectorSettings,
    ) -> Collector:
        collector = Collector(settings)
        instances.append(collector)
        return collector

    async def advance_rotation(**_kwargs: object) -> None:
        nonlocal wait_calls
        await asyncio.sleep(0)
        wait_calls += 1
        if wait_calls == 1:
            clock[0] = markets[1].t0
        else:
            outer_stop.set()

    async def reach_handoff(**_kwargs: object) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(
        forward_collector_module,
        "_wait_for_market_rotation",
        advance_rotation,
    )
    monkeypatch.setattr(
        forward_collector_module,
        "_wait_for_collector_deadline",
        reach_handoff,
    )
    monkeypatch.setattr(
        forward_collector_module.SessionCoverageIndex,
        "coverage_evidence",
        lambda *_args, **_kwargs: ({"session_id": "session-1", "started_at_ns": 0},),
    )

    await collect_current_market_windows(
        family=BTC_15M_MARKET_FAMILY,
        rule_epoch="chainlink-btc-usd-point-v1",
        raw_data_root=tmp_path / "raw",
        catalog_directory=tmp_path / "metadata",
        flush_size=25,
        flush_interval_seconds=60.0,
        shutdown_flush_timeout_seconds=30.0,
        max_pending_events=50,
        max_pending_bytes=1_024,
        binance_spot_depth_snapshot_limit=5_000,
        binance_futures_depth_snapshot_limit=1_000,
        binance_depth_snapshot_retry_initial_seconds=0.25,
        binance_depth_snapshot_retry_max_seconds=10.0,
        polymarket_source_timestamp_regression_tolerance_seconds=1.0,
        ingest_version="test-v2",
        binance_streams=("btcusdt@kline_1s",),
        binance_futures_market_streams=(),
        binance_futures_public_streams=(),
        rotation_poll_seconds=1.0,
        polymarket_capture_lead_seconds=90.0,
        opening_handoff_delay_seconds=60.0,
        stop_event=outer_stop,
        readiness_decision_offsets_seconds=tuple(range(5, 181, 5)),
        readiness_protocol_sha256="a" * 64,
        readiness_max_feature_lookback_seconds=3600,
        readiness_required_sources=("binance_spot",),
        readiness_source_window_offsets_seconds={"binance_spot": (-3600.0, 180.0)},
        gamma_client=Gamma(),
        collector_factory=factory,
        now=lambda: clock[0],
    )

    assert len(instances) == 1
    collector = instances[0]
    assert collector.start_count == 1
    assert collector.stop_count == 1
    assert collector.rotations == [
        int(markets[0].t1.timestamp()),
        int(markets[1].t1.timestamp()),
    ]
    assert [window.token_ids for window in collector.registered] == [
        (markets[2].up_token_id, markets[2].down_token_id)
    ]


def test_follow_catalog_rejects_same_path_with_conflicting_metadata(tmp_path: Path) -> None:
    t0 = datetime(2026, 4, 13, tzinfo=UTC)
    market = _market(t0)
    path = _single_market_catalog_path(directory=tmp_path, market=market)
    conflicting = MarketWindow(
        family=market.family,
        slug=market.slug,
        condition_id=market.condition_id,
        up_token_id="different-up-token",
        down_token_id=market.down_token_id,
        t0=market.t0,
        t1=market.t1,
        rule_epoch=market.rule_epoch,
        rule_hash=market.rule_hash,
    )
    write_market_catalog(
        path=path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(conflicting,)),
        collected_at=t0 - timedelta(minutes=4),
    )

    with pytest.raises(MarketValidationError, match="conflicts"):
        _write_single_market_catalog(
            directory=tmp_path,
            family=BTC_15M_MARKET_FAMILY,
            market=market,
        )


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
        self,
        *,
        stop_event: asyncio.Event,
        binance_streams: tuple[str, ...],
        binance_futures_market_streams: tuple[str, ...],
        binance_futures_public_streams: tuple[str, ...],
        okx_subscriptions: tuple[dict[str, str], ...],
        optional_feeds_enabled: asyncio.Event | None = None,
    ) -> None:
        assert binance_streams == ("btcusdt@trade",)
        assert binance_futures_market_streams == ()
        assert binance_futures_public_streams == ("btcusdt@bookTicker",)
        assert okx_subscriptions == ({"channel": "books5", "instId": "BTC-USDT"},)
        self.started.set()
        await stop_event.wait()


def _market(t0: datetime) -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id="condition",
        up_token_id="up-token",
        down_token_id="down-token",
        t0=t0,
        t1=t0 + BTC_15M_MARKET_FAMILY.window_seconds_as_timedelta,
        rule_epoch="chainlink-btc-usd-point-v1",
        rule_hash="c" * 64,
    )


def _unique_market(t0: datetime, index: int) -> MarketWindow:
    token = str(index + 1)
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id=f"condition-{token}",
        up_token_id=f"up-token-{token}",
        down_token_id=f"down-token-{token}",
        t0=t0,
        t1=t0 + BTC_15M_MARKET_FAMILY.window_seconds_as_timedelta,
        rule_epoch="chainlink-btc-usd-point-v1",
        rule_hash=token * 64,
    )
