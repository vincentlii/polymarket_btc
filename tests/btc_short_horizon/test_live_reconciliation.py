from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from btc_short_horizon.live.reconciliation import (
    ClobStartupReconciler,
    DailyLedgerSnapshot,
    LocalOrderExpectation,
    StartupReadiness,
    account_snapshot_sha256,
)


BASE_TS_NS = 1_782_864_000_000_000_000  # 2026-07-01T00:00:00Z
FUNDER = "0x" + ("1" * 40)
CONDITION_A = "0x" + ("a" * 64)
CONDITION_B = "0x" + ("b" * 64)


class _ClobClient:
    def __init__(
        self,
        *,
        balance: object | None = None,
        orders: object | None = None,
        order_details: dict[str, object] | None = None,
        trades: object | None = None,
    ) -> None:
        self.balance = balance or {"balance": "100000000", "allowance": "50000000"}
        self.orders = orders if orders is not None else [_open_order()]
        self.order_details = order_details or {}
        self.trades = trades if trades is not None else []
        self.calls: list[tuple[str, object]] = []

    def get_balance_allowance(self, params: object) -> object:
        self.calls.append(("balance", params))
        return self.balance

    def get_open_orders(self) -> object:
        self.calls.append(("orders", None))
        return self.orders

    def get_order(self, order_id: str) -> object:
        self.calls.append(("order", order_id))
        return self.order_details[order_id]

    def get_trades(self, params: object) -> object:
        self.calls.append(("trades", params))
        return self.trades


class _Response:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        return self.payload


class _HttpClient:
    def __init__(
        self,
        *,
        positions: list[object] | None = None,
        blocked: bool = False,
    ) -> None:
        self.positions = positions if positions is not None else [_position()]
        self.blocked = blocked
        self.calls: list[tuple[str, dict[str, object] | None, float]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        timeout: float,
    ) -> _Response:
        self.calls.append((url, params, timeout))
        if url.endswith("/api/geoblock"):
            return _Response(
                {
                    "blocked": self.blocked,
                    "ip": "203.0.113.1",
                    "country": "KR",
                    "region": "11",
                }
            )
        if url.endswith("/positions"):
            offset = int((params or {}).get("offset", 0))
            limit = int((params or {}).get("limit", 500))
            return _Response(self.positions[offset : offset + limit])
        raise AssertionError(f"unexpected URL: {url}")


def _open_order(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "venue-order-1",
        "status": "ORDER_STATUS_LIVE",
        "market": CONDITION_A,
        "asset_id": "123456789",
        "side": "BUY",
        "original_size": "10000000",
        "size_matched": "2000000",
        "price": "0.45",
    }
    values.update(overrides)
    return values


def _position(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "proxyWallet": FUNDER,
        "asset": "987654321",
        "conditionId": CONDITION_B,
        "size": 10.0,
        "initialValue": 5.0,
        "currentValue": 6.5,
        "cashPnl": 1.5,
    }
    values.update(overrides)
    return values


def _ledger(**overrides: object) -> DailyLedgerSnapshot:
    values: dict[str, object] = {
        "day": date(2026, 7, 1),
        "realized_pnl": -1.25,
        "observed_at_ns": BASE_TS_NS,
        "covered_through_ns": BASE_TS_NS - 1_000_000_000,
        "ledger_sha256": "a" * 64,
    }
    values.update(overrides)
    return DailyLedgerSnapshot(**values)  # type: ignore[arg-type]


def _readiness(**overrides: object) -> StartupReadiness:
    values: dict[str, object] = {
        "observed_at_ns": BASE_TS_NS,
        "feeds_healthy": True,
        "market_channel_healthy": True,
        "user_channel_healthy": True,
        "heartbeat_healthy": True,
        "clock_healthy": True,
    }
    values.update(overrides)
    return StartupReadiness(**values)  # type: ignore[arg-type]


def _clock() -> Any:
    values = iter((BASE_TS_NS, BASE_TS_NS + 20_000_000))
    return lambda: next(values)


def _expectation(**overrides: object) -> LocalOrderExpectation:
    values: dict[str, object] = {
        "client_order_id": "client-order-1",
        "venue_order_id": "venue-order-1",
        "market_id": CONDITION_A,
        "token_id": "123456789",
        "price": 0.45,
        "original_size": 10.0,
    }
    values.update(overrides)
    return LocalOrderExpectation(**values)  # type: ignore[arg-type]


