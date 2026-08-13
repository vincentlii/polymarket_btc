from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite
from numbers import Integral, Real
from typing import Literal, Protocol

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from scipy.optimize import minimize

from btc_short_horizon.features.schema import FeatureSchema

ModelKind = Literal["logistic", "lightgbm"]
CalibrationMethod = Literal["identity", "sigmoid", "isotonic", "temperature", "beta", "auto"]

_PROBABILITY_EPSILON = 1e-6


class _Calibrator(Protocol):
    def transform(self, probabilities: np.ndarray) -> np.ndarray: ...


@dataclass(frozen=True, slots=True)
class DirectionModelConfig:
    kind: ModelKind = "logistic"
    calibration_method: CalibrationMethod = "auto"
    random_seed: int = 17
    logistic_c: float = 1.0
    lightgbm_num_leaves: int = 15
    lightgbm_max_depth: int = 4
    lightgbm_min_child_samples: int = 100
    lightgbm_learning_rate: float = 0.03
    lightgbm_n_estimators: int = 300
    lightgbm_early_stopping_rounds: int = 50
    min_isotonic_calibration_samples: int = 2_000
    temperature_grid: tuple[float, ...] = (0.5, 0.75, 1.0, 1.5, 2.0)

    def __post_init__(self) -> None:
        if self.kind not in {"logistic", "lightgbm"}:
            raise ValueError(f"unsupported model kind: {self.kind!r}")
        if self.calibration_method not in {
            "identity",
            "sigmoid",
            "isotonic",
            "temperature",
            "beta",
            "auto",
        }:
            raise ValueError(f"unsupported calibration method: {self.calibration_method!r}")
        for name in (
            "random_seed",
            "lightgbm_num_leaves",
            "lightgbm_max_depth",
            "lightgbm_min_child_samples",
            "lightgbm_n_estimators",
            "lightgbm_early_stopping_rounds",
            "min_isotonic_calibration_samples",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral):
                raise ValueError(f"{name} must be an integer")
        if self.random_seed < 0 or self.random_seed > (2**32 - 1):
            raise ValueError("random_seed must lie in [0, 2**32 - 1]")
        if (
            isinstance(self.logistic_c, bool)
            or not isinstance(self.logistic_c, Real)
            or self.logistic_c <= 0.0
            or not isfinite(self.logistic_c)
        ):
            raise ValueError("logistic_c must be finite and > 0")
        if self.lightgbm_num_leaves < 2:
            raise ValueError("lightgbm_num_leaves must be >= 2")
        if self.lightgbm_max_depth < 1:
            raise ValueError("lightgbm_max_depth must be >= 1")
        if (self.lightgbm_num_leaves - 1).bit_length() > self.lightgbm_max_depth:
            raise ValueError("lightgbm_num_leaves must be <= 2 ** lightgbm_max_depth")
        if self.lightgbm_min_child_samples < 1:
            raise ValueError("lightgbm_min_child_samples must be >= 1")
        if (
            isinstance(self.lightgbm_learning_rate, bool)
            or not isinstance(self.lightgbm_learning_rate, Real)
            or self.lightgbm_learning_rate <= 0.0
            or not isfinite(self.lightgbm_learning_rate)
        ):
            raise ValueError("lightgbm_learning_rate must be finite and > 0")
        if self.lightgbm_n_estimators < 1:
            raise ValueError("lightgbm_n_estimators must be >= 1")
        if self.lightgbm_early_stopping_rounds < 1:
            raise ValueError("lightgbm_early_stopping_rounds must be >= 1")
        if self.min_isotonic_calibration_samples < 1:
            raise ValueError("min_isotonic_calibration_samples must be >= 1")
        try:
            temperature_grid = tuple(self.temperature_grid)
        except TypeError as exc:
            raise ValueError("temperature_grid must be an iterable of numbers") from exc
        if not temperature_grid:
            raise ValueError("temperature_grid must not be empty")
        if any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not isfinite(value)
            or value <= 0.0
            for value in temperature_grid
        ):
            raise ValueError("temperature_grid must contain finite values > 0")
        object.__setattr__(
            self, "temperature_grid", tuple(float(value) for value in temperature_grid)
        )


