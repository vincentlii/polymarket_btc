"""Development-only model selection for stage-aware market-relative residuals."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite
from statistics import median
from typing import Any, Literal

from btc_short_horizon.research.lightgbm_tuning import (
    LightGBMSearchCandidate,
    residual_lightgbm_grid,
    residual_logistic_grid,
)
from btc_short_horizon.research.walk_forward import WalkForwardConfig
from btc_short_horizon.strategy import OpeningStage
from btc_short_horizon.strategy import StagePolicyConfig

ModelSearchMode = Literal["logistic", "bounded"]


@dataclass(frozen=True, slots=True)
class CandidateDevelopmentEvidence:
    """One candidate's development-fold evidence; sealed rows are forbidden."""

    fold_metrics: tuple[Mapping[str, float], ...]
    objective_improvements: Mapping[str, float]
    objective_lower_bounds: Mapping[str, float]
    objective_p_values: Mapping[str, float]
    oof_market_count: int
    minimum_leaf_unique_market_count: int | None = None
    direction_balance_gate_passed: bool = True
    sealed_sample_count: int = 0

    def __post_init__(self) -> None:
        if not self.fold_metrics:
            raise ValueError("candidate evidence requires at least one development fold")
        required = {"log_loss", "brier", "net_ev"}
        if set(self.objective_improvements) != required:
            raise ValueError("candidate evidence requires log_loss/brier/net_ev improvements")
        if set(self.objective_lower_bounds) != required:
            raise ValueError("candidate evidence requires log_loss/brier/net_ev lower bounds")
        if set(self.objective_p_values) != required:
            raise ValueError("candidate evidence requires log_loss/brier/net_ev p-values")
        if any(not isfinite(float(value)) for value in self.objective_improvements.values()):
            raise ValueError("candidate objective improvements must be finite")
        if any(not isfinite(float(value)) for value in self.objective_lower_bounds.values()):
            raise ValueError("candidate objective lower bounds must be finite")
        if any(
            not isfinite(float(value)) or not 0.0 <= float(value) <= 1.0
            for value in self.objective_p_values.values()
        ):
            raise ValueError("candidate objective p-values must be finite and in [0, 1]")
        if any(
            "execution_score" not in fold or not isfinite(float(fold["execution_score"]))
            for fold in self.fold_metrics
        ):
            raise ValueError("each development fold requires a finite execution_score")
        if self.oof_market_count < 1:
            raise ValueError("candidate evidence requires independent OOF markets")
        if self.sealed_sample_count != 0:
            raise ValueError("sealed holdout cannot participate in development selection")
        if not isinstance(self.direction_balance_gate_passed, bool):
            raise ValueError("direction_balance_gate_passed must be bool")


CandidateEvaluator = Callable[
    [OpeningStage, LightGBMSearchCandidate, Any], CandidateDevelopmentEvidence
]


