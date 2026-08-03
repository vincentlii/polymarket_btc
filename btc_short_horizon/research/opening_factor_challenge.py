"""Frozen, causal factor families for the BTC opening-proxy challenger."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import log

import numpy as np

from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.binance_history import BinanceKlineHistory
from btc_short_horizon.research.pipeline import DirectionDataset


_NS_PER_SECOND = 1_000_000_000
_FAMILY_NAMES = {
    "boundary_time": (
        "boundary_abs_z",
        "boundary_z_elapsed_fraction",
        "boundary_z_log_remaining",
        "boundary_probability_margin",
    ),
    "path_state": (
        "path_return_5s_minus_30s",
        "path_return_30s_minus_180s",
        "path_return_180s_minus_900s",
        "path_rv_ratio_5s_60s",
        "path_rv_ratio_60s_900s",
        "path_rv_ratio_300s_3600s",
        "path_return_5s_x_180s",
    ),
    "flow_value": (
        "flow_5s_minus_60s",
        "flow_30s_minus_300s",
        "volume_ratio_5s_300s",
        "volume_ratio_30s_900s",
        "vwap_distance_5s_minus_60s",
        "vwap_distance_60s_minus_300s",
        "vwap_distance_300s_minus_3600s",
    ),
    "cross_market": (
        "cross_basis_log",
        "cross_return_1m",
        "cross_return_5m",
        "cross_return_15m",
        "cross_taker_flow_1m",
        "cross_taker_flow_5m",
        "cross_taker_flow_15m",
        "cross_basis_change_5m",
        "cross_basis_change_15m",
    ),
}


class OpeningFactorFamily(StrEnum):
    BOUNDARY_TIME = "boundary_time"
    PATH_STATE = "path_state"
    FLOW_VALUE = "flow_value"
    CROSS_MARKET = "cross_market"


@dataclass(frozen=True, slots=True)
class OpeningFactorDatasetBuild:
    paired_control_dataset: DirectionDataset
    factor_dataset: DirectionDataset
    eligible_market_count: int
    excluded_incomplete_cross_market: int
    families: tuple[OpeningFactorFamily, ...]


def opening_factor_feature_schema(
    *, base_schema: FeatureSchema, families: Sequence[OpeningFactorFamily]
) -> FeatureSchema:
    normalized = _normalize_families(families)
    names = tuple(name for family in normalized for name in _FAMILY_NAMES[family.value])
    return FeatureSchema(
        version=f"btc-opening-factor-challenge-v1:{base_schema.hash}:{'+'.join(item.value for item in normalized)}",
        names=base_schema.names + names,
    )


def build_opening_factor_dataset(
    *,
    control: DirectionDataset,
    spot_klines: BinanceKlineHistory | None = None,
    perp_klines: BinanceKlineHistory | None = None,
    families: Sequence[OpeningFactorFamily] = tuple(OpeningFactorFamily),
) -> OpeningFactorDatasetBuild:
    """Append frozen factors and drop each cross-market-incomplete market wholesale."""

    normalized = _normalize_families(families)
    names = {name: index for index, name in enumerate(control.schema.names)}
    required = set(
        sum(
            (
                list(_base_features(family))
                for family in normalized
                if family is not OpeningFactorFamily.CROSS_MARKET
            ),
            [],
        )
    )
    if not required.issubset(names):
        raise ValueError("control schema lacks required opening factor base features")
    needs_cross = OpeningFactorFamily.CROSS_MARKET in normalized
    if needs_cross and (spot_klines is None or perp_klines is None):
        raise ValueError("cross-market factors require both spot and perp 1-minute histories")
    if needs_cross and (spot_klines.interval_seconds != 60 or perp_klines.interval_seconds != 60):
        raise ValueError("cross-market factors require one-minute kline histories")

    by_group: dict[str, list[int]] = {}
    for index, sample in enumerate(control.samples):
        by_group.setdefault(sample.group_id, []).append(index)
    accepted: list[int] = []
    vectors: dict[int, tuple[float, ...]] = {}
    excluded = 0
    for group_indices in by_group.values():
        cross_by_index: dict[int, tuple[float, ...]] = {}
        if needs_cross:
            assert spot_klines is not None and perp_klines is not None
            for index in group_indices:
                value = _cross_market_values(
                    sample_ts=control.samples[index].feature_ts, spot=spot_klines, perp=perp_klines
                )
                if value is None:
                    excluded += 1
                    break
                cross_by_index[index] = value
            else:
                pass
            if len(cross_by_index) != len(group_indices):
                continue
        for index in group_indices:
            base = control.vectors[index]
            values = _factor_values(base=base, names=names, cross=cross_by_index.get(index))
            appended = tuple(value for family in normalized for value in values[family])
            if not np.isfinite(appended).all():
                raise ValueError("opening factor values must be finite")
            vectors[index] = appended
            accepted.append(index)
    if not accepted:
        raise ValueError("no markets have complete causal cross-market history")
    indices = np.asarray(accepted, dtype=int)
    samples = tuple(control.samples[index] for index in indices)
    weights = control.sample_weights[indices]
    paired = DirectionDataset(
        samples=samples,
        vectors=control.vectors[indices],
        schema=control.schema,
        sample_weights=weights,
    )
    factor_dataset = DirectionDataset(
        samples=samples,
        vectors=np.column_stack(
            (control.vectors[indices], np.asarray([vectors[int(index)] for index in indices]))
        ),
        schema=opening_factor_feature_schema(base_schema=control.schema, families=normalized),
        sample_weights=weights,
    )
    return OpeningFactorDatasetBuild(
        paired_control_dataset=paired,
        factor_dataset=factor_dataset,
        eligible_market_count=len({sample.group_id for sample in samples}),
        excluded_incomplete_cross_market=excluded,
        families=normalized,
    )


def _normalize_families(families: Sequence[OpeningFactorFamily]) -> tuple[OpeningFactorFamily, ...]:
    converted = tuple(OpeningFactorFamily(item) for item in families)
    if not converted or len(set(converted)) != len(converted):
        raise ValueError("opening factor families must be non-empty and unique")
    return tuple(family for family in OpeningFactorFamily if family in converted)


def _base_features(family: OpeningFactorFamily) -> tuple[str, ...]:
    if family is OpeningFactorFamily.BOUNDARY_TIME:
        return ("boundary_z", "elapsed_seconds", "remaining_seconds", "p_boundary_up")
    if family is OpeningFactorFamily.PATH_STATE:
        return tuple(
            f"binance_spot_{kind}_{seconds}s"
            for kind, seconds in (
                ("return", 5),
                ("return", 30),
                ("return", 180),
                ("return", 900),
                ("rv", 5),
                ("rv", 60),
                ("rv", 300),
                ("rv", 900),
                ("rv", 3600),
            )
        )
    if family is OpeningFactorFamily.FLOW_VALUE:
        return tuple(
            f"binance_spot_{kind}_{seconds}s"
            for kind, seconds in (
                ("flow", 5),
                ("flow", 30),
                ("flow", 60),
                ("flow", 300),
                ("volume", 5),
                ("volume", 30),
                ("volume", 300),
                ("volume", 900),
                ("vwap_distance", 5),
                ("vwap_distance", 60),
                ("vwap_distance", 300),
                ("vwap_distance", 3600),
            )
        )
    return ()


def _factor_values(
    *, base: np.ndarray, names: dict[str, int], cross: tuple[float, ...] | None
) -> dict[OpeningFactorFamily, tuple[float, ...]]:
    def value(name: str) -> float:
        return float(base[names[name]])

    remaining = max(1.0, value("remaining_seconds"))
    z = value("boundary_z")
    return {
        OpeningFactorFamily.BOUNDARY_TIME: (
            abs(z),
            z * (value("elapsed_seconds") / 900.0),
            z * log(remaining / 900.0),
            value("p_boundary_up") - 0.5,
        ),
        OpeningFactorFamily.PATH_STATE: (
            value("binance_spot_return_5s") - value("binance_spot_return_30s"),
            value("binance_spot_return_30s") - value("binance_spot_return_180s"),
            value("binance_spot_return_180s") - value("binance_spot_return_900s"),
            _ratio(value("binance_spot_rv_5s"), value("binance_spot_rv_60s")),
            _ratio(value("binance_spot_rv_60s"), value("binance_spot_rv_900s")),
            _ratio(value("binance_spot_rv_300s"), value("binance_spot_rv_3600s")),
            value("binance_spot_return_5s") * value("binance_spot_return_180s"),
        ),
        OpeningFactorFamily.FLOW_VALUE: (
            value("binance_spot_flow_5s") - value("binance_spot_flow_60s"),
            value("binance_spot_flow_30s") - value("binance_spot_flow_300s"),
            _ratio(value("binance_spot_volume_5s"), value("binance_spot_volume_300s")),
            _ratio(value("binance_spot_volume_30s"), value("binance_spot_volume_900s")),
            value("binance_spot_vwap_distance_5s") - value("binance_spot_vwap_distance_60s"),
            value("binance_spot_vwap_distance_60s") - value("binance_spot_vwap_distance_300s"),
            value("binance_spot_vwap_distance_300s") - value("binance_spot_vwap_distance_3600s"),
        ),
        OpeningFactorFamily.CROSS_MARKET: cross or (),
    }


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if abs(denominator) > 1e-12 else 0.0


def _cross_market_values(
    *, sample_ts: datetime, spot: BinanceKlineHistory, perp: BinanceKlineHistory
) -> tuple[float, ...] | None:
    if sample_ts.tzinfo is None or sample_ts.utcoffset() is None:
        raise ValueError("sample timestamp must be timezone-aware")
    decision = _datetime_ns(sample_ts)
    spot_index = _last_available_index(spot, decision)
    perp_index = _last_available_index(perp, decision)
    if spot_index is None or perp_index is None or spot_index < 15 or perp_index < 15:
        return None
    if spot.open_ts_ns[spot_index] != perp.open_ts_ns[perp_index]:
        return None
    if not (
        _contiguous(spot, spot_index - 15, spot_index)
        and _contiguous(perp, perp_index - 15, perp_index)
    ):
        return None

    def basis(offset: int) -> float:
        return log(float(perp.close[perp_index - offset]) / float(spot.close[spot_index - offset]))

    def ret(history: BinanceKlineHistory, index: int, minutes: int) -> float:
        return log(float(history.close[index]) / float(history.close[index - minutes]))

    def flow(history: BinanceKlineHistory, index: int, minutes: int) -> float:
        volume = float(np.sum(history.volume[index - minutes + 1 : index + 1]))
        return (
            float(
                np.sum(
                    (2.0 * history.taker_buy_volume[index - minutes + 1 : index + 1])
                    - history.volume[index - minutes + 1 : index + 1]
                )
                / volume
            )
            if volume > 0.0
            else 0.0
        )

    current = basis(0)
    return (
        current,
        *(
            ret(perp, perp_index, minutes) - ret(spot, spot_index, minutes)
            for minutes in (1, 5, 15)
        ),
        *(
            flow(perp, perp_index, minutes) - flow(spot, spot_index, minutes)
            for minutes in (1, 5, 15)
        ),
        current - basis(5),
        current - basis(15),
    )


def _last_available_index(history: BinanceKlineHistory, decision_ns: int) -> int | None:
    availability = history.open_ts_ns + (history.interval_seconds + 1) * _NS_PER_SECOND
    index = int(np.searchsorted(availability, decision_ns, side="right")) - 1
    return index if index >= 0 else None


def _contiguous(history: BinanceKlineHistory, start: int, end: int) -> bool:
    return np.all(
        np.diff(history.open_ts_ns[start : end + 1]) == history.interval_seconds * _NS_PER_SECOND
    )


def _datetime_ns(value: datetime) -> int:
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


__all__ = [
    "OpeningFactorDatasetBuild",
    "OpeningFactorFamily",
    "build_opening_factor_dataset",
    "opening_factor_feature_schema",
]
