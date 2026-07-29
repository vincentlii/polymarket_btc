from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from btc_short_horizon.live.dashboard_state import (
    BotDashboardSnapshot,
    DashboardAlert,
    DashboardSnapshotStore,
    EquityPoint,
    ExecutionVariantPerformance,
    GateState,
    HealthIndicator,
    HealthState,
    OrderPerformance,
    PerformanceSnapshot,
    StrategyCycle,
    StrategyStage,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def dashboard_snapshot() -> BotDashboardSnapshot:
    return BotDashboardSnapshot(
        generated_at=NOW,
        run_mode="paper",
        strategy=StrategyCycle(
            stage=StrategyStage.CHALLENGE,
            gate_state=GateState.RUNNING,
            next_action="完成 28 日挑战窗口后进行人工 Promotion Review。",
            model_id="btc-opening-champion-v1",
            challenger_model_id="btc-opening-challenger-v2",
            stage_started_at=NOW - timedelta(days=12),
            data_cutoff_at=NOW - timedelta(days=1),
            next_challenge_at=NOW + timedelta(days=2),
            next_review_at=NOW + timedelta(days=16),
            challenger_interval_days=14,
            review_interval_days=28,
            progress_label="已结算市场",
            progress_current=1_152,
            progress_target=2_500,
        ),
        performance=PerformanceSnapshot(
            starting_balance=1_000.0,
            equity=1_024.5,
            available_balance=930.0,
            open_exposure=94.5,
            realized_pnl=20.0,
            unrealized_pnl=4.5,
            today_pnl=3.2,
            max_drawdown=12.4,
            win_rate=0.57,
            order_count=24,
            fill_count=11,
            equity_curve=(
                EquityPoint(NOW - timedelta(hours=2), 1_000.0),
                EquityPoint(NOW - timedelta(hours=1), 1_010.0),
                EquityPoint(NOW, 1_024.5),
            ),
            primary_variant_id="maker_15s",
            variant_summaries=(
                ExecutionVariantPerformance(
                    variant_id="maker_15s",
                    label="Maker 15s",
                    policy="maker",
                    primary=True,
                    starting_balance=1_000.0,
                    equity=1_024.5,
                    realized_pnl=20.0,
                    order_count=24,
                    fill_count=11,
                    taker_fees=0.0,
                ),
            ),
            recent_orders=(
                OrderPerformance(
                    variant_id="maker_15s",
                    order_id="paper-24",
                    market_slug="btc-updown-15m-1784203200",
                    side="up",
                    execution_status="filled",
                    settlement_status="resolved",
                    placed_at=NOW - timedelta(minutes=20),
                    shares=10.0,
                    filled_shares=10.0,
                    entry_price=0.54,
                    p_fair=0.62,
                    market_price=0.55,
                    realized_pnl=4.6,
                    order_latency_ms=84.0,
                ),
            ),
        ),
        health=(
            HealthIndicator(
                key="clob-market-ws",
                label="Polymarket Market WS",
                state=HealthState.OK,
                detail="订单簿更新正常",
                updated_at=NOW,
                latency_ms=41.0,
            ),
            HealthIndicator(
                key="order-api",
                label="Order API P95",
                state=HealthState.WARNING,
                detail="延迟高于近期基线",
                updated_at=NOW,
                latency_ms=182.0,
            ),
        ),
        alerts=(
            DashboardAlert(
                state=HealthState.WARNING,
                message="Order API P95 延迟高于近期基线。",
                created_at=NOW,
            ),
        ),
    )


def test_dashboard_snapshot_round_trips_through_atomic_store(tmp_path) -> None:
    store = DashboardSnapshotStore(tmp_path)

    path = store.write(dashboard_snapshot())

    assert path == tmp_path / "dashboard" / "snapshot.json"
    assert store.read() == dashboard_snapshot()


def test_dashboard_snapshot_rejects_impossible_trade_fill() -> None:
    with pytest.raises(ValueError, match="filled_shares must not exceed shares"):
        OrderPerformance(
            variant_id="maker_15s",
            order_id="paper-1",
            market_slug="btc-updown-15m-1784203200",
            side="up",
            execution_status="filled",
            settlement_status="pending",
            placed_at=NOW,
            shares=1.0,
            filled_shares=2.0,
        )


def test_dashboard_snapshot_rejects_incomplete_progress_pair() -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        StrategyCycle(
            stage=StrategyStage.RESEARCH,
            gate_state=GateState.RUNNING,
            next_action="继续研究。",
            progress_current=1.0,
        )
