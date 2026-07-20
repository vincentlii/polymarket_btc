"""Run the BTC 15m forward collector with local status and stop controls."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
import signal

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root, run_async_entrypoint
else:
    from ._script_helpers import ensure_repo_root, run_async_entrypoint

ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data import BtcForwardCollector, MarketWindow  # noqa: E402
from btc_short_horizon.data.forward import (  # noqa: E402
    DEFAULT_BINANCE_FUTURES_MARKET_STREAMS,
    DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS,
    DEFAULT_BINANCE_STREAMS,
)
from btc_short_horizon.data.session_inventory import (  # noqa: E402
    CollectorStorageLease,
    SessionInventoryRepository,
)
from btc_short_horizon.live.forward_runtime import (  # noqa: E402
    ForwardCollectorRuntimeConfig,
    run_forward_collector_runtime,
)
from btc_short_horizon.live.runtime import (  # noqa: E402
    build_runtime_identity,
    filesystem_usage,
)
from scripts.btc_forward_collector import (  # noqa: E402
    collect_current_market_windows,
    current_market_slug,
)


@dataclass(slots=True)
class _ActiveWindow:
    market: MarketWindow
    lookahead: MarketWindow | None
    collector: BtcForwardCollector


def _effective_market(window: _ActiveWindow, *, now: datetime) -> MarketWindow:
    if window.lookahead is not None and now >= window.lookahead.t0:
        return window.lookahead
    return window.market


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
    )
    parser.add_argument("--rule-epoch", required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--catalog-directory", type=Path)
    parser.add_argument("--status-interval-seconds", type=float, default=5.0)
    parser.add_argument("--feed-stale-after-seconds", type=float, default=30.0)
    parser.add_argument("--feed-startup-grace-seconds", type=float, default=60.0)
    parser.add_argument("--rotation-poll-seconds", type=float)
    parser.add_argument("--opening-handoff-delay-seconds", type=float)
    parser.add_argument("--flush-size", type=int)
    parser.add_argument("--flush-interval-seconds", type=float)
    parser.add_argument("--binance-stream", action="append", default=None)
    parser.add_argument("--binance-futures-market-stream", action="append", default=None)
    parser.add_argument("--binance-futures-public-stream", action="append", default=None)
    return parser.parse_args(argv)


async def run_async(args: argparse.Namespace) -> None:
    if (
        not isfinite(args.feed_stale_after_seconds)
        or not isfinite(args.feed_startup_grace_seconds)
        or args.feed_stale_after_seconds <= 0.0
        or args.feed_startup_grace_seconds < 0.0
    ):
        raise ValueError("feed health timing values are invalid")
    project = load_btc_project_config(args.config)
    runtime_root = args.runtime_root or project.paths.artifact_root / "runtime"
    catalog_directory = (
        args.catalog_directory
        or project.paths.raw_data_root.parent / "metadata" / "forward_catalogs"
    )
    active: _ActiveWindow | None = None
    started_at = datetime.now(UTC)
    binance_streams = tuple(args.binance_stream or DEFAULT_BINANCE_STREAMS)
    binance_futures_market_streams = tuple(
        args.binance_futures_market_stream or DEFAULT_BINANCE_FUTURES_MARKET_STREAMS
    )
    binance_futures_public_streams = tuple(
        args.binance_futures_public_stream or DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS
    )
    identity = build_runtime_identity(
        config_path=args.config,
        ingest_version=project.collection.ingest_version,
    )
    recovered_interrupted_sessions: tuple[str, ...] = ()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    for item in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(item, stop_event.set)
        except NotImplementedError:  # Windows event loops do not expose signal handlers.
            break
        else:
            registered_signals.append(item)

    def on_market_active(
        market: MarketWindow,
        lookahead: MarketWindow | None,
        collector: BtcForwardCollector,
    ) -> None:
        nonlocal active
        collector.configure_required_feeds(
            binance_streams=binance_streams,
            binance_futures_market_streams=binance_futures_market_streams,
            binance_futures_public_streams=binance_futures_public_streams,
        )
        active = _ActiveWindow(market=market, lookahead=lookahead, collector=collector)

    def feed_health() -> tuple[bool, str]:
        if (datetime.now(UTC) - started_at).total_seconds() <= args.feed_startup_grace_seconds:
            return True, "feed_startup_grace"
        if active is None:
            return False, "market_discovery_silent"
        _refresh_required_clob_feeds(active)
        health = active.collector.feed_health(
            now=datetime.now(UTC),
            stale_after_seconds=args.feed_stale_after_seconds,
        )
        return health.healthy, health.reason

    def status_details() -> dict[str, object]:
        details: dict[str, object] = {
            "family": project.primary_family.name,
            "ingest_version": project.collection.ingest_version,
            "scheduled_market": current_market_slug(project.primary_family, datetime.now(UTC)),
            "identity": identity,
            "recovered_interrupted_sessions": list(recovered_interrupted_sessions),
            "storage": {
                "raw_data": filesystem_usage(project.paths.raw_data_root),
                "runtime": filesystem_usage(runtime_root),
            },
        }
        if active is None:
            details["feeds"] = []
            return details
        _refresh_required_clob_feeds(active)
        health = active.collector.feed_health(
            now=datetime.now(UTC),
            stale_after_seconds=args.feed_stale_after_seconds,
        )
        quality = tuple(active.collector.quality_stats.values())
        buffer = active.collector.buffer_stats
        effective_market = _effective_market(active, now=datetime.now(UTC))
        details.update(
            {
                "active_market": effective_market.slug,
                "lookahead_market": (None if active.lookahead is None else active.lookahead.slug),
                "collector_session_id": active.collector.collector_session_id,
                "pending_events": buffer.pending_events,
                "pending_bytes": buffer.pending_bytes,
                "buffer": {
                    "pending_events": buffer.pending_events,
                    "pending_bytes": buffer.pending_bytes,
                    "inflight_events": buffer.inflight_events,
                    "inflight_bytes": buffer.inflight_bytes,
                    "total_events": buffer.total_events,
                    "total_bytes": buffer.total_bytes,
                    "high_water_events": buffer.high_water_events,
                    "high_water_bytes": buffer.high_water_bytes,
                    "flush_count": buffer.flush_count,
                    "flush_failures": buffer.flush_failures,
                    "last_flush_duration_ms": buffer.last_flush_duration_ms,
                    "max_queue_delay_ms": buffer.max_queue_delay_ms,
                },
                "feeds": list(health.feeds),
                "required_feeds_healthy": health.healthy,
                "required_feeds_reason": health.reason,
                "quality": {
                    "streams": len(quality),
                    "total_events": sum(item.total_events for item in quality),
                    "accepted_events": sum(item.accepted_events for item in quality),
                    "gap_events": sum(item.gap_events for item in quality),
                    "stale_events": sum(item.stale_events for item in quality),
                },
            }
        )
        return details

    async def collect(stop_event: asyncio.Event) -> None:
        await collect_current_market_windows(
            family=project.primary_family,
            rule_epoch=args.rule_epoch,
            raw_data_root=project.paths.raw_data_root,
            catalog_directory=catalog_directory,
            flush_size=(
                args.flush_size if args.flush_size is not None else project.collection.flush_size
            ),
            flush_interval_seconds=(
                args.flush_interval_seconds
                if args.flush_interval_seconds is not None
                else project.collection.flush_interval_seconds
            ),
            shutdown_flush_timeout_seconds=(project.collection.shutdown_flush_timeout_seconds),
            max_pending_events=project.collection.max_pending_events,
            max_pending_bytes=project.collection.max_pending_bytes,
            binance_spot_depth_snapshot_limit=(
                project.collection.binance_spot_depth_snapshot_limit
            ),
            binance_futures_depth_snapshot_limit=(
                project.collection.binance_futures_depth_snapshot_limit
            ),
            binance_depth_snapshot_retry_initial_seconds=(
                project.collection.binance_depth_snapshot_retry_initial_seconds
            ),
            binance_depth_snapshot_retry_max_seconds=(
                project.collection.binance_depth_snapshot_retry_max_seconds
            ),
            polymarket_source_timestamp_regression_tolerance_seconds=(
                project.collection.polymarket_source_timestamp_regression_tolerance_seconds
            ),
            ingest_version=project.collection.ingest_version,
            binance_streams=binance_streams,
            binance_futures_market_streams=binance_futures_market_streams,
            binance_futures_public_streams=binance_futures_public_streams,
            rotation_poll_seconds=(
                args.rotation_poll_seconds
                if args.rotation_poll_seconds is not None
                else project.collection.rotation_poll_seconds
            ),
            opening_handoff_delay_seconds=(
                args.opening_handoff_delay_seconds
                if args.opening_handoff_delay_seconds is not None
                else project.collection.opening_handoff_delay_seconds
            ),
            stop_event=stop_event,
            on_market_active=on_market_active,
        )

    def _refresh_required_clob_feeds(window: _ActiveWindow) -> None:
        market = _effective_market(window, now=datetime.now(UTC))
        window.collector.configure_required_polymarket_tokens(
            (market.up_token_id, market.down_token_id)
        )

    lease = CollectorStorageLease(
        project.paths.raw_data_root,
        owner={
            "service": "forward_collector",
            "ingest_version": project.collection.ingest_version,
            "code_revision": str(identity["code_revision"]),
        },
    )
    try:
        with lease:
            recovered_interrupted_sessions = SessionInventoryRepository(
                project.paths.raw_data_root
            ).recover_interrupted_sessions()
            await run_forward_collector_runtime(
                config=ForwardCollectorRuntimeConfig(
                    runtime_root=runtime_root,
                    status_interval_seconds=args.status_interval_seconds,
                ),
                collect=collect,
                status_details=status_details,
                status_health=feed_health,
                stop_event=stop_event,
            )
    finally:
        for item in registered_signals:
            loop.remove_signal_handler(item)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    shutdown_timeout_seconds = load_btc_project_config(
        args.config
    ).collection.shutdown_flush_timeout_seconds
    try:
        run_async_entrypoint(
            run_async(args),
            shutdown_timeout_seconds=shutdown_timeout_seconds,
        )
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