@dataclass(frozen=True, slots=True)
class FittedDirectionModel:
    schema: FeatureSchema
    config: DirectionModelConfig
    estimator: object
    calibrator: _Calibrator
    calibration_selection: CalibrationComparison | None = None

    def predict_raw_up_probability(self, vectors: np.ndarray) -> np.ndarray:
        matrix = _validate_matrix(vectors, schema=self.schema)
        return _predict_positive_probability(
            self.estimator,
            _estimator_matrix(matrix=matrix, schema=self.schema, config=self.config),
        )

    def predict_up_probability(self, vectors: np.ndarray) -> np.ndarray:
        raw = self.predict_raw_up_probability(vectors)
        calibrated = self.calibrator.transform(raw)
        validated = _validate_probability_vector(
            calibrated,
            expected_rows=len(raw),
            name="calibrator probabilities",
        )
        return np.clip(validated, _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON)

    @property
    def config_dict(self) -> dict[str, object]:
        result: dict[str, object] = asdict(self.config)
        if self.calibration_selection is not None:
            result["calibration_selection"] = asdict(self.calibration_selection)
        return result


@dataclass(frozen=True, slots=True)
class _IdentityCalibrator:
    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        return probabilities


@dataclass(frozen=True, slots=True)
class _SigmoidCalibrator:
    estimator: LogisticRegression

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        logits = _probability_logits(probabilities)
        return self.estimator.predict_proba(logits.reshape(-1, 1))[:, 1]


@dataclass(frozen=True, slots=True)
class _IsotonicCalibrator:
    estimator: IsotonicRegression

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        return np.asarray(self.estimator.predict(probabilities), dtype=float)


@dataclass(frozen=True, slots=True)
class _TemperatureCalibrator:
    temperature: float

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        logits = _probability_logits(probabilities)
        return _stable_sigmoid(logits / self.temperature)


@dataclass(frozen=True, slots=True)
class _BetaCalibrator:
    positive_log_coefficient: float
    negative_log_coefficient: float
    intercept: float

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        raw = np.clip(
            np.asarray(probabilities, dtype=float), _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON
        )
        logits = (
            self.positive_log_coefficient * np.log(raw)
            + self.negative_log_coefficient * np.log1p(-raw)
            + self.intercept
        )
        return np.clip(
            _stable_sigmoid(logits),
            _PROBABILITY_EPSILON,
            1.0 - _PROBABILITY_EPSILON,
        )


@dataclass(frozen=True, slots=True)
class CalibrationCandidateScore:
    method: str
    log_loss: float
    brier: float


@dataclass(frozen=True, slots=True)
class CalibrationComparison:
    selected_method: str
    candidates: tuple[CalibrationCandidateScore, ...]


def fit_direction_model(
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
) -> FittedDirectionModel:
    """Fit a direction model from already chronological, leakage-free datasets."""

    effective_config = config or DirectionModelConfig()
    train_matrix = _validate_matrix(train_vectors, schema=schema)
    calibration_matrix = _validate_matrix(calibration_vectors, schema=schema)
    train_target = _validate_binary_labels(
        train_labels, expected_rows=len(train_matrix), name="train_labels"
    )
    calibration_target = _validate_binary_labels(
        calibration_labels,
        expected_rows=len(calibration_matrix),
        name="calibration_labels",
    )
    train_weight_vector = _validate_weights(
        train_weights, expected_rows=len(train_matrix), name="train_weights"
    )
    calibration_weight_vector = _validate_weights(
        calibration_weights, expected_rows=len(calibration_matrix), name="calibration_weights"
    )
    independent_calibration_samples = _validate_independent_sample_count(
        calibration_independent_sample_count,
        maximum_rows=len(calibration_matrix),
    )
    early_stopping_matrix, early_stopping_target, early_stopping_weight_vector = (
        _validate_early_stopping_data(
            vectors=early_stopping_vectors,
            labels=early_stopping_labels,
            weights=early_stopping_weights,
            schema=schema,
        )
    )
    estimator = _fit_estimator(
        train_matrix,
        train_target,
        effective_config,
        schema=schema,
        sample_weights=train_weight_vector,
        early_stopping_matrix=early_stopping_matrix,
        early_stopping_target=early_stopping_target,
        early_stopping_weights=early_stopping_weight_vector,
    )
    raw_calibration = _predict_positive_probability(
        estimator,
        _estimator_matrix(matrix=calibration_matrix, schema=schema, config=effective_config),
    )
    calibrator, calibration_selection = _fit_calibrator(
        raw_probabilities=raw_calibration,
        labels=calibration_target,
        sample_weights=calibration_weight_vector,
        method=effective_config.calibration_method,
        random_seed=effective_config.random_seed,
        min_isotonic_calibration_samples=effective_config.min_isotonic_calibration_samples,
        temperature_grid=effective_config.temperature_grid,
        independent_sample_count=independent_calibration_samples,
    )
    return FittedDirectionModel(
        schema=schema,
        config=effective_config,
        estimator=estimator,
        calibrator=calibrator,
        calibration_selection=calibration_selection,
    )


