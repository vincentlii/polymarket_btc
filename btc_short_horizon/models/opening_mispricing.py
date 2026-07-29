"""Time-aware fair-probability model for the first three minutes after open."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping

import numpy as np

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models.direction import (
    DirectionModelConfig,
    FittedDirectionModel,
    fit_direction_model,
)


def _probability(name: str, value: float) -> None:
    if not isfinite(value) or not 0.0 < value < 1.0:
        raise ValueError(f"{name} must be finite and in (0, 1)")


@dataclass(frozen=True, slots=True)
class OpeningMispricingPrediction:
    """One causally available fair-probability estimate for a BTC 15m market."""

    market_slug: str
    model_version: str
    feature_schema_hash: str
    market_window_start_ts_ns: int
    trigger_ts_ns: int
    p_up: float
    p_boundary_up: float
    p_market_mid_up: float
    data_age_seconds: float
    has_data_gap: bool = False
    structure_valid: bool = True
    tick_unchanged: bool = True
    fee_unchanged: bool = True
    latency_healthy: bool = True

    def __post_init__(self) -> None:
        if not self.market_slug or not self.model_version or not self.feature_schema_hash:
            raise ValueError("market_slug, model_version, and feature_schema_hash are required")
        if (
            self.market_window_start_ts_ns < 0
            or self.trigger_ts_ns < self.market_window_start_ts_ns
        ):
            raise ValueError("prediction timestamps must be ordered and non-negative")
        for name, value in (
            ("p_up", self.p_up),
            ("p_boundary_up", self.p_boundary_up),
            ("p_market_mid_up", self.p_market_mid_up),
        ):
            _probability(name, value)
        if not isfinite(self.data_age_seconds) or self.data_age_seconds < 0.0:
            raise ValueError("data_age_seconds must be finite and >= 0")

    @property
    def elapsed_seconds(self) -> float:
        return (self.trigger_ts_ns - self.market_window_start_ts_ns) / 1_000_000_000


@dataclass(frozen=True, slots=True)
class FittedOpeningMispricingModel:
    """A calibrated fair-probability model with no dependency on pre-open output."""

    model: FittedDirectionModel

    @property
    def schema(self) -> FeatureSchema:
        return self.model.schema

    def predict(
        self,
        *,
        market_slug: str,
        model_version: str,
        market_window_start_ts_ns: int,
        trigger_ts_ns: int,
        feature_values: Mapping[str, float],
        p_boundary_up: float,
        p_market_mid_up: float,
        data_age_seconds: float,
        has_data_gap: bool = False,
        structure_valid: bool = True,
        tick_unchanged: bool = True,
        fee_unchanged: bool = True,
        latency_healthy: bool = True,
    ) -> OpeningMispricingPrediction:
        vector = np.asarray([self.schema.vector_from(feature_values)], dtype=float)
        probability = float(self.model.predict_up_probability(vector)[0])
        return OpeningMispricingPrediction(
            market_slug=market_slug,
            model_version=model_version,
            feature_schema_hash=self.schema.hash,
            market_window_start_ts_ns=market_window_start_ts_ns,
            trigger_ts_ns=trigger_ts_ns,
            p_up=probability,
            p_boundary_up=p_boundary_up,
            p_market_mid_up=p_market_mid_up,
            data_age_seconds=data_age_seconds,
            has_data_gap=has_data_gap,
            structure_valid=structure_valid,
            tick_unchanged=tick_unchanged,
            fee_unchanged=fee_unchanged,
            latency_healthy=latency_healthy,
        )


def fit_opening_mispricing_model(
    *,
    train_vectors: np.ndarray,
    train_labels: np.ndarray,
    calibration_vectors: np.ndarray,
    calibration_labels: np.ndarray,
    schema: FeatureSchema,
    config: DirectionModelConfig | None = None,
    train_weights: np.ndarray | None = None,
    calibration_weights: np.ndarray | None = None,
    early_stopping_vectors: np.ndarray | None = None,
    early_stopping_labels: np.ndarray | None = None,
    early_stopping_weights: np.ndarray | None = None,
    calibration_independent_sample_count: int | None = None,
) -> FittedOpeningMispricingModel:
    """Fit only on leakage-free opening snapshots supplied by the research pipeline."""

    return FittedOpeningMispricingModel(
        model=fit_direction_model(
            train_vectors=train_vectors,
            train_labels=train_labels,
            calibration_vectors=calibration_vectors,
            calibration_labels=calibration_labels,
            schema=schema,
            config=config,
            train_weights=train_weights,
            calibration_weights=calibration_weights,
            early_stopping_vectors=early_stopping_vectors,
            early_stopping_labels=early_stopping_labels,
            early_stopping_weights=early_stopping_weights,
            calibration_independent_sample_count=calibration_independent_sample_count,
        )
    )
