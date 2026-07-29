from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from btc_short_horizon.live.dashboard_state import (
    DashboardSnapshotStore,
    GateState,
    StrategyCycle,
    StrategyStage,
)
from btc_short_horizon.live.gateway import PaperOrderGateway
from btc_short_horizon.live.ledger import DailyAccountLedger, DailyAccountLedgerStore
from btc_short_horizon.live.operations import (
    ExternalReadiness,
    LiveOperationsConfig,
    LiveOperationsController,
    run_live_operations_runtime,
)
from btc_short_horizon.live.reconciliation import (
    StartupReconciliationEvidence,
    StartupReconciliationResult,
    account_snapshot_sha256,
)
from btc_short_horizon.live.risk import AccountSnapshot, TradingSafetyConfig
from btc_short_horizon.live.runtime import RuntimeStatusStore
from btc_short_horizon.live.service import LiveExecutionConfig, LiveExecutionService, LiveMode
from btc_short_horizon.live.user_channel import UserChannelHealth
from btc_short_horizon.live.wal import JsonlWriteAheadLog


NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)
NOW_NS = int(NOW.timestamp() * 1_000_000_000)
DAY = date(2026, 7, 21)
FUNDER = "0x" + ("1" * 40)
CONDITION = "0x" + ("a" * 64)


def _ledger(**overrides: object) -> DailyAccountLedger:
    values: dict[str, object] = {
        "day": DAY,
        "capture_started_at_ns": NOW_NS - 10,
        "source_started_at_ns": NOW_NS - 3,
        "observed_at_ns": NOW_NS,
        "covered_through_ns": NOW_NS - 1,
        "account_address": FUNDER,
        "collateral_balance": 100.0,
        "collateral_allowance": 100.0,
        "position_cost": 4.5,
        "position_value": 5.0,
        "unrealized_pnl": 0.5,
        "daily_realized_pnl": 0.0,
        "cumulative_realized_pnl": 2.0,
        "open_order_notional": 0.0,
        "open_order_count": 0,
        "submitted_order_count": 0,
        "confirmed_fill_count": 0,
        "trade_coverage": (),
        "closed_positions": (),
    }
    values.update(overrides)
    return DailyAccountLedger(**values)  # type: ignore[arg-type]


def _account(**overrides: object) -> AccountSnapshot:
    values: dict[str, object] = {
        "observed_at_ns": NOW_NS,
        "collateral_balance": 100.0,
        "collateral_allowance": 100.0,
        "account_equity": 105.0,
        "unresolved_position_cost": 4.5,
        "open_order_notional": 0.0,
        "daily_realized_pnl": 0.0,
        "unrealized_pnl": 0.5,
        "daily_pnl_day": DAY,
        "working_market_ids": frozenset({CONDITION}),
        "open_orders": 0,
        "feeds_healthy": True,
        "market_channel_healthy": True,
        "user_channel_healthy": True,
        "heartbeat_healthy": True,
        "clock_healthy": True,
        "geo_eligible": True,
        "account_reconciled": True,
    }
    values.update(overrides)
    return AccountSnapshot(**values)  # type: ignore[arg-type]


class _Refresher:
    def __init__(self, ledger: DailyAccountLedger) -> None:
        self.ledger = ledger
        self.calls = 0

    def refresh(self, **_kwargs: object) -> DailyAccountLedger:
        self.calls += 1
        return self.ledger


class _Reconciler:
    def __init__(self, account: AccountSnapshot) -> None:
        self.account = account
        self.ledger_hash: str | None = None

    def reconcile(self, *, expected_orders, ledger, readiness):  # type: ignore[no-untyped-def]
        assert expected_orders == ()
        assert readiness.user_channel_healthy
        self.ledger_hash = ledger.ledger_sha256
        evidence = StartupReconciliationEvidence(
            source_started_at_ns=NOW_NS - 2,
            observed_at_ns=self.account.observed_at_ns,
            expected_open_order_ids=frozenset(),
            venue_open_order_ids=frozenset(),
            missing_open_order_ids=frozenset(),
            unexpected_open_order_ids=frozenset(),
            recovered_orders=(),
            terminal_orders=(),
            unmatched_client_order_ids=frozenset(),
            pending_trade_ids=frozenset(),
            ledger_sha256=ledger.ledger_sha256,
            account_snapshot_sha256=account_snapshot_sha256(self.account),
            country="KR",
            region="11",
        )
        return StartupReconciliationResult(account=self.account, evidence=evidence)