def _fit_estimator(
    matrix: np.ndarray,
    labels: np.ndarray,
    config: DirectionModelConfig,
    *,
    schema: FeatureSchema,
    sample_weights: np.ndarray | None,
    early_stopping_matrix: np.ndarray | None,
    early_stopping_target: np.ndarray | None,
    early_stopping_weights: np.ndarray | None,
) -> object:
    if config.kind == "logistic":
        return Pipeline(
            steps=(
                ("scaler", StandardScaler()),
                (
                    "model",
                    LogisticRegression(
                        C=config.logistic_c,
                        class_weight=None,
                        max_iter=2_000,
                        random_state=config.random_seed,
                        solver="lbfgs",
                    ),
                ),
            )
        ).fit(
            matrix,
            labels,
            scaler__sample_weight=sample_weights,
            model__sample_weight=sample_weights,
        )

    try:
        from lightgbm import LGBMClassifier, early_stopping
    except ImportError as exc:  # pragma: no cover - dependency is locked in project config
        raise RuntimeError("lightgbm is required for kind='lightgbm'") from exc
    estimator = LGBMClassifier(
        objective="binary",
        class_weight=None,
        learning_rate=config.lightgbm_learning_rate,
        max_depth=config.lightgbm_max_depth,
        min_child_samples=config.lightgbm_min_child_samples,
        n_estimators=config.lightgbm_n_estimators,
        num_leaves=config.lightgbm_num_leaves,
        random_state=config.random_seed,
        verbosity=-1,
    )
    if early_stopping_matrix is None or early_stopping_target is None:
        return estimator.fit(
            _feature_frame(matrix=matrix, schema=schema),
            labels,
            sample_weight=sample_weights,
        )
    return estimator.fit(
        _feature_frame(matrix=matrix, schema=schema),
        labels,
        sample_weight=sample_weights,
        eval_set=[
            (_feature_frame(matrix=early_stopping_matrix, schema=schema), early_stopping_target)
        ],
        eval_sample_weight=[early_stopping_weights],
        eval_metric="binary_logloss",
        callbacks=[early_stopping(config.lightgbm_early_stopping_rounds, verbose=False)],
    )


