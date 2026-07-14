from __future__ import annotations

from pathlib import Path

import pytest

from btc_short_horizon.live import (
    AccountSnapshot,
    JsonlWriteAheadLog,
    LiveOrder,
    LiveOrderStatus,
    LiveTrade,
    LiveTradeStatus,
    TradingSafetyConfig,
    evaluate_order_risk,
)


def _account(**overrides: object) -> AccountSnapshot:
    values: dict[str, object] = {
        "available_balance": 100.0,
        "unresolved_notional": 0.0,
        "daily_realized_pnl": 0.0,
        "working_markets": 0,
        "open_orders": 0,
        "feeds_healthy": True,
        "geo_eligible": True,
    }
    values.update(overrides)
    return AccountSnapshot(**values)  # type: ignore[arg-type]


def test_risk_guard_defaults_to_disabled() -> None:
    decision = evaluate_order_risk(
        config=TradingSafetyConfig(), account=_account(), order_notional=2.0
    )
    assert decision.allowed is False
    assert decision.reason == "trading_disabled"


def test_risk_guard_enforces_balance_fraction() -> None:
    config = TradingSafetyConfig(trading_enabled=True)
    decision = evaluate_order_risk(config=config, account=_account(), order_notional=6.0)
    assert decision.allowed is False
    assert decision.reason == "unresolved_notional_limit"


def test_order_and_trade_transitions_are_strict() -> None:
    order = LiveOrder("client-1", "market", "token", 0.45, 5.0)
    assert order.transition(LiveOrderStatus.SIGNED).status is LiveOrderStatus.SIGNED
    with pytest.raises(ValueError, match="invalid order transition"):
        order.transition(LiveOrderStatus.LIVE)

    trade = LiveTrade("trade-1", "client-1", 0.45, 2.0)
    trade = trade.transition(LiveTradeStatus.MINED)
    assert trade.status is LiveTradeStatus.MINED
    with pytest.raises(ValueError, match="invalid trade transition"):
        trade.transition(LiveTradeStatus.MATCHED)


def test_wal_round_trips_dataclass_payload(tmp_path: Path) -> None:
    wal = JsonlWriteAheadLog(tmp_path / "events.jsonl")
    wal.append(
        event_type="order_created",
        ts_ns=1,
        payload=LiveOrder("client-1", "market", "token", 0.45, 5.0),
    )
    assert wal.read()[0]["payload"]["client_order_id"] == "client-1"


def test_wal_recursively_serializes_nested_live_state(tmp_path: Path) -> None:
    wal = JsonlWriteAheadLog(tmp_path / "nested.jsonl")
    order = LiveOrder("client-1", "market", "token", 0.45, 5.0)

    wal.append(event_type="nested", ts_ns=1, payload={"order": order})

    assert wal.read()[0]["payload"]["order"]["status"] == "created"