class _Channel:
    def __init__(self) -> None:
        self._health = UserChannelHealth(
            connected=True,
            pong_healthy=True,
            gap_detected=True,
            generation=1,
        )
        self.acknowledged: list[int] = []

    @property
    def health(self) -> UserChannelHealth:
        return self._health

    def acknowledge_reconciliation(self, generation: int) -> None:
        assert generation == self._health.generation
        self.acknowledged.append(generation)
        self._health = UserChannelHealth(
            connected=True,
            pong_healthy=True,
            gap_detected=False,
            generation=generation,
        )


def _controller(
    tmp_path: Path,
    *,
    ledger: DailyAccountLedger | None = None,
    account: AccountSnapshot | None = None,
) -> tuple[LiveOperationsController, LiveExecutionService, _Channel]:
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            risk=TradingSafetyConfig(trading_enabled=False),
            heartbeat_timeout_seconds=10.0,
        ),
        wal=JsonlWriteAheadLog(tmp_path / "execution.jsonl"),
        gateway=PaperOrderGateway(),
    )
    channel = _Channel()
    controller = LiveOperationsController(
        config=LiveOperationsConfig(
            heartbeat_interval_seconds=5.0,
            reconciliation_interval_seconds=30.0,
        ),
        service=service,
        user_channel=channel,  # type: ignore[arg-type]
        ledger_refresher=_Refresher(ledger or _ledger()),  # type: ignore[arg-type]
        ledger_store=DailyAccountLedgerStore(tmp_path),
        reconciler=_Reconciler(account or _account()),  # type: ignore[arg-type]
    )
    return controller, service, channel


def _readiness() -> ExternalReadiness:
    return ExternalReadiness(
        observed_at_ns=NOW_NS,
        feeds_healthy=True,
        market_channel_healthy=True,
        clock_healthy=True,
    )


def _cycle() -> StrategyCycle:
    return StrategyCycle(
        stage=StrategyStage.CANARY,
        gate_state=GateState.PENDING,
        next_action="等待人工 Canary 发布审批。",
        model_id="opening-v2",
    )


def test_controller_restores_heartbeats_reconciles_and_projects_dashboard(tmp_path: Path) -> None:
    controller, service, channel = _controller(tmp_path)

    assert controller.restore() == 0
    controller.send_heartbeat(ts_ns=NOW_NS - 1)
    result = controller.refresh_and_reconcile(readiness=_readiness(), ts_ns=NOW_NS + 1)
    snapshot = controller.build_dashboard_snapshot(strategy=_cycle(), now=NOW)

    assert result.account.account_reconciled
    assert service.startup_reconciled
    assert channel.acknowledged == [1]
    assert controller.account == _account()
    assert snapshot.performance is not None
    assert snapshot.performance.equity == pytest.approx(105.0)
    assert snapshot.run_mode == "canary"
    assert all(item.state.value == "ok" for item in snapshot.health)
    assert not snapshot.alerts


def test_controller_halts_when_ledger_and_independent_reconciliation_disagree(
    tmp_path: Path,
) -> None:
    controller, service, channel = _controller(
        tmp_path,
        account=_account(account_equity=104.0),
    )
    controller.restore()
    controller.send_heartbeat(ts_ns=NOW_NS - 1)

    with pytest.raises(ValueError, match="account equity"):
        controller.refresh_and_reconcile(readiness=_readiness(), ts_ns=NOW_NS + 1)

    assert service.halted
    assert service.recovery_required
    assert channel.acknowledged == []


