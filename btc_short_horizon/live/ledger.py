"""Content-addressed daily account ledger and honest dashboard projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
import json
from math import isclose, isfinite
import os
from pathlib import Path
import re
from uuid import uuid4

from btc_short_horizon.live.dashboard_state import (
    EquityPoint,
    OrderPerformance,
    PerformanceSnapshot,
)
from btc_short_horizon.live.reconciliation import DailyLedgerSnapshot, LedgerTradeCoverage
from btc_short_horizon.live.wal import exclusive_file_lock


_LEDGER_SCHEMA_VERSION = 1
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LedgerClosedPosition:
    """One resolved BTC market position reported by the Data API."""

    market_id: str
    token_id: str
    market_slug: str
    side: str
    closed_at_ns: int
    shares: float
    entry_price: float
    realized_pnl: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_id", _condition_id(self.market_id))
        object.__setattr__(self, "token_id", _token_id(self.token_id))
        if not isinstance(self.market_slug, str) or not self.market_slug.startswith(
            "btc-updown-15m-"
        ):
            raise ValueError("closed position must be a BTC 15m market slug")
        if self.side not in {"up", "down"}:
            raise ValueError("closed position side must be 'up' or 'down'")
        _nonnegative_int(self.closed_at_ns, "closed_at_ns")
        _positive(self.shares, "shares")
        _probability(self.entry_price, "entry_price")
        _finite(self.realized_pnl, "realized_pnl")

    def to_json(self) -> dict[str, object]:
        return {
            "market_id": self.market_id,
            "token_id": self.token_id,
            "market_slug": self.market_slug,
            "side": self.side,
            "closed_at_ns": self.closed_at_ns,
            "shares": self.shares,
            "entry_price": self.entry_price,
            "realized_pnl": self.realized_pnl,
        }

    @classmethod
    def from_json(cls, raw: object) -> LedgerClosedPosition:
        value = _mapping(raw, "closed position")
        return cls(
            market_id=_text(value.get("market_id"), "market_id"),
            token_id=_text(value.get("token_id"), "token_id"),
            market_slug=_text(value.get("market_slug"), "market_slug"),
            side=_text(value.get("side"), "side"),
            closed_at_ns=_integer(value.get("closed_at_ns"), "closed_at_ns"),
            shares=_number(value.get("shares"), "shares"),
            entry_price=_number(value.get("entry_price"), "entry_price"),
            realized_pnl=_number(value.get("realized_pnl"), "realized_pnl"),
        )


@dataclass(frozen=True, slots=True)
class DailyAccountLedger:
    """One complete UTC-day account observation from authenticated/public venue APIs."""

    day: date
    capture_started_at_ns: int
    source_started_at_ns: int
    observed_at_ns: int
    covered_through_ns: int
    account_address: str
    collateral_balance: float
    collateral_allowance: float
    position_cost: float
    position_value: float
    unrealized_pnl: float
    daily_realized_pnl: float
    cumulative_realized_pnl: float
    open_order_notional: float
    open_order_count: int
    submitted_order_count: int
    confirmed_fill_count: int
    trade_coverage: tuple[LedgerTradeCoverage, ...]
    closed_positions: tuple[LedgerClosedPosition, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.day, date) or isinstance(self.day, datetime):
            raise ValueError("ledger day must be a date")
        for name in (
            "capture_started_at_ns",
            "source_started_at_ns",
            "observed_at_ns",
            "covered_through_ns",
        ):
            _nonnegative_int(getattr(self, name), name)
        if not (
            self.capture_started_at_ns
            <= self.source_started_at_ns
            <= self.covered_through_ns
            <= self.observed_at_ns
        ):
            raise ValueError("ledger timestamps are not causally ordered")
        observed_day = datetime.fromtimestamp(self.observed_at_ns / 1_000_000_000, tz=UTC).date()
        if observed_day != self.day:
            raise ValueError("ledger day does not match observed_at_ns")
        if not isinstance(self.account_address, str) or not _ADDRESS.fullmatch(
            self.account_address
        ):
            raise ValueError("account_address must be a 0x-prefixed 20-byte address")
        object.__setattr__(self, "account_address", self.account_address.casefold())
        for name in (
            "collateral_balance",
            "collateral_allowance",
            "position_cost",
            "position_value",
            "open_order_notional",
        ):
            _nonnegative(getattr(self, name), name)
        for name in ("unrealized_pnl", "daily_realized_pnl", "cumulative_realized_pnl"):
            _finite(getattr(self, name), name)
        if not isclose(
            self.position_value - self.position_cost,
            self.unrealized_pnl,
            abs_tol=1e-6,
        ):
            raise ValueError("unrealized_pnl does not reconcile position value and cost")
        for name in ("open_order_count", "submitted_order_count", "confirmed_fill_count"):
            _nonnegative_int(getattr(self, name), name)
        coverage = tuple(self.trade_coverage)
        if not all(isinstance(item, LedgerTradeCoverage) for item in coverage):
            raise ValueError("trade_coverage must contain LedgerTradeCoverage records")
        coverage_ids = tuple(item.trade_id for item in coverage)
        if len(set(coverage_ids)) != len(coverage_ids):
            raise ValueError("trade_coverage contains a duplicate trade ID")
        if any(
            max(item.match_time_ns, item.last_update_ns) > self.covered_through_ns
            for item in coverage
        ):
            raise ValueError("trade_coverage extends beyond covered_through_ns")
        closed = tuple(self.closed_positions)
        if not all(isinstance(item, LedgerClosedPosition) for item in closed):
            raise ValueError("closed_positions must contain LedgerClosedPosition records")
        closed_keys = tuple((item.market_id, item.token_id, item.closed_at_ns) for item in closed)
        if len(set(closed_keys)) != len(closed_keys):
            raise ValueError("closed_positions contains a duplicate position")
        if any(
            datetime.fromtimestamp(item.closed_at_ns / 1_000_000_000, tz=UTC).date() != self.day
            for item in closed
        ):
            raise ValueError("closed_positions must belong to the ledger UTC day")
        if not isclose(
            sum(item.realized_pnl for item in closed),
            self.daily_realized_pnl,
            abs_tol=1e-6,
        ):
            raise ValueError("daily_realized_pnl does not reconcile closed positions")
        object.__setattr__(self, "trade_coverage", coverage)
        object.__setattr__(self, "closed_positions", closed)

    @property
    def equity(self) -> float:
        return self.collateral_balance + self.position_value

    def to_reconciliation_snapshot(self, ledger_sha256: str) -> DailyLedgerSnapshot:
        return DailyLedgerSnapshot(
            day=self.day,
            realized_pnl=self.daily_realized_pnl,
            observed_at_ns=self.observed_at_ns,
            covered_through_ns=self.covered_through_ns,
            trade_coverage=self.trade_coverage,
            ledger_sha256=ledger_sha256,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "capture_started_at_ns": self.capture_started_at_ns,
            "source_started_at_ns": self.source_started_at_ns,
            "observed_at_ns": self.observed_at_ns,
            "covered_through_ns": self.covered_through_ns,
            "account_address": self.account_address,
            "collateral_balance": self.collateral_balance,
            "collateral_allowance": self.collateral_allowance,
            "position_cost": self.position_cost,
            "position_value": self.position_value,
            "unrealized_pnl": self.unrealized_pnl,
            "daily_realized_pnl": self.daily_realized_pnl,
            "cumulative_realized_pnl": self.cumulative_realized_pnl,
            "open_order_notional": self.open_order_notional,
            "open_order_count": self.open_order_count,
            "submitted_order_count": self.submitted_order_count,
            "confirmed_fill_count": self.confirmed_fill_count,
            "trade_coverage": [_trade_to_json(item) for item in self.trade_coverage],
            "closed_positions": [item.to_json() for item in self.closed_positions],
        }

    @classmethod
    def from_json(cls, raw: object) -> DailyAccountLedger:
        value = _mapping(raw, "daily account ledger")
        day_value = _text(value.get("day"), "day")
        try:
            ledger_day = date.fromisoformat(day_value)
        except ValueError as exc:
            raise ValueError("ledger day must be ISO-8601") from exc
        return cls(
            day=ledger_day,
            capture_started_at_ns=_integer(
                value.get("capture_started_at_ns"), "capture_started_at_ns"
            ),
            source_started_at_ns=_integer(
                value.get("source_started_at_ns"), "source_started_at_ns"
            ),
            observed_at_ns=_integer(value.get("observed_at_ns"), "observed_at_ns"),
            covered_through_ns=_integer(value.get("covered_through_ns"), "covered_through_ns"),
            account_address=_text(value.get("account_address"), "account_address"),
            collateral_balance=_number(value.get("collateral_balance"), "collateral_balance"),
            collateral_allowance=_number(value.get("collateral_allowance"), "collateral_allowance"),
            position_cost=_number(value.get("position_cost"), "position_cost"),
            position_value=_number(value.get("position_value"), "position_value"),
            unrealized_pnl=_number(value.get("unrealized_pnl"), "unrealized_pnl"),
            daily_realized_pnl=_number(value.get("daily_realized_pnl"), "daily_realized_pnl"),
            cumulative_realized_pnl=_number(
                value.get("cumulative_realized_pnl"), "cumulative_realized_pnl"
            ),
            open_order_notional=_number(value.get("open_order_notional"), "open_order_notional"),
            open_order_count=_integer(value.get("open_order_count"), "open_order_count"),
            submitted_order_count=_integer(
                value.get("submitted_order_count"), "submitted_order_count"
            ),
            confirmed_fill_count=_integer(
                value.get("confirmed_fill_count"), "confirmed_fill_count"
            ),
            trade_coverage=tuple(
                _trade_from_json(item)
                for item in _sequence(value.get("trade_coverage"), "trade_coverage")
            ),
            closed_positions=tuple(
                LedgerClosedPosition.from_json(item)
                for item in _sequence(value.get("closed_positions"), "closed_positions")
            ),
        )


@dataclass(frozen=True, slots=True)
class LedgerWriteReceipt:
    ledger_sha256: str
    snapshot_path: Path
    pointer_path: Path


class DailyAccountLedgerStore:
    """Immutable snapshots plus one atomically replaced daily pointer."""

    def __init__(self, runtime_root: Path) -> None:
        self.root = runtime_root / "ledger"

    def write(self, ledger: DailyAccountLedger) -> LedgerWriteReceipt:
        payload = ledger.to_json()
        payload_bytes = _canonical_json(payload)
        digest = sha256(payload_bytes).hexdigest()
        day_root = self.root / "days" / ledger.day.isoformat()
        snapshot_path = day_root / "snapshots" / f"{digest}.json"
        pointer_path = day_root / "latest.json"
        envelope = _canonical_json(
            {
                "schema_version": _LEDGER_SCHEMA_VERSION,
                "ledger_sha256": digest,
                "payload": payload,
            }
        )
        with exclusive_file_lock(self.root / "ledger.lock"):
            try:
                current, current_digest = self._read_pointer(pointer_path)
            except FileNotFoundError:
                current = None
                current_digest = None
            if current is not None:
                if ledger.observed_at_ns < current.observed_at_ns:
                    raise ValueError("an older ledger snapshot cannot replace latest")
                if ledger.observed_at_ns == current.observed_at_ns:
                    if digest != current_digest:
                        raise ValueError("conflicting ledger snapshots share observed_at_ns")
                    return LedgerWriteReceipt(digest, snapshot_path, pointer_path)
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            if snapshot_path.exists():
                if snapshot_path.read_bytes() != envelope:
                    raise ValueError("content-addressed ledger snapshot conflicts on disk")
            else:
                _write_new_file(snapshot_path, envelope)
            _atomic_write_json(
                pointer_path,
                {
                    "schema_version": _LEDGER_SCHEMA_VERSION,
                    "day": ledger.day.isoformat(),
                    "ledger_sha256": digest,
                    "observed_at_ns": ledger.observed_at_ns,
                },
            )
        return LedgerWriteReceipt(digest, snapshot_path, pointer_path)

    def read(self, day: date) -> tuple[DailyAccountLedger, str]:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError("day must be a date")
        return self._read_pointer(self.root / "days" / day.isoformat() / "latest.json")

    def _read_pointer(self, pointer_path: Path) -> tuple[DailyAccountLedger, str]:
        pointer = _mapping(_read_json(pointer_path), "ledger pointer")
        if pointer.get("schema_version") != _LEDGER_SCHEMA_VERSION:
            raise ValueError("unsupported ledger pointer schema")
        day_value = _text(pointer.get("day"), "ledger pointer day")
        digest = _text(pointer.get("ledger_sha256"), "ledger pointer SHA-256")
        if not _SHA256.fullmatch(digest):
            raise ValueError("ledger pointer SHA-256 is invalid")
        observed_at_ns = _integer(pointer.get("observed_at_ns"), "pointer observed_at_ns")
        snapshot_path = pointer_path.parent / "snapshots" / f"{digest}.json"
        envelope = _mapping(_read_json(snapshot_path), "ledger snapshot")
        if envelope.get("schema_version") != _LEDGER_SCHEMA_VERSION:
            raise ValueError("unsupported ledger snapshot schema")
        if envelope.get("ledger_sha256") != digest:
            raise ValueError("ledger snapshot hash does not match pointer")
        payload = _mapping(envelope.get("payload"), "ledger snapshot payload")
        if sha256(_canonical_json(payload)).hexdigest() != digest:
            raise ValueError("ledger snapshot payload hash mismatch")
        ledger = DailyAccountLedger.from_json(payload)
        if ledger.day.isoformat() != day_value or ledger.observed_at_ns != observed_at_ns:
            raise ValueError("ledger pointer identity does not match snapshot")
        return ledger, digest


def ledger_performance_snapshot(
    history: Sequence[DailyAccountLedger],
    *,
    recent_trade_limit: int = 10,
) -> PerformanceSnapshot:
    """Project only account-observed values; expected edge never enters PnL."""

    ledgers = tuple(history)
    if not ledgers:
        raise ValueError("ledger history is required")
    if isinstance(recent_trade_limit, bool) or recent_trade_limit < 1:
        raise ValueError("recent_trade_limit must be >= 1")
    if any(
        right.observed_at_ns <= left.observed_at_ns for left, right in zip(ledgers, ledgers[1:])
    ):
        raise ValueError("ledger history must be strictly ordered")
    if len({item.account_address for item in ledgers}) != 1:
        raise ValueError("ledger history cannot mix accounts")
    current = ledgers[-1]
    points = tuple(
        EquityPoint(
            timestamp=datetime.fromtimestamp(item.observed_at_ns / 1_000_000_000, tz=UTC),
            equity=item.equity,
        )
        for item in ledgers
    )
    peak = points[0].equity
    max_drawdown = 0.0
    for point in points:
        peak = max(peak, point.equity)
        max_drawdown = max(max_drawdown, peak - point.equity)
    closed: dict[tuple[str, str, int], LedgerClosedPosition] = {}
    for ledger in ledgers:
        for position in ledger.closed_positions:
            closed[(position.market_id, position.token_id, position.closed_at_ns)] = position
    recent = tuple(sorted(closed.values(), key=lambda item: item.closed_at_ns, reverse=True))
    wins = sum(item.realized_pnl > 0.0 for item in recent)
    trade_rows = tuple(
        OrderPerformance(
            variant_id="live",
            order_id=f"ledger:{item.market_id}:{item.token_id}",
            market_slug=item.market_slug,
            side=item.side,
            execution_status="filled",
            settlement_status="resolved",
            placed_at=datetime.fromtimestamp(item.closed_at_ns / 1_000_000_000, tz=UTC),
            shares=item.shares,
            filled_shares=item.shares,
            entry_price=item.entry_price,
            realized_pnl=item.realized_pnl,
        )
        for item in recent[:recent_trade_limit]
    )
    return PerformanceSnapshot(
        starting_balance=points[0].equity,
        equity=current.equity,
        available_balance=max(
            0.0,
            min(current.collateral_balance, current.collateral_allowance)
            - current.open_order_notional,
        ),
        open_exposure=current.position_cost + current.open_order_notional,
        realized_pnl=current.cumulative_realized_pnl,
        unrealized_pnl=current.unrealized_pnl,
        today_pnl=current.daily_realized_pnl + current.unrealized_pnl,
        max_drawdown=max_drawdown,
        win_rate=(None if not recent else wins / len(recent)),
        order_count=current.submitted_order_count,
        fill_count=current.confirmed_fill_count,
        equity_curve=points,
        recent_orders=trade_rows,
    )


def _trade_to_json(value: LedgerTradeCoverage) -> dict[str, object]:
    return {
        "trade_id": value.trade_id,
        "status": value.status.value,
        "match_time_ns": value.match_time_ns,
        "last_update_ns": value.last_update_ns,
        "transaction_hash": value.transaction_hash,
    }


def _trade_from_json(raw: object) -> LedgerTradeCoverage:
    value = _mapping(raw, "trade coverage")
    transaction_hash = value.get("transaction_hash")
    if transaction_hash is not None and not isinstance(transaction_hash, str):
        raise ValueError("trade transaction_hash must be a string or null")
    return LedgerTradeCoverage(
        trade_id=_text(value.get("trade_id"), "trade_id"),
        status=_text(value.get("status"), "trade status"),
        match_time_ns=_integer(value.get("match_time_ns"), "match_time_ns"),
        last_update_ns=_integer(value.get("last_update_ns"), "last_update_ns"),
        transaction_hash=transaction_hash,
    )


def _atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    encoded = _canonical_json(value)
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new_file(path: Path, encoded: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid ledger JSON: {path}") from exc


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _finite(value: object, name: str) -> None:
    _number(value, name)


def _nonnegative(value: object, name: str) -> None:
    if _number(value, name) < 0.0:
        raise ValueError(f"{name} must be >= 0")


def _positive(value: object, name: str) -> None:
    if _number(value, name) <= 0.0:
        raise ValueError(f"{name} must be > 0")


def _probability(value: object, name: str) -> None:
    number = _number(value, name)
    if not 0.0 < number < 1.0:
        raise ValueError(f"{name} must be in (0, 1)")


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _nonnegative_int(value: object, name: str) -> None:
    _integer(value, name)


def _condition_id(value: object) -> str:
    if not isinstance(value, str) or not _CONDITION_ID.fullmatch(value):
        raise ValueError("market_id must be a condition ID")
    return value.casefold()


def _token_id(value: object) -> str:
    if not isinstance(value, str) or not value.isdigit() or not 0 < int(value) < 2**256:
        raise ValueError("token_id must be a positive unsigned 256-bit integer")
    return str(int(value))


__all__ = [
    "DailyAccountLedger",
    "DailyAccountLedgerStore",
    "LedgerClosedPosition",
    "LedgerWriteReceipt",
    "ledger_performance_snapshot",
]
