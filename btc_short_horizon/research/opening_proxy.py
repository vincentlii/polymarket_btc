"""Fast, explicitly limited historical proxy for post-open BTC fair probability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import erf, log, sqrt
from typing import Sequence

import numpy as np

from btc_short_horizon.data.contracts import MarketOutcome, MarketWindow
from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample


_NANOS_PER_SECOND = 1_000_000_000
_WINDOWS_SECONDS = (5, 15, 30, 60, 180, 300, 900, 1_800, 3_600)


@dataclass(frozen=True, slots=True)
class OpeningProxyDatasetBuild:
    """A proxy dataset whose samples are evenly weighted per market window."""

    dataset: DirectionDataset
    requested_markets: int
    excluded_void_markets: int
    excluded_insufficient_history: int
    excluded_kline_gaps: int
    snapshots_per_market: int


def opening_proxy_feature_schema(interval_seconds: int) -> FeatureSchema:
    windows = _windows_for_interval(interval_seconds)
    names = [
        "elapsed_seconds",
        "remaining_seconds",
        "opening_proxy_log_return",
        "remaining_sigma",
        "boundary_z",
        "p_boundary_up",
    ]
    for seconds in windows:
        names.extend(
            (
                f"binance_spot_return_{seconds}s",
                f"binance_spot_rv_{seconds}s",
                f"binance_spot_flow_{seconds}s",
                f"binance_spot_volume_{seconds}s",
                f"binance_spot_vwap_distance_{seconds}s",
            )
        )
    names.append("data_age_seconds")
    return FeatureSchema(
        version=f"btc-opening-proxy-kline-{interval_seconds}s-v1",
        names=tuple(names),
    )


def build_opening_proxy_dataset(
    *,
    markets: Sequence[MarketWindow],
    klines: BinanceKlineHistory,
    snapshot_seconds: int = 5,
    entry_start_seconds: int = 3,
    entry_end_seconds: int = 180,
    availability_delay: timedelta = timedelta(seconds=1),
) -> OpeningProxyDatasetBuild:
    """Build 3–180 second snapshots without claiming Binance equals Chainlink/CLOB.

    This is a model-feasibility proxy only: it uses a conservative availability
    delay and the last causally available Binance price at the market boundary.
    It does not contain Polymarket prices, CLOB depth, or the final Chainlink
    reference and therefore cannot establish trading or maker profitability.
    """

    _validate_timing(
        snapshot_seconds=snapshot_seconds,
        entry_start_seconds=entry_start_seconds,
        entry_end_seconds=entry_end_seconds,
        interval_seconds=klines.interval_seconds,
        availability_delay=availability_delay,
    )
    snapshot_offsets = _snapshot_offsets(
        snapshot_seconds=snapshot_seconds,
        entry_start_seconds=entry_start_seconds,
        entry_end_seconds=entry_end_seconds,
    )
    schema = opening_proxy_feature_schema(klines.interval_seconds)
    interval_ns = klines.interval_seconds * _NANOS_PER_SECOND
    available_ts_ns = (
        klines.open_ts_ns
        + interval_ns
        + round(availability_delay.total_seconds() * _NANOS_PER_SECOND)
    )
    windows = _windows_for_interval(klines.interval_seconds)
    max_window_ns = max(windows) * _NANOS_PER_SECOND
    samples: list[ResearchSample] = []
    vectors: list[tuple[float, ...]] = []
    weights: list[float] = []
    excluded_void = 0
    excluded_history = 0
    excluded_gaps = 0

    for market in sorted(markets, key=lambda item: item.t0):
        if market.resolution not in {MarketOutcome.UP, MarketOutcome.DOWN}:
            excluded_void += 1
            continue
        assert market.label_available_ts is not None
        market_start_ns = _datetime_to_ns(market.t0)
        reference_index = int(np.searchsorted(available_ts_ns, market_start_ns, side="right")) - 1
        if reference_index < 0:
            excluded_history += 1
            continue
        market_samples: list[tuple[ResearchSample, tuple[float, ...]]] = []
        failure: str | None = None
        for elapsed_seconds in snapshot_offsets:
            decision_ns = market_start_ns + elapsed_seconds * _NANOS_PER_SECOND
            start = int(np.searchsorted(available_ts_ns, decision_ns - max_window_ns, side="left"))
            end = int(np.searchsorted(available_ts_ns, decision_ns, side="right"))
            if (
                start >= end
                or reference_index >= end
                or not _has_full_history(
                    available_ts_ns=available_ts_ns,
                    start=start,
                    decision_ns=decision_ns,
                    max_window_ns=max_window_ns,
                    interval_ns=interval_ns,
                )
            ):
                failure = "history"
                break
            if np.any(np.diff(klines.open_ts_ns[start:end]) != interval_ns):
                failure = "gap"
                break
            vector = _feature_vector(
                klines=klines,
                available_ts_ns=available_ts_ns,
                start=start,
                end=end,
                reference_index=reference_index,
                decision_ns=decision_ns,
                elapsed_seconds=elapsed_seconds,
                schema=schema,
                windows=windows,
            )
            market_samples.append(
                (
                    ResearchSample(
                        sample_id=f"{market.slug}@{decision_ns}",
                        feature_ts=datetime.fromtimestamp(decision_ns / _NANOS_PER_SECOND, tz=UTC),
                        label_available_ts=market.label_available_ts,
                        label=1 if market.resolution is MarketOutcome.UP else 0,
                        group_id=market.slug,
                    ),
                    vector,
                )
            )
        if failure == "gap":
            excluded_gaps += 1
            continue
        if failure is not None:
            excluded_history += 1
            continue
        per_snapshot_weight = 1.0 / len(market_samples)
        for sample, vector in market_samples:
            samples.append(sample)
            vectors.append(vector)
            weights.append(per_snapshot_weight)

    if not samples:
        raise ValueError("no resolved markets have complete causal Binance opening-proxy history")
    return OpeningProxyDatasetBuild(
        dataset=DirectionDataset(
            samples=tuple(samples),
            vectors=np.asarray(vectors, dtype=float),
            schema=schema,
            sample_weights=np.asarray(weights, dtype=float),
        ),
        requested_markets=len(markets),
        excluded_void_markets=excluded_void,
        excluded_insufficient_history=excluded_history,
        excluded_kline_gaps=excluded_gaps,
        snapshots_per_market=len(snapshot_offsets),
    )


def _feature_vector(
    *,
    klines: BinanceKlineHistory,
    available_ts_ns: np.ndarray,
    start: int,
    end: int,
    reference_index: int,
    decision_ns: int,
    elapsed_seconds: int,
    schema: FeatureSchema,
    windows: Sequence[int],
) -> tuple[float, ...]:
    latest_close = float(klines.close[end - 1])
    opening_reference = float(klines.close[reference_index])
    values: dict[str, float] = {}
    rv_300 = 0.0
    for seconds in windows:
        window_start = max(
            start,
            int(
                np.searchsorted(
                    available_ts_ns,
                    decision_ns - seconds * _NANOS_PER_SECOND,
                    side="left",
                )
            ),
        )
        close = klines.close[window_start:end]
        volume = klines.volume[window_start:end]
        quote_volume = klines.quote_volume[window_start:end]
        taker_buy_volume = klines.taker_buy_volume[window_start:end]
        log_returns = np.diff(np.log(close))
        realized_volatility = float(np.sqrt(np.sum(log_returns * log_returns)))
        total_volume = float(np.sum(volume))
        vwap = float(np.sum(quote_volume) / total_volume) if total_volume > 0.0 else latest_close
        values.update(
            {
                f"binance_spot_return_{seconds}s": float(np.log(latest_close / close[0])),
                f"binance_spot_rv_{seconds}s": realized_volatility,
                f"binance_spot_flow_{seconds}s": (
                    float(np.sum((2.0 * taker_buy_volume) - volume) / total_volume)
                    if total_volume > 0.0
                    else 0.0
                ),
                f"binance_spot_volume_{seconds}s": total_volume,
                f"binance_spot_vwap_distance_{seconds}s": (latest_close / vwap) - 1.0,
            }
        )
        if seconds == 300:
            rv_300 = realized_volatility
    boundary_return = log(latest_close / opening_reference)
    remaining_seconds = 900.0 - elapsed_seconds
    remaining_sigma = rv_300 * sqrt(remaining_seconds / 300.0) if rv_300 > 0.0 else 0.0
    boundary_z = boundary_return / remaining_sigma if remaining_sigma > 0.0 else 0.0
    p_boundary_up = 0.5 * (1.0 + erf(boundary_z / sqrt(2.0))) if remaining_sigma > 0.0 else 0.5
    values.update(
        {
            "elapsed_seconds": float(elapsed_seconds),
            "remaining_seconds": remaining_seconds,
            "opening_proxy_log_return": boundary_return,
            "remaining_sigma": remaining_sigma,
            "boundary_z": boundary_z,
            "p_boundary_up": max(1e-6, min(1.0 - 1e-6, p_boundary_up)),
            "data_age_seconds": max(
                0.0,
                (decision_ns - available_ts_ns[end - 1]) / _NANOS_PER_SECOND,
            ),
        }
    )
    return schema.vector_from(values)


def _has_full_history(
    *,
    available_ts_ns: np.ndarray,
    start: int,
    decision_ns: int,
    max_window_ns: int,
    interval_ns: int,
) -> bool:
    return available_ts_ns[start] <= decision_ns - max_window_ns + interval_ns


def _snapshot_offsets(
    *, snapshot_seconds: int, entry_start_seconds: int, entry_end_seconds: int
) -> tuple[int, ...]:
    first = ((entry_start_seconds + snapshot_seconds - 1) // snapshot_seconds) * snapshot_seconds
    return tuple(range(first, entry_end_seconds + 1, snapshot_seconds))


def _windows_for_interval(interval_seconds: int) -> tuple[int, ...]:
    if interval_seconds == 1:
        return _WINDOWS_SECONDS
    if interval_seconds == 60:
        return tuple(value for value in _WINDOWS_SECONDS if value >= 60)
    raise ValueError("supported kline intervals are 1 second and 1 minute")


def _validate_timing(
    *,
    snapshot_seconds: int,
    entry_start_seconds: int,
    entry_end_seconds: int,
    interval_seconds: int,
    availability_delay: timedelta,
) -> None:
    if (
        snapshot_seconds < interval_seconds
        or snapshot_seconds % interval_seconds != 0
        or entry_start_seconds <= 0
        or entry_end_seconds < entry_start_seconds
        or entry_end_seconds >= 900
    ):
        raise ValueError("opening proxy snapshot and entry timing are invalid")
    if availability_delay < timedelta(0):
        raise ValueError("availability_delay must be non-negative")


def _datetime_to_ns(value: datetime) -> int:
    return int(value.timestamp() * _NANOS_PER_SECOND)


__all__ = [
    "OpeningProxyDatasetBuild",
    "build_opening_proxy_dataset",
    "opening_proxy_feature_schema",
]