def _fit_calibrator(
    *,
    raw_probabilities: np.ndarray,
    labels: np.ndarray,
    sample_weights: np.ndarray | None,
    method: CalibrationMethod,
    random_seed: int,
    min_isotonic_calibration_samples: int,
    temperature_grid: tuple[float, ...],
    independent_sample_count: int | None,
) -> tuple[_Calibrator, CalibrationComparison | None]:
    if method == "identity":
        return _IdentityCalibrator(), None
    if len(np.unique(labels)) != 2:
        raise ValueError("calibration labels must contain both outcome classes")
    if method == "sigmoid":
        return (
            _fit_sigmoid_calibrator(
                raw_probabilities=raw_probabilities,
                labels=labels,
                sample_weights=sample_weights,
                random_seed=random_seed,
            ),
            None,
        )
    if method == "beta":
        return (
            _fit_beta_calibrator(
                raw_probabilities=raw_probabilities,
                labels=labels,
                sample_weights=sample_weights,
            ),
            None,
        )
    if method == "auto":
        candidate_list: list[tuple[str, _Calibrator]] = [
            ("identity", _IdentityCalibrator()),
            (
                "sigmoid",
                _fit_sigmoid_calibrator(
                    raw_probabilities=raw_probabilities,
                    labels=labels,
                    sample_weights=sample_weights,
                    random_seed=random_seed,
                ),
            ),
            (
                "beta",
                _fit_beta_calibrator(
                    raw_probabilities=raw_probabilities,
                    labels=labels,
                    sample_weights=sample_weights,
                ),
            ),
            (
                "temperature",
                _fit_temperature_calibrator(
                    raw_probabilities=raw_probabilities,
                    labels=labels,
                    sample_weights=sample_weights,
                    grid=temperature_grid,
                ),
            ),
        ]
        if (
            independent_sample_count is not None
            and independent_sample_count >= min_isotonic_calibration_samples
        ):
            estimator = IsotonicRegression(out_of_bounds="clip")
            estimator.fit(raw_probabilities, labels, sample_weight=sample_weights)
            candidate_list.append(("isotonic", _IsotonicCalibrator(estimator=estimator)))
        return _choose_guarded_calibrator(
            raw_probabilities=raw_probabilities,
            labels=labels,
            sample_weights=sample_weights,
            candidates=tuple(candidate_list),
        )
    if method == "temperature":
        return (
            _fit_temperature_calibrator(
                raw_probabilities=raw_probabilities,
                labels=labels,
                sample_weights=sample_weights,
                grid=temperature_grid,
            ),
            None,
        )
    if independent_sample_count is None:
        raise ValueError(
            "isotonic calibration requires independent calibration samples to be counted explicitly"
        )
    if independent_sample_count < min_isotonic_calibration_samples:
        raise ValueError(
            "isotonic calibration requires at least "
            f"{min_isotonic_calibration_samples} independent calibration samples"
        )
    estimator = IsotonicRegression(out_of_bounds="clip")
    estimator.fit(raw_probabilities, labels, sample_weight=sample_weights)
    return _IsotonicCalibrator(estimator=estimator), None


def _fit_sigmoid_calibrator(
    *,
    raw_probabilities: np.ndarray,
    labels: np.ndarray,
    sample_weights: np.ndarray | None,
    random_seed: int,
) -> _SigmoidCalibrator:
    estimator = LogisticRegression(max_iter=1_000, random_state=random_seed, solver="lbfgs")
    estimator.fit(
        _probability_logits(raw_probabilities).reshape(-1, 1),
        labels,
        sample_weight=sample_weights,
    )
    return _SigmoidCalibrator(estimator=estimator)


def _fit_beta_calibrator(
    *,
    raw_probabilities: np.ndarray,
    labels: np.ndarray,
    sample_weights: np.ndarray | None,
) -> _BetaCalibrator:
    """Fit monotonic beta calibration with constrained coefficients."""

    raw = np.clip(
        np.asarray(raw_probabilities, dtype=float), _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON
    )
    target = np.asarray(labels, dtype=float)
    weights = (
        np.ones(len(target), dtype=float)
        if sample_weights is None
        else np.asarray(sample_weights, dtype=float)
    )

    def objective(parameters: np.ndarray) -> float:
        logits = parameters[0] * np.log(raw) + parameters[1] * np.log1p(-raw) + parameters[2]
        probabilities = np.clip(
            _stable_sigmoid(logits), _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON
        )
        losses = -(target * np.log(probabilities) + (1.0 - target) * np.log1p(-probabilities))
        return float(np.average(losses, weights=weights))

    result = minimize(
        objective,
        x0=np.asarray([1.0, -1.0, 0.0]),
        method="L-BFGS-B",
        bounds=((0.0, None), (None, 0.0), (None, None)),
    )
    if not result.success or not np.isfinite(result.x).all():
        raise RuntimeError(f"beta calibration failed: {result.message}")
    return _BetaCalibrator(
        positive_log_coefficient=float(result.x[0]),
        negative_log_coefficient=float(result.x[1]),
        intercept=float(result.x[2]),
    )


def _choose_guarded_calibrator(
    *,
    raw_probabilities: np.ndarray,
    labels: np.ndarray,
    sample_weights: np.ndarray | None,
    candidates: tuple[tuple[str, _Calibrator], ...],
) -> tuple[_Calibrator, CalibrationComparison]:
    if not candidates or candidates[0][0] != "identity":
        raise ValueError("calibration candidates must begin with identity")
    scored: list[tuple[_Calibrator, CalibrationCandidateScore]] = []
    for method, calibrator in candidates:
        transformed = calibrator.transform(raw_probabilities)
        log_loss, brier = _proper_scores(transformed, labels, sample_weights)
        scored.append(
            (calibrator, CalibrationCandidateScore(method=method, log_loss=log_loss, brier=brier))
        )
    identity = scored[0][1]
    eligible = tuple(
        item
        for item in scored[1:]
        if item[1].log_loss < identity.log_loss and item[1].brier < identity.brier
    )
    selected = (
        min(eligible, key=lambda item: (item[1].log_loss, item[1].brier, item[1].method))
        if eligible
        else scored[0]
    )
    return selected[0], CalibrationComparison(
        selected_method=selected[1].method,
        candidates=tuple(item[1] for item in scored),
    )


