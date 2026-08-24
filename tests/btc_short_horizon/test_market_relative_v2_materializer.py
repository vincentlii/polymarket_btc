from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import log

from btc_short_horizon.features import (
    BtcBookTop,
    BtcTrade,
    OpeningFeatureObservation,
    opening_feature_schema,
)
from btc_short_horizon.research.market_relative_v2 import (
    MarketRelativeV2FactorFamily,
    market_relative_v2_profile_families,
    market_relative_v2_required_venue_sources,
    materialize_market_relative_v2,
)
from btc_short_horizon.research.opening_features import (
    ForwardFeatureSourceSummary,
    ForwardFeatureStateEvent,
    ForwardOpeningFeatureBuild,
)


def _state_event(
    source: str, instrument: str, value: BtcBookTop | BtcTrade
) -> ForwardFeatureStateEvent:
    return ForwardFeatureStateEvent(
        raw_source=source,
        state_source=source,
        state_instrument=instrument,
        source_ts_ns=value.source_ts_ns,
        collector_receive_ts_ns=value.available_ts_ns,
        available_ts_ns=value.available_ts_ns,
        sequence_or_hash=f"{source}:{value.available_ts_ns}:{instrument}",
        collector_session_id="session",
        epoch_id=0,
        value=value,
    )


def _book(
    source: str, instrument: str, *, bid: float, ask: float, ts: int
) -> ForwardFeatureStateEvent:
    return _state_event(
        source,
        instrument,
        BtcBookTop(
            source_ts_ns=ts,
            available_ts_ns=ts,
            bid=bid,
            ask=ask,
            bid_size=3.0,
            ask_size=1.0,
            source=source,
            instrument=instrument,
        ),
    )


def _trade(
    source: str,
    instrument: str,
    *,
    price: float,
    quantity: float,
    side: str,
    ts: int,
) -> ForwardFeatureStateEvent:
    return _state_event(
        source,
        instrument,
        BtcTrade(
            source_ts_ns=ts,
            available_ts_ns=ts,
            price=price,
            quantity=quantity,
            aggressor_side=side,
            source=source,
            instrument=instrument,
        ),
    )


def _build(decision: int) -> ForwardOpeningFeatureBuild:
    schema = opening_feature_schema()
    values = {name: 0.0 for name in schema.names}
    values.update(
        elapsed_seconds=5.0,
        remaining_seconds=895.0,
        data_age_seconds=0.1,
        p_boundary_up=0.58,
    )
    for source, price in (
        ("binance_spot", 100.0),
        ("binance_perp", 101.0),
        ("okx_spot", 102.0),
        ("okx_swap", 103.0),
    ):
        for seconds in (1, 5, 15):
            values[f"{source}_return_{seconds}s"] = log((price + 1.0) / price)
            values[f"{source}_flow_{seconds}s"] = (price - (price + 1.0) * 2.0) / (
                price + (price + 1.0) * 2.0
            )
        values[f"{source}_book_imbalance"] = 0.5
    values["consensus_dispersion_bps"] = 1.5 / 102.5 * 10_000.0
    events = [
        _book("polymarket_clob", "up", bid=0.49, ask=0.51, ts=decision - 1),
        _book("polymarket_clob", "down", bid=0.48, ask=0.50, ts=decision - 1),
    ]
    for source, instrument, price in (
        ("binance_spot", "BTCUSDT", 100.0),
        ("binance_perp", "BTCUSDT", 101.0),
        ("okx_spot", "BTC-USDT", 102.0),
        ("okx_swap", "BTC-USDT-SWAP", 103.0),
    ):
        events.extend(
            (
                _trade(
                    source,
                    instrument,
                    price=price,
                    quantity=1.0,
                    side="buy",
                    ts=decision - 900_000_000,
                ),
                _trade(
                    source,
                    instrument,
                    price=price + 1.0,
                    quantity=2.0,
                    side="sell",
                    ts=decision - 100_000_000,
                ),
                _book(source, instrument, bid=price, ask=price + 2.0, ts=decision - 50_000_000),
            )
        )
    return ForwardOpeningFeatureBuild(
        observations=(
            OpeningFeatureObservation(
                market_window_start_ns=decision - 5_000_000_000,
                ts_event=decision,
                ts_init=decision,
                feature_schema_hash=schema.hash,
                values=schema.vector_from(values),
                p_boundary_up=0.58,
                p_market_mid_up=0.51,
            ),
        ),
        input_events=tuple(events),
        source_summaries=tuple(
            ForwardFeatureSourceSummary(source, 1, 10, 10, 0)
            for source in (
                "polymarket_clob",
                "binance_spot",
                "binance_perp",
                "okx_spot",
                "okx_swap",
            )
        ),
    )


