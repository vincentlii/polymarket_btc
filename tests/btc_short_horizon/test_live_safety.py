from __future__ import annotations

from datetime import date
import json
from math import nan
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from btc_short_horizon.live import (
    AccountSnapshot,
    JsonlWriteAheadLog,
    LiveOrder,
    LiveOrderFillStatus,
    LiveOrderStatus,
    LiveTrade,
    LiveTradeStatus,
    RiskReservations,
    TradingSafetyConfig,
    evaluate_order_risk,
)


def _account(**overrides: object) -> AccountSnapshot:
    values: dict[str, object] = {
        "observed_at_ns": 1_000_000_000,
        "collateral_balance": 100.0,
        "collateral_allowance": 100.0,
        "account_equity": 100.0,
        "unresolved_position_cost": 0.0,
        "open_order_notional": 0.0,
        "daily_realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "daily_pnl_day": date(1970, 1, 1),
        "working_market_ids": frozenset(),
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


def test_risk_guard_defaults_to_disabled() -> None:
    decision = evaluate_order_risk(
        config=TradingSafetyConfig(),
        account=_account(),
        order_notional=2.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )
    assert decision.allowed is False
    assert decision.reason == "trading_disabled"


def test_risk_guard_enforces_balance_fraction() -> None:
    config = TradingSafetyConfig(trading_enabled=True)
    decision = evaluate_order_risk(
        config=config,
        account=_account(),
        order_notional=6.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )
    assert decision.allowed is False
    assert decision.reason == "unresolved_notional_limit"


def test_risk_guard_projects_local_reservations_before_network_submission() -> None:
    config = TradingSafetyConfig(
        trading_enabled=True,
        max_balance_fraction=1.0,
        max_unresolved_notional=10.0,
    )
    reservations = RiskReservations(
        notional=4.0,
        order_count=1,
        market_ids=frozenset({"market"}),
    )

    decision = evaluate_order_risk(
        config=config,
        account=_account(unresolved_position_cost=4.0),
        reservations=reservations,
        order_notional=3.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )

    assert not decision.allowed
    assert decision.reason == "unresolved_notional_limit"


def test_risk_guard_counts_unique_projected_markets_and_all_projected_orders() -> None:
    config = TradingSafetyConfig(
        trading_enabled=True,
        max_working_markets=1,
        max_open_orders=2,
        max_balance_fraction=1.0,
        max_unresolved_notional=100.0,
    )
    account = _account(working_market_ids=frozenset({"market"}), open_orders=1)

    same_market = evaluate_order_risk(
        config=config,
        account=account,
        order_notional=1.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )
    new_market = evaluate_order_risk(
        config=config,
        account=account,
        order_notional=1.0,
        market_id="other",
        now_ts_ns=1_000_000_001,
    )
    pending_order = evaluate_order_risk(
        config=config,
        account=account,
        reservations=RiskReservations(1.0, 1, frozenset({"market"})),
        order_notional=1.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )

    assert same_market.allowed
    assert new_market.reason == "working_market_limit"
    assert pending_order.reason == "open_order_limit"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"feeds_healthy": False}, "feeds_unhealthy"),
        ({"market_channel_healthy": False}, "market_channel_unhealthy"),
        ({"user_channel_healthy": False}, "user_channel_unhealthy"),
        ({"heartbeat_healthy": False}, "heartbeat_unhealthy"),
        ({"clock_healthy": False}, "clock_unhealthy"),
        ({"geo_eligible": False}, "geo_ineligible"),
        ({"account_reconciled": False}, "account_not_reconciled"),
    ],
)
def test_risk_guard_requires_every_live_safety_dependency(
    changes: dict[str, object], reason: str
) -> None:
    decision = evaluate_order_risk(
        config=TradingSafetyConfig(
            trading_enabled=True,
            max_balance_fraction=1.0,
            max_unresolved_notional=100.0,
        ),
        account=_account(**changes),
        order_notional=1.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )

    assert decision.reason == reason


def test_risk_guard_rejects_stale_future_and_wrong_daily_epoch_snapshots() -> None:
    config = TradingSafetyConfig(
        trading_enabled=True,
        max_account_snapshot_age_seconds=1.0,
        max_balance_fraction=1.0,
        max_unresolved_notional=100.0,
    )

    stale = evaluate_order_risk(
        config=config,
        account=_account(observed_at_ns=1),
        order_notional=1.0,
        market_id="market",
        now_ts_ns=2_000_000_002,
    )
    future = evaluate_order_risk(
        config=config,
        account=_account(observed_at_ns=2_000_000_000),
        order_notional=1.0,
        market_id="market",
        now_ts_ns=1_000_000_000,
    )
    wrong_day = evaluate_order_risk(
        config=config,
        account=_account(daily_pnl_day=date(2026, 7, 20)),
        order_notional=1.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )

    assert stale.reason == "account_snapshot_stale"
    assert future.reason == "account_snapshot_in_future"
    assert wrong_day.reason == "daily_pnl_epoch_mismatch"


