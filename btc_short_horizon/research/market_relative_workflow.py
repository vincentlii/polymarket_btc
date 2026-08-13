"""Small orchestration seam for the fail-closed market-relative research CLI."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

StageEvaluator = Callable[[Any], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ResearchWorkflowResult:
    status: str
    failed_stage: str
    ablations: dict[str, object]
    promotion: dict[str, object]


def evaluate_research_workflow(
    *,
    datasets_by_ablation: Mapping[str, Sequence[Any]],
    selected_ablation: str,
    minimum_eligible_markets: int,
    merge_datasets: Callable[[Sequence[Any]], Any],
    evaluate_stages: StageEvaluator,
) -> ResearchWorkflowResult:
    """Execute every supplied ablation and report the first fail-closed selected gate."""

    if selected_ablation not in datasets_by_ablation:
        raise ValueError("selected_ablation must exist in datasets_by_ablation")
    if minimum_eligible_markets < 1:
        raise ValueError("minimum_eligible_markets must be >= 1")
    receipts: dict[str, object] = {}
    for name, datasets in datasets_by_ablation.items():
        values = tuple(datasets)
        if len(values) < minimum_eligible_markets:
            receipts[name] = {
                "status": "no_go",
                "failed_stage": "data_audit",
                "eligible_market_count": len(values),
                "sample_order_lineage_sha256": _sample_order_lineage(values),
            }
            continue
        try:
            stage_receipt = dict(evaluate_stages(merge_datasets(values)))
        except ValueError as exc:
            receipts[name] = {
                "status": "no_go",
                "failed_stage": "stage_oof",
                "error": str(exc),
                "sample_order_lineage_sha256": _sample_order_lineage(values),
            }
            continue
        receipts[name] = {"status": "complete", "stage_oof": stage_receipt}
        receipts[name]["sample_order_lineage_sha256"] = _sample_order_lineage(values)

    selected = receipts[selected_ablation]
    if selected.get("status") != "complete":  # type: ignore[union-attr]
        failed_stage = str(selected.get("failed_stage", "data_audit"))  # type: ignore[union-attr]
    elif _has_nonpositive_lower_bound(selected["stage_oof"]):  # type: ignore[index]
        failed_stage = "p_lower"
    else:
        failed_stage = "promotion"
    return ResearchWorkflowResult(
        status="no_go",
        failed_stage=failed_stage,
        ablations=receipts,
        promotion={"status": "blocked", "reason": "sealed_holdout_receipt_required"},
    )


def _has_nonpositive_lower_bound(stage_receipt: object) -> bool:
    if not isinstance(stage_receipt, Mapping) or not stage_receipt:
        raise ValueError("stage receipt must be a non-empty mapping")
    required_units = {"market", "day", "week"}
    for evidence in stage_receipt.values():
        if not isinstance(evidence, Mapping):
            raise ValueError("stage evidence must be a mapping")
        objectives = evidence.get("p_lower", {"legacy": evidence})
        if not isinstance(objectives, Mapping):
            raise ValueError("stage p_lower evidence must be a mapping")
        for objective in objectives.values():
            if not isinstance(objective, Mapping) or set(objective) != required_units:
                raise ValueError("each objective requires market/day/week evidence")
            for value in objective.values():
                if not isinstance(value, Mapping):
                    raise ValueError("p_lower evidence must be a mapping")
                lower = value.get("lower_bound")
                if not isinstance(lower, int | float) or isinstance(lower, bool):
                    raise ValueError("p_lower evidence requires a numeric lower_bound")
                if lower <= 0.0:
                    return True
    return False


def _sample_order_lineage(datasets: Sequence[Any]) -> str:
    digest = sha256()
    for dataset in datasets:
        direction = getattr(dataset, "dataset", dataset)
        for sample in direction.samples:
            digest.update(sample.sample_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(sample.group_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(sample.feature_ts.isoformat().encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()


__all__ = ["ResearchWorkflowResult", "evaluate_research_workflow"]
