from __future__ import annotations

import pytest

from btc_short_horizon.features import (
    BtcBookTop,
    BtcReferencePrice,
    BtcTrade,
    OpeningFeatureState,
    build_opening_feature_observations,
    opening_feature_schema,
)


_SECOND = 1_000_000_000
_T0 = 1_000 * _SECOND


def _state() -> OpeningFeatureState:
    return OpeningFeatureState(
        up_token_id="up-token",
        down_token_id="down-token",
        required_venue_sources=("binance_spot",),
    )


def _events(*, include_future: bool = False):
    events = [
        BtcReferencePrice(source_ts_ns=_T0 - 1, available_ts_ns=_T0 - 1, price=100.0),
        BtcReferencePrice(
            source_ts_ns=_T0 + 4 * _SECOND, available_ts_ns=_T0 + 4 * _SECOND, price=100.0
        ),
        BtcTrade(
            source_ts_ns=_T0 + _SECOND, available_ts_ns=_T0 + _SECOND, price=101.0, quantity=1.0
        ),
        BtcTrade(
            source_ts_ns=_T0 + 4 * _SECOND,
            available_ts_ns=_T0 + 4 * _SECOND,
            price=102.0,
            quantity=2.0,
        ),
        BtcBookTop(
            source_ts_ns=_T0 + 4 * _SECOND,
            available_ts_ns=_T0 + 4 * _SECOND,
            bid=101.9,
            ask=102.1,
            bid_size=2.0,
            ask_size=1.0,
        ),
        BtcBookTop(
            source_ts_ns=_T0 + 4 * _SECOND,
            available_ts_ns=_T0 + 4 * _SECOND,
            bid=0.59,
            ask=0.61,
            bid_size=10.0,
            ask_size=11.0,
            source="polymarket_clob",
            instrument="up-token",
        ),
        BtcBookTop(
            source_ts_ns=_T0 + 4 * _SECOND,
            available_ts_ns=_T0 + 4 * _SECOND,
            bid=0.38,
            ask=0.40,
            bid_size=8.0,
            ask_size=9.0,
            source="polymarket_clob",
            instrument="down-token",
        ),
    ]
    if include_future:
        events.append(
            BtcTrade(
                source_ts_ns=_T0 + 6 * _SECOND,
                available_ts_ns=_T0 + 6 * _SECOND,
                price=80.0,
                quantity=10.0,
            )
        )
    return tuple(events)


def test_opening_feature_snapshot_is_causal_and_has_dual_token_market_probability() -> None:
    state = _state()
    decision = _T0 + 5 * _SECOND
    observation = build_opening_feature_observations(
        state=state,
        events=_events(include_future=True),
        decision_times_ns=(decision,),
        market_window_start_ns=_T0,
    )[0]
    values = opening_feature_schema().mapping_from(observation.values)

    assert observation.eligible
    assert observation.p_boundary_up > 0.5
    assert observation.p_market_mid_up == pytest.approx(0.605)
    assert values["binance_spot_return_5s"] > 0.0
    assert values["elapsed_seconds"] == pytest.approx(5.0)
    assert values["remaining_seconds"] == pytest.approx(895.0)


def test_each_stream_has_its_own_ordering_contract() -> None:
    state = _state()
    state.update(
        BtcTrade(
            source_ts_ns=10 * _SECOND,
            available_ts_ns=10 * _SECOND,
            price=100.0,
            quantity=1.0,
        )
    )
    state.update(
        BtcTrade(
            source_ts_ns=5 * _SECOND,
            available_ts_ns=5 * _SECOND,
            price=100.0,
            quantity=1.0,
            source="okx_spot",
            instrument="BTC-USDT",
        )
    )

    with pytest.raises(ValueError, match="ordered"):
        state.update(
            BtcTrade(
                source_ts_ns=9 * _SECOND,
                available_ts_ns=9 * _SECOND,
                price=100.0,
                quantity=1.0,
            )
        )


def test_complement_mismatch_and_chainlink_gap_make_observation_ineligible() -> None:
    state = _state()
    for event in _events():
        state.update(event)
    state.update(
        BtcBookTop(
            source_ts_ns=_T0 + 5 * _SECOND,
            available_ts_ns=_T0 + 5 * _SECOND,
            bid=0.10,
            ask=0.12,
            bid_size=5.0,
            ask_size=5.0,
            source="polymarket_clob",
            instrument="down-token",
        )
    )
    state.mark_gap(source="chainlink", instrument="btc/usd")

    observation = state.snapshot(
        decision_ts_ns=_T0 + 5 * _SECOND,
        market_window_start_ns=_T0,
    )

    assert not observation.eligible
    assert "polymarket_complement_mismatch" in observation.quality_flags
    assert "gap" in observation.quality_flags
    assert "reference_unavailable" in observation.quality_flags
