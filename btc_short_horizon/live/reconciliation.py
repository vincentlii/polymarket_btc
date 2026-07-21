"""Fail-closed multi-source startup reconciliation for live execution."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
import re
from time import time_ns
from typing import Any

from btc_short_horizon.live.risk import AccountSnapshot


_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_FIXED_SCALE = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class StartupReconciliationConfig:
    request_timeout_seconds: float = 10.0
    max_source_window_seconds: float = 5.0
    max_local_snapshot_age_seconds: float = 5.0
    max_future_clock_skew_seconds: float = 0.25
    positions_page_size: int = 500
    max_position_records: int = 10_000

    def __post_init__(self) -> None:
        for name in (
            "request_timeout_seconds",
            "max_source_window_seconds",
            "max_local_snapshot_age_seconds",
            "max_future_clock_skew_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"{name} must be a finite number > 0")
        if isinstance(self.positions_page_size, bool) or not 1 <= self.positions_page_size <= 500:
            raise ValueError("positions_page_size must be in [1, 500]")
        if (
            isinstance(self.max_position_records, bool)
            or not 1 <= self.max_position_records <= 10_000
        ):
            raise ValueError("max_position_records must be in [1, 10000]")


class VenueTradeStatus(StrEnum):
    MATCHED = "TRADE_STATUS_MATCHED"
    MINED = "TRADE_STATUS_MINED"
    CONFIRMED = "TRADE_STATUS_CONFIRMED"
    RETRYING = "TRADE_STATUS_RETRYING"
    FAILED = "TRADE_STATUS_FAILED"


@dataclass(frozen=True, slots=True)
class LedgerTradeCoverage:
    """Exact authenticated trade revision included in one durable ledger snapshot."""

    trade_id: str
    status: VenueTradeStatus
    match_time_ns: int
    last_update_ns: int
    transaction_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trade_id", _required_text(self.trade_id, "trade ID"))
        if not isinstance(self.status, VenueTradeStatus):
            object.__setattr__(self, "status", VenueTradeStatus(self.status))
        _nonnegative_int(self.match_time_ns, "trade match_time_ns")
        _nonnegative_int(self.last_update_ns, "trade last_update_ns")
        if self.last_update_ns // 1_000_000_000 < self.match_time_ns // 1_000_000_000:
            raise ValueError("trade last_update_ns cannot precede the match second")
        if self.transaction_hash is not None:
            if not isinstance(self.transaction_hash, str) or not _TRANSACTION_HASH.fullmatch(
                self.transaction_hash
            ):
                raise ValueError("trade transaction_hash must be a 0x-prefixed 32-byte hash")
            object.__setattr__(self, "transaction_hash", self.transaction_hash.casefold())


@dataclass(frozen=True, slots=True)
class DailyLedgerSnapshot:
    day: date
    realized_pnl: float
    observed_at_ns: int
    covered_through_ns: int
    trade_coverage: tuple[LedgerTradeCoverage, ...]
    ledger_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.day, date) or isinstance(self.day, datetime):
            raise ValueError("ledger day must be a date")
        _finite_number(self.realized_pnl, "ledger realized_pnl")
        _nonnegative_int(self.observed_at_ns, "ledger observed_at_ns")
        _nonnegative_int(self.covered_through_ns, "ledger covered_through_ns")
        if self.covered_through_ns > self.observed_at_ns:
            raise ValueError("ledger coverage cannot exceed its observation time")
        if not isinstance(self.trade_coverage, tuple) or not all(
            isinstance(item, LedgerTradeCoverage) for item in self.trade_coverage
        ):
            raise ValueError("trade_coverage must be a tuple of LedgerTradeCoverage records")
        trade_ids = tuple(item.trade_id for item in self.trade_coverage)
        if len(set(trade_ids)) != len(trade_ids):
            raise ValueError("trade_coverage contains a duplicate trade ID")
        if any(
            max(item.match_time_ns, item.last_update_ns) > self.covered_through_ns
            for item in self.trade_coverage
        ):
            raise ValueError("trade coverage extends beyond covered_through_ns")
        if not isinstance(self.ledger_sha256, str) or not _SHA256.fullmatch(self.ledger_sha256):
            raise ValueError("ledger_sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class StartupReadiness:
    observed_at_ns: int
    feeds_healthy: bool
    market_channel_healthy: bool
    user_channel_healthy: bool
    heartbeat_healthy: bool
    clock_healthy: bool

    def __post_init__(self) -> None:
        _nonnegative_int(self.observed_at_ns, "readiness observed_at_ns")
        for name in (
            "feeds_healthy",
            "market_channel_healthy",
            "user_channel_healthy",
            "heartbeat_healthy",
            "clock_healthy",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class LocalOrderExpectation:
    """Durable local identity expected to be open at the venue."""

    client_order_id: str
    venue_order_id: str | None
    market_id: str
    token_id: str
    price: float
    original_size: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "client_order_id",
            _required_text(self.client_order_id, "local client order ID"),
        )
        if self.venue_order_id is not None:
            object.__setattr__(
                self,
                "venue_order_id",
                _required_text(self.venue_order_id, "local venue order ID"),
            )
        object.__setattr__(self, "market_id", _condition_id(self.market_id, "local market"))
        object.__setattr__(self, "token_id", _token_id(self.token_id, "local token"))
        _probability(self.price, "local price")
        _positive_number(self.original_size, "local original_size")


@dataclass(frozen=True, slots=True)
class VenueOpenOrder:
    """Identity and remaining exposure returned by the authenticated CLOB API."""

    order_id: str
    market_id: str
    token_id: str
    price: float
    original_size: float
    matched_size: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", _required_text(self.order_id, "venue order ID"))
        object.__setattr__(self, "market_id", _condition_id(self.market_id, "venue market"))
        object.__setattr__(self, "token_id", _token_id(self.token_id, "venue token"))
        _probability(self.price, "venue price")
        _positive_number(self.original_size, "venue original_size")
        _nonnegative_number(self.matched_size, "venue matched_size")
        if self.matched_size > self.original_size:
            raise ValueError("venue matched_size exceeds original_size")

    @property
    def remaining_notional(self) -> float:
        return (self.original_size - self.matched_size) * self.price


@dataclass(frozen=True, slots=True)
class RecoveredOrderBinding:
    """Unique REST proof binding a response-lost local order to its venue ID."""

    client_order_id: str
    venue_order_id: str
    market_id: str
    token_id: str
    price: float
    original_size: float

    def __post_init__(self) -> None:
        LocalOrderExpectation(
            client_order_id=self.client_order_id,
            venue_order_id=self.venue_order_id,
            market_id=self.market_id,
            token_id=self.token_id,
            price=self.price,
            original_size=self.original_size,
        )


class TerminalVenueOrderStatus(StrEnum):
    INVALID = "ORDER_STATUS_INVALID"
    CANCELED_MARKET_RESOLVED = "ORDER_STATUS_CANCELED_MARKET_RESOLVED"
    CANCELED = "ORDER_STATUS_CANCELED"
    MATCHED = "ORDER_STATUS_MATCHED"


@dataclass(frozen=True, slots=True)
class RecoveredTerminalOrder:
    """Authenticated proof that a durable local order is no longer open."""

    client_order_id: str
    venue_order_id: str
    market_id: str
    token_id: str
    price: float
    original_size: float
    matched_size: float
    status: TerminalVenueOrderStatus

    def __post_init__(self) -> None:
        LocalOrderExpectation(
            client_order_id=self.client_order_id,
            venue_order_id=self.venue_order_id,
            market_id=self.market_id,
            token_id=self.token_id,
            price=self.price,
            original_size=self.original_size,
        )
        _nonnegative_number(self.matched_size, "terminal matched_size")
        if self.matched_size > self.original_size:
            raise ValueError("terminal matched_size exceeds original_size")
        if not isinstance(self.status, TerminalVenueOrderStatus):
            object.__setattr__(self, "status", TerminalVenueOrderStatus(self.status))
        if (
            self.status is TerminalVenueOrderStatus.MATCHED
            and abs(self.matched_size - self.original_size) > 1e-9
        ):
            raise ValueError("matched terminal order must be completely matched")
        if self.status is TerminalVenueOrderStatus.INVALID and self.matched_size > 1e-12:
            raise ValueError("invalid terminal order cannot have matched size")


@dataclass(frozen=True, slots=True)
class StartupReconciliationEvidence:
    source_started_at_ns: int
    observed_at_ns: int
    expected_open_order_ids: frozenset[str]
    venue_open_order_ids: frozenset[str]
    missing_open_order_ids: frozenset[str]
    unexpected_open_order_ids: frozenset[str]
    recovered_orders: tuple[RecoveredOrderBinding, ...]
    terminal_orders: tuple[RecoveredTerminalOrder, ...]
    unmatched_client_order_ids: frozenset[str]
    pending_trade_ids: frozenset[str]
    ledger_sha256: str
    account_snapshot_sha256: str
    country: str
    region: str

    def __post_init__(self) -> None:
        _nonnegative_int(self.source_started_at_ns, "evidence source_started_at_ns")
        _nonnegative_int(self.observed_at_ns, "evidence observed_at_ns")
        if self.observed_at_ns < self.source_started_at_ns:
            raise ValueError("evidence observation precedes its source window")
        for name in (
            "expected_open_order_ids",
            "venue_open_order_ids",
            "missing_open_order_ids",
            "unexpected_open_order_ids",
            "unmatched_client_order_ids",
            "pending_trade_ids",
        ):
            object.__setattr__(self, name, _text_set(getattr(self, name), name))
        if not isinstance(self.recovered_orders, tuple) or not all(
            isinstance(item, RecoveredOrderBinding) for item in self.recovered_orders
        ):
            raise ValueError("recovered_orders must be a tuple of recovered order bindings")
        if not isinstance(self.terminal_orders, tuple) or not all(
            isinstance(item, RecoveredTerminalOrder) for item in self.terminal_orders
        ):
            raise ValueError("terminal_orders must be a tuple of terminal order proofs")
        recovered_client_ids = tuple(item.client_order_id for item in self.recovered_orders)
        recovered_venue_ids = tuple(item.venue_order_id for item in self.recovered_orders)
        if len(set(recovered_client_ids)) != len(recovered_client_ids):
            raise ValueError("recovered_orders contains a duplicate client order ID")
        if len(set(recovered_venue_ids)) != len(recovered_venue_ids):
            raise ValueError("recovered_orders contains a duplicate venue order ID")
        if not set(recovered_venue_ids) <= self.expected_open_order_ids:
            raise ValueError("recovered venue orders must be expected")
        if not set(recovered_venue_ids) <= self.venue_open_order_ids:
            raise ValueError("recovered venue orders must exist at the venue")
        if set(recovered_client_ids) & self.unmatched_client_order_ids:
            raise ValueError("a local order cannot be both recovered and unmatched")
        terminal_client_ids = tuple(item.client_order_id for item in self.terminal_orders)
        terminal_venue_ids = tuple(item.venue_order_id for item in self.terminal_orders)
        if len(set(terminal_client_ids)) != len(terminal_client_ids):
            raise ValueError("terminal_orders contains a duplicate client order ID")
        if len(set(terminal_venue_ids)) != len(terminal_venue_ids):
            raise ValueError("terminal_orders contains a duplicate venue order ID")
        if set(terminal_client_ids) & (set(recovered_client_ids) | self.unmatched_client_order_ids):
            raise ValueError("a local order cannot have conflicting recovery outcomes")
        if set(terminal_venue_ids) & (
            set(recovered_venue_ids) | self.expected_open_order_ids | self.venue_open_order_ids
        ):
            raise ValueError("a terminal venue order cannot also be open")
        if self.missing_open_order_ids != (
            self.expected_open_order_ids - self.venue_open_order_ids
        ):
            raise ValueError("evidence missing order set is inconsistent")
        if self.unexpected_open_order_ids != (
            self.venue_open_order_ids - self.expected_open_order_ids
        ):
            raise ValueError("evidence unexpected order set is inconsistent")
        for name in ("ledger_sha256", "account_snapshot_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if not isinstance(self.country, str) or len(self.country) != 2:
            raise ValueError("evidence country must be an ISO alpha-2 code")
        if not isinstance(self.region, str) or len(self.region) > 16:
            raise ValueError("evidence region must be a short string")

    @property
    def reconciled(self) -> bool:
        return not (
            self.missing_open_order_ids
            or self.unexpected_open_order_ids
            or self.unmatched_client_order_ids
            or self.pending_trade_ids
        )


@dataclass(frozen=True, slots=True)
class StartupReconciliationResult:
    account: AccountSnapshot
    evidence: StartupReconciliationEvidence


@dataclass(frozen=True, slots=True)
class _PositionTotals:
    initial_value: float
    current_value: float
    unrealized_pnl: float
    market_ids: frozenset[str]


class ClobStartupReconciler:
    """Read CLOB, Data API, geoblock, runtime health, and the local ledger once."""

    def __init__(
        self,
        clob_client: Any,
        *,
        http_client: Any,
        funder: str,
        signature_type: int,
        config: StartupReconciliationConfig = StartupReconciliationConfig(),
        clock_ns: Callable[[], int] = time_ns,
    ) -> None:
        if not isinstance(funder, str) or not _ADDRESS.fullmatch(funder):
            raise ValueError("funder must be a 0x-prefixed 20-byte address")
        if isinstance(signature_type, bool) or signature_type not in {0, 1, 2, 3}:
            raise ValueError("signature_type must be one of 0, 1, 2, or 3")
        self.clob_client = clob_client
        self.http_client = http_client
        self.funder = funder
        self.signature_type = signature_type
        self.config = config
        self.clock_ns = clock_ns

    def reconcile(
        self,
        *,
        expected_orders: Sequence[LocalOrderExpectation],
        ledger: DailyLedgerSnapshot,
        readiness: StartupReadiness,
    ) -> StartupReconciliationResult:
        local_orders = _local_order_expectations(expected_orders)
        started_at_ns = self.clock_ns()
        _nonnegative_int(started_at_ns, "reconciliation start time")
        self._validate_local_snapshot_age(
            started_at_ns,
            ledger.observed_at_ns,
            name="ledger snapshot",
        )
        self._validate_local_snapshot_age(
            started_at_ns,
            readiness.observed_at_ns,
            name="readiness snapshot",
        )
        current_day = datetime.fromtimestamp(started_at_ns / 1_000_000_000, tz=UTC).date()
        if ledger.day != current_day:
            raise ValueError("daily ledger day does not match the reconciliation UTC day")

        balance, allowance = self._fetch_collateral()
        orders = self._fetch_open_orders()
        terminal_orders = self._fetch_terminal_orders(local_orders, orders)
        pending_trade_ids = self._fetch_pending_trade_ids(ledger)
        positions = self._fetch_positions()
        blocked, country, region = self._fetch_geo()
        observed_at_ns = self.clock_ns()
        _nonnegative_int(observed_at_ns, "reconciliation observation time")
        if observed_at_ns < started_at_ns:
            raise ValueError("reconciliation clock moved backwards")
        if observed_at_ns - started_at_ns > int(
            self.config.max_source_window_seconds * 1_000_000_000
        ):
            raise ValueError("startup reconciliation source window exceeded its limit")

        venue_ids = frozenset(order.order_id for order in orders)
        expected, missing, unexpected, recovered, unmatched = _match_open_orders(
            local_orders,
            orders,
            terminal_orders,
        )
        reconciled = not (missing or unexpected or unmatched or pending_trade_ids)
        working_market_ids = frozenset(order.market_id for order in orders) | positions.market_ids
        account = AccountSnapshot(
            observed_at_ns=observed_at_ns,
            collateral_balance=balance,
            collateral_allowance=allowance,
            account_equity=balance + positions.current_value,
            unresolved_position_cost=positions.initial_value,
            open_order_notional=sum(order.remaining_notional for order in orders),
            daily_realized_pnl=ledger.realized_pnl,
            unrealized_pnl=positions.unrealized_pnl,
            daily_pnl_day=ledger.day,
            working_market_ids=working_market_ids,
            open_orders=len(orders),
            feeds_healthy=readiness.feeds_healthy,
            market_channel_healthy=readiness.market_channel_healthy,
            user_channel_healthy=readiness.user_channel_healthy,
            heartbeat_healthy=readiness.heartbeat_healthy,
            clock_healthy=readiness.clock_healthy,
            geo_eligible=not blocked,
            account_reconciled=reconciled,
        )
        evidence = StartupReconciliationEvidence(
            source_started_at_ns=started_at_ns,
            observed_at_ns=observed_at_ns,
            expected_open_order_ids=expected,
            venue_open_order_ids=venue_ids,
            missing_open_order_ids=missing,
            unexpected_open_order_ids=unexpected,
            recovered_orders=recovered,
            terminal_orders=terminal_orders,
            unmatched_client_order_ids=unmatched,
            pending_trade_ids=pending_trade_ids,
            ledger_sha256=ledger.ledger_sha256,
            account_snapshot_sha256=account_snapshot_sha256(account),
            country=country,
            region=region,
        )
        return StartupReconciliationResult(account=account, evidence=evidence)

    def _validate_local_snapshot_age(self, now_ns: int, observed_ns: int, *, name: str) -> None:
        age_ns = now_ns - observed_ns
        if age_ns < -int(self.config.max_future_clock_skew_seconds * 1_000_000_000):
            raise ValueError(f"{name} is future-dated")
        if age_ns > int(self.config.max_local_snapshot_age_seconds * 1_000_000_000):
            raise ValueError(f"{name} is stale")

    def _fetch_collateral(self) -> tuple[float, float]:
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install the project's live dependency group.") from exc
        raw = self.clob_client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self.signature_type,
            )
        )
        if not isinstance(raw, Mapping):
            raise ValueError("balance response must be an object")
        return (
            _fixed_amount(raw.get("balance"), "balance"),
            _fixed_amount(raw.get("allowance"), "allowance"),
        )

    def _fetch_open_orders(self) -> tuple[VenueOpenOrder, ...]:
        raw = self.clob_client.get_open_orders()
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
            raise ValueError("open orders response must be an array")
        orders: list[VenueOpenOrder] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("open order must be an object")
            order_id, market_id, token_id, price, original_size, matched_size = (
                _venue_order_identity(item)
            )
            if order_id in seen:
                raise ValueError("open order response contains a duplicate ID")
            seen.add(order_id)
            if item.get("status") != "ORDER_STATUS_LIVE":
                raise ValueError("open orders endpoint returned a non-live status")
            orders.append(
                VenueOpenOrder(
                    order_id=order_id,
                    market_id=market_id,
                    token_id=token_id,
                    price=price,
                    original_size=original_size,
                    matched_size=matched_size,
                )
            )
        return tuple(orders)

    def _fetch_terminal_orders(
        self,
        local_orders: tuple[LocalOrderExpectation, ...],
        open_orders: tuple[VenueOpenOrder, ...],
    ) -> tuple[RecoveredTerminalOrder, ...]:
        open_ids = {order.order_id for order in open_orders}
        result: list[RecoveredTerminalOrder] = []
        for local in local_orders:
            venue_order_id = local.venue_order_id
            if venue_order_id is None or venue_order_id in open_ids:
                continue
            try:
                raw = self.clob_client.get_order(venue_order_id)
            except Exception as exc:
                if (
                    exc.__class__.__name__ == "PolyApiException"
                    and getattr(exc, "status_code", None) == 404
                ):
                    continue
                raise
            if not isinstance(raw, Mapping):
                raise ValueError("single order response must be an object")
            order_id, market_id, token_id, price, original_size, matched_size = (
                _venue_order_identity(raw)
            )
            if order_id != venue_order_id:
                raise ValueError("single order response returned a different order ID")
            status_value = raw.get("status")
            if status_value == "ORDER_STATUS_LIVE":
                raise ValueError("live order was omitted from the complete open-orders response")
            try:
                status = TerminalVenueOrderStatus(status_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("single order response has an unsupported status") from exc
            terminal = RecoveredTerminalOrder(
                client_order_id=local.client_order_id,
                venue_order_id=order_id,
                market_id=market_id,
                token_id=token_id,
                price=price,
                original_size=original_size,
                matched_size=matched_size,
                status=status,
            )
            if not _same_order_identity(local, terminal):
                raise ValueError("terminal venue order identity does not match durable local state")
            result.append(terminal)
        return tuple(result)

    def _fetch_pending_trade_ids(self, ledger: DailyLedgerSnapshot) -> frozenset[str]:
        try:
            from py_clob_client_v2 import TradeParams
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install the project's live dependency group.") from exc
        day_start_ns = int(
            datetime(
                ledger.day.year,
                ledger.day.month,
                ledger.day.day,
                tzinfo=UTC,
            ).timestamp()
            * 1_000_000_000
        )
        earliest_ns = min(
            (item.match_time_ns for item in ledger.trade_coverage),
            default=day_start_ns,
        )
        after_seconds = max(0, min(day_start_ns, earliest_ns) // 1_000_000_000 - 1)
        raw = self.clob_client.get_trades(
            TradeParams(maker_address=self.funder, after=after_seconds)
        )
        if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
            raise ValueError("trades response must be an array")
        covered = {item.trade_id: item for item in ledger.trade_coverage}
        observed: dict[str, LedgerTradeCoverage] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("trade must be an object")
            trade_id = _required_text(item.get("id"), "trade ID")
            if trade_id in observed:
                raise ValueError("trades response contains a duplicate trade ID")
            try:
                status = VenueTradeStatus(item.get("status"))
            except (TypeError, ValueError) as exc:
                raise ValueError("trade has an unsupported status") from exc
            match_seconds = _unix_seconds(item.get("match_time"), "trade match_time")
            raw_match_nano = item.get("match_time_nano")
            if raw_match_nano is None:
                match_ns = match_seconds * 1_000_000_000
            else:
                match_ns = _unix_nanoseconds(raw_match_nano, "trade match_time_nano")
                if match_ns // 1_000_000_000 != match_seconds:
                    raise ValueError("trade match timestamps are inconsistent")
            last_update_ns = (
                _unix_seconds(item.get("last_update"), "trade last_update") * 1_000_000_000
            )
            if last_update_ns < match_seconds * 1_000_000_000:
                raise ValueError("trade last_update precedes match_time")
            transaction_hash = item.get("transaction_hash")
            if transaction_hash is not None and not isinstance(transaction_hash, str):
                raise ValueError("trade transaction_hash must be a string or null")
            record = LedgerTradeCoverage(
                trade_id=trade_id,
                status=status,
                match_time_ns=match_ns,
                last_update_ns=last_update_ns,
                transaction_hash=transaction_hash,
            )
            observed[trade_id] = record

        pending = set(covered) ^ set(observed)
        for trade_id in set(covered) & set(observed):
            local = covered[trade_id]
            venue = observed[trade_id]
            if venue.match_time_ns != local.match_time_ns:
                raise ValueError("trade match_time changed after ledger persistence")
            if (
                venue.status is not local.status
                or venue.last_update_ns != local.last_update_ns
                or venue.transaction_hash != local.transaction_hash
            ):
                pending.add(trade_id)
        return frozenset(pending)

    def _fetch_positions(self) -> _PositionTotals:
        records: list[Mapping[str, object]] = []
        offset = 0
        while True:
            response = self.http_client.get(
                "https://data-api.polymarket.com/positions",
                params={
                    "user": self.funder,
                    "sizeThreshold": 0,
                    "limit": self.config.positions_page_size,
                    "offset": offset,
                },
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("positions response must be an array")
            if not all(isinstance(item, Mapping) for item in payload):
                raise ValueError("position must be an object")
            records.extend(payload)
            if len(records) > self.config.max_position_records:
                raise ValueError("positions response exceeded the configured completeness bound")
            if len(payload) < self.config.positions_page_size:
                break
            offset += self.config.positions_page_size
            if offset >= self.config.max_position_records:
                raise ValueError("positions pagination reached the completeness bound")

        initial_value = 0.0
        current_value = 0.0
        unrealized_pnl = 0.0
        market_ids: set[str] = set()
        seen_assets: set[str] = set()
        for item in records:
            proxy_wallet = _required_text(item.get("proxyWallet"), "position funder")
            if proxy_wallet.casefold() != self.funder.casefold():
                raise ValueError("position funder does not match configured funder")
            asset = _required_text(item.get("asset"), "position asset")
            if asset in seen_assets:
                raise ValueError("positions response contains a duplicate position asset")
            seen_assets.add(asset)
            market_id = _condition_id(item.get("conditionId"), "position conditionId")
            size = _decimal_amount(item.get("size"), "position size")
            position_initial = _decimal_amount(item.get("initialValue"), "position initialValue")
            position_current = _decimal_amount(item.get("currentValue"), "position currentValue")
            position_cash_pnl = _decimal_amount(item.get("cashPnl"), "position cashPnl")
            if min(size, position_initial, position_current) < 0.0:
                raise ValueError("position values must be non-negative")
            if size > 0.0:
                market_ids.add(market_id)
                initial_value += position_initial
                current_value += position_current
                unrealized_pnl += position_cash_pnl
        return _PositionTotals(
            initial_value,
            current_value,
            unrealized_pnl,
            frozenset(market_ids),
        )

    def _fetch_geo(self) -> tuple[bool, str, str]:
        response = self.http_client.get(
            "https://polymarket.com/api/geoblock",
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("geoblock response must be an object")
        blocked = payload.get("blocked")
        if not isinstance(blocked, bool):
            raise ValueError("geoblock response has no boolean blocked field")
        country = _required_text(payload.get("country"), "geoblock country")
        region_value = payload.get("region")
        if not isinstance(region_value, str):
            raise ValueError("geoblock region must be a string")
        region = region_value.strip()
        if len(country) != 2 or len(region) > 16:
            raise ValueError("geoblock location fields have an invalid format")
        return blocked, country.upper(), region


def _local_order_expectations(
    values: Sequence[LocalOrderExpectation],
) -> tuple[LocalOrderExpectation, ...]:
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        raise ValueError("expected_orders must be a sequence")
    result = tuple(values)
    if not all(isinstance(item, LocalOrderExpectation) for item in result):
        raise ValueError("expected_orders must contain only local order expectations")
    client_ids = tuple(item.client_order_id for item in result)
    if len(set(client_ids)) != len(client_ids):
        raise ValueError("expected_orders contains a duplicate client order ID")
    venue_ids = tuple(item.venue_order_id for item in result if item.venue_order_id is not None)
    if len(set(venue_ids)) != len(venue_ids):
        raise ValueError("expected_orders contains a duplicate venue order ID")
    unresolved_identities = tuple(
        (item.market_id, item.token_id, item.price, item.original_size)
        for item in result
        if item.venue_order_id is None
    )
    if len(set(unresolved_identities)) != len(unresolved_identities):
        raise ValueError("unidentified local orders must have unique order identities")
    return result


def _venue_order_identity(
    item: Mapping[str, object],
) -> tuple[str, str, str, float, float, float]:
    order_id = _required_text(item.get("id"), "venue order ID")
    if item.get("side") != "BUY":
        raise ValueError("the dedicated bot account contains a non-BUY order")
    market_id = _condition_id(item.get("market"), "venue order market")
    token_id = _token_id(item.get("asset_id"), "venue order asset_id")
    original_size = _fixed_amount(item.get("original_size"), "original_size")
    matched_size = _fixed_amount(item.get("size_matched"), "size_matched")
    if matched_size > original_size:
        raise ValueError("venue order matched size exceeds original size")
    price = _decimal_amount(item.get("price"), "venue order price")
    if not 0.0 < price < 1.0:
        raise ValueError("venue order price must be in (0, 1)")
    return order_id, market_id, token_id, price, original_size, matched_size


def _match_open_orders(
    local_orders: tuple[LocalOrderExpectation, ...],
    venue_orders: tuple[VenueOpenOrder, ...],
    terminal_orders: tuple[RecoveredTerminalOrder, ...],
) -> tuple[
    frozenset[str],
    frozenset[str],
    frozenset[str],
    tuple[RecoveredOrderBinding, ...],
    frozenset[str],
]:
    venue_by_id = {order.order_id: order for order in venue_orders}
    terminal_by_client = {order.client_order_id: order for order in terminal_orders}
    unmatched_venue = dict(venue_by_id)
    expected_ids: set[str] = set()
    missing_ids: set[str] = set()
    unidentified: list[LocalOrderExpectation] = []

    for local in local_orders:
        if local.client_order_id in terminal_by_client:
            continue
        if local.venue_order_id is None:
            unidentified.append(local)
            continue
        expected_ids.add(local.venue_order_id)
        venue = venue_by_id.get(local.venue_order_id)
        if venue is None:
            missing_ids.add(local.venue_order_id)
            continue
        if not _same_order_identity(local, venue):
            raise ValueError("venue order identity does not match durable local state")
        unmatched_venue.pop(venue.order_id)

    recovered: list[RecoveredOrderBinding] = []
    unmatched_clients: set[str] = set()
    for local in unidentified:
        candidates = tuple(
            order for order in unmatched_venue.values() if _same_order_identity(local, order)
        )
        if len(candidates) > 1:
            raise ValueError("response-lost local order matches multiple venue orders")
        if not candidates:
            unmatched_clients.add(local.client_order_id)
            continue
        venue = candidates[0]
        unmatched_venue.pop(venue.order_id)
        expected_ids.add(venue.order_id)
        recovered.append(
            RecoveredOrderBinding(
                client_order_id=local.client_order_id,
                venue_order_id=venue.order_id,
                market_id=local.market_id,
                token_id=local.token_id,
                price=local.price,
                original_size=local.original_size,
            )
        )

    return (
        frozenset(expected_ids),
        frozenset(missing_ids),
        frozenset(unmatched_venue),
        tuple(recovered),
        frozenset(unmatched_clients),
    )


def _same_order_identity(
    local: LocalOrderExpectation,
    venue: VenueOpenOrder | RecoveredTerminalOrder,
) -> bool:
    return (
        local.market_id == venue.market_id
        and local.token_id == venue.token_id
        and abs(local.price - venue.price) <= 1e-12
        and abs(local.original_size - venue.original_size) <= 1e-9
    )


def account_snapshot_sha256(account: AccountSnapshot) -> str:
    payload = asdict(account)
    payload["daily_pnl_day"] = account.daily_pnl_day.isoformat()
    payload["working_market_ids"] = sorted(account.working_market_ids)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _fixed_amount(value: object, name: str) -> float:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{name} must be a finite fixed-math integer")
    return float(Decimal(str(value)) / _FIXED_SCALE)


def _decimal_amount(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not amount.is_finite():
        raise ValueError(f"{name} must be a finite number")
    return float(amount)


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _nonnegative_number(value: object, name: str) -> float:
    result = _finite_number(value, name)
    if result < 0.0:
        raise ValueError(f"{name} must be >= 0")
    return result


def _positive_number(value: object, name: str) -> float:
    result = _nonnegative_number(value, name)
    if result <= 0.0:
        raise ValueError(f"{name} must be > 0")
    return result


def _probability(value: object, name: str) -> float:
    result = _positive_number(value, name)
    if result >= 1.0:
        raise ValueError(f"{name} must be in (0, 1)")
    return result


def _unix_seconds(value: object, name: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{name} must be non-negative Unix seconds")
    return int(str(value))


def _unix_nanoseconds(value: object, name: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{name} must be non-negative Unix nanoseconds")
    return int(str(value))


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _condition_id(value: object, name: str) -> str:
    text = _required_text(value, name)
    if not _CONDITION_ID.fullmatch(text):
        raise ValueError(f"{name} must be a condition ID")
    return text.casefold()


def _token_id(value: object, name: str) -> str:
    text = _required_text(value, name)
    if not text.isdigit() or not 0 < int(text) < 2**256:
        raise ValueError(f"{name} must be a positive unsigned 256-bit integer")
    return str(int(text))


def _text_set(values: Iterable[str], name: str) -> frozenset[str]:
    if isinstance(values, str | bytes):
        raise ValueError(f"{name}s must be a set")
    try:
        result = frozenset(values)
    except TypeError as exc:
        raise ValueError(f"{name}s must be iterable") from exc
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{name}s must be non-empty strings")
    return result


__all__ = [
    "ClobStartupReconciler",
    "DailyLedgerSnapshot",
    "LedgerTradeCoverage",
    "LocalOrderExpectation",
    "RecoveredOrderBinding",
    "RecoveredTerminalOrder",
    "StartupReadiness",
    "StartupReconciliationConfig",
    "StartupReconciliationEvidence",
    "StartupReconciliationResult",
    "TerminalVenueOrderStatus",
    "VenueOpenOrder",
    "VenueTradeStatus",
    "account_snapshot_sha256",
]
