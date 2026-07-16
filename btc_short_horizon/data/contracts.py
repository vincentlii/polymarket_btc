"""Stable data contracts for BTC Up/Down market research."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

_SLUG_PREFIX_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class MarketValidationError(ValueError):
    """Raised when a market data contract violates its causal invariants."""


class MarketCollectionMode(str, Enum):
    """Defines whether a market family is in the current research scope."""

    COMPLETE = "complete"
    COLLECTION_ONLY = "collection_only"


class MarketOutcome(str, Enum):
    """Final outcome labels for a binary BTC Up/Down market."""

    UP = "up"
    DOWN = "down"
    VOID = "void"


def _normalize_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise MarketValidationError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketValidationError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC)


def _normalize_window_time(value: datetime, *, field_name: str) -> datetime:
    normalized = _normalize_utc(value, field_name=field_name)
    if normalized.microsecond != 0:
        raise MarketValidationError(f"{field_name} must have whole-second precision.")
    return normalized


def _require_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise MarketValidationError(f"{field_name} must be a string.")
    normalized = value.strip()
    if not normalized:
        raise MarketValidationError(f"{field_name} must not be empty.")
    return normalized


def _epoch_seconds(value: datetime) -> int:
    return int(value.timestamp())


@dataclass(frozen=True)
class BtcMarketFamily:
    """Configures a canonical BTC Up/Down market family and its slug format."""

    name: str
    slug_prefix: str
    window_seconds: int
    collection_mode: MarketCollectionMode

    def __post_init__(self) -> None:
        name = _require_text(self.name, field_name="name").casefold()
        slug_prefix = _require_text(self.slug_prefix, field_name="slug_prefix").casefold()
        if not _SLUG_PREFIX_PATTERN.fullmatch(slug_prefix):
            raise MarketValidationError(
                "slug_prefix must use lowercase letters, digits, and hyphens."
            )
        if isinstance(self.window_seconds, bool) or not isinstance(self.window_seconds, int):
            raise MarketValidationError("window_seconds must be an integer.")
        if self.window_seconds <= 0 or self.window_seconds % 60 != 0:
            raise MarketValidationError(
                "window_seconds must be a positive whole number of minutes."
            )
        try:
            collection_mode = MarketCollectionMode(self.collection_mode)
        except ValueError as exc:
            raise MarketValidationError(
                f"Unsupported collection_mode {self.collection_mode!r}."
            ) from exc

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "slug_prefix", slug_prefix)
        object.__setattr__(self, "collection_mode", collection_mode)

    @property
    def window_minutes(self) -> int:
        """Return the canonical window duration encoded in the slug."""
        return self.window_seconds // 60

    @property
    def window_seconds_as_timedelta(self) -> timedelta:
        """Return the configured interval as a ``timedelta`` for metadata parsing."""
        return timedelta(seconds=self.window_seconds)

    @property
    def is_collection_only(self) -> bool:
        """Return whether this family is collected but excluded from strategy research."""
        return self.collection_mode is MarketCollectionMode.COLLECTION_ONLY

    def slug_for(self, t0: datetime) -> str:
        """Return the canonical epoch-based slug for a market window start."""
        start = _normalize_window_time(t0, field_name="t0")
        epoch_seconds = _epoch_seconds(start)
        if epoch_seconds % self.window_seconds != 0:
            raise MarketValidationError(
                f"t0 must align to the {self.window_seconds}-second {self.name} window."
            )
        return f"{self.slug_prefix}-{self.window_minutes}m-{epoch_seconds}"

    def parse_slug(self, slug: str) -> datetime:
        """Parse one canonical slug and return its UTC market window start."""
        if not isinstance(slug, str) or slug != slug.strip().casefold():
            raise MarketValidationError(
                "slug must be a canonical lowercase string without whitespace."
            )

        pattern = re.compile(
            rf"^{re.escape(self.slug_prefix)}-{self.window_minutes}m-(?P<epoch>[0-9]+)$"
        )
        match = pattern.fullmatch(slug)
        if match is None:
            raise MarketValidationError(f"slug {slug!r} does not belong to {self.name}.")

        try:
            t0 = datetime.fromtimestamp(int(match.group("epoch")), UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise MarketValidationError(f"slug {slug!r} has an invalid epoch.") from exc
        if self.slug_for(t0) != slug:
            raise MarketValidationError(f"slug {slug!r} is not a canonical aligned market window.")
        return t0


BTC_15M_MARKET_FAMILY = BtcMarketFamily(
    name="btc_updown_15m",
    slug_prefix="btc-updown",
    window_seconds=15 * 60,
    collection_mode=MarketCollectionMode.COMPLETE,
)

BTC_5M_MARKET_FAMILY = BtcMarketFamily(
    name="btc_updown_5m",
    slug_prefix="btc-updown",
    window_seconds=5 * 60,
    collection_mode=MarketCollectionMode.COLLECTION_ONLY,
)


@dataclass(frozen=True)
class MarketWindow:
    """Validated metadata and outcome availability for one BTC market window."""

    family: BtcMarketFamily
    slug: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    t0: datetime
    t1: datetime
    rule_epoch: str
    rule_hash: str
    resolution: MarketOutcome | None = None
    label_available_ts: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.family, BtcMarketFamily):
            raise MarketValidationError("family must be a BtcMarketFamily.")

        slug = _require_text(self.slug, field_name="slug")
        t0 = _normalize_window_time(self.t0, field_name="t0")
        t1 = _normalize_window_time(self.t1, field_name="t1")
        expected_slug = self.family.slug_for(t0)
        if slug != expected_slug:
            raise MarketValidationError(
                f"slug {slug!r} does not match the canonical {self.family.name} window {expected_slug!r}."
            )
        if t1 != t0 + timedelta(seconds=self.family.window_seconds):
            raise MarketValidationError(
                f"t1 must equal t0 plus {self.family.window_seconds} seconds for {self.family.name}."
            )

        condition_id = _require_text(self.condition_id, field_name="condition_id")
        up_token_id = _require_text(self.up_token_id, field_name="up_token_id")
        down_token_id = _require_text(self.down_token_id, field_name="down_token_id")
        if up_token_id == down_token_id:
            raise MarketValidationError("up_token_id and down_token_id must be different.")

        rule_epoch = _require_text(self.rule_epoch, field_name="rule_epoch")
        rule_hash = _require_text(self.rule_hash, field_name="rule_hash")
        if not _SHA256_PATTERN.fullmatch(rule_hash):
            raise MarketValidationError("rule_hash must be a SHA-256 hexadecimal digest.")

        resolution = self._normalize_resolution(self.resolution)
        label_available_ts = self._normalize_label_available_ts(
            self.label_available_ts,
            resolution=resolution,
            t1=t1,
        )

        object.__setattr__(self, "slug", slug)
        object.__setattr__(self, "condition_id", condition_id)
        object.__setattr__(self, "up_token_id", up_token_id)
        object.__setattr__(self, "down_token_id", down_token_id)
        object.__setattr__(self, "t0", t0)
        object.__setattr__(self, "t1", t1)
        object.__setattr__(self, "rule_epoch", rule_epoch)
        object.__setattr__(self, "rule_hash", rule_hash.casefold())
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(self, "label_available_ts", label_available_ts)

    @staticmethod
    def _normalize_resolution(value: MarketOutcome | str | None) -> MarketOutcome | None:
        if value is None:
            return None
        try:
            return MarketOutcome(value)
        except ValueError as exc:
            raise MarketValidationError(f"Unsupported resolution {value!r}.") from exc

    @staticmethod
    def _normalize_label_available_ts(
        value: datetime | None,
        *,
        resolution: MarketOutcome | None,
        t1: datetime,
    ) -> datetime | None:
        if resolution is None:
            if value is not None:
                raise MarketValidationError("label_available_ts requires a final resolution label.")
            return None
        if value is None:
            raise MarketValidationError("A resolved market requires label_available_ts.")

        label_available_ts = _normalize_utc(value, field_name="label_available_ts")
        if label_available_ts < t1:
            raise MarketValidationError("label_available_ts cannot precede t1.")
        return label_available_ts

    @property
    def winning_token_id(self) -> str | None:
        """Return the resolved winning token, or ``None`` for unresolved/void markets."""
        if self.resolution is MarketOutcome.UP:
            return self.up_token_id
        if self.resolution is MarketOutcome.DOWN:
            return self.down_token_id
        return None

    @property
    def is_resolved(self) -> bool:
        """Return whether this catalog record has a final label available for training."""
        return self.resolution is not None


@dataclass(frozen=True)
class TimedMarketEvent:
    """Causal timing metadata shared by raw and derived market events."""

    source_ts: datetime
    collector_receive_ts: datetime | None
    available_ts: datetime
    sequence_or_hash: str
    source: str
    instrument: str
    schema_version: str
    ingest_version: str

    def __post_init__(self) -> None:
        source_ts = _normalize_utc(self.source_ts, field_name="source_ts")
        collector_receive_ts = (
            None
            if self.collector_receive_ts is None
            else _normalize_utc(self.collector_receive_ts, field_name="collector_receive_ts")
        )
        available_ts = _normalize_utc(self.available_ts, field_name="available_ts")
        if available_ts < source_ts:
            raise MarketValidationError("available_ts cannot precede source_ts.")
        if collector_receive_ts is not None and available_ts < collector_receive_ts:
            raise MarketValidationError("available_ts cannot precede collector_receive_ts.")

        object.__setattr__(self, "source_ts", source_ts)
        object.__setattr__(self, "collector_receive_ts", collector_receive_ts)
        object.__setattr__(self, "available_ts", available_ts)
        object.__setattr__(
            self,
            "sequence_or_hash",
            _require_text(self.sequence_or_hash, field_name="sequence_or_hash"),
        )
        object.__setattr__(
            self, "source", _require_text(self.source, field_name="source").casefold()
        )
        object.__setattr__(
            self, "instrument", _require_text(self.instrument, field_name="instrument")
        )
        object.__setattr__(
            self, "schema_version", _require_text(self.schema_version, field_name="schema_version")
        )
        object.__setattr__(
            self, "ingest_version", _require_text(self.ingest_version, field_name="ingest_version")
        )

    @property
    def is_event_time_only(self) -> bool:
        """Return whether this event has no observed collector receive timestamp."""
        return self.collector_receive_ts is None


__all__ = [
    "BTC_15M_MARKET_FAMILY",
    "BTC_5M_MARKET_FAMILY",
    "BtcMarketFamily",
    "MarketCollectionMode",
    "MarketOutcome",
    "MarketValidationError",
    "MarketWindow",
    "TimedMarketEvent",
]
