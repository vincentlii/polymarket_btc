from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from btc_short_horizon.live.ledger import (
    DailyAccountLedger,
    DailyAccountLedgerStore,
    LedgerClosedPosition,
    ledger_performance_snapshot,
)
from btc_short_horizon.live.reconciliation import LedgerTradeCoverage


DAY = date(2026, 7, 21)
BASE_NS = int(datetime(2026, 7, 21, 12, tzinfo=UTC).timestamp() * 1_000_000_000)
FUNDER = "0x" + ("1" * 40)
CONDITION = "0x" + ("a" * 64)


def _trade(**overrides: object) -> LedgerTradeCoverage:
    values: dict[str, object] = {
        "trade_id": "trade-1",
        "status": "TRADE_STATUS_CONFIRMED",
        "match_time_ns": BASE_NS - 2_000_000_000,
        "last_update_ns": BASE_NS - 1_000_000_000,
        "transaction_hash": "0x" + ("b" * 64),
    }
    values.update(overrides)
    return LedgerTradeCoverage(**values)  # type: ignore[arg-type]


def _closed(**overrides: object) -> LedgerClosedPosition:
    values: dict[str, object] = {
        "market_id": CONDITION,
        "token_id": "123456789",
        "market_slug": "btc-updown-15m-1784634300",
        "side": "up",
        "closed_at_ns": BASE_NS - 500_000_000,
        "shares": 10.0,
        "entry_price": 0.45,
        "realized_pnl": 5.5,
    }
    values.update(overrides)
    return LedgerClosedPosition(**values)  # type: ignore[arg-type]


def _ledger(**overrides: object) -> DailyAccountLedger:
    values: dict[str, object] = {
        "day": DAY,
        "capture_started_at_ns": BASE_NS - 10_000_000_000,
        "source_started_at_ns": BASE_NS - 100_000_000,
        "observed_at_ns": BASE_NS,
        "covered_through_ns": BASE_NS,
        "account_address": FUNDER,
        "collateral_balance": 100.0,
        "collateral_allowance": 100.0,
        "position_cost": 4.5,
        "position_value": 5.0,
        "unrealized_pnl": 0.5,
        "daily_realized_pnl": 5.5,
        "cumulative_realized_pnl": 7.0,
        "open_order_notional": 3.0,
        "open_order_count": 1,
        "submitted_order_count": 1,
        "confirmed_fill_count": 1,
        "trade_coverage": (_trade(),),
        "closed_positions": (_closed(),),
    }
    values.update(overrides)
    return DailyAccountLedger(**values)  # type: ignore[arg-type]


def test_daily_ledger_store_is_content_addressed_atomic_and_round_trips(tmp_path) -> None:
    store = DailyAccountLedgerStore(tmp_path)

    receipt = store.write(_ledger())

    assert receipt.snapshot_path.name == f"{receipt.ledger_sha256}.json"
    assert receipt.pointer_path == tmp_path / "ledger" / "days" / DAY.isoformat() / "latest.json"
    loaded, digest = store.read(DAY)
    assert loaded == _ledger()
    assert digest == receipt.ledger_sha256
    assert loaded.to_reconciliation_snapshot(digest).trade_coverage == (_trade(),)


def test_daily_ledger_store_rejects_snapshot_tampering(tmp_path) -> None:
    store = DailyAccountLedgerStore(tmp_path)
    receipt = store.write(_ledger())
    receipt.snapshot_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="ledger snapshot"):
        store.read(DAY)


def test_daily_ledger_rejects_inconsistent_accounting_and_duplicate_trade_ids() -> None:
    with pytest.raises(ValueError, match="unrealized_pnl"):
        _ledger(unrealized_pnl=0.4)

    with pytest.raises(ValueError, match="duplicate trade ID"):
        _ledger(trade_coverage=(_trade(), _trade()))

    with pytest.raises(ValueError, match="daily_realized_pnl"):
        _ledger(daily_realized_pnl=5.4)


def test_ledger_performance_projection_uses_only_observed_account_values() -> None:
    first = _ledger(
        observed_at_ns=BASE_NS - 5_000_000_000,
        covered_through_ns=BASE_NS - 5_000_000_000,
        source_started_at_ns=BASE_NS - 5_100_000_000,
        trade_coverage=(),
        closed_positions=(),
        daily_realized_pnl=0.0,
        cumulative_realized_pnl=1.5,
        collateral_balance=95.0,
        position_cost=4.0,
        position_value=4.5,
        unrealized_pnl=0.5,
        open_order_notional=0.0,
        open_order_count=0,
        submitted_order_count=0,
        confirmed_fill_count=0,
    )
    current = _ledger()

    performance = ledger_performance_snapshot((first, current))

    assert performance.starting_balance == pytest.approx(99.5)
    assert performance.equity == pytest.approx(105.0)
    assert performance.available_balance == pytest.approx(97.0)
    assert performance.open_exposure == pytest.approx(7.5)
    assert performance.realized_pnl == pytest.approx(7.0)
    assert performance.unrealized_pnl == pytest.approx(0.5)
    assert performance.today_pnl == pytest.approx(6.0)
    assert performance.max_drawdown == pytest.approx(0.0)
    assert performance.order_count == 1
    assert performance.fill_count == 1
    assert len(performance.equity_curve) == 2
    assert performance.recent_orders[0].market_slug == "btc-updown-15m-1784634300"
    assert performance.recent_orders[0].order_latency_ms is None


def test_ledger_store_rejects_an_older_snapshot_replacing_latest(tmp_path) -> None:
    store = DailyAccountLedgerStore(tmp_path)
    store.write(_ledger())

    with pytest.raises(ValueError, match="older"):
        store.write(
            _ledger(
                observed_at_ns=BASE_NS - 1,
                covered_through_ns=BASE_NS - 1,
                source_started_at_ns=BASE_NS - 2,
            )
        )
