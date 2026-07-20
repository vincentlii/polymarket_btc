"""Collect BTC-only Polymarket, Chainlink, and Binance public feed evidence."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path

import httpx

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root, run_async_entrypoint
else:
    from ._script_helpers import ensure_repo_root, run_async_entrypoint

ensure_repo_root(__file__)

from btc_short_horizon.config import BtcProjectConfig, load_btc_project_config  # noqa: E402
from btc_short_horizon.data.catalog_io import read_market_catalog, write_market_catalog  # noqa: E402
from btc_short_horizon.data.contracts import (  # noqa: E402
    BtcMarketFamily,
    MarketValidationError,
    MarketWindow,
)
from btc_short_horizon.data.forward import (  # noqa: E402
    DEFAULT_BINANCE_FUTURES_MARKET_STREAMS,
    DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS,
    DEFAULT_BINANCE_STREAMS,
    BtcForwardCollector,
)
from btc_short_horizon.data.gamma import GammaMarketClient  # noqa: E402
from btc_short_horizon.data.market_catalog import MarketCatalog  # noqa: E402
from btc_short_horizon.data.session_inventory import (  # noqa: E402
    CollectorStorageLease,
    SessionInventoryRepository,
)


@dataclass(frozen=True, slots=True)
class WindowCollectorSettings:
    flush_size: int
    flush_interval_seconds: float
    shutdown_flush_timeout_seconds: float
    max_pending_events: int
    max_pending_bytes: int
    binance_spot_depth_snapshot_limit: int
    binance_futures_depth_snapshot_limit: int
    binance_depth_snapshot_retry_initial_seconds: float
    binance_depth_snapshot_retry_max_seconds: float
    polymarket_source_timestamp_regression_tolerance_seconds: float
    ingest_version: str
    epoch_id_offset: int


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
        help="BTC project TOML configuration.",
    )
    parser.add_argument(
        "--token-id",
        action="append",
        help="Polymarket CLOB token ID to collect. Repeat for Up and Down.",
    )
    parser.add_argument(
        "--market-catalog",
        type=Path,
        help="Validated Gamma market catalog JSON used to select current token IDs.",
    )
    parser.add_argument(
        "--market-slug",
        help="Market slug to select from --market-catalog.",
    )
    parser.add_argument(
        "--follow-current",
        action="store_true",
        help="Continuously discover and collect the current BTC window, rotating at each boundary.",
    )
    parser.add_argument(
        "--family",
        choices=("15m", "5m"),
        default="15m",
        help="BTC family to follow when --follow-current is enabled.",
    )
    parser.add_argument(
        "--rule-epoch",
        help="Verified market-rule epoch required by --follow-current.",
    )
    parser.add_argument(
        "--catalog-directory",
        type=Path,
        help="Directory for one immutable validated Gamma catalog per followed market.",
    )
    parser.add_argument(
        "--rotation-poll-seconds",
        type=float,
        help="Override the configured Gamma retry interval while a new market is unavailable.",
    )
    parser.add_argument(
        "--opening-handoff-delay-seconds",
        type=float,
        help=(
            "Keep the current+lookahead subscription alive this long after rotation; "
            "defaults to the configured shadow-safe handoff delay."
        ),
    )
    parser.add_argument(
        "--binance-stream",
        action="append",
        default=None,
        help=(
            "Binance Spot combined-stream name. Defaults only to BTCUSDT kline_1s; "
            "repeat explicitly for additional research streams."
        ),
    )
    parser.add_argument(
        "--binance-futures-market-stream",
        action="append",
        default=None,
        help="Binance Futures market-stream name; disabled unless repeated explicitly.",
    )
    parser.add_argument(
        "--binance-futures-public-stream",
        action="append",
        default=None,
        help="Binance Futures public-stream name; disabled unless repeated explicitly.",
    )
    parser.add_argument(
        "--flush-size",
        type=int,
        help="Override the configured maximum buffered events before a raw-data flush.",
    )
    parser.add_argument(
        "--flush-interval-seconds",
        type=float,
        help="Override the configured maximum raw-data flush interval.",
    )
    return parser.parse_args(argv)


def build_collector(
    args: argparse.Namespace, *, config: BtcProjectConfig | None = None
) -> BtcForwardCollector:
    if args.follow_current:
        raise ValueError("--follow-current creates one collector per discovered market window")
    config = config or load_btc_project_config(args.config)
    token_ids = _token_ids(args)
    flush_size, flush_interval_seconds, _rotation_poll_seconds = _collection_settings(args, config)
    return BtcForwardCollector(
        raw_data_root=config.paths.raw_data_root,
        polymarket_token_ids=token_ids,
        flush_size=flush_size,
        flush_interval_seconds=flush_interval_seconds,
        shutdown_flush_timeout_seconds=config.collection.shutdown_flush_timeout_seconds,
        ingest_version=config.collection.ingest_version,
        max_pending_events=config.collection.max_pending_events,
        max_pending_bytes=config.collection.max_pending_bytes,
        binance_spot_depth_snapshot_limit=(config.collection.binance_spot_depth_snapshot_limit),
        binance_futures_depth_snapshot_limit=(
            config.collection.binance_futures_depth_snapshot_limit
        ),
        binance_depth_snapshot_retry_initial_seconds=(
            config.collection.binance_depth_snapshot_retry_initial_seconds
        ),
        binance_depth_snapshot_retry_max_seconds=(
            config.collection.binance_depth_snapshot_retry_max_seconds
        ),
        polymarket_source_timestamp_regression_tolerance_seconds=(
            config.collection.polymarket_source_timestamp_regression_tolerance_seconds
        ),
    )


def _token_ids(args: argparse.Namespace) -> tuple[str, ...]:
    has_catalog = args.market_catalog is not None
    has_explicit_tokens = bool(args.token_id)
    if has_catalog == has_explicit_tokens:
        raise ValueError("provide either --token-id values or --market-catalog with --market-slug")
    if has_catalog:
        if not args.market_slug:
            raise ValueError("--market-slug is required with --market-catalog")
        market = read_market_catalog(args.market_catalog).require(args.market_slug)
        return (market.up_token_id, market.down_token_id)
    if args.market_slug:
        raise ValueError("--market-slug requires --market-catalog")
    token_ids = tuple(args.token_id)
    if len(token_ids) != 2:
        raise ValueError("--token-id must be repeated exactly twice for the Up and Down tokens")
    return token_ids


async def collect(args: argparse.Namespace) -> None:
    config = load_btc_project_config(args.config)
    with CollectorStorageLease(
        config.paths.raw_data_root,
        owner={
            "service": "forward_collector_cli",
            "ingest_version": config.collection.ingest_version,
        },
    ):
        SessionInventoryRepository(config.paths.raw_data_root).recover_interrupted_sessions()
        await _collect_with_storage_lease(args, config=config)


async def _collect_with_storage_lease(
    args: argparse.Namespace,
    *,
    config: BtcProjectConfig,
) -> None:
    streams = tuple(args.binance_stream or DEFAULT_BINANCE_STREAMS)
    futures_market_streams = tuple(
        args.binance_futures_market_stream or DEFAULT_BINANCE_FUTURES_MARKET_STREAMS
    )
    futures_public_streams = tuple(
        args.binance_futures_public_stream or DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS
    )
    flush_size, flush_interval_seconds, rotation_poll_seconds = _collection_settings(args, config)
    if args.follow_current:
        _validate_follow_current_args(args)
        opening_handoff_delay_seconds = (
            args.opening_handoff_delay_seconds
            if args.opening_handoff_delay_seconds is not None
            else config.collection.opening_handoff_delay_seconds
        )
        await collect_current_market_windows(
            family=_follow_family(config, args.family),
            rule_epoch=args.rule_epoch,
            raw_data_root=config.paths.raw_data_root,
            catalog_directory=(
                args.catalog_directory
                or config.paths.raw_data_root.parent / "metadata" / "forward_catalogs"
            ),
            flush_size=flush_size,
            flush_interval_seconds=flush_interval_seconds,
            shutdown_flush_timeout_seconds=config.collection.shutdown_flush_timeout_seconds,
            max_pending_events=config.collection.max_pending_events,
            max_pending_bytes=config.collection.max_pending_bytes,
            binance_spot_depth_snapshot_limit=(config.collection.binance_spot_depth_snapshot_limit),
            binance_futures_depth_snapshot_limit=(
                config.collection.binance_futures_depth_snapshot_limit
            ),
            binance_depth_snapshot_retry_initial_seconds=(
                config.collection.binance_depth_snapshot_retry_initial_seconds
            ),
            binance_depth_snapshot_retry_max_seconds=(
                config.collection.binance_depth_snapshot_retry_max_seconds
            ),
            polymarket_source_timestamp_regression_tolerance_seconds=(
                config.collection.polymarket_source_timestamp_regression_tolerance_seconds
            ),
            ingest_version=config.collection.ingest_version,
            binance_streams=streams,
            binance_futures_market_streams=futures_market_streams,
            binance_futures_public_streams=futures_public_streams,
            rotation_poll_seconds=rotation_poll_seconds,
            opening_handoff_delay_seconds=opening_handoff_delay_seconds,
            stop_event=asyncio.Event(),
        )
        return
    collector = build_collector(args, config=config)
    stop_event = asyncio.Event()
    await collector.collect_forever(
        stop_event=stop_event,
        binance_streams=streams,
        binance_futures_market_streams=futures_market_streams,
        binance_futures_public_streams=futures_public_streams,
    )


async def collect_current_market_windows(
    *,
    family: BtcMarketFamily,
    rule_epoch: str,
    raw_data_root: Path,
    catalog_directory: Path,
    flush_size: int,
    flush_interval_seconds: float,
    shutdown_flush_timeout_seconds: float,
    max_pending_events: int,
    max_pending_bytes: int,
    binance_spot_depth_snapshot_limit: int,
    binance_futures_depth_snapshot_limit: int,
    binance_depth_snapshot_retry_initial_seconds: float,
    binance_depth_snapshot_retry_max_seconds: float,
    polymarket_source_timestamp_regression_tolerance_seconds: float,
    ingest_version: str,
    binance_streams: Sequence[str],
    binance_futures_market_streams: Sequence[str],
    binance_futures_public_streams: Sequence[str],
    rotation_poll_seconds: float,
    opening_handoff_delay_seconds: float,
    stop_event: asyncio.Event,
    gamma_client: GammaMarketClient | None = None,
    collector_factory: Callable[
        [Path, tuple[str, ...], WindowCollectorSettings],
        BtcForwardCollector,
    ]
    | None = None,
    on_market_active: Callable[[MarketWindow, MarketWindow | None, BtcForwardCollector], None]
    | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Collect current+next BTC tokens and rotate only after the next opening window."""

    if not rule_epoch:
        raise ValueError("rule_epoch is required")
    if not isfinite(rotation_poll_seconds) or rotation_poll_seconds <= 0.0:
        raise ValueError("rotation_poll_seconds must be finite and > 0")
    if not isfinite(opening_handoff_delay_seconds) or opening_handoff_delay_seconds <= 0.0:
        raise ValueError("opening_handoff_delay_seconds must be finite and > 0")
    client = gamma_client or GammaMarketClient()
    factory = collector_factory or _build_window_collector
    while not stop_event.is_set():
        current_time = _as_utc(now())
        slug = current_market_slug(family, current_time)
        lookahead_slug = next_market_slug(family, current_time)
        try:
            catalog = await client.discover_catalog(
                family=family,
                rule_epoch=rule_epoch,
                closed=False,
                slugs=(slug, lookahead_slug),
            )
        except httpx.HTTPError as exc:
            print(f"Gamma discovery failed for {slug}: {exc}; retrying.")
            await _wait_or_stop(stop_event, rotation_poll_seconds)
            continue
        market = catalog.get(slug)
        if market is None:
            print(f"Gamma has not published current market {slug}; retrying.")
            await _wait_or_stop(stop_event, rotation_poll_seconds)
            continue
        if market.t1 <= current_time:
            await _wait_or_stop(stop_event, rotation_poll_seconds)
            continue
        lookahead = catalog.get(lookahead_slug)
        if lookahead is not None and lookahead.t0 != market.t1:
            raise ValueError("Gamma lookahead market does not start at the current window end")
        catalog_path = _write_single_market_catalog(
            directory=catalog_directory,
            family=family,
            market=market,
        )
        token_ids = (market.up_token_id, market.down_token_id)
        if lookahead is not None:
            _write_single_market_catalog(
                directory=catalog_directory,
                family=family,
                market=lookahead,
            )
            token_ids = (
                *token_ids,
                lookahead.up_token_id,
                lookahead.down_token_id,
            )
        collector_stop = asyncio.Event()
        collector = factory(
            raw_data_root,
            token_ids,
            WindowCollectorSettings(
                flush_size=flush_size,
                flush_interval_seconds=flush_interval_seconds,
                shutdown_flush_timeout_seconds=shutdown_flush_timeout_seconds,
                max_pending_events=max_pending_events,
                max_pending_bytes=max_pending_bytes,
                binance_spot_depth_snapshot_limit=(binance_spot_depth_snapshot_limit),
                binance_futures_depth_snapshot_limit=(binance_futures_depth_snapshot_limit),
                binance_depth_snapshot_retry_initial_seconds=(
                    binance_depth_snapshot_retry_initial_seconds
                ),
                binance_depth_snapshot_retry_max_seconds=(binance_depth_snapshot_retry_max_seconds),
                polymarket_source_timestamp_regression_tolerance_seconds=(
                    polymarket_source_timestamp_regression_tolerance_seconds
                ),
                ingest_version=ingest_version,
                epoch_id_offset=int(market.t0.timestamp()),
            ),
        )
        if on_market_active is not None:
            on_market_active(market, lookahead, collector)
        worker = asyncio.create_task(
            collector.collect_forever(
                stop_event=collector_stop,
                binance_streams=binance_streams,
                binance_futures_market_streams=binance_futures_market_streams,
                binance_futures_public_streams=binance_futures_public_streams,
            ),
            name=f"btc-forward-{market.slug}",
        )
        handoff_delay = opening_handoff_delay_seconds if lookahead is not None else 0.0
        print(
            f"Collecting {market.slug}"
            f"{' + ' + lookahead.slug if lookahead is not None else ''} "
            f"until {(market.t1 + timedelta(seconds=handoff_delay)).isoformat()} "
            f"using {catalog_path}."
        )
        try:
            await _wait_for_market_rotation(
                stop_event=stop_event,
                worker=worker,
                market=market,
                handoff_delay_seconds=handoff_delay,
                now=now,
            )
        finally:
            collector_stop.set()
            await worker