def test_reconciler_builds_projected_account_from_all_authoritative_sources() -> None:
    clob = _ClobClient()
    http = _HttpClient()
    reconciler = ClobStartupReconciler(
        clob,
        http_client=http,
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    )

    result = reconciler.reconcile(
        expected_orders=(_expectation(),),
        ledger=_ledger(),
        readiness=_readiness(),
    )

    assert result.account.collateral_balance == pytest.approx(100.0)
    assert result.account.collateral_allowance == pytest.approx(50.0)
    assert result.account.open_order_notional == pytest.approx(3.6)
    assert result.account.unresolved_position_cost == pytest.approx(5.0)
    assert result.account.account_equity == pytest.approx(106.5)
    assert result.account.daily_realized_pnl == pytest.approx(-1.25)
    assert result.account.unrealized_pnl == pytest.approx(1.5)
    assert result.account.working_market_ids == {CONDITION_A, CONDITION_B}
    assert result.account.open_orders == 1
    assert result.account.geo_eligible
    assert result.account.account_reconciled
    assert result.evidence.reconciled
    assert result.evidence.account_snapshot_sha256 == account_snapshot_sha256(result.account)
    assert result.evidence.country == "KR"
    assert "203.0.113.1" not in repr(result.evidence)
    assert [call[0] for call in clob.calls] == ["balance", "orders", "trades"]
    positions_call = next(call for call in http.calls if call[0].endswith("/positions"))
    assert positions_call[1] == {
        "user": FUNDER,
        "sizeThreshold": 0,
        "limit": 500,
        "offset": 0,
    }


def test_response_lost_order_is_uniquely_recovered_by_full_identity() -> None:
    result = ClobStartupReconciler(
        _ClobClient(),
        http_client=_HttpClient(),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    ).reconcile(
        expected_orders=(_expectation(venue_order_id=None),),
        ledger=_ledger(),
        readiness=_readiness(),
    )

    assert result.evidence.reconciled
    assert result.evidence.expected_open_order_ids == {"venue-order-1"}
    assert result.evidence.unmatched_client_order_ids == frozenset()
    assert len(result.evidence.recovered_orders) == 1
    recovered = result.evidence.recovered_orders[0]
    assert recovered.client_order_id == "client-order-1"
    assert recovered.venue_order_id == "venue-order-1"


def test_precomputed_order_id_recovers_an_order_that_finished_during_the_crash() -> None:
    terminal = _open_order(
        status="ORDER_STATUS_MATCHED",
        size_matched="10000000",
    )
    result = ClobStartupReconciler(
        _ClobClient(
            orders=[],
            order_details={"venue-order-1": terminal},
        ),
        http_client=_HttpClient(),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    ).reconcile(
        expected_orders=(_expectation(),),
        ledger=_ledger(),
        readiness=_readiness(),
    )

    assert result.evidence.reconciled
    assert not result.evidence.expected_open_order_ids
    assert not result.evidence.missing_open_order_ids
    assert len(result.evidence.terminal_orders) == 1
    assert result.evidence.terminal_orders[0].status == "ORDER_STATUS_MATCHED"
    assert result.evidence.terminal_orders[0].matched_size == pytest.approx(10.0)


def test_response_lost_order_recovery_rejects_ambiguous_venue_matches() -> None:
    duplicate_identity = _open_order(id="venue-order-2")

    with pytest.raises(ValueError, match="matches multiple"):
        ClobStartupReconciler(
            _ClobClient(orders=[_open_order(), duplicate_identity]),
            http_client=_HttpClient(),
            funder=FUNDER,
            signature_type=2,
            clock_ns=_clock(),
        ).reconcile(
            expected_orders=(_expectation(venue_order_id=None),),
            ledger=_ledger(),
            readiness=_readiness(),
        )


def test_known_venue_order_must_match_the_full_durable_identity() -> None:
    with pytest.raises(ValueError, match="identity"):
        ClobStartupReconciler(
            _ClobClient(orders=[_open_order(asset_id="999")]),
            http_client=_HttpClient(),
            funder=FUNDER,
            signature_type=2,
            clock_ns=_clock(),
        ).reconcile(
            expected_orders=(_expectation(),),
            ledger=_ledger(),
            readiness=_readiness(),
        )


def test_unmatched_response_lost_order_keeps_the_account_unreconciled() -> None:
    result = ClobStartupReconciler(
        _ClobClient(orders=[]),
        http_client=_HttpClient(positions=[]),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    ).reconcile(
        expected_orders=(_expectation(venue_order_id=None),),
        ledger=_ledger(),
        readiness=_readiness(),
    )

    assert not result.account.account_reconciled
    assert result.evidence.unmatched_client_order_ids == {"client-order-1"}


