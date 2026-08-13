from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from btc_short_horizon.features import FeatureSchema
from btc_short_horizon.research.market_relative_workflow import evaluate_research_workflow
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.walk_forward import ResearchSample


def _dataset(slug: str) -> DirectionDataset:
    now = datetime(2026, 7, 1, tzinfo=UTC)
    return DirectionDataset(
        samples=(ResearchSample(slug, now, now + timedelta(minutes=15), 1, slug),),
        vectors=np.asarray([[1.0]]),
        schema=FeatureSchema(version="workflow-test", names=("x",)),
    )


def _merge(values):  # type: ignore[no-untyped-def]
    return values[0]


def _stages(lower: float):
    return lambda _dataset: {
        stage: {unit: {"lower_bound": lower} for unit in ("market", "day", "week")}
        for stage in ("early", "price_discovery", "mid_early")
    }


def test_workflow_executes_ablation_stage_oof_and_p_lower_before_promotion_block() -> None:
    result = evaluate_research_workflow(
        datasets_by_ablation={"anchor": (_dataset("a"),), "anchor+pm": (_dataset("b"),)},
        selected_ablation="anchor+pm",
        minimum_eligible_markets=1,
        merge_datasets=_merge,
        evaluate_stages=_stages(0.01),
    )

    assert set(result.ablations) == {"anchor", "anchor+pm"}
    assert all(value["status"] == "complete" for value in result.ablations.values())
    assert len(result.ablations["anchor+pm"]["sample_order_lineage_sha256"]) == 64
    assert result.failed_stage == "promotion"
    assert result.promotion == {
        "status": "blocked",
        "reason": "sealed_holdout_receipt_required",
    }


def test_workflow_failed_stage_is_data_audit_or_p_lower_not_ready_placeholder() -> None:
    data_failure = evaluate_research_workflow(
        datasets_by_ablation={"selected": ()},
        selected_ablation="selected",
        minimum_eligible_markets=1,
        merge_datasets=_merge,
        evaluate_stages=_stages(0.01),
    )
    lower_failure = evaluate_research_workflow(
        datasets_by_ablation={"selected": (_dataset("a"),)},
        selected_ablation="selected",
        minimum_eligible_markets=1,
        merge_datasets=_merge,
        evaluate_stages=_stages(-0.001),
    )

    assert data_failure.failed_stage == "data_audit"
    assert lower_failure.failed_stage == "p_lower"


def test_candidate_worse_than_q_pm_is_no_go_but_all_positive_objectives_reach_promotion() -> None:
    def stages(lower: float):
        return lambda _dataset: {
            stage: {
                "p_lower": {
                    objective: {unit: {"lower_bound": lower} for unit in ("market", "day", "week")}
                    for objective in ("brier", "log_loss", "net_ev")
                }
            }
            for stage in ("early", "price_discovery", "mid_early")
        }

    worse = evaluate_research_workflow(
        datasets_by_ablation={"relative": (_dataset("a"),)},
        selected_ablation="relative",
        minimum_eligible_markets=1,
        merge_datasets=_merge,
        evaluate_stages=stages(-0.001),
    )
    better = evaluate_research_workflow(
        datasets_by_ablation={"relative": (_dataset("a"),)},
        selected_ablation="relative",
        minimum_eligible_markets=1,
        merge_datasets=_merge,
        evaluate_stages=stages(0.001),
    )

    assert worse.failed_stage == "p_lower"
    assert better.failed_stage == "promotion"
