from __future__ import annotations

from dataclasses import replace

import pytest

from btc_short_horizon.strategy.causal_pair import (
    CausalPairSession,
    PairBookEvidence,
    PairValidationError,
)
from btc_short_horizon.strategy.types import SideBook, TokenSide, VisibleBookLevel


def _book(token: str, bid: float, ask: float) -> SideBook:
    return SideBook(
        token_id=token,
        bids=(VisibleBookLevel(bid, 10.0),),
        asks=(VisibleBookLevel(ask, 10.0),),
        tick_size=0.01,
        minimum_order_size=1.0,
    )


def _evidence(side: TokenSide, token: str, bid: float, ask: float) -> PairBookEvidence:
    return PairBookEvidence(
        side=side,
        token_id=token,
        book=_book(token, bid, ask),
        source_ts_ns=9_900_000_000,
        receive_ts_ns=9_950_000_000,
        update_epoch=3,
        gap_epoch=0,
        collector_session_id="session-a",
        sequence_valid=True,
        full_depth_available=True,
    )


def _session() -> CausalPairSession:
    return CausalPairSession(
        market_slug="btc-updown-15m-1",
        condition_id="condition-1",
        rule_epoch="chainlink-btc-usd-point-v1",
        rule_hash="a" * 64,
        up_token_id="up",
        down_token_id="down",
        t0_ns=0,
        t1_ns=900_000_000_000,
    )


def test_pair_anchor_uses_both_independent_books() -> None:
    pair = _session().validate(
        up=_evidence(TokenSide.UP, "up", 0.39, 0.41),
        down=_evidence(TokenSide.DOWN, "down", 0.59, 0.61),
        decision_ts_ns=10_000_000_000,
        maximum_age_ns=200_000_000,
    )

    assert pair.anchor_version == "normalized-independent-midpoints-v1"
    assert pair.p_market_up == pytest.approx(0.4)
    assert pair.up.book.token_id == "up"
    assert pair.down.book.token_id == "down"


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda item: replace(item, token_id="wrong"), "identity"),
        (
            lambda item: replace(item, source_ts_ns=8_900_000_000, receive_ts_ns=9_000_000_000),
            "stale",
        ),
        (lambda item: replace(item, gap_epoch=1), "gap"),
        (lambda item: replace(item, collector_session_id="session-b"), "collector session"),
        (lambda item: replace(item, sequence_valid=False), "sequence"),
        (lambda item: replace(item, full_depth_available=False), "full depth"),
    ),
)
def test_pair_validation_fails_closed_on_invalid_evidence(mutation, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PairValidationError, match=message):
        _session().validate(
            up=mutation(_evidence(TokenSide.UP, "up", 0.39, 0.41)),
            down=_evidence(TokenSide.DOWN, "down", 0.59, 0.61),
            decision_ts_ns=10_000_000_000,
            maximum_age_ns=200_000_000,
        )


def test_pair_validation_rejects_noncomplementary_books() -> None:
    with pytest.raises(PairValidationError, match="complement"):
        _session().validate(
            up=_evidence(TokenSide.UP, "up", 0.69, 0.71),
            down=_evidence(TokenSide.DOWN, "down", 0.59, 0.61),
            decision_ts_ns=10_000_000_000,
            maximum_age_ns=200_000_000,
            maximum_midpoint_sum_deviation=0.05,
        )