def _materialize(
    decision: int,
    build: ForwardOpeningFeatureBuild,
    families: tuple[MarketRelativeV2FactorFamily, ...],
):
    return materialize_market_relative_v2(
        market_slug="btc-updown-15m-1800000000",
        up_token_id="up",
        down_token_id="down",
        label=1,
        label_available_ts=datetime.fromtimestamp(decision / 1e9, tz=UTC) + timedelta(minutes=15),
        direction_p_up_by_decision={decision: 0.62},
        raw_build=build,
        families=families,
    )


def test_v2_materializer_builds_causal_dual_token_dataset() -> None:
    decision = 1_800_000_005_000_000_000
    result = _materialize(
        decision,
        _build(decision),
        (MarketRelativeV2FactorFamily.ANCHOR, MarketRelativeV2FactorFamily.PM_DUAL_TOKEN),
    )
    assert result.dataset is not None
    assert result.dataset.samples[0].group_id == "btc-updown-15m-1800000000"
    assert "pm_complement_ask_excess" in result.dataset.schema.names


def test_v2_profiles_keep_historical_core_separate_from_forward_flow() -> None:
    assert market_relative_v2_required_venue_sources("core") == ()
    assert market_relative_v2_required_venue_sources("trade_flow") == ("binance_spot",)
    assert market_relative_v2_required_venue_sources("flow") == ("binance_spot",)
    assert market_relative_v2_profile_families("core") == (
        MarketRelativeV2FactorFamily.ANCHOR,
        MarketRelativeV2FactorFamily.PM_DUAL_TOKEN,
    )
    trade_flow = market_relative_v2_profile_families("trade_flow")
    assert trade_flow[-1] is MarketRelativeV2FactorFamily.BINANCE_SPOT_TRADE_FLOW


def test_v2_materializer_fails_closed_with_explicit_missing_source_coverage() -> None:
    decision = 1_800_000_005_000_000_000
    build = _build(decision)
    result = _materialize(
        decision,
        ForwardOpeningFeatureBuild(
            observations=build.observations,
            input_events=build.input_events,
            source_summaries=tuple(
                item for item in build.source_summaries if not item.raw_source.startswith("okx")
            ),
        ),
        (MarketRelativeV2FactorFamily.OKX_SPOT_FLOW_BOOK,),
    )
    assert result.dataset is None
    assert result.coverage.excluded_reasons == ("missing_source:okx_spot",)


def test_v2_multisource_features_use_available_events_and_ignore_future() -> None:
    decision = 1_800_000_005_000_000_000
    build = _build(decision)
    future = _trade(
        "binance_spot",
        "BTCUSDT",
        price=999.0,
        quantity=100.0,
        side="buy",
        ts=decision + 1,
    )
    result = _materialize(
        decision,
        ForwardOpeningFeatureBuild(
            observations=build.observations,
            input_events=(*build.input_events, future),
            source_summaries=build.source_summaries,
        ),
        (
            MarketRelativeV2FactorFamily.BINANCE_SPOT_FLOW_BOOK,
            MarketRelativeV2FactorFamily.BINANCE_PERP_FLOW_BOOK,
            MarketRelativeV2FactorFamily.OKX_SPOT_FLOW_BOOK,
            MarketRelativeV2FactorFamily.OKX_SWAP_FLOW_BOOK,
            MarketRelativeV2FactorFamily.CROSS_VENUE,
        ),
    )
    assert result.dataset is not None
    values = dict(zip(result.dataset.schema.names, result.dataset.vectors[0], strict=True))
    assert values["binance_spot_return_1s"] < 0.02
    assert values["binance_spot_flow_1s"] < 0.0
    assert values["okx_swap_book_imbalance"] == 0.5
    assert values["consensus_dispersion_bps"] > 0.0
