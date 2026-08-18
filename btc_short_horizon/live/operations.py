"""Periodic live control-plane orchestration around the execution safety core."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isclose, isfinite
from time import monotonic, time_ns
from typing import Protocol

from btc_short_horizon.live.dashboard_state import (
    BotDashboardSnapshot,
    DashboardSnapshotStore,
    DashboardAlert,
    HealthIndicator,
    HealthState,
    StrategyCycle,
)
from btc_short_horizon.live.ledger import (
    DailyAccountLedger,
    DailyAccountLedgerStore,
    ledger_performance_snapshot,
)
from btc_short_horizon.live.reconciliation import (
    ClobStartupReconciler,
    StartupReadiness,
    StartupReconciliationResult,
)
from btc_short_horizon.live.risk import AccountSnapshot
from btc_short_horizon.live.runtime import RuntimeStatus, RuntimeStatusStore
from btc_short_horizon.live.service import LiveExecutionService
from btc_short_horizon.live.user_channel import AuthenticatedUserChannel, UserChannelHealth


@dataclass(frozen=True, slots=True)
class LiveOperationsConfig:
    heartbeat_interval_seconds: float = 5.0
    reconciliation_interval_seconds: float = 30.0
    status_interval_seconds: float = 5.0
    startup_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 15.0
    max_external_readiness_age_seconds: float = 5.0
    max_dashboard_points: int = 2_016
    account_tolerance: float = 1e-6

    def __post_init__(self) -> None:
        for name in (
            "heartbeat_interval_seconds",
            "reconciliation_interval_seconds",
            "status_interval_seconds",
            "startup_timeout_seconds",
            "shutdown_timeout_seconds",
            "max_external_readiness_age_seconds",
            "account_tolerance",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"{name} must be finite and > 0")
        if (
            isinstance(self.max_dashboard_points, bool)
            or not isinstance(self.max_dashboard_points, int)
            or self.max_dashboard_points < 2
        ):
            raise ValueError("max_dashboard_points must be an integer >= 2")


@dataclass(frozen=True, slots=True)
class ExternalReadiness:
    """Credential-free state supplied by the market-data/clock supervisor."""

    observed_at_ns: int
    feeds_healthy: bool
    market_channel_healthy: bool
    clock_healthy: bool

    def __post_init__(self) -> None:
        _timestamp(self.observed_at_ns, "external readiness observed_at_ns")
        for name in ("feeds_healthy", "market_channel_healthy", "clock_healthy"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")


class AccountLedgerRefresher(Protocol):
    def refresh(
        self,
        *,
        capture_started_at_ns: int,
        submitted_order_count: int,
        confirmed_fill_count: int,
        previous: DailyAccountLedger | None = None,
    ) -> DailyAccountLedger: ...


class StartupReconciler(Protocol):
    def reconcile(
        self,
        *,
        expected_orders: Sequence[object],
        ledger: object,
        readiness: StartupReadiness,
    ) -> StartupReconciliationResult: ...


class UserChannel(Protocol):
    @property
    def health(self) -> UserChannelHealth: ...

    def acknowledge_reconciliation(self, generation: int) -> None: ...


class CollectingUserChannel(UserChannel, Protocol):
    async def collect_forever(
        self,
        *,
        stop_event: asyncio.Event,
        on_event: object,
        on_health: object,
    ) -> None: ...


class LiveOperationsController:
    """Serialize heartbeat, ledger, REST reconciliation, and User-WS gap gates."""

    def __init__(
        self,
        *,
        config: LiveOperationsConfig,
        service: LiveExecutionService,
        user_channel: UserChannel | AuthenticatedUserChannel,
        ledger_refresher: AccountLedgerRefresher,
        ledger_store: DailyAccountLedgerStore,
        reconciler: StartupReconciler | ClobStartupReconciler,
    ) -> None:
        if config.heartbeat_interval_seconds >= service.config.heartbeat_timeout_seconds:
            raise ValueError("heartbeat interval must be shorter than the service timeout")
        self.config = config
        self.service = service
        self.user_channel = user_channel
        self.ledger_refresher = ledger_refresher
        self.ledger_store = ledger_store
        self.reconciler = reconciler
        self.account: AccountSnapshot | None = None
        self.ledger: DailyAccountLedger | None = None
        self.ledger_sha256: str | None = None
        self.last_heartbeat_ts_ns: int | None = None
        self.last_reconciliation_ts_ns: int | None = None
        self.last_error: str | None = None
        self._capture_started_at_ns: int | None = None
        self._history: list[DailyAccountLedger] = []
        self._restored = False

    def restore(self) -> int:
        if self._restored:
            raise ValueError("live operations have already restored the execution WAL")
        restored = self.service.restore_from_wal()
        self._restored = True
        return restored

    def send_heartbeat(self, *, ts_ns: int) -> str:
        self._require_restored()
        _timestamp(ts_ns, "heartbeat ts_ns")
        try:
            heartbeat_id = self.service.send_venue_heartbeat(ts_ns=ts_ns)
        except Exception as exc:
            self.last_error = f"heartbeat:{type(exc).__name__}"
            raise
        self.last_heartbeat_ts_ns = ts_ns
        return heartbeat_id

    def refresh_and_reconcile(
        self,
        *,
        readiness: ExternalReadiness,
        ts_ns: int,
    ) -> StartupReconciliationResult:
        self._require_restored()
        _timestamp(ts_ns, "reconciliation ts_ns")
        self._validate_external_readiness(readiness, ts_ns=ts_ns)
        channel_health = self.user_channel.health
        if not channel_health.connected or not channel_health.pong_healthy:
            raise ValueError("user channel is not connected with a current PONG")
        if not self._heartbeat_healthy(ts_ns):
            raise ValueError("venue heartbeat is not current")
        if self._capture_started_at_ns is None:
            self._capture_started_at_ns = readiness.observed_at_ns
        progress = self.service.canary_progress
        try:
            ledger = self.ledger_refresher.refresh(
                capture_started_at_ns=self._capture_started_at_ns,
                submitted_order_count=progress.submitted_orders,
                confirmed_fill_count=progress.fills,
                previous=self.ledger,
            )
            receipt = self.ledger_store.write(ledger)
            startup_readiness = StartupReadiness(
                observed_at_ns=readiness.observed_at_ns,
                feeds_healthy=readiness.feeds_healthy,
                market_channel_healthy=readiness.market_channel_healthy,
                user_channel_healthy=True,
                heartbeat_healthy=True,
                clock_healthy=readiness.clock_healthy,
            )
            result = self.reconciler.reconcile(
                expected_orders=self.service.startup_order_expectations,
                ledger=ledger.to_reconciliation_snapshot(receipt.ledger_sha256),
                readiness=startup_readiness,
            )
            self._assert_independent_account_match(ledger, result.account)
            self.service.complete_startup_reconciliation(
                account=result.account,
                evidence=result.evidence,
                ts_ns=ts_ns,
            )
            self.user_channel.acknowledge_reconciliation(channel_health.generation)
        except Exception as exc:
            self.last_error = f"reconciliation:{type(exc).__name__}"
            self.service.emergency_stop(ts_ns=ts_ns, reason="operations_reconciliation_failed")
            raise
        self.ledger = ledger
        self.ledger_sha256 = receipt.ledger_sha256
        self.account = result.account
        self.last_reconciliation_ts_ns = ts_ns
        self.last_error = None
        if not self._history or ledger.observed_at_ns > self._history[-1].observed_at_ns:
            self._history.append(ledger)
            if len(self._history) > self.config.max_dashboard_points:
                del self._history[: len(self._history) - self.config.max_dashboard_points]
        return result

    def handle_user_event(self, event: Mapping[str, object], *, ts_ns: int) -> bool:
        self._require_restored()
        applied = self.service.reconcile_user_event(event, ts_ns=ts_ns)
        if not applied and self.service.halted:
            self.last_error = "user_channel:event_rejected"
        return applied

    def handle_user_channel_health(self, *, ts_ns: int) -> bool:
        self._require_restored()
        health = self.user_channel.health
        if not self.service.startup_reconciled or health.ready:
            return False
        self.last_error = "user_channel:gap"
        return self.service.emergency_stop(ts_ns=ts_ns, reason="user_channel_gap")

    def enforce_heartbeat_timeout(self, *, ts_ns: int) -> bool:
        self._require_restored()
        triggered = self.service.enforce_heartbeat_timeout(now_ts_ns=ts_ns)
        if triggered:
            self.last_error = "heartbeat:timeout"
        return triggered

    def shutdown(self, *, ts_ns: int, reason: str) -> bool:
        self._require_restored()
        return self.service.emergency_stop(ts_ns=ts_ns, reason=reason)

    def build_dashboard_snapshot(
        self,
        *,
        strategy: StrategyCycle,
        now: datetime,
    ) -> BotDashboardSnapshot:
        current_time = _utc(now)
        current_ns = int(current_time.timestamp() * 1_000_000_000)
        health = self._health_indicators(current_time, current_ns)
        alerts = tuple(
            DashboardAlert(
                state=item.state,
                message=item.detail,
                created_at=item.updated_at,
            )
            for item in health
            if item.state in {HealthState.WARNING, HealthState.ERROR}
        )
        performance = (
            None if not self._history else ledger_performance_snapshot(tuple(self._history))
        )
        return BotDashboardSnapshot(
            generated_at=current_time,
            run_mode=self.service.config.mode.value,
            strategy=strategy,
            performance=performance,
            health=health,
            alerts=alerts,
        )

    def _health_indicators(
        self,
        now: datetime,
        now_ns: int,
    ) -> tuple[HealthIndicator, ...]:
        channel = self.user_channel.health
        heartbeat_ok = self._heartbeat_healthy(now_ns)
        reconciliation_ok = (
            self.account is not None
            and self.service.startup_reconciled
            and not self.service.recovery_required
            and not self.service.halted
        )
        ledger_ok = self.ledger is not None and self.ledger_sha256 is not None
        items = (
            (
                "user-channel",
                "Polymarket User WS",
                channel.ready,
                "User channel synchronized"
                if channel.ready
                else "User channel gap or PONG failure",
            ),
            (
                "venue-heartbeat",
                "CLOB dead-man heartbeat",
                heartbeat_ok,
                "Heartbeat current" if heartbeat_ok else "Heartbeat missing or stale",
            ),
            (
                "account-reconciliation",
                "Account reconciliation",
                reconciliation_ok,
                (
                    "Ledger, orders, trades and positions agree"
                    if reconciliation_ok
                    else "Account is halted or not reconciled"
                ),
            ),
            (
                "account-ledger",
                "Durable account ledger",
                ledger_ok,
                "Content-addressed ledger current" if ledger_ok else "No durable ledger snapshot",
            ),
        )
        return tuple(
            HealthIndicator(
                key=key,
                label=label,
                state=HealthState.OK if ok else HealthState.ERROR,
                detail=detail,
                updated_at=now,
            )
            for key, label, ok, detail in items
        )

    def _assert_independent_account_match(
        self,
        ledger: DailyAccountLedger,
        account: AccountSnapshot,
    ) -> None:
        expected = {
            "collateral balance": (ledger.collateral_balance, account.collateral_balance),
            "collateral allowance": (
                ledger.collateral_allowance,
                account.collateral_allowance,
            ),
            "account equity": (ledger.equity, account.account_equity),
            "position cost": (ledger.position_cost, account.unresolved_position_cost),
            "open-order notional": (
                ledger.open_order_notional,
                account.open_order_notional,
            ),
            "daily realized PnL": (
                ledger.daily_realized_pnl,
                account.daily_realized_pnl,
            ),
            "unrealized PnL": (ledger.unrealized_pnl, account.unrealized_pnl),
        }
        for name, (ledger_value, account_value) in expected.items():
            if not isclose(
                ledger_value,
                account_value,
                abs_tol=self.config.account_tolerance,
            ):
                raise ValueError(f"ledger and reconciliation disagree on {name}")
        if ledger.open_order_count != account.open_orders:
            raise ValueError("ledger and reconciliation disagree on open-order count")
        if ledger.day != account.daily_pnl_day:
            raise ValueError("ledger and reconciliation disagree on UTC PnL day")

    def _validate_external_readiness(
        self,
        readiness: ExternalReadiness,
        *,
        ts_ns: int,
    ) -> None:
        age_ns = ts_ns - readiness.observed_at_ns
        if age_ns < 0:
            raise ValueError("external readiness is future-dated")
        if age_ns > int(self.config.max_external_readiness_age_seconds * 1_000_000_000):
            raise ValueError("external readiness is stale")

    def _heartbeat_healthy(self, ts_ns: int) -> bool:
        if self.last_heartbeat_ts_ns is None:
            return False
        age_ns = ts_ns - self.last_heartbeat_ts_ns
        return 0 <= age_ns <= int(self.service.config.heartbeat_timeout_seconds * 1_000_000_000)

    def _require_restored(self) -> None:
        if not self._restored:
            raise RuntimeError("restore the execution WAL before running live operations")


async def run_live_operations_runtime(
    *,
    controller: LiveOperationsController,
    readiness_provider: Callable[[], ExternalReadiness],
    strategy_provider: Callable[[], StrategyCycle],
    dashboard_store: DashboardSnapshotStore,
    status_store: RuntimeStatusStore,
    stop_event: asyncio.Event | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    clock_ns: Callable[[], int] = time_ns,
) -> None:
    """Run the control plane; strategy decisions remain a separate caller boundary."""

    runtime_stop = stop_event or asyncio.Event()
    started_at = _utc(now())
    controller.restore()
    _write_runtime_status(
        controller=controller,
        store=status_store,
        state="starting",
        healthy=True,
        started_at=started_at,
        now=now,
    )

    async def on_event(event: Mapping[str, object], received_at_ns: int) -> None:
        await asyncio.to_thread(
            controller.handle_user_event,
            event,
            ts_ns=received_at_ns,
        )

    async def on_health() -> None:
        await asyncio.to_thread(controller.handle_user_channel_health, ts_ns=clock_ns())

    channel = controller.user_channel
    if not hasattr(channel, "collect_forever"):
        raise TypeError("live operations require a collecting User channel")
    channel_task = asyncio.create_task(
        channel.collect_forever(  # type: ignore[attr-defined]
            stop_event=runtime_stop,
            on_event=on_event,
            on_health=on_health,
        ),
        name="btc-user-channel",
    )
    primary_failure: Exception | None = None
    cancelled = False
    try:
        await _wait_for_user_channel(
            channel=channel,
            channel_task=channel_task,
            stop_event=runtime_stop,
            timeout_seconds=controller.config.startup_timeout_seconds,
        )
        startup_ts_ns = clock_ns()
        await asyncio.to_thread(controller.send_heartbeat, ts_ns=startup_ts_ns)
        readiness = await asyncio.to_thread(readiness_provider)
        await asyncio.to_thread(
            controller.refresh_and_reconcile,
            readiness=readiness,
            ts_ns=clock_ns(),
        )
        await _publish_operations_projection(
            controller=controller,
            strategy_provider=strategy_provider,
            dashboard_store=dashboard_store,
            status_store=status_store,
            state="running",
            started_at=started_at,
            now=now,
        )
        current = monotonic()
        next_heartbeat = current + controller.config.heartbeat_interval_seconds
        next_reconciliation = current + controller.config.reconciliation_interval_seconds
        next_status = current + controller.config.status_interval_seconds
        while not runtime_stop.is_set():
            current = monotonic()
            timeout = max(
                0.0,
                min(next_heartbeat, next_reconciliation, next_status) - current,
            )
            try:
                await asyncio.wait_for(runtime_stop.wait(), timeout=timeout)
            except TimeoutError:
                pass
            if runtime_stop.is_set():
                break
            current = monotonic()
            current_ns = clock_ns()
            if current >= next_heartbeat:
                await asyncio.to_thread(controller.send_heartbeat, ts_ns=current_ns)
                next_heartbeat = current + controller.config.heartbeat_interval_seconds
            if current >= next_reconciliation:
                readiness = await asyncio.to_thread(readiness_provider)
                await asyncio.to_thread(
                    controller.refresh_and_reconcile,
                    readiness=readiness,
                    ts_ns=clock_ns(),
                )
                next_reconciliation = current + controller.config.reconciliation_interval_seconds
            await asyncio.to_thread(controller.enforce_heartbeat_timeout, ts_ns=clock_ns())
            if current >= next_status:
                await _publish_operations_projection(
                    controller=controller,
                    strategy_provider=strategy_provider,
                    dashboard_store=dashboard_store,
                    status_store=status_store,
                    state="running",
                    started_at=started_at,
                    now=now,
                )
                next_status = current + controller.config.status_interval_seconds
            if channel_task.done():
                await channel_task
                raise RuntimeError("User channel exited without a stop request")
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as exc:
        primary_failure = exc
        raise
    finally:
        cleanup_failure: Exception | None = None
        shutdown_ts_ns = clock_ns()
        try:
            await asyncio.to_thread(
                controller.shutdown,
                ts_ns=shutdown_ts_ns,
                reason=("runtime_failure" if primary_failure is not None else "runtime_stop"),
            )
        except Exception as shutdown_exc:
            cleanup_failure = shutdown_exc
        runtime_stop.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(channel_task),
                timeout=controller.config.shutdown_timeout_seconds,
            )
        except TimeoutError:
            channel_task.cancel()
            await asyncio.gather(channel_task, return_exceptions=True)
            if cleanup_failure is None:
                cleanup_failure = TimeoutError("User channel exceeded the shutdown deadline")
        except Exception as channel_exc:
            if cleanup_failure is None:
                cleanup_failure = channel_exc
        reported_failure = primary_failure or cleanup_failure
        final_state = "failed" if reported_failure is not None else "stopped"
        _write_runtime_status(
            controller=controller,
            store=status_store,
            state=final_state,
            healthy=reported_failure is None,
            started_at=started_at,
            now=now,
            error=reported_failure,
        )
        if primary_failure is None and not cancelled and cleanup_failure is not None:
            raise cleanup_failure


async def _wait_for_user_channel(
    *,
    channel: UserChannel,
    channel_task: asyncio.Task[None],
    stop_event: asyncio.Event,
    timeout_seconds: float,
) -> None:
    deadline = monotonic() + timeout_seconds
    while True:
        if channel.health.connected and channel.health.pong_healthy:
            return
        if stop_event.is_set():
            raise RuntimeError("live operations stopped before User channel startup")
        if channel_task.done():
            await channel_task
            raise RuntimeError("User channel exited during startup")
        if monotonic() >= deadline:
            raise TimeoutError("User channel did not become ready before startup timeout")
        await asyncio.sleep(min(0.05, max(0.0, deadline - monotonic())))


async def _publish_operations_projection(
    *,
    controller: LiveOperationsController,
    strategy_provider: Callable[[], StrategyCycle],
    dashboard_store: DashboardSnapshotStore,
    status_store: RuntimeStatusStore,
    state: str,
    started_at: datetime,
    now: Callable[[], datetime],
) -> None:
    current_time = _utc(now())
    strategy = await asyncio.to_thread(strategy_provider)
    snapshot = controller.build_dashboard_snapshot(strategy=strategy, now=current_time)
    await asyncio.to_thread(dashboard_store.write, snapshot)
    _write_runtime_status(
        controller=controller,
        store=status_store,
        state=state,
        healthy=all(item.state is HealthState.OK for item in snapshot.health),
        started_at=started_at,
        now=lambda: current_time,
    )


def _write_runtime_status(
    *,
    controller: LiveOperationsController,
    store: RuntimeStatusStore,
    state: str,
    healthy: bool,
    started_at: datetime,
    now: Callable[[], datetime],
    error: Exception | None = None,
) -> None:
    progress = controller.service.canary_progress
    details: dict[str, object] = {
        "startup_reconciled": controller.service.startup_reconciled,
        "halted": controller.service.halted,
        "recovery_required": controller.service.recovery_required,
        "ledger_sha256": controller.ledger_sha256,
        "account_observed_at_ns": (
            None if controller.account is None else controller.account.observed_at_ns
        ),
        "user_channel_generation": controller.user_channel.health.generation,
        "user_channel_ready": controller.user_channel.health.ready,
        "last_heartbeat_ts_ns": controller.last_heartbeat_ts_ns,
        "last_reconciliation_ts_ns": controller.last_reconciliation_ts_ns,
        "last_error": controller.last_error,
        "canary": {
            "submitted_orders": progress.submitted_orders,
            "confirmed_fills": progress.fills,
            "ready_for_extended_canary": progress.ready_for_extended_canary,
            "ready_for_scale_review": progress.ready_for_scale_review,
        },
    }
    if error is not None:
        details["error_type"] = type(error).__name__
    store.write(
        RuntimeStatus(
            service="live_operations",
            mode=controller.service.config.mode.value,
            state=state,
            healthy=healthy,
            started_at=started_at,
            updated_at=_utc(now()),
            details=details,
        )
    )


def _timestamp(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dashboard clock must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "ExternalReadiness",
    "LiveOperationsConfig",
    "LiveOperationsController",
    "run_live_operations_runtime",
]