def _fit_temperature_calibrator(
    *,
    raw_probabilities: np.ndarray,
    labels: np.ndarray,
    sample_weights: np.ndarray | None,
    grid: tuple[float, ...],
) -> _Calibrator:
    """Use temperature only when it improves both proper scores over identity.

    Temperature scaling preserves a probability of 0.5 and therefore cannot inject
    a calibration-period directional prior into a later regime.
    """

    raw = np.clip(
        _validate_probability_vector(
            raw_probabilities,
            expected_rows=len(labels),
            name="raw calibration probabilities",
        ),
        _PROBABILITY_EPSILON,
        1.0 - _PROBABILITY_EPSILON,
    )
    target = np.asarray(labels, dtype=float)
    identity_log_loss, identity_brier = _proper_scores(raw, target, sample_weights)
    best: tuple[float, float, float] | None = None
    for temperature in sorted(set(float(value) for value in grid)):
        calibrated = _TemperatureCalibrator(temperature).transform(raw)
        log_loss, brier = _proper_scores(calibrated, target, sample_weights)
        if log_loss < identity_log_loss and brier < identity_brier:
            candidate = (log_loss, brier, temperature)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
    return _IdentityCalibrator() if best is None else _TemperatureCalibrator(best[2])


def _proper_scores(
    probabilities: np.ndarray, labels: np.ndarray, sample_weights: np.ndarray | None
) -> tuple[float, float]:
    clipped = np.clip(
        _validate_probability_vector(
            probabilities,
            expected_rows=len(labels),
            name="scoring probabilities",
        ),
        _PROBABILITY_EPSILON,
        1.0 - _PROBABILITY_EPSILON,
    )
    weights = np.ones(len(labels), dtype=float) if sample_weights is None else sample_weights
    log_loss = -float(
        np.average(
            labels * np.log(clipped) + (1.0 - labels) * np.log(1.0 - clipped), weights=weights
        )
    )
    brier = float(np.average((clipped - labels) ** 2, weights=weights))
    return log_loss, brier


def _predict_positive_probability(estimator: object, matrix: np.ndarray) -> np.ndarray:
    predict_proba = getattr(estimator, "predict_proba", None)
    if not callable(predict_proba):
        raise TypeError(f"estimator does not expose predict_proba: {type(estimator)!r}")
    probabilities = np.asarray(predict_proba(matrix), dtype=float)
    if probabilities.ndim != 2 or probabilities.shape[1] != 2:
        raise ValueError("binary estimator must return exactly two probability columns")
    if probabilities.shape[0] != len(matrix):
        raise ValueError("binary estimator probability row count does not match input")
    if not np.isfinite(probabilities).all():
        raise ValueError("binary estimator probabilities must be finite")
    if np.any((probabilities < 0.0) | (probabilities > 1.0)):
        raise ValueError("binary estimator probabilities must lie in [0, 1]")
    if not np.allclose(probabilities.sum(axis=1), 1.0, rtol=0.0, atol=1e-6):
        raise ValueError("binary estimator probability rows must sum to one")
    return np.clip(probabilities[:, 1], _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON)


def _estimator_matrix(
    *, matrix: np.ndarray, schema: FeatureSchema, config: DirectionModelConfig
) -> np.ndarray | pd.DataFrame:
    if config.kind == "lightgbm":
        return _feature_frame(matrix=matrix, schema=schema)
    return matrix


def _feature_frame(*, matrix: np.ndarray, schema: FeatureSchema) -> pd.DataFrame:
    return pd.DataFrame(matrix, columns=schema.names, copy=False)


