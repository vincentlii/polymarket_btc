"""Logistic residual probabilities anchored to an eligible Polymarket pair."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

import numpy as np
from scipy.optimize import minimize

from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models.direction import CalibrationComparison, _fit_calibrator
from btc_short_horizon.strategy.types import TokenSide


_EPSILON = 1e-6


def relative_probability(
    market_up_probability: np.ndarray,
    residual_logit: np.ndarray,
) -> np.ndarray:
    anchor = _probabilities(market_up_probability, name="market_up_probability")
    residual = np.asarray(residual_logit, dtype=float)
    if residual.ndim != 1 or len(residual) != len(anchor) or not np.isfinite(residual).all():
        raise ValueError("residual_logit must be a finite vector aligned with the market anchor")
    logits = np.log(anchor / (1.0 - anchor)) + residual
    return np.clip(_sigmoid(logits), _EPSILON, 1.0 - _EPSILON)


@dataclass(frozen=True, slots=True)
class ProbabilityInterval:
    up_lower: float
    up_point: float
    up_upper: float

    def __post_init__(self) -> None:
        values = (self.up_lower, self.up_point, self.up_upper)
        if any(not isfinite(value) or not 0.0 < value < 1.0 for value in values):
            raise ValueError("probability interval values must be finite and in (0, 1)")
        if not self.up_lower <= self.up_point <= self.up_upper:
            raise ValueError("probability interval bounds must contain the point estimate")

    def point_fair(self, side: TokenSide | str) -> float:
        normalized = TokenSide(side)
        return self.up_point if normalized is TokenSide.UP else 1.0 - self.up_point

    def robust_fair(self, side: TokenSide | str) -> float:
        normalized = TokenSide(side)
        return self.up_lower if normalized is TokenSide.UP else 1.0 - self.up_upper


@dataclass(frozen=True, slots=True)
class FittedMarketRelativeOffsetModel:
    schema: FeatureSchema
    feature_mean: np.ndarray
    feature_scale: np.ndarray
    intercept: float
    coefficients: np.ndarray
    parameter_covariance: np.ndarray
    calibrator: object
    calibration_selection: CalibrationComparison
    calibration_bias: float

    def predict_raw_up_probability(
        self, vectors: np.ndarray, market_up_probability: np.ndarray
    ) -> np.ndarray:
        matrix = _matrix(vectors, schema=self.schema)
        anchor = _probabilities(market_up_probability, name="market_up_probability")
        if len(matrix) != len(anchor):
            raise ValueError("market anchor must align one-to-one with feature vectors")
        standardized = (matrix - self.feature_mean) / self.feature_scale
        residual = self.intercept + standardized @ self.coefficients
        return relative_probability(anchor, residual)

    def predict_up_probability(
        self, vectors: np.ndarray, market_up_probability: np.ndarray
    ) -> np.ndarray:
        raw = self.predict_raw_up_probability(vectors, market_up_probability)
        transformed = np.asarray(self.calibrator.transform(raw), dtype=float)
        return _probabilities(transformed, name="calibrated probability")

    def predict_probability_intervals(
        self,
        vectors: np.ndarray,
        market_up_probability: np.ndarray,
        *,
        z_value: float = 1.96,
    ) -> tuple[ProbabilityInterval, ...]:
        if not isfinite(z_value) or z_value <= 0.0:
            raise ValueError("z_value must be finite and > 0")
        matrix = _matrix(vectors, schema=self.schema)
        point = self.predict_up_probability(matrix, market_up_probability)
        standardized = (matrix - self.feature_mean) / self.feature_scale
        design = np.column_stack((np.ones(len(matrix)), standardized))
        logit_variance = np.einsum(
            "ij,jk,ik->i", design, self.parameter_covariance, design, optimize=True
        )
        standard_error = point * (1.0 - point) * np.sqrt(np.maximum(logit_variance, 0.0))
        radius = z_value * standard_error + self.calibration_bias
        return tuple(
            ProbabilityInterval(
                up_lower=float(np.clip(probability - uncertainty, _EPSILON, 1.0 - _EPSILON)),
                up_point=float(probability),
                up_upper=float(np.clip(probability + uncertainty, _EPSILON, 1.0 - _EPSILON)),
            )
            for probability, uncertainty in zip(point, radius, strict=True)
        )


def fit_market_relative_offset_model(
    *,
    train_vectors: np.ndarray,
    train_labels: np.ndarray,
    train_market_up_probability: np.ndarray,
    calibration_vectors: np.ndarray,
    calibration_labels: np.ndarray,
    calibration_market_up_probability: np.ndarray,
    schema: FeatureSchema,
    logistic_c: float = 0.1,
    train_weights: np.ndarray | None = None,
    calibration_weights: np.ndarray | None = None,
    random_seed: int = 17,
) -> FittedMarketRelativeOffsetModel:
    if isinstance(logistic_c, bool) or not isfinite(logistic_c) or logistic_c <= 0.0:
        raise ValueError("logistic_c must be finite and > 0")
    train_matrix = _matrix(train_vectors, schema=schema)
    calibration_matrix = _matrix(calibration_vectors, schema=schema)
    train_target = _labels(train_labels, rows=len(train_matrix), name="train_labels")
    calibration_target = _labels(
        calibration_labels, rows=len(calibration_matrix), name="calibration_labels"
    )
    train_anchor = _probabilities(train_market_up_probability, name="train_market_up_probability")
    calibration_anchor = _probabilities(
        calibration_market_up_probability, name="calibration_market_up_probability"
    )
    if len(train_anchor) != len(train_matrix) or len(calibration_anchor) != len(calibration_matrix):
        raise ValueError("market anchors must align with their feature partitions")
    train_weight = _weights(train_weights, rows=len(train_matrix), name="train_weights")
    calibration_weight = _weights(
        calibration_weights, rows=len(calibration_matrix), name="calibration_weights"
    )
    feature_mean = np.average(train_matrix, axis=0, weights=train_weight)
    variance = np.average((train_matrix - feature_mean) ** 2, axis=0, weights=train_weight)
    feature_scale = np.sqrt(variance)
    feature_scale[feature_scale <= 1e-12] = 1.0
    standardized = (train_matrix - feature_mean) / feature_scale
    design = np.column_stack((np.ones(len(standardized)), standardized))
    anchor_logits = np.log(train_anchor / (1.0 - train_anchor))
    regularization = 1.0 / logistic_c

    def objective(parameters: np.ndarray) -> float:
        probabilities = _sigmoid(anchor_logits + design @ parameters)
        losses = -(
            train_target * np.log(np.clip(probabilities, _EPSILON, 1.0))
            + (1.0 - train_target) * np.log(np.clip(1.0 - probabilities, _EPSILON, 1.0))
        )
        penalty = 0.5 * regularization * float(parameters[1:] @ parameters[1:])
        return float(np.average(losses, weights=train_weight)) + penalty / len(train_matrix)

    result = minimize(
        objective,
        x0=np.zeros(design.shape[1], dtype=float),
        method="L-BFGS-B",
    )
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError(f"market-relative logistic fit failed: {result.message}")
    fitted_train = _sigmoid(anchor_logits + design @ result.x)
    hessian_weights = train_weight * fitted_train * (1.0 - fitted_train)
    hessian = design.T @ (design * hessian_weights[:, None])
    hessian[1:, 1:] += np.eye(design.shape[1] - 1) * regularization
    covariance = np.linalg.pinv(hessian)
    calibration_standardized = (calibration_matrix - feature_mean) / feature_scale
    calibration_residual = result.x[0] + calibration_standardized @ result.x[1:]
    raw_calibration = relative_probability(calibration_anchor, calibration_residual)
    calibrator, comparison = _fit_calibrator(
        raw_probabilities=raw_calibration,
        labels=calibration_target,
        sample_weights=calibration_weight,
        method="auto",
        random_seed=random_seed,
        min_isotonic_calibration_samples=2_000,
        temperature_grid=(1.0,),
        independent_sample_count=None,
    )
    if comparison is None:
        raise RuntimeError(
            "guarded market-relative calibration did not produce comparison evidence"
        )
    calibrated = np.asarray(calibrator.transform(raw_calibration), dtype=float)
    calibration_bias = abs(
        float(np.average(calibration_target - calibrated, weights=calibration_weight))
    )
    return FittedMarketRelativeOffsetModel(
        schema=schema,
        feature_mean=feature_mean,
        feature_scale=feature_scale,
        intercept=float(result.x[0]),
        coefficients=np.asarray(result.x[1:], dtype=float),
        parameter_covariance=np.asarray(covariance, dtype=float),
        calibrator=calibrator,
        calibration_selection=comparison,
        calibration_bias=calibration_bias,
    )


def _matrix(values: np.ndarray, *, schema: FeatureSchema) -> np.ndarray:
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or not len(matrix) or matrix.shape[1] != len(schema.names):
        raise ValueError("feature matrix must be non-empty and match the schema")
    if not np.isfinite(matrix).all():
        raise ValueError("feature matrix must be finite")
    return matrix


def _labels(values: np.ndarray, *, rows: int, name: str) -> np.ndarray:
    labels = np.asarray(values)
    if labels.ndim != 1 or len(labels) != rows or set(labels.tolist()) != {0, 1}:
        raise ValueError(f"{name} must align with rows and contain both classes")
    return labels.astype(float)


def _probabilities(values: np.ndarray, *, name: str) -> np.ndarray:
    probabilities = np.asarray(values, dtype=float)
    if (
        probabilities.ndim != 1
        or not len(probabilities)
        or not np.isfinite(probabilities).all()
        or np.any((probabilities <= 0.0) | (probabilities >= 1.0))
    ):
        raise ValueError(f"{name} must be a finite non-empty vector in (0, 1)")
    return probabilities


def _weights(values: np.ndarray | None, *, rows: int, name: str) -> np.ndarray:
    weights = np.ones(rows, dtype=float) if values is None else np.asarray(values, dtype=float)
    if (
        weights.ndim != 1
        or len(weights) != rows
        or not np.isfinite(weights).all()
        or np.any(weights <= 0.0)
    ):
        raise ValueError(f"{name} must contain finite positive values aligned with rows")
    return weights


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -700.0, 700.0)
    return 1.0 / (1.0 + np.exp(-clipped))


__all__ = [
    "FittedMarketRelativeOffsetModel",
    "ProbabilityInterval",
    "fit_market_relative_offset_model",
    "relative_probability",
]