def test_pending_trade_or_unexpected_open_order_prevents_reconciliation() -> None:
    trade = {
        "id": "trade-new",
        "status": "TRADE_STATUS_MATCHED",
        "match_time_nano": str(BASE_TS_NS + 1),
        "match_time": str(BASE_TS_NS // 1_000_000_000),
        "last_update": str(BASE_TS_NS // 1_000_000_000),
    }
    result = ClobStartupReconciler(
        _ClobClient(trades=[trade]),
        http_client=_HttpClient(),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    ).reconcile(
        expected_orders=(),
        ledger=_ledger(),
        readiness=_readiness(),
    )

    assert not result.account.account_reconciled
    assert result.evidence.pending_trade_ids == {"trade-new"}
    assert result.evidence.unexpected_open_order_ids == {"venue-order-1"}


def test_trade_already_covered_by_the_ledger_is_not_reported_pending() -> None:
    covered_through_ns = BASE_TS_NS - 1_000_000_000
    trade = {
        "id": "trade-covered",
        "status": "TRADE_STATUS_CONFIRMED",
        "match_time_nano": str(covered_through_ns),
        "match_time": str(covered_through_ns // 1_000_000_000),
        "last_update": str(covered_through_ns // 1_000_000_000),
    }
    result = ClobStartupReconciler(
        _ClobClient(orders=[], trades=[trade]),
        http_client=_HttpClient(positions=[]),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    ).reconcile(
        expected_orders=(),
        ledger=_ledger(covered_through_ns=covered_through_ns),
        readiness=_readiness(),
    )

    assert result.account.account_reconciled
    assert not result.evidence.pending_trade_ids


def test_geo_block_is_reported_without_making_partial_sources_look_missing() -> None:
    result = ClobStartupReconciler(
        _ClobClient(orders=[]),
        http_client=_HttpClient(positions=[], blocked=True),
        funder=FUNDER,
        signature_type=3,
        clock_ns=_clock(),
    ).reconcile(
        expected_orders=(),
        ledger=_ledger(),
        readiness=_readiness(),
    )

    assert result.account.account_reconciled
    assert not result.account.geo_eligible
    assert result.evidence.reconciled


@pytest.mark.parametrize(
    ("balance", "orders", "positions", "message"),
    [
        ({"balance": "NaN", "allowance": "1"}, [], [], "balance"),
        ({"balance": "1", "allowance": "1"}, [_open_order(side="SELL")], [], "BUY"),
        (
            {"balance": "1", "allowance": "1"},
            [],
            [_position(proxyWallet="0x" + "9" * 40)],
            "funder",
        ),
        ({"balance": "1", "allowance": "1"}, [_open_order(size_matched="11000000")], [], "matched"),
    ],
)
def test_reconciler_rejects_malformed_or_wrong_identity_data(
    balance: object,
    orders: object,
    positions: list[object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ClobStartupReconciler(
            _ClobClient(balance=balance, orders=orders),
            http_client=_HttpClient(positions=positions),
            funder=FUNDER,
            signature_type=2,
            clock_ns=_clock(),
        ).reconcile(
            expected_orders=(),
            ledger=_ledger(),
            readiness=_readiness(),
        )


def test_reconciler_rejects_duplicate_positions_across_pages() -> None:
    duplicate = _position()

    with pytest.raises(ValueError, match="duplicate position"):
        ClobStartupReconciler(
            _ClobClient(orders=[]),
            http_client=_HttpClient(positions=[duplicate, duplicate]),
            funder=FUNDER,
            signature_type=2,
            clock_ns=_clock(),
        ).reconcile(
            expected_orders=(),
            ledger=_ledger(),
            readiness=_readiness(),
        )


def test_reconciler_rejects_stale_local_ledger_and_runtime_health() -> None:
    reconciler = ClobStartupReconciler(
        _ClobClient(orders=[]),
        http_client=_HttpClient(positions=[]),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    )

    with pytest.raises(ValueError, match="ledger snapshot is stale"):
        reconciler.reconcile(
            expected_orders=(),
            ledger=_ledger(
                observed_at_ns=BASE_TS_NS - 20_000_000_000,
                covered_through_ns=BASE_TS_NS - 21_000_000_000,
            ),
            readiness=_readiness(),
        )

    reconciler = ClobStartupReconciler(
        _ClobClient(orders=[]),
        http_client=_HttpClient(positions=[]),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    )
    with pytest.raises(ValueError, match="readiness snapshot is stale"):
        reconciler.reconcile(
            expected_orders=(),
            ledger=_ledger(),
            readiness=_readiness(observed_at_ns=BASE_TS_NS - 20_000_000_000),
        )


def test_daily_ledger_day_must_match_reconciliation_utc_day() -> None:
    reconciler = ClobStartupReconciler(
        _ClobClient(orders=[]),
        http_client=_HttpClient(positions=[]),
        funder=FUNDER,
        signature_type=2,
        clock_ns=_clock(),
    )

    with pytest.raises(ValueError, match="UTC day"):
        reconciler.reconcile(
            expected_orders=(),
            ledger=_ledger(day=date(2026, 6, 30)),
            readiness=_readiness(),
        )


def test_fixture_timestamp_is_the_expected_utc_day() -> None:
    assert datetime.fromtimestamp(BASE_TS_NS / 1_000_000_000, tz=UTC).date() == date(2026, 7, 1)
