"""Fast, explicitly limited historical proxy for post-open BTC fair probability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import erf, log, sqrt
from numbers import Integral
from typing import Mapping, Sequence

import numpy as np

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY
from btc_short_horizon.data.contracts import MarketOutcome, MarketWindow
from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample


_NANOS_PER_SECOND = 1_000_000_000
_WINDOWS_SECONDS = (5, 15, 30, 60, 180, 300, 900, 1_800, 3_600)


class CausalFeatureUnavailableError(ValueError):
    """Causal kline evidence is temporarily insufficient for one prediction."""


class OpeningRegime(StrEnum):
    """Named slices of the frozen post-open three-minute research protocol."""

    EARLY = "early_3s_to_30s"
    PRICE_DISCOVERY = "price_discovery_35s_to_90s"
    MID_EARLY = "mid_early_95s_to_180s"


OPENING_REGIMES = tuple(OpeningRegime)


def opening_regime_for_elapsed_seconds(elapsed_seconds: float) -> OpeningRegime:
    """Return the pre-registered regime for one 3--180 second decision."""

    if 3.0 <= elapsed_seconds <= 30.0:
        return OpeningRegime.EARLY
    if 35.0 <= elapsed_seconds <= 90.0:
        return OpeningRegime.PRICE_DISCOVERY
    if 95.0 <= elapsed_seconds <= 180.0:
        return OpeningRegime.MID_EARLY
    raise ValueError("elapsed_seconds falls outside the frozen three-minute protocol")


def opening_proxy_protocol(
    *,
    entry_start_seconds: int,
    entry_end_seconds: int,
    snapshot_seconds: int,
) -> dict[str, object]:
    """Return serializable timing metadata that makes an artifact runnable safely."""

    offsets_ms = opening_proxy_decision_offsets_ms(
        cadence_ms=snapshot_seconds * 1_000,
        entry_start_seconds=entry_start_seconds,
        entry_end_seconds=entry_end_seconds,
    )
    regimes: list[dict[str, object]] = []
    for regime in OPENING_REGIMES:
        regime_offsets = [
            offset_ms // 1_000
            for offset_ms in offsets_ms
            if opening_regime_for_elapsed_seconds(offset_ms / 1_000) is regime
        ]
        if regime_offsets:
            regimes.append(
                {
                    "name": regime.value,
                    "start_seconds": min(regime_offsets),
                    "end_seconds": max(regime_offsets),
                }
            )
    return {
        "version": 2,
        "entry_start_seconds": entry_start_seconds,
        "entry_end_seconds": entry_end_seconds,
        "snapshot_seconds": snapshot_seconds,
        "decision_offsets_ms": list(offsets_ms),
        "regimes": regimes,
    }


def validate_opening_proxy_protocol(
    metadata_config: Mapping[str, object], *, expected: Mapping[str, object]
) -> None:
    """Fail closed when a model artifact was trained for another timing protocol."""

    observed = metadata_config.get("opening_proxy_protocol")
    if observed != dict(expected):
        raise ValueError("model artifact opening-proxy protocol does not match this runtime")


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


def opening_proxy_decision_offsets_ms(
    *,
    cadence_ms: int,
    entry_start_seconds: int,
    entry_end_seconds: int,
) -> tuple[int, ...]:
    """Return cadence-aligned offsets anchored to market open."""

    if isinstance(cadence_ms, bool) or not isinstance(cadence_ms, Integral) or cadence_ms < 1:
        raise ValueError("cadence_ms must be >= 1")
    if (
        any(
            isinstance(value, bool) or not isinstance(value, Integral)
            for value in (entry_start_seconds, entry_end_seconds)
        )
        or entry_start_seconds < 0
        or entry_end_seconds < entry_start_seconds
        or entry_end_seconds > 180
    ):
        raise ValueError("entry window is invalid")
    start_ms = entry_start_seconds * 1_000
    end_ms = entry_end_seconds * 1_000
    first_ms = ((start_ms + cadence_ms - 1) // cadence_ms) * cadence_ms
    offsets = tuple(range(first_ms, end_ms + 1, cadence_ms))
    if not offsets:
        raise ValueError("entry window contains no cadence-aligned decision")
    return offsets


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

    ordered_markets = tuple(sorted(markets, key=lambda item: (item.t0, item.slug)))
    if not ordered_markets:
        raise ValueError("markets must not be empty")
    if len({market.slug for market in ordered_markets}) != len(ordered_markets):
        raise ValueError("market slugs must be unique")
    if any(market.family != BTC_15M_MARKET_FAMILY for market in ordered_markets):
        raise ValueError("opening proxy datasets require the BTC 15m market family")
    if len({market.rule_epoch for market in ordered_markets}) != 1:
        raise ValueError("one opening proxy dataset cannot mix market rule epochs")
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

    for market in ordered_markets:
        if market.resolution not in {MarketOutcome.UP, MarketOutcome.DOWN}:
            excluded_void += 1
            continue
        if market.label_available_ts is None:
            raise ValueError(f"resolved market {market.slug!r} is missing label availability")
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
        requested_markets=len(ordered_markets),
        excluded_void_markets=excluded_void,
        excluded_insufficient_history=excluded_history,
        excluded_kline_gaps=excluded_gaps,
        snapshots_per_market=len(snapshot_offsets),
    )


def opening_proxy_feature_values_at(
    *,
    klines: BinanceKlineHistory,
    market_start: datetime,
    decision_time: datetime,
    availability_delay: timedelta = timedelta(seconds=1),
) -> dict[str, float]:
    """Build one runtime vector with the exact historical proxy semantics."""

    if market_start.tzinfo is None or decision_time.tzinfo is None:
        raise ValueError("market_start and decision_time must be timezone-aware")
    if availability_delay < timedelta(0):
        raise ValueError("availability_delay must be non-negative")
    market_start_ns = _datetime_to_ns(market_start)
    decision_ns = _datetime_to_ns(decision_time)
    elapsed_seconds = (decision_ns - market_start_ns) / _NANOS_PER_SECOND
    if elapsed_seconds <= 0.0 or elapsed_seconds >= 900.0:
        raise ValueError("decision_time must fall inside the BTC 15m market window")
    interval_ns = klines.interval_seconds * _NANOS_PER_SECOND
    available_ts_ns = (
        klines.open_ts_ns
        + interval_ns
        + round(availability_delay.total_seconds() * _NANOS_PER_SECOND)
    )
    reference_index = int(np.searchsorted(available_ts_ns, market_start_ns, side="right")) - 1
    if reference_index < 0:
        raise CausalFeatureUnavailableError(
            "no causally available opening reference; "
            f"decision_ts_ns={decision_ns} available_tail_ts_ns={int(available_ts_ns[-1])}"
        )
    windows = _windows_for_interval(klines.interval_seconds)
    max_window_ns = max(windows) * _NANOS_PER_SECOND
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
        raise CausalFeatureUnavailableError(
            "incomplete causal Binance history at decision_time; "
            f"decision_ts_ns={decision_ns} available_tail_ts_ns={int(available_ts_ns[-1])}"
        )
    if np.any(np.diff(klines.open_ts_ns[start:end]) != interval_ns):
        raise CausalFeatureUnavailableError(
            "causal Binance history contains a kline gap; "
            f"decision_ts_ns={decision_ns} available_tail_ts_ns={int(available_ts_ns[end - 1])}"
        )
    schema = opening_proxy_feature_schema(klines.interval_seconds)
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
    return schema.mapping_from(vector)


def _feature_vector(
    *,
    klines: BinanceKlineHistory,
    available_ts_ns: np.ndarray,
    start: int,
    end: int,
    reference_index: int,
    decision_ns: int,
    elapsed_seconds: float,
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
        if window_start >= end:
            raise CausalFeatureUnavailableError(
                "incomplete causal Binance history for "
                f"{seconds}s feature window; decision_ts_ns={decision_ns} "
                f"available_tail_ts_ns={int(available_ts_ns[end - 1])}"
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
    return tuple(
        offset_ms // 1_000
        for offset_ms in opening_proxy_decision_offsets_ms(
            cadence_ms=snapshot_seconds * 1_000,
            entry_start_seconds=entry_start_seconds,
            entry_end_seconds=entry_end_seconds,
        )
    )


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
    if any(
        isinstance(value, bool) or not isinstance(value, Integral)
        for value in (
            snapshot_seconds,
            entry_start_seconds,
            entry_end_seconds,
            interval_seconds,
        )
    ) or (
        snapshot_seconds < interval_seconds
        or snapshot_seconds % interval_seconds != 0
        or entry_start_seconds <= 0
        or entry_end_seconds < entry_start_seconds
        or entry_end_seconds > 180
    ):
        raise ValueError("opening proxy snapshot and entry timing are invalid")
    if not isinstance(availability_delay, timedelta) or availability_delay < timedelta(0):
        raise ValueError("availability_delay must be non-negative")


def _datetime_to_ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


__all__ = [
    "CausalFeatureUnavailableError",
    "OPENING_REGIMES",
    "OpeningRegime",
    "OpeningProxyDatasetBuild",
    "build_opening_proxy_dataset",
    "opening_proxy_decision_offsets_ms",
    "opening_proxy_feature_values_at",
    "opening_proxy_feature_schema",
    "opening_proxy_protocol",
    "opening_regime_for_elapsed_seconds",
    "validate_opening_proxy_protocol",
]
