"""Strict venue-source refresh for the durable BTC account ledger."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from math import isclose, isfinite
import re
from time import time_ns
from typing import Any

from btc_short_horizon.live.ledger import DailyAccountLedger, LedgerClosedPosition
from btc_short_horizon.live.reconciliation import LedgerTradeCoverage, VenueTradeStatus


_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_TRANSACTION_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_FIXED_SCALE = Decimal(1_000_000)
_DATA_API = "https://data-api.polymarket.com"


@dataclass(frozen=True, slots=True)
class LedgerRefreshConfig:
    request_timeout_seconds: float = 10.0
    max_source_window_seconds: float = 5.0
    positions_page_size: int = 500
    closed_positions_page_size: int = 50
    max_position_records: int = 10_000
    max_closed_position_records: int = 100_000

    def __post_init__(self) -> None:
        for name in ("request_timeout_seconds", "max_source_window_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not isfinite(value)
                or value <= 0.0
            ):
                raise ValueError(f"{name} must be finite and > 0")
        if not 1 <= self.positions_page_size <= 500:
            raise ValueError("positions_page_size must be in [1, 500]")
        if not 1 <= self.closed_positions_page_size <= 50:
            raise ValueError("closed_positions_page_size must be in [1, 50]")
        if not 1 <= self.max_position_records <= 10_000:
            raise ValueError("max_position_records must be in [1, 10000]")
        if not 1 <= self.max_closed_position_records <= 100_000:
            raise ValueError("max_closed_position_records must be in [1, 100000]")


class ClobAccountLedgerRefresher:
    """Read complete authoritative account sources into one immutable observation."""

    def __init__(
        self,
        clob_client: Any,
        *,
        http_client: Any,
        funder: str,
        signature_type: int,
        config: LedgerRefreshConfig = LedgerRefreshConfig(),
        clock_ns: Callable[[], int] = time_ns,
    ) -> None:
        if not isinstance(funder, str) or not _ADDRESS.fullmatch(funder):
            raise ValueError("funder must be a 0x-prefixed 20-byte address")
        if isinstance(signature_type, bool) or signature_type not in {0, 1, 2, 3}:
            raise ValueError("signature_type must be one of 0, 1, 2, or 3")
        self.clob_client = clob_client
        self.http_client = http_client
        self.funder = funder.casefold()
        self.signature_type = signature_type
        self.config = config
        self.clock_ns = clock_ns

    def refresh(
        self,
        *,
        capture_started_at_ns: int,
        submitted_order_count: int,
        confirmed_fill_count: int,
        previous: DailyAccountLedger | None = None,
    ) -> DailyAccountLedger:
        _nonnegative_int(capture_started_at_ns, "capture_started_at_ns")
        _nonnegative_int(submitted_order_count, "submitted_order_count")
        _nonnegative_int(confirmed_fill_count, "confirmed_fill_count")
        source_started_at_ns = self.clock_ns()
        _nonnegative_int(source_started_at_ns, "source_started_at_ns")
        current_day = datetime.fromtimestamp(source_started_at_ns / 1_000_000_000, tz=UTC).date()
        if previous is not None:
            if previous.account_address != self.funder:
                raise ValueError("previous ledger belongs to a different account")
            if previous.observed_at_ns > source_started_at_ns:
                raise ValueError("previous ledger is future-dated")
            capture_started_at_ns = previous.capture_started_at_ns

        collateral_balance, collateral_allowance = self._fetch_collateral()
        open_order_notional, open_order_count = self._fetch_open_orders()
        trade_coverage = self._fetch_trades(current_day=current_day, previous=previous)
        covered_through_ns = self.clock_ns()
        _nonnegative_int(covered_through_ns, "covered_through_ns")
        if covered_through_ns < source_started_at_ns:
            raise ValueError("ledger refresh clock moved backwards")
        latest_trade_ns = max(
            (max(item.match_time_ns, item.last_update_ns) for item in trade_coverage),
            default=0,
        )
        if latest_trade_ns > covered_through_ns:
            raise ValueError("authenticated trade timestamps are future-dated")

        position_cost, position_value, unrealized_pnl = self._fetch_current_positions()
        closed_all = self._fetch_closed_positions()
        observed_at_ns = self.clock_ns()
        _nonnegative_int(observed_at_ns, "observed_at_ns")
        if observed_at_ns < covered_through_ns:
            raise ValueError("ledger refresh clock moved backwards")
        if observed_at_ns - source_started_at_ns > int(
            self.config.max_source_window_seconds * 1_000_000_000
        ):
            raise ValueError("ledger source window exceeded its limit")
        if any(item.closed_at_ns > observed_at_ns for item in closed_all):
            raise ValueError("closed position timestamp is future-dated")

        closed_today = tuple(
            item
            for item in closed_all
            if datetime.fromtimestamp(item.closed_at_ns / 1_000_000_000, tz=UTC).date()
            == current_day
        )
        return DailyAccountLedger(
            day=current_day,
            capture_started_at_ns=capture_started_at_ns,
            source_started_at_ns=source_started_at_ns,
            observed_at_ns=observed_at_ns,
            covered_through_ns=covered_through_ns,
            account_address=self.funder,
            collateral_balance=collateral_balance,
            collateral_allowance=collateral_allowance,
            position_cost=position_cost,
            position_value=position_value,
            unrealized_pnl=unrealized_pnl,
            daily_realized_pnl=sum(item.realized_pnl for item in closed_today),
            cumulative_realized_pnl=sum(item.realized_pnl for item in closed_all),
            open_order_notional=open_order_notional,
            open_order_count=open_order_count,
            submitted_order_count=submitted_order_count,
            confirmed_fill_count=confirmed_fill_count,
            trade_coverage=trade_coverage,
            closed_positions=closed_today,
        )

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

    def _fetch_open_orders(self) -> tuple[float, int]:
        raw = self.clob_client.get_open_orders()
        records = _records(raw, "open orders response")
        total = Decimal(0)
        seen: set[str] = set()
        for item in records:
            order_id = _text(item.get("id"), "open order ID")
            if order_id in seen:
                raise ValueError("open orders response contains a duplicate ID")
            seen.add(order_id)
            if item.get("status") != "ORDER_STATUS_LIVE":
                raise ValueError("open orders endpoint returned a non-live order")
            if item.get("side") != "BUY":
                raise ValueError("the dedicated bot account contains a non-BUY order")
            _condition_id(item.get("market"), "open order market")
            _token_id(item.get("asset_id"), "open order asset")
            original = _fixed_decimal(item.get("original_size"), "original_size")
            matched = _fixed_decimal(item.get("size_matched"), "size_matched")
            price = _decimal(item.get("price"), "open order price")
            if original <= 0 or matched < 0 or matched > original:
                raise ValueError("open order sizes are inconsistent")
            if not Decimal(0) < price < Decimal(1):
                raise ValueError("open order price must be in (0, 1)")
            total += (original - matched) * price
        return float(total), len(records)

    def _fetch_trades(
        self,
        *,
        current_day: object,
        previous: DailyAccountLedger | None,
    ) -> tuple[LedgerTradeCoverage, ...]:
        try:
            from py_clob_client_v2 import TradeParams
        except ImportError as exc:  # pragma: no cover - optional live dependency
            raise RuntimeError("Install the project's live dependency group.") from exc
        if not hasattr(current_day, "year"):
            raise ValueError("current_day must be a date")
        day_start_seconds = int(
            datetime(current_day.year, current_day.month, current_day.day, tzinfo=UTC).timestamp()
        )
        carry = ()
        if previous is not None:
            carry = tuple(
                item
                for item in previous.trade_coverage
                if item.status not in {VenueTradeStatus.CONFIRMED, VenueTradeStatus.FAILED}
            )
        earliest_seconds = min(
            (item.match_time_ns // 1_000_000_000 for item in carry),
            default=day_start_seconds,
        )
        raw = self.clob_client.get_trades(
            TradeParams(
                maker_address=self.funder,
                after=max(0, min(day_start_seconds, earliest_seconds) - 1),
            )
        )
        records = _records(raw, "trades response")
        result: list[LedgerTradeCoverage] = []
        seen: set[str] = set()
        for item in records:
            trade_id = _text(item.get("id"), "trade ID")
            if trade_id in seen:
                raise ValueError("trades response contains a duplicate trade ID")
            seen.add(trade_id)
            maker_address = _text(item.get("maker_address"), "trade maker address")
            if maker_address.casefold() != self.funder:
                raise ValueError("trade maker address does not match the dedicated bot account")
            try:
                status = VenueTradeStatus(item.get("status"))
            except (TypeError, ValueError) as exc:
                raise ValueError("trade has an unsupported status") from exc
            match_seconds = _unix(item.get("match_time"), "trade match_time")
            match_nano = item.get("match_time_nano")
            match_ns = (
                match_seconds * 1_000_000_000
                if match_nano is None
                else _unix(match_nano, "trade match_time_nano")
            )
            if match_ns // 1_000_000_000 != match_seconds:
                raise ValueError("trade match timestamps are inconsistent")
            last_update_ns = _unix(item.get("last_update"), "trade last_update") * 1_000_000_000
            transaction_hash = item.get("transaction_hash")
            if transaction_hash is not None and (
                not isinstance(transaction_hash, str)
                or not _TRANSACTION_HASH.fullmatch(transaction_hash)
            ):
                raise ValueError("trade transaction_hash is invalid")
            result.append(
                LedgerTradeCoverage(
                    trade_id=trade_id,
                    status=status,
                    match_time_ns=match_ns,
                    last_update_ns=last_update_ns,
                    transaction_hash=transaction_hash,
                )
            )
        return tuple(sorted(result, key=lambda item: (item.match_time_ns, item.trade_id)))

    def _fetch_current_positions(self) -> tuple[float, float, float]:
        records = self._data_api_records(
            path="positions",
            page_size=self.config.positions_page_size,
            maximum=self.config.max_position_records,
            extra={"sizeThreshold": 0},
        )
        cost = Decimal(0)
        value = Decimal(0)
        cash_pnl = Decimal(0)
        seen: set[str] = set()
        for item in records:
            self._validate_position_identity(item)
            asset = _token_id(item.get("asset"), "position asset")
            if asset in seen:
                raise ValueError("positions response contains a duplicate current position")
            seen.add(asset)
            initial = _decimal(item.get("initialValue"), "position initialValue")
            current = _decimal(item.get("currentValue"), "position currentValue")
            cash = _decimal(item.get("cashPnl"), "position cashPnl")
            realized = _decimal(item.get("realizedPnl"), "open-position realizedPnl")
            size = _decimal(item.get("size"), "position size")
            if min(initial, current, size) < 0:
                raise ValueError("current position values must be non-negative")
            if realized != 0:
                raise ValueError("open-position realizedPnl prevents exact daily attribution")
            if not isclose(float(current - initial), float(cash), abs_tol=1e-6):
                raise ValueError("position cashPnl does not reconcile value and cost")
            cost += initial
            value += current
            cash_pnl += cash
        return float(cost), float(value), float(cash_pnl)

    def _fetch_closed_positions(self) -> tuple[LedgerClosedPosition, ...]:
        records = self._data_api_records(
            path="closed-positions",
            page_size=self.config.closed_positions_page_size,
            maximum=self.config.max_closed_position_records,
            extra={"sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        )
        result: list[LedgerClosedPosition] = []
        seen: set[tuple[str, int]] = set()
        for item in records:
            self._validate_position_identity(item)
            asset = _token_id(item.get("asset"), "closed position asset")
            closed_seconds = _unix(item.get("timestamp"), "closed position timestamp")
            key = (asset, closed_seconds)
            if key in seen:
                raise ValueError("closed positions response contains a duplicate position")
            seen.add(key)
            outcome = _text(item.get("outcome"), "closed position outcome").casefold()
            if outcome not in {"up", "down"}:
                raise ValueError("closed BTC position outcome must be Up or Down")
            result.append(
                LedgerClosedPosition(
                    market_id=_condition_id(item.get("conditionId"), "closed position conditionId"),
                    token_id=asset,
                    market_slug=_btc_slug(item.get("slug")),
                    side=outcome,
                    closed_at_ns=closed_seconds * 1_000_000_000,
                    shares=float(_positive_decimal(item.get("totalBought"), "totalBought")),
                    entry_price=float(_probability(item.get("avgPrice"), "avgPrice")),
                    realized_pnl=float(_decimal(item.get("realizedPnl"), "realizedPnl")),
                )
            )
        return tuple(sorted(result, key=lambda item: (item.closed_at_ns, item.token_id)))

    def _validate_position_identity(self, item: Mapping[str, object]) -> None:
        wallet = _text(item.get("proxyWallet"), "position proxyWallet")
        if wallet.casefold() != self.funder:
            raise ValueError("position funder does not match the dedicated bot account")
        _condition_id(item.get("conditionId"), "position conditionId")
        _btc_slug(item.get("slug"))

    def _data_api_records(
        self,
        *,
        path: str,
        page_size: int,
        maximum: int,
        extra: Mapping[str, object],
    ) -> tuple[Mapping[str, object], ...]:
        result: list[Mapping[str, object]] = []
        offset = 0
        while True:
            params = {
                "user": self.funder,
                **dict(extra),
                "limit": page_size,
                "offset": offset,
            }
            response = self.http_client.get(
                f"{_DATA_API}/{path}",
                params=params,
                timeout=self.config.request_timeout_seconds,
            )
            response.raise_for_status()
            page = _records(response.json(), f"{path} response")
            result.extend(page)
            if len(result) > maximum:
                raise ValueError(f"{path} response exceeded its completeness bound")
            if len(page) < page_size:
                return tuple(result)
            offset += page_size
            if offset >= maximum:
                raise ValueError(f"{path} pagination reached its completeness bound")


def _records(value: object, name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(f"{name} must be an array")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"{name} contains a non-object record")
    return tuple(value)  # type: ignore[return-value]


def _fixed_decimal(value: object, name: str) -> Decimal:
    return _decimal(value, name) / _FIXED_SCALE


def _fixed_amount(value: object, name: str) -> float:
    amount = _fixed_decimal(value, name)
    if amount < 0:
        raise ValueError(f"{name} must be non-negative")
    return float(amount)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _positive_decimal(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be > 0")
    return result


def _probability(value: object, name: str) -> Decimal:
    result = _positive_decimal(value, name)
    if result >= 1:
        raise ValueError(f"{name} must be in (0, 1)")
    return result


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _condition_id(value: object, name: str) -> str:
    text = _text(value, name)
    if not _CONDITION_ID.fullmatch(text):
        raise ValueError(f"{name} must be a condition ID")
    return text.casefold()


def _token_id(value: object, name: str) -> str:
    text = _text(value, name)
    if not text.isdigit() or not 0 < int(text) < 2**256:
        raise ValueError(f"{name} must be a positive unsigned 256-bit integer")
    return str(int(text))


def _btc_slug(value: object) -> str:
    text = _text(value, "position slug")
    if not text.startswith("btc-updown-15m-"):
        raise ValueError("the dedicated bot account contains a non-BTC 15m position")
    return text


def _unix(value: object, name: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError(f"{name} must be a non-negative Unix timestamp")
    return int(str(value))


def _nonnegative_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


__all__ = ["ClobAccountLedgerRefresher", "LedgerRefreshConfig"]
