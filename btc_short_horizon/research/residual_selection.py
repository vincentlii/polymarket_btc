"""Frozen development-only ranking and gate for the residual challenger."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from statistics import median


@dataclass(frozen=True, slots=True)
class LeafAudit:
    unique_markets_by_leaf: dict[int, int]
    invalid_leaves: tuple[int, ...]
    minimum_markets_per_leaf: int

    @classmethod
    def from_assignments(
        cls,
        *,
        leaf_by_sample: Sequence[int],
        market_by_sample: Sequence[str],
        minimum_markets_per_leaf: int,
    ) -> LeafAudit:
        if len(leaf_by_sample) != len(market_by_sample) or not leaf_by_sample:
            raise ValueError("leaf and market assignments must be aligned and non-empty")
        if minimum_markets_per_leaf < 1:
            raise ValueError("minimum_markets_per_leaf must be >= 1")
        markets: dict[int, set[str]] = defaultdict(set)
        for leaf, market in zip(leaf_by_sample, market_by_sample, strict=True):
            if isinstance(leaf, bool) or not isinstance(leaf, int) or leaf < 0:
                raise ValueError("leaf assignments must be non-negative integers")
            if not market or market.strip() != market:
                raise ValueError("market identities must be non-empty and trimmed")
            markets[leaf].add(market)
        counts = {leaf: len(values) for leaf, values in sorted(markets.items())}
        return cls(
            unique_markets_by_leaf=counts,
            invalid_leaves=tuple(
                leaf for leaf, count in counts.items() if count < minimum_markets_per_leaf
            ),
            minimum_markets_per_leaf=minimum_markets_per_leaf,
        )


@dataclass(frozen=True, slots=True)
class CandidateWindowScore:
    candidate_id: str
    window_scores: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.window_scores:
            raise ValueError("candidate ID and window scores are required")
        if any(not isfinite(value) for value in self.window_scores):
            raise ValueError("window scores must be finite")

    @property
    def median_score(self) -> float:
        return float(median(self.window_scores))


def rank_by_median_window_score(
    candidates: Sequence[CandidateWindowScore],
) -> tuple[CandidateWindowScore, ...]:
    if not candidates or len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValueError("candidate scores must be non-empty and uniquely identified")
    return tuple(sorted(candidates, key=lambda item: (-item.median_score, item.candidate_id)))


@dataclass(frozen=True, slots=True)
class MultipleComparisonDecision:
    family_alpha: float
    candidate_count: int
    adjusted_alpha: float
    accepted_candidate_ids: tuple[str, ...]


def apply_bonferroni_correction(
    candidate_p_values: Mapping[str, float],
    *,
    family_alpha: float = 0.05,
) -> MultipleComparisonDecision:
    """Apply one frozen family-wise error correction before ranking candidates."""

    if not candidate_p_values or len(candidate_p_values) != len(set(candidate_p_values)):
        raise ValueError("candidate p-values must be non-empty and uniquely identified")
    if not isfinite(family_alpha) or not 0.0 < family_alpha < 1.0:
        raise ValueError("family_alpha must lie in (0, 1)")
    for candidate_id, p_value in candidate_p_values.items():
        if not candidate_id or candidate_id.strip() != candidate_id:
            raise ValueError("candidate IDs must be non-empty and trimmed")
        if not isfinite(p_value) or not 0.0 <= p_value <= 1.0:
            raise ValueError("candidate p-values must lie in [0, 1]")
    adjusted_alpha = family_alpha / len(candidate_p_values)
    return MultipleComparisonDecision(
        family_alpha=family_alpha,
        candidate_count=len(candidate_p_values),
        adjusted_alpha=adjusted_alpha,
        accepted_candidate_ids=tuple(
            candidate_id
            for candidate_id, p_value in sorted(candidate_p_values.items())
            if p_value <= adjusted_alpha
        ),
    )


def require_identical_paired_markets(
    *,
    shared_stage_markets: Sequence[str],
    independent_stage_markets: Sequence[str],
) -> tuple[str, ...]:
    """Return the deterministic comparison population or reject an unpaired study."""

    shared = tuple(shared_stage_markets)
    independent = tuple(independent_stage_markets)
    if not shared or len(set(shared)) != len(shared) or len(set(independent)) != len(independent):
        raise ValueError("stage comparisons require unique non-empty market identities")
    if set(shared) != set(independent):
        raise ValueError("shared and independent stage models must cover identical markets")
    return tuple(sorted(shared))


@dataclass(frozen=True, slots=True)
class DevelopmentGateEvidence:
    paired_log_loss_ci_lower: float
    paired_brier_ci_lower: float
    calibration_safe: bool
    robust_edge_ci_lower: float
    opportunity_count: int
    maximum_direction_share: float
    maximum_price_bucket_share: float
    operationally_valid: bool


@dataclass(frozen=True, slots=True)
class DevelopmentGateDecision:
    accepted: bool
    failures: tuple[str, ...]


def evaluate_development_gate(
    evidence: DevelopmentGateEvidence,
    *,
    minimum_opportunities: int = 300,
    maximum_concentration: float = 0.80,
) -> DevelopmentGateDecision:
    failures: list[str] = []
    if evidence.paired_log_loss_ci_lower <= 0.0 or evidence.paired_brier_ci_lower <= 0.0:
        failures.append("probability_improvement_not_supported")
    if not evidence.calibration_safe:
        failures.append("calibration_not_safe")
    if evidence.robust_edge_ci_lower <= 0.0:
        failures.append("robust_edge_not_supported")
    if evidence.opportunity_count < minimum_opportunities:
        failures.append("insufficient_opportunities")
    if (
        evidence.maximum_direction_share > maximum_concentration
        or evidence.maximum_price_bucket_share > maximum_concentration
    ):
        failures.append("selection_concentration_too_high")
    if not evidence.operationally_valid:
        failures.append("operational_evidence_invalid")
    return DevelopmentGateDecision(accepted=not failures, failures=tuple(failures))


__all__ = [
    "CandidateWindowScore",
    "DevelopmentGateDecision",
    "DevelopmentGateEvidence",
    "LeafAudit",
    "MultipleComparisonDecision",
    "apply_bonferroni_correction",
    "evaluate_development_gate",
    "rank_by_median_window_score",
    "require_identical_paired_markets",
]
