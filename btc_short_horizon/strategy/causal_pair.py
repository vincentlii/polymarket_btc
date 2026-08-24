"""Causal two-token book identity and freshness contract."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Integral

from btc_short_horizon.strategy.types import SideBook, TokenSide


class PairValidationError(ValueError):
    """The two books cannot support one causal binary-market decision."""


@dataclass(frozen=True, slots=True)
class PairBookEvidence:
    side: TokenSide
    token_id: str
    book: SideBook
    source_ts_ns: int
    receive_ts_ns: int
    update_epoch: int
    gap_epoch: int
    collector_session_id: str
    sequence_valid: bool
    full_depth_available: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", TokenSide(self.side))
        if not self.token_id or self.book.token_id != self.token_id:
            raise PairValidationError("book token identity mismatch")
        for name in ("source_ts_ns", "receive_ts_ns", "update_epoch", "gap_epoch"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
                raise PairValidationError(f"{name} must be a non-negative integer")
        if self.source_ts_ns > self.receive_ts_ns:
            raise PairValidationError("source timestamp cannot follow receive timestamp")
        if (
            not self.collector_session_id
            or self.collector_session_id.strip() != self.collector_session_id
        ):
            raise PairValidationError("collector_session_id must be non-empty and trimmed")
        if not isinstance(self.sequence_valid, bool) or not isinstance(
            self.full_depth_available, bool
        ):
            raise PairValidationError("book quality flags must be bool")


@dataclass(frozen=True, slots=True)
class ValidatedCausalPair:
    session: CausalPairSession
    up: PairBookEvidence
    down: PairBookEvidence
    decision_ts_ns: int
    p_market_up: float
    anchor_version: str = "normalized-independent-midpoints-v1"


@dataclass(frozen=True, slots=True)
class CausalPairSession:
    market_slug: str
    condition_id: str
    rule_epoch: str
    rule_hash: str
    up_token_id: str
    down_token_id: str
    t0_ns: int
    t1_ns: int

    def __post_init__(self) -> None:
        for name in (
            "market_slug",
            "condition_id",
            "rule_epoch",
            "rule_hash",
            "up_token_id",
            "down_token_id",
        ):
            value = getattr(self, name)
            if not value or value.strip() != value:
                raise ValueError(f"{name} must be non-empty and trimmed")
        if self.up_token_id == self.down_token_id:
            raise ValueError("Up and Down token IDs must differ")
        if len(self.rule_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.rule_hash
        ):
            raise ValueError("rule_hash must be a lowercase SHA-256 digest")
        if (
            isinstance(self.t0_ns, bool)
            or isinstance(self.t1_ns, bool)
            or not isinstance(self.t0_ns, Integral)
            or not isinstance(self.t1_ns, Integral)
            or self.t0_ns < 0
            or self.t1_ns <= self.t0_ns
        ):
            raise ValueError("market timestamps must be ordered non-negative integers")

    def validate(
        self,
        *,
        up: PairBookEvidence,
        down: PairBookEvidence,
        decision_ts_ns: int,
        maximum_age_ns: int,
        maximum_pair_skew_ns: int = 200_000_000,
        maximum_midpoint_sum_deviation: float = 0.10,
    ) -> ValidatedCausalPair:
        if up.side is not TokenSide.UP or down.side is not TokenSide.DOWN:
            raise PairValidationError("pair sides do not match Up/Down identity")
        if up.token_id != self.up_token_id or down.token_id != self.down_token_id:
            raise PairValidationError("pair token identity does not match the market session")
        if up.collector_session_id != down.collector_session_id:
            raise PairValidationError("pair books must share one collector session")
        for name, value in (
            ("decision_ts_ns", decision_ts_ns),
            ("maximum_age_ns", maximum_age_ns),
            ("maximum_pair_skew_ns", maximum_pair_skew_ns),
        ):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
                raise PairValidationError(f"{name} must be a non-negative integer")
        if not self.t0_ns <= decision_ts_ns < self.t1_ns:
            raise PairValidationError("decision timestamp falls outside the market session")
        for item in (up, down):
            if item.receive_ts_ns > decision_ts_ns:
                raise PairValidationError("book evidence arrives after the decision")
            if decision_ts_ns - item.receive_ts_ns > maximum_age_ns:
                raise PairValidationError("pair book evidence is stale")
            if item.gap_epoch != 0:
                raise PairValidationError("pair book evidence has an unresolved gap")
            if not item.sequence_valid:
                raise PairValidationError("pair book sequence is invalid")
            if not item.full_depth_available:
                raise PairValidationError("pair requires full depth")
        if abs(up.receive_ts_ns - down.receive_ts_ns) > maximum_pair_skew_ns:
            raise PairValidationError("pair book receive timestamps are too far apart")
        if (
            isinstance(maximum_midpoint_sum_deviation, bool)
            or not isfinite(maximum_midpoint_sum_deviation)
            or not 0.0 <= maximum_midpoint_sum_deviation < 1.0
        ):
            raise PairValidationError("maximum midpoint sum deviation is invalid")
        midpoint_sum = up.book.midpoint + down.book.midpoint
        if abs(midpoint_sum - 1.0) > maximum_midpoint_sum_deviation:
            raise PairValidationError("pair books are not complementary")
        if up.book.best_bid + down.book.best_bid > 1.0 + maximum_midpoint_sum_deviation:
            raise PairValidationError("pair bid books cross their binary complement")
        if up.book.best_ask + down.book.best_ask < 1.0 - maximum_midpoint_sum_deviation:
            raise PairValidationError("pair ask books cross their binary complement")
        p_market_up = up.book.midpoint / midpoint_sum
        if not 0.0 < p_market_up < 1.0:
            raise PairValidationError("normalized pair midpoint is not a probability")
        return ValidatedCausalPair(
            session=self,
            up=up,
            down=down,
            decision_ts_ns=int(decision_ts_ns),
            p_market_up=p_market_up,
        )


__all__ = [
    "CausalPairSession",
    "PairBookEvidence",
    "PairValidationError",
    "ValidatedCausalPair",
]