def current_market_slug(family: BtcMarketFamily, now: datetime) -> str:
    current_time = _as_utc(now)
    epoch_seconds = int(current_time.timestamp())
    window_start = epoch_seconds - epoch_seconds % family.window_seconds
    return family.slug_for(datetime.fromtimestamp(window_start, UTC))


def next_market_slug(family: BtcMarketFamily, now: datetime) -> str:
    current_time = _as_utc(now)
    epoch_seconds = int(current_time.timestamp())
    next_start = epoch_seconds - epoch_seconds % family.window_seconds + family.window_seconds
    return family.slug_for(datetime.fromtimestamp(next_start, UTC))


def _write_single_market_catalog(
    *, directory: Path, family: BtcMarketFamily, market: MarketWindow
) -> Path:
    path = directory / f"{market.slug}-{market.rule_hash}.json"
    if path.exists():
        existing = read_market_catalog(path)
        if existing.families != (family,) or existing.windows() != (market,):
            raise MarketValidationError(
                f"existing follow-current catalog conflicts with Gamma metadata: {path}"
            )
        return path
    write_market_catalog(
        path=path,
        catalog=MarketCatalog(families=(family,), windows=(market,)),
    )
    return path


def _validate_follow_current_args(args: argparse.Namespace) -> None:
    if not args.rule_epoch:
        raise ValueError("--rule-epoch is required with --follow-current")
    if args.token_id or args.market_catalog or args.market_slug:
        raise ValueError(
            "--follow-current discovers current token IDs; do not combine it with token or catalog inputs"
        )


