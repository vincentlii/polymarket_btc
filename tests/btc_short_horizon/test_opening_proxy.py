from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.opening_proxy import (
    OpeningRegime,
    build_opening_proxy_dataset,
    opening_proxy_protocol,
    opening_proxy_decision_offsets_ms,
    opening_proxy_feature_values_at,
    opening_proxy_feature_schema,
    opening_regime_for_elapsed_seconds,
    validate_opening_proxy_protocol,
)
from scripts import btc_opening_mispricing_proxy as proxy_script


_SECOND = 1_000_000_000


def test_opening_decisions_align_to_market_cadence_not_entry_window_start() -> None:
    assert opening_proxy_decision_offsets_ms(
        cadence_ms=5_000,
        entry_start_seconds=3,
        entry_end_seconds=180,
    ) == tuple(range(5_000, 180_001, 5_000))


def test_opening_regimes_cover_the_frozen_three_minute_protocol() -> None:
    assert [opening_regime_for_elapsed_seconds(value) for value in (5, 30, 35, 90, 95, 180)] == [
        OpeningRegime.EARLY,
        OpeningRegime.EARLY,
        OpeningRegime.PRICE_DISCOVERY,
        OpeningRegime.PRICE_DISCOVERY,
        OpeningRegime.MID_EARLY,
        OpeningRegime.MID_EARLY,
    ]
    for gap_value in (31, 34.999, 90.001, 94.999, 181):
        with pytest.raises(ValueError, match="three-minute"):
            opening_regime_for_elapsed_seconds(gap_value)


def test_opening_proxy_protocol_rejects_an_early_30_second_artifact() -> None:
    expected = opening_proxy_protocol(
        entry_start_seconds=3,
        entry_end_seconds=180,
        snapshot_seconds=5,
    )
    early_30 = opening_proxy_protocol(
        entry_start_seconds=3,
        entry_end_seconds=30,
        snapshot_seconds=5,
    )

    validate_opening_proxy_protocol(
        {"opening_proxy_protocol": expected},
        expected=expected,
    )
    with pytest.raises(ValueError, match="protocol"):
        validate_opening_proxy_protocol(
            {"opening_proxy_protocol": early_30},
            expected=expected,
        )


def test_opening_proxy_protocol_records_actual_cadence_regime_boundaries() -> None:
    protocol = opening_proxy_protocol(
        entry_start_seconds=3,
        entry_end_seconds=180,
        snapshot_seconds=10,
    )

    assert protocol["version"] == 2
    assert protocol["regimes"] == [
        {"name": "early_3s_to_30s", "start_seconds": 10, "end_seconds": 30},
        {"name": "price_discovery_35s_to_90s", "start_seconds": 40, "end_seconds": 90},
        {"name": "mid_early_95s_to_180s", "start_seconds": 100, "end_seconds": 180},
    ]


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


def test_single_runtime_feature_vector_matches_the_training_dataset() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    history = _history(start)
    build = build_opening_proxy_dataset(
        markets=(_market(start),),
        klines=history,
        entry_start_seconds=5,
        entry_end_seconds=5,
    )

    values = opening_proxy_feature_values_at(
        klines=history,
        market_start=start,
        decision_time=start + timedelta(seconds=5),
    )

    assert build.dataset.schema.vector_from(values) == pytest.approx(build.dataset.vectors[0])


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


def test_opening_proxy_rejects_duplicate_markets_and_mixed_rule_epochs() -> None:
    start = datetime(2026, 1, 1, 2, tzinfo=UTC)
    market = _market(start)
    with pytest.raises(ValueError, match="unique"):
        build_opening_proxy_dataset(markets=(market, market), klines=_history(start))
    with pytest.raises(ValueError, match="rule epochs"):
        build_opening_proxy_dataset(
            markets=(
                market,
                replace(
                    _market(start + timedelta(minutes=15)),
                    rule_epoch="rule-v2",
                ),
            ),
            klines=_history(start),
        )


def test_minute_proxy_schema_only_uses_resolvable_windows() -> None:
    schema = opening_proxy_feature_schema(60)

    assert "binance_spot_return_60s" in schema.names
    assert "binance_spot_return_5s" not in schema.names


def test_materialized_proxy_loader_filters_entry_window_and_reweights_markets(
    tmp_path: Path,
) -> None:
    loader = proxy_script.load_materialized_opening_proxy_dataset
    schema = opening_proxy_feature_schema(1)
    path = tmp_path / "dataset.parquet"
    rows: list[dict[str, object]] = []
    for market_index in range(2):
        market_start = datetime(2026, 1, 1, market_index, tzinfo=UTC)
        for elapsed_seconds in (5, 10, 15, 20, 25, 30, 35):
            row = {name: 0.0 for name in schema.names}
            row.update(
                {
                    "sample_id": (
                        f"market-{market_index}@"
                        f"{int((market_start + timedelta(seconds=elapsed_seconds)).timestamp() * _SECOND)}"
                    ),
                    "feature_ts": market_start + timedelta(seconds=elapsed_seconds),
                    "label_available_ts": market_start + timedelta(minutes=16),
                    "label": market_index,
                    "sample_weight": 1.0 / 7.0,
                    "elapsed_seconds": float(elapsed_seconds),
                }
            )
            rows.append(row)
    pd.DataFrame(rows).to_parquet(path, index=False)

    dataset = loader(
        path=path,
        interval_seconds=1,
        snapshot_seconds=5,
        entry_start_seconds=3,
        entry_end_seconds=30,
    )

    assert len(dataset.samples) == 12
    assert [sample.group_id for sample in dataset.samples] == [
        "market-0",
    ] * 6 + ["market-1"] * 6
    assert dataset.sample_weights == pytest.approx([1.0 / 6.0] * 12)