def test_risk_guard_enforces_collateral_and_allowance_after_open_orders() -> None:
    config = TradingSafetyConfig(
        trading_enabled=True,
        max_balance_fraction=1.0,
        max_unresolved_notional=100.0,
    )
    decision = evaluate_order_risk(
        config=config,
        account=_account(
            collateral_balance=10.0,
            collateral_allowance=5.0,
            open_order_notional=3.0,
        ),
        reservations=RiskReservations(1.0, 1, frozenset({"market"})),
        order_notional=2.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )

    assert not decision.allowed
    assert decision.reason == "insufficient_collateral_or_allowance"


def test_risk_guard_does_not_ignore_open_position_losses() -> None:
    decision = evaluate_order_risk(
        config=TradingSafetyConfig(
            trading_enabled=True,
            daily_loss_limit=10.0,
            max_balance_fraction=1.0,
            max_unresolved_notional=100.0,
        ),
        account=_account(daily_realized_pnl=-3.0, unrealized_pnl=-7.0),
        order_notional=1.0,
        market_id="market",
        now_ts_ns=1_000_000_001,
    )

    assert not decision.allowed
    assert decision.reason == "daily_loss_limit"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TradingSafetyConfig(trading_enabled=1),
        lambda: TradingSafetyConfig(max_open_orders=True),
        lambda: _account(feeds_healthy=1),
    ],
)
def test_live_risk_types_reject_boolean_numeric_or_health_values(factory) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        factory()


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

    confirmed_directly = LiveTrade("trade-2", "client-1", 0.45, 1.0).transition(
        LiveTradeStatus.CONFIRMED
    )
    assert confirmed_directly.status is LiveTradeStatus.CONFIRMED


def test_order_reconciles_order_and_trade_channels_without_double_counting() -> None:
    order = LiveOrder("client-1", "market", "token", 0.45, 1.0)
    order = order.transition(LiveOrderStatus.SIGNED)
    order = order.transition(LiveOrderStatus.SUBMITTED)
    order = order.transition(LiveOrderStatus.LIVE, venue_order_id="venue-1")

    order = order.record_order_cumulative_match(0.4)
    order = order.record_trade_cumulative_match(0.4)

    assert order.matched_size == pytest.approx(0.4)
    assert order.fill_status is LiveOrderFillStatus.PARTIALLY_FILLED
    assert order.status is LiveOrderStatus.LIVE

    filled = order.record_trade_cumulative_match(1.0)
    assert filled.matched_size == pytest.approx(1.0)
    assert filled.fill_status is LiveOrderFillStatus.FILLED
    assert filled.status is LiveOrderStatus.FILLED


def test_order_cumulative_match_sources_are_individually_monotonic() -> None:
    order = LiveOrder("client-1", "market", "token", 0.45, 1.0)
    order = order.record_order_cumulative_match(0.5)

    with pytest.raises(ValueError, match="cannot decrease"):
        order.record_order_cumulative_match(0.4)


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


def test_wal_records_a_verified_monotonic_hash_chain(tmp_path: Path) -> None:
    wal = JsonlWriteAheadLog(tmp_path / "verified.jsonl")

    wal.append(event_type="first", ts_ns=1, payload={"value": 1})
    wal.append(event_type="second", ts_ns=2, payload={"value": 2})

    records = wal.read()
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["prev_hash"] == "0" * 64
    assert records[1]["prev_hash"] == records[0]["record_hash"]
    assert all(len(record["record_hash"]) == 64 for record in records)


def test_wal_appends_a_batch_with_one_contiguous_hash_chain(tmp_path: Path) -> None:
    wal = JsonlWriteAheadLog(tmp_path / "batch.jsonl")

    wal.append_many(
        (
            ("first", 1, {"value": 1}),
            ("second", 2, {"value": 2}),
            ("third", 3, {"value": 3}),
        )
    )

    records = wal.read()
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert records[1]["prev_hash"] == records[0]["record_hash"]
    assert records[2]["prev_hash"] == records[1]["record_hash"]


def test_wal_fails_closed_for_tampering_and_truncated_tail(tmp_path: Path) -> None:
    path = tmp_path / "tampered.jsonl"
    wal = JsonlWriteAheadLog(path)
    wal.append(event_type="first", ts_ns=1, payload={"value": 1})
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["value"] = 2
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hash"):
        wal.read()

    path.write_text('{"schema_version":1', encoding="utf-8")
    with pytest.raises(ValueError, match="truncated"):
        wal.read()


def test_wal_rejects_nonfinite_and_credential_shaped_payloads(tmp_path: Path) -> None:
    wal = JsonlWriteAheadLog(tmp_path / "safe.jsonl")

    with pytest.raises(ValueError, match="finite JSON"):
        wal.append(event_type="bad", ts_ns=1, payload={"value": nan})
    with pytest.raises(ValueError, match="credential"):
        wal.append(event_type="bad", ts_ns=1, payload={"apiKey": "do-not-write"})
    with pytest.raises(ValueError, match="ts_ns"):
        wal.append(event_type="bad", ts_ns=True, payload={})


def test_wal_serializes_two_writer_instances_without_duplicate_sequences(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.jsonl"
    first = JsonlWriteAheadLog(path)
    second = JsonlWriteAheadLog(path)

    def append(index: int) -> None:
        writer = first if index % 2 else second
        writer.append(event_type="event", ts_ns=index, payload={"index": index})

    with ThreadPoolExecutor(max_workers=4) as executor:
        tuple(executor.map(append, range(1, 41)))

    records = first.read()
    assert [record["sequence"] for record in records] == list(range(1, 41))
