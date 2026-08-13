"""Bounded LightGBM residual helpers with independent-market leaf audits."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

import lightgbm as lgb
import numpy as np

from btc_short_horizon.models.market_relative import relative_probability
from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.models.market_relative import ProbabilityInterval


@dataclass(frozen=True, slots=True)
class FittedMarketRelativeLightGBM:
    booster: lgb.Booster
    schema: FeatureSchema
    probability_uncertainty_radius: float

    def __post_init__(self) -> None:
        if (
            not isfinite(self.probability_uncertainty_radius)
            or not 0.0 < self.probability_uncertainty_radius < 0.5
        ):
            raise ValueError("probability_uncertainty_radius must be finite and in (0, 0.5)")

    def predict_up_probability(
        self,
        vectors: np.ndarray,
        market_up_probability: np.ndarray,
    ) -> np.ndarray:
        matrix = _matrix(vectors)
        anchor = _probabilities(market_up_probability)
        if len(matrix) != len(anchor):
            raise ValueError("market anchor must align with feature vectors")
        residual = np.asarray(
            self.booster.predict(matrix, raw_score=True, num_iteration=self.booster.best_iteration),
            dtype=float,
        )
        return relative_probability(anchor, residual)

    def predict_probability_intervals(
        self,
        vectors: np.ndarray,
        market_up_probability: np.ndarray,
    ) -> tuple[ProbabilityInterval, ...]:
        point = self.predict_up_probability(vectors, market_up_probability)
        radius = self.probability_uncertainty_radius
        return tuple(
            ProbabilityInterval(
                up_lower=float(np.clip(probability - radius, 1e-6, 1.0 - 1e-6)),
                up_point=float(probability),
                up_upper=float(np.clip(probability + radius, 1e-6, 1.0 - 1e-6)),
            )
            for probability in point
        )


def fit_market_relative_lightgbm(
    *,
    train_vectors: np.ndarray,
    train_labels: np.ndarray,
    train_market_up_probability: np.ndarray,
    validation_vectors: np.ndarray,
    validation_labels: np.ndarray,
    validation_market_up_probability: np.ndarray,
    schema: FeatureSchema,
    probability_uncertainty_radius: float,
    num_leaves: int,
    max_depth: int,
    min_child_samples: int,
    learning_rate: float,
    n_estimators: int,
    early_stopping_rounds: int,
    random_seed: int,
    train_weights: np.ndarray | None = None,
    validation_weights: np.ndarray | None = None,
) -> FittedMarketRelativeLightGBM:
    """Fit residual trees from a PM logit init score on a disjoint validation set."""

    train = _matrix(train_vectors)
    validation = _matrix(validation_vectors)
    if train.shape[1] != len(schema.names):
        raise ValueError("training feature count must match the schema")
    if train.shape[1] != validation.shape[1]:
        raise ValueError("training and validation feature counts must match")
    train_target = _labels(train_labels, len(train))
    validation_target = _labels(validation_labels, len(validation))
    train_anchor = _probabilities(train_market_up_probability)
    validation_anchor = _probabilities(validation_market_up_probability)
    if len(train_anchor) != len(train) or len(validation_anchor) != len(validation):
        raise ValueError("market anchors must align with their partitions")
    train_weight = _weights(train_weights, len(train))
    validation_weight = _weights(validation_weights, len(validation))
    dataset = lgb.Dataset(
        train,
        label=train_target,
        weight=train_weight,
        init_score=_logit(train_anchor),
        free_raw_data=False,
    )
    valid = lgb.Dataset(
        validation,
        label=validation_target,
        weight=validation_weight,
        init_score=_logit(validation_anchor),
        reference=dataset,
        free_raw_data=False,
    )
    booster = lgb.train(
        {
            "objective": "binary",
            "metric": "binary_logloss",
            "num_leaves": num_leaves,
            "max_depth": max_depth,
            "min_child_samples": min_child_samples,
            "learning_rate": learning_rate,
            "seed": random_seed,
            "feature_fraction_seed": random_seed,
            "bagging_seed": random_seed,
            "data_random_seed": random_seed,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": 1,
            "verbosity": -1,
        },
        dataset,
        num_boost_round=n_estimators,
        valid_sets=(valid,),
        callbacks=(lgb.early_stopping(early_stopping_rounds, verbose=False),),
    )
    return FittedMarketRelativeLightGBM(
        booster=booster,
        schema=schema,
        probability_uncertainty_radius=probability_uncertainty_radius,
    )


def minimum_leaf_unique_market_count(
    *,
    leaf_indices: np.ndarray,
    market_slugs: Sequence[str],
) -> int:
    """Return the smallest independent-market count across every fitted leaf."""

    matrix = np.asarray(leaf_indices)
    if matrix.ndim == 1:
        matrix = matrix.reshape(-1, 1)
    if matrix.ndim != 2 or not len(matrix) or len(matrix) != len(market_slugs):
        raise ValueError("leaf_indices must align one-to-one with market_slugs")
    if any(not isinstance(slug, str) or not slug for slug in market_slugs):
        raise ValueError("market_slugs must be non-empty strings")
    counts: list[int] = []
    for tree in range(matrix.shape[1]):
        by_leaf: dict[int, set[str]] = defaultdict(set)
        for row, slug in zip(matrix[:, tree], market_slugs, strict=True):
            if isinstance(row, bool) or not float(row).is_integer() or row < 0:
                raise ValueError("leaf indices must be non-negative integers")
            by_leaf[int(row)].add(slug)
        counts.extend(len(slugs) for slugs in by_leaf.values())
    return min(counts)


def require_minimum_leaf_unique_markets(
    *,
    leaf_indices: np.ndarray,
    market_slugs: Sequence[str],
    minimum_markets_per_leaf: int,
) -> int:
    """Fail artifact publication when any fitted leaf lacks independent markets."""

    if minimum_markets_per_leaf < 1:
        raise ValueError("minimum_markets_per_leaf must be >= 1")
    observed = minimum_leaf_unique_market_count(
        leaf_indices=leaf_indices,
        market_slugs=market_slugs,
    )
    if observed < minimum_markets_per_leaf:
        raise ValueError(
            "artifact requires at least "
            f"{minimum_markets_per_leaf} independent markets per leaf; observed {observed}"
        )
    return observed


def _matrix(values: np.ndarray) -> np.ndarray:
    matrix = np.asarray(values, dtype=float)
    if matrix.ndim != 2 or not len(matrix) or not np.isfinite(matrix).all():
        raise ValueError("feature matrix must be finite and non-empty")
    return matrix


def _labels(values: np.ndarray, rows: int) -> np.ndarray:
    labels = np.asarray(values)
    if labels.ndim != 1 or len(labels) != rows or set(labels.tolist()) != {0, 1}:
        raise ValueError("labels must align with rows and contain both classes")
    return labels.astype(float)


def _probabilities(values: np.ndarray) -> np.ndarray:
    probabilities = np.asarray(values, dtype=float)
    if (
        probabilities.ndim != 1
        or not len(probabilities)
        or not np.isfinite(probabilities).all()
        or np.any((probabilities <= 0.0) | (probabilities >= 1.0))
    ):
        raise ValueError("probabilities must be finite and in (0, 1)")
    return probabilities


def _weights(values: np.ndarray | None, rows: int) -> np.ndarray:
    result = np.ones(rows, dtype=float) if values is None else np.asarray(values, dtype=float)
    if (
        result.ndim != 1
        or len(result) != rows
        or not np.isfinite(result).all()
        or np.any(result <= 0.0)
    ):
        raise ValueError("weights must be finite, positive, and row-aligned")
    return result


def _logit(probabilities: np.ndarray) -> np.ndarray:
    return np.log(probabilities / (1.0 - probabilities))


__all__ = [
    "FittedMarketRelativeLightGBM",
    "fit_market_relative_lightgbm",
    "minimum_leaf_unique_market_count",
    "require_minimum_leaf_unique_markets",
]