def test_user_channel_gap_after_reconciliation_immediately_halts_and_cancels(
    tmp_path: Path,
) -> None:
    controller, service, channel = _controller(tmp_path)
    controller.restore()
    controller.send_heartbeat(ts_ns=NOW_NS - 1)
    controller.refresh_and_reconcile(readiness=_readiness(), ts_ns=NOW_NS + 1)
    channel._health = UserChannelHealth(
        connected=False,
        pong_healthy=False,
        gap_detected=True,
        generation=2,
    )

    assert controller.handle_user_channel_health(ts_ns=NOW_NS + 2)
    assert service.halted
    assert service.recovery_required


def test_operations_configuration_prevents_heartbeat_slower_than_service_timeout(
    tmp_path: Path,
) -> None:
    service = LiveExecutionService(
        config=LiveExecutionConfig(
            mode=LiveMode.CANARY,
            heartbeat_timeout_seconds=5.0,
        ),
        wal=JsonlWriteAheadLog(tmp_path / "execution.jsonl"),
        gateway=PaperOrderGateway(),
    )

    with pytest.raises(ValueError, match="heartbeat interval"):
        LiveOperationsController(
            config=LiveOperationsConfig(heartbeat_interval_seconds=5.0),
            service=service,
            user_channel=_Channel(),  # type: ignore[arg-type]
            ledger_refresher=_Refresher(_ledger()),  # type: ignore[arg-type]
            ledger_store=DailyAccountLedgerStore(tmp_path),
            reconciler=_Reconciler(_account()),  # type: ignore[arg-type]
        )


def test_async_runtime_connects_reconciles_publishes_and_stops_boundedly(tmp_path: Path) -> None:
    controller, service, _ = _controller(tmp_path)

    class Channel(_Channel):
        async def collect_forever(self, *, stop_event, on_event, on_health):  # type: ignore[no-untyped-def]
            del on_event
            await on_health()
            await stop_event.wait()

    channel = Channel()
    controller.user_channel = channel
    stop_event = asyncio.Event()

    def readiness() -> ExternalReadiness:
        stop_event.set()
        return _readiness()

    asyncio.run(
        run_live_operations_runtime(
            controller=controller,
            readiness_provider=readiness,
            strategy_provider=_cycle,
            dashboard_store=DashboardSnapshotStore(tmp_path),
            status_store=RuntimeStatusStore(tmp_path),
            stop_event=stop_event,
            now=lambda: NOW,
            clock_ns=lambda: NOW_NS + 1,
        )
    )

    status = RuntimeStatusStore(tmp_path).read("live_operations")
    assert status is not None
    assert status.state == "stopped"
    assert DashboardSnapshotStore(tmp_path).read() is not None
    assert service.halted


def test_async_runtime_propagates_user_channel_shutdown_failure(tmp_path: Path) -> None:
    controller, service, _ = _controller(tmp_path)

    class Channel(_Channel):
        async def collect_forever(self, *, stop_event, on_event, on_health):  # type: ignore[no-untyped-def]
            del on_event
            await on_health()
            await stop_event.wait()
            raise RuntimeError("channel shutdown failed")

    controller.user_channel = Channel()
    stop_event = asyncio.Event()

    def readiness() -> ExternalReadiness:
        stop_event.set()
        return _readiness()

    with pytest.raises(RuntimeError, match="channel shutdown failed"):
        asyncio.run(
            run_live_operations_runtime(
                controller=controller,
                readiness_provider=readiness,
                strategy_provider=_cycle,
                dashboard_store=DashboardSnapshotStore(tmp_path),
                status_store=RuntimeStatusStore(tmp_path),
                stop_event=stop_event,
                now=lambda: NOW,
                clock_ns=lambda: NOW_NS + 1,
            )
        )

    status = RuntimeStatusStore(tmp_path).read("live_operations")
    assert status is not None
    assert status.state == "failed"
    assert status.details["error_type"] == "RuntimeError"
    assert service.halted
