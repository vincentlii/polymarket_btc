from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from btc_short_horizon.live.ledger_sources import (
    ClobAccountLedgerRefresher,
    LedgerRefreshConfig,
)


BASE_NS = int(datetime(2026, 7, 21, 12, tzinfo=UTC).timestamp() * 1_000_000_000)
DAY_START_SECONDS = int(datetime(2026, 7, 21, tzinfo=UTC).timestamp())
FUNDER = "0x" + ("1" * 40)
CONDITION = "0x" + ("a" * 64)


def _open_order(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "order-1",
        "status": "ORDER_STATUS_LIVE",
        "market": CONDITION,
        "asset_id": "123",
        "side": "BUY",
        "original_size": "10000000",
        "size_matched": "2000000",
        "price": "0.45",
    }
    value.update(overrides)
    return value


def _trade(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "trade-1",
        "status": "TRADE_STATUS_CONFIRMED",
        "match_time": str(BASE_NS // 1_000_000_000 - 2),
        "match_time_nano": str(BASE_NS - 2_000_000_000),
        "last_update": str(BASE_NS // 1_000_000_000 - 1),
        "maker_address": FUNDER,
        "transaction_hash": "0x" + ("b" * 64),
    }
    value.update(overrides)
    return value


def _position(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "proxyWallet": FUNDER,
        "asset": "123",
        "conditionId": CONDITION,
        "size": 10.0,
        "initialValue": 4.5,
        "currentValue": 5.0,
        "cashPnl": 0.5,
        "realizedPnl": 0.0,
        "slug": "btc-updown-15m-1784634300",
        "outcome": "Up",
    }
    value.update(overrides)
    return value


def _closed(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "proxyWallet": FUNDER,
        "asset": "456",
        "conditionId": "0x" + ("c" * 64),
        "avgPrice": 0.45,
        "totalBought": 10.0,
        "realizedPnl": 5.5,
        "timestamp": BASE_NS // 1_000_000_000 - 1,
        "slug": "btc-updown-15m-1784633400",
        "outcome": "Up",
    }
    value.update(overrides)
    return value


class _Clob:
    def __init__(
        self,
        *,
        balance: object | None = None,
        orders: object | None = None,
        trades: object | None = None,
    ) -> None:
        self.balance = balance or {"balance": "100000000", "allowance": "90000000"}
        self.orders = [_open_order()] if orders is None else orders
        self.trades = [_trade()] if trades is None else trades
        self.trade_params: object | None = None

    def get_balance_allowance(self, _params: object) -> object:
        return self.balance

    def get_open_orders(self) -> object:
        return self.orders

    def get_trades(self, params: object) -> object:
        self.trade_params = params
        return self.trades


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class _Http:
    def __init__(
        self,
        *,
        positions: list[object] | None = None,
        closed: list[object] | None = None,
    ) -> None:
        self.positions = [_position()] if positions is None else positions
        self.closed = [_closed()] if closed is None else closed
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        timeout: float,
    ) -> _Response:
        assert timeout == 10.0
        self.calls.append((url, params))
        records = self.positions if url.endswith("/positions") else self.closed
        offset = int(params["offset"])
        limit = int(params["limit"])
        return _Response(records[offset : offset + limit])


def _clock(*values: int) -> Any:
    iterator = iter(values or (BASE_NS, BASE_NS + 10, BASE_NS + 20))
    return lambda: next(iterator)


def test_refresher_builds_complete_exact_daily_ledger() -> None:
    clob = _Clob()
    http = _Http()
    refresher = ClobAccountLedgerRefresher(
        clob,
        http_client=http,
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    )

    ledger = refresher.refresh(
        capture_started_at_ns=BASE_NS - 100,
        submitted_order_count=7,
        confirmed_fill_count=3,
    )

    assert ledger.source_started_at_ns == BASE_NS
    assert ledger.covered_through_ns == BASE_NS + 10
    assert ledger.observed_at_ns == BASE_NS + 20
    assert ledger.collateral_balance == pytest.approx(100.0)
    assert ledger.collateral_allowance == pytest.approx(90.0)
    assert ledger.open_order_notional == pytest.approx(3.6)
    assert ledger.position_cost == pytest.approx(4.5)
    assert ledger.position_value == pytest.approx(5.0)
    assert ledger.daily_realized_pnl == pytest.approx(5.5)
    assert ledger.cumulative_realized_pnl == pytest.approx(5.5)
    assert ledger.submitted_order_count == 7
    assert ledger.confirmed_fill_count == 3
    assert ledger.trade_coverage[0].trade_id == "trade-1"
    assert ledger.closed_positions[0].side == "up"
    assert getattr(clob.trade_params, "after") == DAY_START_SECONDS - 1
    assert [params["limit"] for _, params in http.calls] == [500, 50]


@pytest.mark.parametrize(
    ("positions", "message"),
    [
        ([_position(slug="eth-updown-15m-1")], "BTC 15m"),
        ([_position(realizedPnl=1.0)], "open-position realizedPnl"),
        ([_position(cashPnl=0.4)], "cashPnl"),
        ([_position(), _position()], "duplicate current position"),
    ],
)
def test_refresher_rejects_non_dedicated_or_inconsistent_positions(
    positions: list[object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ClobAccountLedgerRefresher(
            _Clob(),
            http_client=_Http(positions=positions),
            funder=FUNDER,
            signature_type=2,
            clock_ns=_clock(),
        ).refresh(
            capture_started_at_ns=BASE_NS - 100,
            submitted_order_count=0,
            confirmed_fill_count=0,
        )


def test_refresher_rejects_trade_identity_and_source_window_failures() -> None:
    with pytest.raises(ValueError, match="maker address"):
        ClobAccountLedgerRefresher(
            _Clob(trades=[_trade(maker_address="0x" + ("9" * 40))]),
            http_client=_Http(),
            funder=FUNDER,
            signature_type=2,
            clock_ns=_clock(),
        ).refresh(
            capture_started_at_ns=BASE_NS - 100,
            submitted_order_count=0,
            confirmed_fill_count=0,
        )

    with pytest.raises(ValueError, match="source window"):
        ClobAccountLedgerRefresher(
            _Clob(),
            http_client=_Http(),
            funder=FUNDER,
            signature_type=2,
            config=LedgerRefreshConfig(max_source_window_seconds=1.0),
            clock_ns=_clock(BASE_NS, BASE_NS + 10, BASE_NS + 2_000_000_000),
        ).refresh(
            capture_started_at_ns=BASE_NS - 100,
            submitted_order_count=0,
            confirmed_fill_count=0,
        )


def test_refresher_keeps_prior_days_in_cumulative_but_not_daily_pnl() -> None:
    ledger = ClobAccountLedgerRefresher(
        _Clob(),
        http_client=_Http(
            closed=[
                _closed(),
                _closed(asset="789", timestamp=DAY_START_SECONDS - 1, realizedPnl=-1),
            ]
        ),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    ).refresh(
        capture_started_at_ns=BASE_NS - 100,
        submitted_order_count=0,
        confirmed_fill_count=0,
    )

    assert ledger.daily_realized_pnl == pytest.approx(5.5)
    assert ledger.cumulative_realized_pnl == pytest.approx(4.5)
    assert len(ledger.closed_positions) == 1