def select_market_relative_models(
    *,
    stage_inputs: Mapping[OpeningStage, Any],
    evaluate_candidate: CandidateEvaluator,
    mode: ModelSearchMode = "logistic",
    random_seed: int = 17,
    minimum_markets_per_leaf: int = 100,
    familywise_alpha: float = 0.05,
) -> dict[str, object]:
    """Evaluate a frozen candidate set independently in each development stage.

    The caller owns fold construction. Supplying the same immutable stage input to
    every candidate makes fold reuse explicit and keeps the sealed holdout outside
    this interface.
    """

    if mode not in {"logistic", "bounded"}:
        raise ValueError("mode must be logistic or bounded")
    if minimum_markets_per_leaf < 1:
        raise ValueError("minimum_markets_per_leaf must be >= 1")
    if not isfinite(familywise_alpha) or not 0.0 < familywise_alpha < 1.0:
        raise ValueError("familywise_alpha must be in (0, 1)")
    if set(stage_inputs) != set(OpeningStage):
        raise ValueError("model selection requires exactly the three opening stages")

    stages: dict[str, object] = {}
    for stage in OpeningStage:
        candidates = tuple(
            _stage_candidate(item, stage)
            for item in residual_logistic_grid(random_seed=random_seed)
        )
        if mode == "bounded":
            candidates += tuple(
                _stage_candidate(item, stage)
                for item in residual_lightgbm_grid(random_seed=random_seed)
            )
        bonferroni_test_count = len(candidates) * 3
        adjusted_alpha = familywise_alpha / bonferroni_test_count
        evaluated = []
        for candidate in candidates:
            evidence = evaluate_candidate(stage, candidate, stage_inputs[stage])
            leaf_gate = candidate.config.kind == "logistic" or (
                evidence.minimum_leaf_unique_market_count is not None
                and evidence.minimum_leaf_unique_market_count >= minimum_markets_per_leaf
            )
            multiple_gate = all(
                evidence.objective_lower_bounds[name] > 0.0
                and evidence.objective_p_values[name] <= adjusted_alpha
                for name in ("log_loss", "brier", "net_ev")
            )
            median_execution_score = median(
                float(fold["execution_score"]) for fold in evidence.fold_metrics
            )
            paper_experiment_gate = bool(
                leaf_gate
                and evidence.direction_balance_gate_passed
                and median_execution_score > 0.0
                and evidence.objective_improvements["log_loss"] >= 0.0
                and evidence.objective_improvements["brier"] >= 0.0
                and evidence.objective_improvements["net_ev"] > 0.0
            )
            evaluated.append(
                {
                    "name": candidate.name,
                    "kind": candidate.config.kind,
                    "config": asdict(candidate.config),
                    "config_sha256": candidate.manifest_hash,
                    "fold_metrics": [dict(value) for value in evidence.fold_metrics],
                    "objective_improvements": dict(evidence.objective_improvements),
                    "objective_lower_bounds": dict(evidence.objective_lower_bounds),
                    "objective_p_values": dict(evidence.objective_p_values),
                    "median_fold_execution_score": median_execution_score,
                    "oof_market_count": evidence.oof_market_count,
                    "minimum_leaf_unique_market_count": (evidence.minimum_leaf_unique_market_count),
                    "leaf_gate_passed": leaf_gate,
                    "multiple_comparison_gate_passed": multiple_gate,
                    "direction_balance_gate_passed": evidence.direction_balance_gate_passed,
                    "paper_experiment_gate_passed": paper_experiment_gate,
                }
            )
        logistic = [value for value in evaluated if value["kind"] == "logistic"]
        best_logistic = max(logistic, key=_rank)
        eligible_lgb = [
            value
            for value in evaluated
            if value["kind"] == "lightgbm"
            and value["leaf_gate_passed"]
            and value["multiple_comparison_gate_passed"]
            and value["direction_balance_gate_passed"]
            and _rank(value) > _rank(best_logistic)
        ]
        champion = max(eligible_lgb, key=_rank) if eligible_lgb else best_logistic
        paper_candidates = [value for value in evaluated if value["paper_experiment_gate_passed"]]
        paper_leader = max(paper_candidates, key=_rank) if paper_candidates else None
        stages[stage.value] = {
            "candidate_count": len(evaluated),
            "bonferroni_test_count": bonferroni_test_count,
            "bonferroni_adjusted_alpha": adjusted_alpha,
            "bonferroni_confidence": 1.0 - adjusted_alpha,
            "champion_name": champion["name"],
            "champion_config_sha256": champion["config_sha256"],
            "champion_config": champion["config"],
            "selection_gate_passed": bool(
                champion["multiple_comparison_gate_passed"]
                and champion["direction_balance_gate_passed"]
                and champion["leaf_gate_passed"]
            ),
            "paper_experiment_gate_passed": paper_leader is not None,
            "paper_experiment_leader_name": (
                paper_leader["name"] if paper_leader is not None else None
            ),
            "paper_experiment_config_sha256": (
                paper_leader["config_sha256"] if paper_leader is not None else None
            ),
            "paper_experiment_config": (
                paper_leader["config"] if paper_leader is not None else None
            ),
            "candidates": evaluated,
        }

    receipt = {
        "schema_version": "market-relative-model-selection-v1",
        "status": "complete",
        "mode": mode,
        "sealed_holdout_evaluated": False,
        "minimum_markets_per_leaf_required": minimum_markets_per_leaf,
        "familywise_alpha": familywise_alpha,
        "stages": stages,
    }
    receipt["selection_receipt_sha256"] = selection_receipt_sha256(receipt)
    return receipt


