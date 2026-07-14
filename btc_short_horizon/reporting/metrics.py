"""Probability, calibration, and maker-edge metrics from immutable records."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression

_PROBABILITY_EPSILON = 1e-6


def _require_probability(name: str, value: float) -> None:
    if not isfinite(value) or not 0.0 < value < 1.0:
        raise ValueError(f"{name} must be finite and in (0, 1), got {value!r}")


@dataclass(frozen=True, slots=True)
class ProbabilityEvaluation:
    market_slug: str
    p_up: float
    outcome_up: int

    def __post_init__(self) -> None:
        if not self.market_slug:
            raise ValueError("market_slug is required")
        _require_probability("p_up", self.p_up)
        if self.outcome_up not in {0, 1}:
            raise ValueError("outcome_up must be binary")


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_probability: float | None
    observed_rate: float | None
    absolute_error: float | None


@dataclass(frozen=True, slots=True)
class ProbabilityMetrics:
    count: int
    brier_score: float
    log_loss: float
    expected_calibration_error: float
    calibration_intercept: float | None
    calibration_slope: float | None
    bins: tuple[CalibrationBin, ...]


@dataclass(frozen=True, slots=True)
class FairProbabilityAttribution:
    """Decompose an outcome-side fair-value edge without using pre-open state."""

    boundary_probability_edge: float
    opening_information_edge: float
    maker_discount: float
    market_mispricing_edge: float

    @property
    def total(self) -> float:
        return self.boundary_probability_edge + self.opening_information_edge + self.maker_discount


@dataclass(frozen=True, slots=True)
class MakerFillEvaluation:
    market_slug: str
    side: str
    p_boundary: float
    p_fair: float
    p_market: float
    entry_price: float
    shares: float
    fee: float
    rebate: float
    outcome: int

    def __post_init__(self) -> None:
        if not self.market_slug:
            raise ValueError("market_slug is required")
        if self.side not in {"up", "down"}:
            raise ValueError("side must be 'up' or 'down'")
        _require_probability("p_boundary", self.p_boundary)
        _require_probability("p_fair", self.p_fair)
        _require_probability("p_market", self.p_market)
        _require_probability("entry_price", self.entry_price)
        for name, value in (("shares", self.shares), ("fee", self.fee), ("rebate", self.rebate)):
            if not isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and >= 0")
        if self.shares <= 0.0:
            raise ValueError("shares must be > 0")
        if self.outcome not in {0, 1}:
            raise ValueError("outcome must be binary")

    @property
    def attribution(self) -> FairProbabilityAttribution:
        return FairProbabilityAttribution(
            boundary_probability_edge=self.p_boundary - 0.5,
            opening_information_edge=self.p_fair - self.p_boundary,
            maker_discount=0.5 - self.entry_price,
            market_mispricing_edge=self.p_fair - self.p_market,
        )

    @property
    def expected_edge_per_share(self) -> float:
        return self.p_fair - self.entry_price

    @property
    def net_expected_value(self) -> float:
        return self.shares * self.expected_edge_per_share - self.fee + self.rebate

    @property
    def realized_pnl(self) -> float:
        return self.shares * (self.outcome - self.entry_price) - self.fee + self.rebate


@dataclass(frozen=True, slots=True)
class MakerFillSummary:
    count: int
    total_shares: float
    net_expected_value: float
    net_expected_value_per_share: float
    realized_pnl: float
    realized_pnl_per_share: float
    total_fee: float
    total_rebate: float


def evaluate_probabilities(
    evaluations: Sequence[ProbabilityEvaluation], *, bin_edges: Sequence[float] | None = None
) -> ProbabilityMetrics:
    """Compute Brier, log loss, ECE, and calibration diagnostics without leakage."""

    if not evaluations:
        raise ValueError("evaluations must not be empty")
    probabilities = np.asarray([item.p_up for item in evaluations], dtype=float)
    outcomes = np.asarray([item.outcome_up for item in evaluations], dtype=float)
    brier_score = float(np.mean((probabilities - outcomes) ** 2))
    clipped = np.clip(probabilities, _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON)
    log_loss = float(
        -np.mean(outcomes * np.log(clipped) + (1.0 - outcomes) * np.log(1.0 - clipped))
    )
    edges = _normalize_bin_edges(bin_edges)
    bins = _calibration_bins(probabilities, outcomes, edges)
    ece = sum((bin_.count / len(evaluations)) * (bin_.absolute_error or 0.0) for bin_ in bins)
    intercept, slope = _calibration_regression(probabilities, outcomes)
    return ProbabilityMetrics(
        count=len(evaluations),
        brier_score=brier_score,
        log_loss=log_loss,
        expected_calibration_error=ece,
        calibration_intercept=intercept,
        calibration_slope=slope,
        bins=bins,
    )


def evaluate_maker_fills(evaluations: Sequence[MakerFillEvaluation]) -> MakerFillSummary:
    """Aggregate fill-level net edge and settlement PnL, including fees and rebates."""

    if not evaluations:
        raise ValueError("evaluations must not be empty")
    total_shares = sum(item.shares for item in evaluations)
    net_expected_value = sum(item.net_expected_value for item in evaluations)
    realized_pnl = sum(item.realized_pnl for item in evaluations)
    return MakerFillSummary(
        count=len(evaluations),
        total_shares=total_shares,
        net_expected_value=net_expected_value,
        net_expected_value_per_share=net_expected_value / total_shares,
        realized_pnl=realized_pnl,
        realized_pnl_per_share=realized_pnl / total_shares,
        total_fee=sum(item.fee for item in evaluations),
        total_rebate=sum(item.rebate for item in evaluations),
    )


def _normalize_bin_edges(edges: Sequence[float] | None) -> tuple[float, ...]:
    normalized = tuple(edges) if edges is not None else tuple(index / 10 for index in range(0, 11))
    if len(normalized) < 2 or normalized[0] != 0.0 or normalized[-1] != 1.0:
        raise ValueError("bin_edges must begin at 0.0 and end at 1.0")
    if any(not isfinite(edge) or edge < 0.0 or edge > 1.0 for edge in normalized):
        raise ValueError("bin_edges must be finite probabilities")
    if any(right <= left for left, right in zip(normalized, normalized[1:])):
        raise ValueError("bin_edges must be strictly increasing")
    return normalized


def _calibration_bins(
    probabilities: np.ndarray, outcomes: np.ndarray, edges: tuple[float, ...]
) -> tuple[CalibrationBin, ...]:
    bins: list[CalibrationBin] = []
    for index, (lower, upper) in enumerate(zip(edges, edges[1:])):
        mask = (probabilities >= lower) & (
            probabilities <= upper if index == len(edges) - 2 else probabilities < upper
        )
        selected_probabilities = probabilities[mask]
        selected_outcomes = outcomes[mask]
        if len(selected_probabilities) == 0:
            bins.append(CalibrationBin(lower, upper, 0, None, None, None))
            continue
        mean_probability = float(np.mean(selected_probabilities))
        observed_rate = float(np.mean(selected_outcomes))
        bins.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                count=len(selected_probabilities),
                mean_probability=mean_probability,
                observed_rate=observed_rate,
                absolute_error=abs(mean_probability - observed_rate),
            )
        )
    return tuple(bins)


def _calibration_regression(
    probabilities: np.ndarray, outcomes: np.ndarray
) -> tuple[float | None, float | None]:
    if len(np.unique(outcomes)) != 2:
        return None, None
    logits = np.log(
        np.clip(probabilities, _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON)
        / (1.0 - np.clip(probabilities, _PROBABILITY_EPSILON, 1.0 - _PROBABILITY_EPSILON))
    )
    try:
        estimator = LogisticRegression(C=np.inf, solver="lbfgs", max_iter=2_000).fit(
            logits.reshape(-1, 1), outcomes.astype(int)
        )
    except ValueError:
        return None, None
    return float(estimator.intercept_[0]), float(estimator.coef_[0, 0])