def _probability_logits(probabilities: np.ndarray) -> np.ndarray:
    raw = np.asarray(probabilities, dtype=float)
    clipped = np.clip(
        _validate_probability_vector(
            raw,
            expected_rows=len(raw) if raw.ndim else 0,
            name="probabilities",
        ),
        _PROBABILITY_EPSILON,
        1.0 - _PROBABILITY_EPSILON,
    )
    return np.log(clipped / (1.0 - clipped))


def _validate_matrix(vectors: np.ndarray, *, schema: FeatureSchema) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("feature vectors must be a 2D matrix")
    if matrix.shape[0] == 0:
        raise ValueError("feature vectors must not be empty")
    if matrix.shape[1] != len(schema.names):
        raise ValueError(
            f"feature vector width {matrix.shape[1]} does not match schema width {len(schema.names)}"
        )
    if not np.isfinite(matrix).all():
        raise ValueError("feature vectors must be finite")
    return matrix


def _validate_binary_labels(labels: np.ndarray, *, expected_rows: int, name: str) -> np.ndarray:
    raw = np.asarray(labels)
    if raw.ndim != 1 or len(raw) != expected_rows:
        raise ValueError(f"{name} must be a one-dimensional vector of length {expected_rows}")
    if np.issubdtype(raw.dtype, np.bool_) or not (
        np.issubdtype(raw.dtype, np.integer) or np.issubdtype(raw.dtype, np.floating)
    ):
        raise ValueError(f"{name} must contain numeric binary classes 0 and 1")
    numeric = np.asarray(raw, dtype=float)
    if not np.isfinite(numeric).all() or np.any((numeric != 0.0) & (numeric != 1.0)):
        raise ValueError(f"{name} must contain both binary classes 0 and 1")
    target = numeric.astype(int)
    unique = set(target.tolist())
    if not unique.issubset({0, 1}) or len(unique) != 2:
        raise ValueError(f"{name} must contain both binary classes 0 and 1")
    return target


def _validate_independent_sample_count(value: int | None, *, maximum_rows: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError("calibration_independent_sample_count must be an integer")
    if value < 1 or value > maximum_rows:
        raise ValueError(
            "calibration_independent_sample_count must lie between 1 and calibration rows"
        )
    return int(value)


def _validate_probability_vector(
    values: np.ndarray, *, expected_rows: int, name: str
) -> np.ndarray:
    probabilities = np.asarray(values, dtype=float)
    if probabilities.ndim != 1 or len(probabilities) != expected_rows:
        raise ValueError(f"{name} must be a one-dimensional vector of length {expected_rows}")
    if not np.isfinite(probabilities).all():
        raise ValueError(f"{name} must be finite")
    if np.any((probabilities < 0.0) | (probabilities > 1.0)):
        raise ValueError(f"{name} must lie in [0, 1]")
    return probabilities


def _stable_sigmoid(values: np.ndarray) -> np.ndarray:
    result = np.empty_like(values, dtype=float)
    nonnegative = values >= 0.0
    result[nonnegative] = 1.0 / (1.0 + np.exp(-values[nonnegative]))
    exponent = np.exp(values[~nonnegative])
    result[~nonnegative] = exponent / (1.0 + exponent)
    return result


def _validate_weights(
    weights: np.ndarray | None, *, expected_rows: int, name: str
) -> np.ndarray | None:
    if weights is None:
        return None
    vector = np.asarray(weights, dtype=float)
    if vector.ndim != 1 or len(vector) != expected_rows:
        raise ValueError(f"{name} must be a one-dimensional vector of length {expected_rows}")
    if not np.isfinite(vector).all() or np.any(vector <= 0.0):
        raise ValueError(f"{name} must contain only finite values > 0")
    return vector


def _validate_early_stopping_data(
    *,
    vectors: np.ndarray | None,
    labels: np.ndarray | None,
    weights: np.ndarray | None,
    schema: FeatureSchema,
) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    if vectors is None and labels is None and weights is None:
        return None, None, None
    if vectors is None or labels is None:
        raise ValueError("early stopping vectors and labels must be provided together")
    matrix = _validate_matrix(vectors, schema=schema)
    target = _validate_binary_labels(
        labels,
        expected_rows=len(matrix),
        name="early_stopping_labels",
    )
    return (
        matrix,
        target,
        _validate_weights(weights, expected_rows=len(matrix), name="early_stopping_weights"),
    )
