"""Run the BTC 15m forward collector with local status and stop controls."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
import os
from pathlib import Path
import signal

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root, run_async_entrypoint
else:
    from ._script_helpers import ensure_repo_root, run_async_entrypoint

ensure_repo_root(__file__)

from btc_short_horizon.config import BtcProjectConfig, load_btc_project_config  # noqa: E402
from btc_short_horizon.data import BtcForwardCollector, MarketWindow  # noqa: E402
from btc_short_horizon.data.disk_pressure import DiskFeedSupervisor  # noqa: E402
from btc_short_horizon.data.forward import (  # noqa: E402
    AdmittedEventBuffer,
)
from btc_short_horizon.data.session_inventory import (  # noqa: E402
    CollectorStorageLease,
    SessionInventoryRepository,
)
from btc_short_horizon.live.forward_runtime import (  # noqa: E402
    ForwardCollectorRuntimeConfig,
    run_forward_collector_runtime,
)
from btc_short_horizon.live.paper_runtime import ResearchPaperRuntime  # noqa: E402
from btc_short_horizon.live.runtime import (  # noqa: E402
    RuntimeStatus,
    RuntimeStatusStore,
    build_runtime_identity,
    filesystem_usage,
)
from scripts.btc_forward_collector import (  # noqa: E402
    _readiness_protocol,
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


def _captured_market(
    window: _ActiveWindow,
    *,
    now: datetime,
    lead_seconds: float,
    handoff_seconds: float,
) -> MarketWindow | None:
    for market in (window.market, window.lookahead):
        if market is None:
            continue
        if (
            market.t0 - timedelta(seconds=lead_seconds)
            <= now
            < market.t0 + timedelta(seconds=handoff_seconds)
        ):
            return market
    return None


def _refresh_required_clob_feeds(
    window: _ActiveWindow,
    *,
    now: datetime,
    lead_seconds: float,
    handoff_seconds: float,
) -> MarketWindow | None:
    market = _captured_market(
        window,
        now=now,
        lead_seconds=lead_seconds,
        handoff_seconds=handoff_seconds,
    )
    tokens = () if market is None else (market.up_token_id, market.down_token_id)
    window.collector.configure_required_polymarket_tokens(tokens)
    return market


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
    parser.add_argument("--polymarket-capture-lead-seconds", type=float)
    parser.add_argument("--opening-handoff-delay-seconds", type=float)
    parser.add_argument("--flush-size", type=int)
    parser.add_argument("--flush-interval-seconds", type=float)
    parser.add_argument("--binance-stream", action="append", default=None)
    parser.add_argument("--binance-futures-market-stream", action="append", default=None)
    parser.add_argument("--binance-futures-public-stream", action="append", default=None)
    parser.add_argument("--paper-model-directory", type=Path)
    market_relative_directory = os.environ.get("BTC_MARKET_RELATIVE_MODEL_DIRECTORY", "").strip()
    parser.add_argument(
        "--paper-market-relative-model-directory",
        type=Path,
        default=Path(market_relative_directory) if market_relative_directory else None,
    )
    parser.add_argument("--paper-starting-balance", type=float, default=1_000.0)
    parser.add_argument("--paper-event-buffer-size", type=int, default=20_000)
    return parser.parse_args(argv)


def _initialize_research_paper(
    *,
    project: BtcProjectConfig,
    model_directory: Path,
    market_relative_model_directory: Path | None = None,
    runtime_root: Path,
    rule_epoch: str,
    starting_balance: float,
    max_events: int,
) -> tuple[AdmittedEventBuffer | None, ResearchPaperRuntime | None]:
    started_at = datetime.now(UTC)
    try:
        buffer = AdmittedEventBuffer(max_events=max_events)
        runtime = ResearchPaperRuntime(
            project=project,
            model_directory=model_directory,
            market_relative_model_directory=market_relative_model_directory,
            runtime_root=runtime_root,
            rule_epoch=rule_epoch,
            event_buffer=buffer,
            starting_balance=starting_balance,
        )
    except Exception as exc:
        RuntimeStatusStore(runtime_root).write(
            RuntimeStatus(
                service="research_paper",
                mode="paper",
                state="failed",
                healthy=False,
                started_at=started_at,
                updated_at=datetime.now(UTC),
                details={
                    "ready": False,
                    "last_error": f"{type(exc).__name__}: {exc}",
                    "credentials_loaded": False,
                    "real_orders_enabled": False,
                },
            )
        )
        return None, None
    return buffer, runtime


async def _run_research_paper_supervisor(
    *,
    initial_runtime: ResearchPaperRuntime,
    create_runtime: Callable[[], ResearchPaperRuntime],
    on_runtime: Callable[[ResearchPaperRuntime], None],
    stop_event: asyncio.Event,
    retry_seconds: float = 1.0,
) -> None:
    runtime = initial_runtime
    while not stop_event.is_set():
        try:
            await runtime.run(stop_event=stop_event)
        except Exception:
            runtime.close()
            if stop_event.is_set():
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=retry_seconds)
            except TimeoutError:
                pass
            if stop_event.is_set():
                return
            while not stop_event.is_set():
                try:
                    runtime = create_runtime()
                except Exception:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=retry_seconds)
                    except TimeoutError:
                        continue
                else:
                    break
            if stop_event.is_set():
                return
            on_runtime(runtime)
            continue
        return


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
    binance_streams = tuple(args.binance_stream or project.collection.binance_spot_streams)
    binance_futures_market_streams = tuple(
        args.binance_futures_market_stream or project.collection.binance_futures_market_streams
    )
    binance_futures_public_streams = tuple(
        args.binance_futures_public_stream or project.collection.binance_futures_public_streams
    )
    okx_subscriptions = tuple(
        {"channel": item.channel, "instId": item.instrument}
        for item in project.collection.okx_subscriptions
    )
    polymarket_capture_lead_seconds = (
        args.polymarket_capture_lead_seconds
        if args.polymarket_capture_lead_seconds is not None
        else project.collection.polymarket_capture_lead_seconds
    )
    opening_handoff_delay_seconds = (
        args.opening_handoff_delay_seconds
        if args.opening_handoff_delay_seconds is not None
        else project.collection.opening_handoff_delay_seconds
    )
    identity = build_runtime_identity(
        config_path=args.config,
        ingest_version=project.collection.ingest_version,
    )
    recovered_interrupted_sessions: tuple[str, ...] = ()
    stop_event = asyncio.Event()
    paper_buffer: AdmittedEventBuffer | None = None
    paper_runtime: ResearchPaperRuntime | None = None
    if args.paper_model_directory is not None:
        paper_buffer, paper_runtime = _initialize_research_paper(
            project=project,
            model_directory=args.paper_model_directory,
            market_relative_model_directory=args.paper_market_relative_model_directory,
            runtime_root=runtime_root,
            rule_epoch=args.rule_epoch,
            starting_balance=args.paper_starting_balance,
            max_events=args.paper_event_buffer_size,
        )
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
        first_activation = active is None or active.collector is not collector
        collector.configure_required_feeds(
            binance_streams=binance_streams,
            binance_futures_market_streams=binance_futures_market_streams,
            binance_futures_public_streams=binance_futures_public_streams,
            okx_subscriptions=okx_subscriptions,
        )
        if paper_buffer is not None and paper_runtime is not None:
            if first_activation:
                collector.subscribe_admitted_events(paper_buffer)
            paper_runtime.register_markets(market, lookahead)
        active = _ActiveWindow(market=market, lookahead=lookahead, collector=collector)
        _refresh_required_clob_feeds(
            active,
            now=datetime.now(UTC),
            lead_seconds=polymarket_capture_lead_seconds,
            handoff_seconds=opening_handoff_delay_seconds,
        )

    def feed_health() -> tuple[bool, str]:
        if (datetime.now(UTC) - started_at).total_seconds() <= args.feed_startup_grace_seconds:
            return True, "feed_startup_grace"
        if active is None:
            return False, "market_discovery_silent"
        # Disk pressure degrades optional/extended evidence but never restarts core feeds.
        _refresh_required_clob_feeds(
            active,
            now=datetime.now(UTC),
            lead_seconds=polymarket_capture_lead_seconds,
            handoff_seconds=opening_handoff_delay_seconds,
        )
        health = active.collector.feed_health(
            now=datetime.now(UTC),
            stale_after_seconds=args.feed_stale_after_seconds,
        )
        return health.healthy, health.reason

    def status_details() -> dict[str, object]:
        raw_usage = filesystem_usage(project.paths.raw_data_root)
        free_gib = float(raw_usage["free_bytes"]) / (1024.0**3)
        disk_pressure = project.collection.disk_protection.evaluate_free_gib(free_gib)
        details: dict[str, object] = {
            "family": project.primary_family.name,
            "ingest_version": project.collection.ingest_version,
            "scheduled_market": current_market_slug(project.primary_family, datetime.now(UTC)),
            "identity": identity,
            "recovered_interrupted_sessions": list(recovered_interrupted_sessions),
            "storage": {
                "raw_data": raw_usage,
                "runtime": filesystem_usage(runtime_root),
            },
            "disk_pressure": {
                "state": disk_pressure.value,
                "free_gib": free_gib,
                "warning_free_gib": project.collection.disk_protection.warning_free_gib,
                "optional_feeds_free_gib": (
                    project.collection.disk_protection.optional_feeds_free_gib
                ),
                "extended_capture_free_gib": (
                    project.collection.disk_protection.extended_capture_free_gib
                ),
                "automatic_deletion": False,
            },
        }
        if active is None:
            details["feeds"] = []
            return details
        status_now = datetime.now(UTC)
        captured_market = _refresh_required_clob_feeds(
            active,
            now=status_now,
            lead_seconds=polymarket_capture_lead_seconds,
            handoff_seconds=opening_handoff_delay_seconds,
        )
        health = active.collector.feed_health(
            now=datetime.now(UTC),
            stale_after_seconds=args.feed_stale_after_seconds,
        )
        quality = tuple(active.collector.quality_stats.values())
        buffer = active.collector.buffer_stats
        effective_market = _effective_market(active, now=status_now)
        details.update(
            {
                "active_market": effective_market.slug,
                "lookahead_market": (None if active.lookahead is None else active.lookahead.slug),
                "clob_capture_active": captured_market is not None,
                "clob_capture_market": (None if captured_market is None else captured_market.slug),
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
        def create_paper_runtime() -> ResearchPaperRuntime:
            assert paper_buffer is not None
            return ResearchPaperRuntime(
                project=project,
                model_directory=args.paper_model_directory,
                market_relative_model_directory=args.paper_market_relative_model_directory,
                runtime_root=runtime_root,
                rule_epoch=args.rule_epoch,
                event_buffer=paper_buffer,
                starting_balance=args.paper_starting_balance,
            )

        def on_paper_runtime(runtime: ResearchPaperRuntime) -> None:
            nonlocal paper_runtime
            paper_runtime = runtime
            if active is not None:
                runtime.register_markets(active.market, active.lookahead)

        paper_task = (
            None
            if paper_runtime is None
            else asyncio.create_task(
                _run_research_paper_supervisor(
                    initial_runtime=paper_runtime,
                    create_runtime=create_paper_runtime,
                    on_runtime=on_paper_runtime,
                    stop_event=stop_event,
                ),
                name="btc-research-paper",
            )
        )
        try:
            optional_feeds_enabled = asyncio.Event()
            optional_feeds_enabled.set()
            extended_capture_enabled = asyncio.Event()
            extended_capture_enabled.set()
            disk_supervisor = DiskFeedSupervisor(project.collection.disk_protection)

            async def supervise_disk_feeds() -> None:
                while not stop_event.is_set():
                    free_bytes = filesystem_usage(project.paths.raw_data_root)["free_bytes"]
                    disk_supervisor.update(float(free_bytes) / (1024.0**3))
                    if disk_supervisor.optional_feeds_enabled:
                        optional_feeds_enabled.set()
                    else:
                        optional_feeds_enabled.clear()
                    if disk_supervisor.state.value == "suspend_extended_capture":
                        extended_capture_enabled.clear()
                    else:
                        extended_capture_enabled.set()
                    await asyncio.sleep(5.0)

            disk_task = asyncio.create_task(supervise_disk_feeds(), name="btc-disk-feed-supervisor")
            await collect_current_market_windows(
                family=project.primary_family,
                rule_epoch=args.rule_epoch,
                raw_data_root=project.paths.raw_data_root,
                catalog_directory=catalog_directory,
                flush_size=(
                    args.flush_size
                    if args.flush_size is not None
                    else project.collection.flush_size
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
                okx_subscriptions=okx_subscriptions,
                rotation_poll_seconds=(
                    args.rotation_poll_seconds
                    if args.rotation_poll_seconds is not None
                    else project.collection.rotation_poll_seconds
                ),
                polymarket_capture_lead_seconds=polymarket_capture_lead_seconds,
                opening_handoff_delay_seconds=opening_handoff_delay_seconds,
                stop_event=stop_event,
                on_market_active=on_market_active,
                **_readiness_protocol(project),
                optional_feeds_enabled=optional_feeds_enabled,
                extended_capture_enabled=extended_capture_enabled,
            )
        finally:
            if "disk_task" in locals():
                disk_task.cancel()
                await asyncio.gather(disk_task, return_exceptions=True)
            if paper_task is not None:
                if not stop_event.is_set():
                    stop_event.set()
                await paper_task

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