def selection_receipt_sha256(receipt: Mapping[str, object]) -> str:
    payload = {key: value for key, value in receipt.items() if key != "selection_receipt_sha256"}
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate_selection_receipt(receipt: Mapping[str, object]) -> Mapping[str, object]:
    if receipt.get("schema_version") != "market-relative-model-selection-v1":
        raise ValueError("unsupported model-selection receipt schema")
    if receipt.get("status") != "complete" or receipt.get("sealed_holdout_evaluated") is not False:
        raise ValueError("artifact fitting requires a complete development-only selection receipt")
    expected = selection_receipt_sha256(receipt)
    if receipt.get("selection_receipt_sha256") != expected:
        raise ValueError("model-selection receipt SHA-256 mismatch")
    stages = receipt.get("stages")
    if not isinstance(stages, Mapping) or set(stages) != {stage.value for stage in OpeningStage}:
        raise ValueError("selection receipt requires exactly three stage champions")
    return receipt


def make_oof_candidate_evaluator(
    *,
    confidence: float,
    resamples: int,
    seed: int,
    execution_cost_per_share: float,
    minimum_trade_edge: float,
    minimum_order_size: float,
    tail_quarantine_price: float,
    minimum_side_opportunities: int = 30,
    maximum_side_share: float = 0.80,
    stage_policy: StagePolicyConfig | None = None,
    split_config: WalkForwardConfig | None = None,
) -> CandidateEvaluator:
    """Adapt the real anchored OOF runner to the selection contract."""

    cache: dict[tuple[str, int], Mapping[str, object]] = {}

    def evaluate(
        stage: OpeningStage, candidate: LightGBMSearchCandidate, anchored: Any
    ) -> CandidateDevelopmentEvidence:
        from btc_short_horizon.research.market_relative_stage_oof import (
            run_market_relative_stage_oof,
        )

        config_hash = sha256(
            json.dumps(asdict(candidate.config), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cache_key = (config_hash, id(anchored))
        if cache_key not in cache:
            cache[cache_key] = run_market_relative_stage_oof(
                anchored,
                confidence=confidence,
                resamples=resamples,
                seed=seed,
                model_config=candidate.config,
                execution_cost_per_share=execution_cost_per_share,
                minimum_trade_edge=minimum_trade_edge,
                minimum_order_size=minimum_order_size,
                tail_quarantine_price=tail_quarantine_price,
                minimum_side_opportunities=minimum_side_opportunities,
                maximum_side_share=maximum_side_share,
                stage_policy=stage_policy,
                split_config=split_config,
            )
        stage_receipt = cache[cache_key][stage.value]
        if not isinstance(stage_receipt, Mapping):
            raise ValueError("stage OOF receipt must be a mapping")
        metrics = stage_receipt["metrics"]
        p_lower = stage_receipt["p_lower"]
        assert isinstance(metrics, Mapping) and isinstance(p_lower, Mapping)
        improvements = {
            name: float(metrics[name]["improvement"]) for name in ("log_loss", "brier", "net_ev")
        }
        lower_bounds = {
            name: min(float(unit["lower_bound"]) for unit in p_lower[name].values())
            for name in ("log_loss", "brier", "net_ev")
        }
        p_values = {
            name: max(float(unit["p_value"]) for unit in p_lower[name].values())
            for name in ("log_loss", "brier", "net_ev")
        }
        return CandidateDevelopmentEvidence(
            fold_metrics=tuple(stage_receipt["fold_metrics"]),
            objective_improvements=improvements,
            objective_lower_bounds=lower_bounds,
            objective_p_values=p_values,
            oof_market_count=int(stage_receipt["oof_market_count"]),
            minimum_leaf_unique_market_count=stage_receipt.get("minimum_leaf_unique_market_count"),
            direction_balance_gate_passed=bool(stage_receipt["direction_balance"]["gate_passed"]),
        )

    return evaluate


def _stage_candidate(
    candidate: LightGBMSearchCandidate, stage: OpeningStage
) -> LightGBMSearchCandidate:
    return LightGBMSearchCandidate(
        name=f"{stage.value}:{candidate.name}", stage=stage.value, config=candidate.config
    )


def _rank(value: Mapping[str, object]) -> tuple[float, float, float, str]:
    objectives = value["objective_improvements"]
    assert isinstance(objectives, Mapping)
    return (
        float(value["median_fold_execution_score"]),
        float(objectives["log_loss"]),
        float(objectives["brier"]),
        str(value["name"]),
    )


__all__ = [
    "CandidateDevelopmentEvidence",
    "ModelSearchMode",
    "select_market_relative_models",
    "selection_receipt_sha256",
    "validate_selection_receipt",
    "make_oof_candidate_evaluator",
]