def test_materialized_proxy_loader_rejects_unordered_parts(tmp_path: Path) -> None:
    schema = opening_proxy_feature_schema(1)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    for elapsed_seconds in (10, 5):
        row = {name: 0.0 for name in schema.names}
        row.update(
            {
                "sample_id": (
                    f"market@{int((start + timedelta(seconds=elapsed_seconds)).timestamp() * _SECOND)}"
                ),
                "feature_ts": start + timedelta(seconds=elapsed_seconds),
                "label_available_ts": start + timedelta(minutes=16),
                "label": 1,
                "sample_weight": 0.5,
                "elapsed_seconds": float(elapsed_seconds),
            }
        )
        rows.append(row)
    path = tmp_path / "unordered.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)

    with pytest.raises(ValueError, match="chronological"):
        proxy_script.load_materialized_opening_proxy_dataset(
            path=path,
            interval_seconds=1,
            snapshot_seconds=5,
            entry_start_seconds=3,
            entry_end_seconds=10,
        )


def test_materialized_proxy_loader_does_not_bulk_materialize_with_pandas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = opening_proxy_feature_schema(1)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    for elapsed_seconds in (5, 10):
        row = {name: 0.0 for name in schema.names}
        row.update(
            {
                "sample_id": (
                    f"market@{int((start + timedelta(seconds=elapsed_seconds)).timestamp() * _SECOND)}"
                ),
                "feature_ts": start + timedelta(seconds=elapsed_seconds),
                "label_available_ts": start + timedelta(minutes=16),
                "label": 1,
                "sample_weight": 0.5,
                "elapsed_seconds": float(elapsed_seconds),
            }
        )
        rows.append(row)
    path = tmp_path / "streamed.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)

    def fail_bulk_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("materialized loader must stream Parquet batches")

    monkeypatch.setattr(proxy_script.pd, "read_parquet", fail_bulk_read)
    dataset = proxy_script.load_materialized_opening_proxy_dataset(
        path=path,
        interval_seconds=1,
        snapshot_seconds=5,
        entry_start_seconds=3,
        entry_end_seconds=10,
    )

    assert len(dataset.samples) == 2
    assert dataset.sample_weights == pytest.approx([0.5, 0.5])


def test_materialized_proxy_loader_applies_a_deterministic_market_stride(
    tmp_path: Path,
) -> None:
    schema = opening_proxy_feature_schema(1)
    rows: list[dict[str, object]] = []
    for market_index in range(3):
        start = datetime(2026, 1, 1, market_index, tzinfo=UTC)
        for elapsed_seconds in (5, 10):
            row = {name: 0.0 for name in schema.names}
            row.update(
                {
                    "sample_id": (
                        f"market-{market_index}@"
                        f"{int((start + timedelta(seconds=elapsed_seconds)).timestamp() * _SECOND)}"
                    ),
                    "feature_ts": start + timedelta(seconds=elapsed_seconds),
                    "label_available_ts": start + timedelta(minutes=16),
                    "label": market_index % 2,
                    "sample_weight": 0.5,
                    "elapsed_seconds": float(elapsed_seconds),
                }
            )
            rows.append(row)
    path = tmp_path / "strided.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)

    dataset = proxy_script.load_materialized_opening_proxy_dataset(
        path=path,
        interval_seconds=1,
        snapshot_seconds=5,
        entry_start_seconds=3,
        entry_end_seconds=10,
        market_stride=2,
    )

    assert [sample.group_id for sample in dataset.samples] == [
        "market-0",
        "market-0",
        "market-2",
        "market-2",
    ]
    assert dataset.sample_weights == pytest.approx([0.5] * 4)

    with pytest.raises(ValueError, match="study catalog"):
        proxy_script.load_materialized_opening_proxy_dataset(
            path=path,
            interval_seconds=1,
            snapshot_seconds=5,
            entry_start_seconds=3,
            entry_end_seconds=10,
            market_stride=2,
            expected_market_group_ids=("market-0", "wrong-market"),
        )


def test_materialized_proxy_loader_rejects_fractional_integer_fields(tmp_path: Path) -> None:
    schema = opening_proxy_feature_schema(1)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    row = {name: 0.0 for name in schema.names}
    row.update(
        {
            "sample_id": f"market@{int((start + timedelta(seconds=5)).timestamp() * _SECOND)}",
            "feature_ts": start + timedelta(seconds=5),
            "label_available_ts": start + timedelta(minutes=16),
            "label": 0.5,
            "elapsed_seconds": 5.0,
        }
    )
    path = tmp_path / "fractional.parquet"
    pd.DataFrame([row]).to_parquet(path, index=False)

    with pytest.raises(ValueError, match="label.*integers"):
        proxy_script.load_materialized_opening_proxy_dataset(
            path=path,
            interval_seconds=1,
            snapshot_seconds=5,
            entry_start_seconds=5,
            entry_end_seconds=5,
        )
