from btc_short_horizon.data.disk_pressure import DiskFeedSupervisor, DiskProtectionPolicy
import asyncio
from btc_short_horizon.data.disk_pressure import supervise_optional_feed


def test_optional_feeds_shed_and_recover_with_hysteresis_core_never_stops() -> None:
    supervisor = DiskFeedSupervisor(DiskProtectionPolicy(20, 15, 10), recovery_margin_gib=2)
    assert not supervisor.update(14).value == "normal"
    shed = supervisor.subscriptions(
        core_spot=("trade",), optional_perp=("aggTrade",), optional_okx=({"channel": "books5"},)
    )
    assert shed == {"binance_spot": ("trade",), "binance_perp": (), "okx": ()}
    supervisor.update(16)
    assert not supervisor.optional_feeds_enabled
    supervisor.update(18)
    assert supervisor.optional_feeds_enabled


def test_hysteresis_never_blocks_escalation_to_critical_pressure() -> None:
    supervisor = DiskFeedSupervisor(DiskProtectionPolicy(20, 15, 10), recovery_margin_gib=2)
    assert supervisor.update(14).value == "shed_optional_feeds"
    assert supervisor.update(5).value == "suspend_extended_capture"


def test_optional_socket_is_cancelled_and_restarted() -> None:
    async def scenario() -> None:
        enabled, stopped = asyncio.Event(), asyncio.Event()
        enabled.set()
        starts: list[asyncio.Event] = []

        async def run_once(local_stop: asyncio.Event) -> None:
            starts.append(local_stop)
            await local_stop.wait()

        task = asyncio.create_task(
            supervise_optional_feed(stop_event=stopped, enabled_event=enabled, run_once=run_once)
        )
        await asyncio.sleep(0)
        enabled.clear()
        await asyncio.sleep(0.2)
        assert starts[0].is_set()
        enabled.set()
        await asyncio.sleep(0.2)
        assert len(starts) == 2
        stopped.set()
        enabled.clear()
        await task

    asyncio.run(scenario())


def test_optional_socket_failure_is_isolated_and_retried() -> None:
    async def scenario() -> None:
        enabled, stopped = asyncio.Event(), asyncio.Event()
        enabled.set()
        attempts = 0

        async def run_once(_local_stop: asyncio.Event) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("optional socket")
            stopped.set()

        await supervise_optional_feed(
            stop_event=stopped,
            enabled_event=enabled,
            run_once=run_once,
            retry_seconds=0,
        )
        assert attempts == 2

    asyncio.run(scenario())
