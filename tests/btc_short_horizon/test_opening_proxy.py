from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_proxy import (
    build_opening_proxy_dataset,
    opening_proxy_feature_schema,
)


_SECOND = 1_000_000_000


def _market(start: datetime) -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(start),
        condition_id="condition",
        up_token_id="up",
        down_token_id="down",
        t0=start,
        t1=start + timedelta(minutes=15),
        rule_epoch="rule-v1",
        rule_hash="a" * 64,
        resolution=MarketOutcome.UP,
        label_available_ts=start + timedelta(minutes=16),
    )


def _history(start: datetime, *, mutate_unavailable_tail: bool = False) -> BinanceKlineHistory:
    opens = np.arange(
        int((start - timedelta(hours=2)).timestamp() * _SECOND),
        int((start + timedelta(minutes=4)).timestamp() * _SECOND),
        _SECOND,
        dtype=np.int64,
    )
    close = 100_000.0 + np.arange(len(opens), dtype=float)
    if mutate_unavailable_tail:
        first_future = int(
            np.searchsorted(opens, int((start + timedelta(seconds=4)).timestamp() * _SECOND))
        )
        close[first_future:] += 1_000_000.0
    return BinanceKlineHistory(
        open_ts_ns=opens,
        close=close,
        volume=np.ones(len(opens)),
        quote_volume=close,
        taker_buy_volume=np.full(len(opens), 0.6),
    )


def test_opening_proxy_generates_evenly_weighted_causal_snapshots_per_market() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    build = build_opening_proxy_dataset(markets=(_market(start),), klines=_history(start))
    values = build.dataset.schema.mapping_from(build.dataset.vectors[0])

    assert build.snapshots_per_market == 36
    assert len(build.dataset.samples) == 36
    assert build.dataset.samples[0].sample_id.endswith(
        f"@{int((start + timedelta(seconds=5)).timestamp() * _SECOND)}"
    )
    assert np.sum(build.dataset.sample_weights) == pytest.approx(1.0)
    assert values["elapsed_seconds"] == pytest.approx(5.0)
    assert values["remaining_seconds"] == pytest.approx(895.0)
    assert values["p_boundary_up"] > 0.5
    assert values["data_age_seconds"] >= 0.0


def test_opening_proxy_never_reads_a_kline_unavailable_at_decision_time() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    baseline = build_opening_proxy_dataset(
        markets=(_market(start),),
        klines=_history(start),
        entry_start_seconds=5,
        entry_end_seconds=5,
    )
    mutated = build_opening_proxy_dataset(
        markets=(_market(start),),
        klines=_history(start, mutate_unavailable_tail=True),
        entry_start_seconds=5,
        entry_end_seconds=5,
    )

    assert mutated.dataset.vectors[0] == pytest.approx(baseline.dataset.vectors[0])


def test_opening_proxy_excludes_a_market_with_a_lookback_gap() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    history = _history(start)
    missing = int(np.searchsorted(history.open_ts_ns, int(start.timestamp() * _SECOND))) - 100
    gapped = BinanceKlineHistory(
        open_ts_ns=np.delete(history.open_ts_ns, missing),
        close=np.delete(history.close, missing),
        volume=np.delete(history.volume, missing),
        quote_volume=np.delete(history.quote_volume, missing),
        taker_buy_volume=np.delete(history.taker_buy_volume, missing),
    )

    with pytest.raises(ValueError, match="no resolved markets"):
        build_opening_proxy_dataset(markets=(_market(start),), klines=gapped)


def test_minute_proxy_schema_only_uses_resolvable_windows() -> None:
    schema = opening_proxy_feature_schema(60)

    assert "binance_spot_return_60s" in schema.names
    assert "binance_spot_return_5s" not in schema.names