def _follow_family(config: BtcProjectConfig, name: str) -> BtcMarketFamily:
    if name == "15m":
        return config.primary_family
    if name == "5m":
        return config.collection_only_family
    raise ValueError(f"unsupported BTC family {name!r}")


def _collection_settings(
    args: argparse.Namespace, config: BtcProjectConfig
) -> tuple[int, float, float]:
    return (
        args.flush_size if args.flush_size is not None else config.collection.flush_size,
        (
            args.flush_interval_seconds
            if args.flush_interval_seconds is not None
            else config.collection.flush_interval_seconds
        ),
        (
            args.rotation_poll_seconds
            if args.rotation_poll_seconds is not None
            else config.collection.rotation_poll_seconds
        ),
    )


def _build_window_collector(
    raw_data_root: Path,
    token_ids: tuple[str, ...],
    settings: WindowCollectorSettings,
) -> BtcForwardCollector:
    return BtcForwardCollector(
        raw_data_root=raw_data_root,
        polymarket_token_ids=token_ids,
        flush_size=settings.flush_size,
        flush_interval_seconds=settings.flush_interval_seconds,
        shutdown_flush_timeout_seconds=settings.shutdown_flush_timeout_seconds,
        ingest_version=settings.ingest_version,
        epoch_id_offset=settings.epoch_id_offset,
        max_pending_events=settings.max_pending_events,
        max_pending_bytes=settings.max_pending_bytes,
        binance_spot_depth_snapshot_limit=(settings.binance_spot_depth_snapshot_limit),
        binance_futures_depth_snapshot_limit=(settings.binance_futures_depth_snapshot_limit),
        binance_depth_snapshot_retry_initial_seconds=(
            settings.binance_depth_snapshot_retry_initial_seconds
        ),
        binance_depth_snapshot_retry_max_seconds=(
            settings.binance_depth_snapshot_retry_max_seconds
        ),
        polymarket_source_timestamp_regression_tolerance_seconds=(
            settings.polymarket_source_timestamp_regression_tolerance_seconds
        ),
    )


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


async def _wait_for_market_rotation(
    *,
    stop_event: asyncio.Event,
    worker: asyncio.Task[None],
    market: MarketWindow,
    handoff_delay_seconds: float,
    now: Callable[[], datetime],
) -> None:
    handoff_end = market.t1 + timedelta(seconds=handoff_delay_seconds)
    seconds_until_end = max(0.0, (handoff_end - _as_utc(now())).total_seconds())
    stopper = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            (worker, stopper),
            timeout=seconds_until_end,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker in done:
            await worker
            if not stop_event.is_set():
                raise RuntimeError(
                    f"collector stopped before {market.slug} reached its handoff end"
                )
    finally:
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("current time must be timezone-aware")
    return value.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    shutdown_timeout_seconds = load_btc_project_config(
        args.config
    ).collection.shutdown_flush_timeout_seconds
    try:
        run_async_entrypoint(
            collect(args),
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
