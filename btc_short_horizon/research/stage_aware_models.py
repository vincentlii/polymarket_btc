"""Stage-specific dataset seams for the BTC 15m direction challenger."""

from __future__ import annotations

from dataclasses import dataclass
from btc_short_horizon.data import BTC_15M_MARKET_FAMILY
from btc_short_horizon.models import DirectionModelConfig
from btc_short_horizon.research.pipeline import DirectionDataset, WalkForwardModelRun
from btc_short_horizon.research.walk_forward import WalkForwardConfig
from btc_short_horizon.strategy import OpeningStage, StagePolicyConfig
from btc_short_horizon.research.pipeline import run_walk_forward_model


def sample_elapsed_seconds(sample_id: str) -> float:
    """Decode the canonical ``slug@decision_ns`` identity without labels."""

    try:
        slug, decision_ns_text = sample_id.rsplit("@", 1)
        decision_ns = int(decision_ns_text)
    except (AttributeError, ValueError) as exc:
        raise ValueError("sample_id must use slug@decision_ns") from exc
    t0_ns = int(BTC_15M_MARKET_FAMILY.parse_slug(slug).timestamp() * 1_000_000_000)
    return (decision_ns - t0_ns) / 1_000_000_000


def select_stage_dataset(
    dataset: DirectionDataset,
    *,
    stage: OpeningStage,
    policy: StagePolicyConfig | None = None,
) -> DirectionDataset:
    """Select complete rows for one stage while preserving market groups."""

    effective_policy = policy or StagePolicyConfig.default()
    rule = next((item for item in effective_policy.rules if item.stage is stage), None)
    if rule is None:
        raise ValueError(f"stage {stage.value} is not present in policy")
    indices = tuple(
        index
        for index, sample in enumerate(dataset.samples)
        if rule.start_seconds <= sample_elapsed_seconds(sample.sample_id) <= rule.end_seconds
    )
    if not indices:
        raise ValueError(f"dataset has no samples for stage {stage.value}")
    matrix = dataset.vectors[list(indices)]
    weights = dataset.sample_weights[list(indices)] if dataset.sample_weights is not None else None
    return DirectionDataset(
        samples=tuple(dataset.samples[index] for index in indices),
        vectors=matrix,
        schema=dataset.schema,
        sample_weights=weights,
    )


@dataclass(frozen=True, slots=True)
class StageModelRuns:
    early: WalkForwardModelRun | None
    price_discovery: WalkForwardModelRun | None
    mid_early: WalkForwardModelRun | None

    def for_stage(self, stage: OpeningStage) -> WalkForwardModelRun | None:
        return {
            OpeningStage.EARLY: self.early,
            OpeningStage.PRICE_DISCOVERY: self.price_discovery,
            OpeningStage.MID_EARLY: self.mid_early,
        }[stage]


def run_stage_walk_forward_models(
    dataset: DirectionDataset,
    *,
    policy: StagePolicyConfig | None = None,
    split_config: WalkForwardConfig | None = None,
    model_config: DirectionModelConfig | None = None,
) -> StageModelRuns:
    """Fit one leakage-free walk-forward model/calibrator per stage."""

    effective_policy = policy or StagePolicyConfig.default()
    runs: dict[OpeningStage, WalkForwardModelRun | None] = {}
    for stage in OpeningStage:
        try:
            stage_dataset = select_stage_dataset(dataset, stage=stage, policy=effective_policy)
        except ValueError:
            runs[stage] = None
            continue
        runs[stage] = run_walk_forward_model(
            dataset=stage_dataset,
            split_config=split_config,
            model_config=model_config,
        )
    return StageModelRuns(
        early=runs[OpeningStage.EARLY],
        price_discovery=runs[OpeningStage.PRICE_DISCOVERY],
        mid_early=runs[OpeningStage.MID_EARLY],
    )


__all__ = [
    "StageModelRuns",
    "run_stage_walk_forward_models",
    "sample_elapsed_seconds",
    "select_stage_dataset",
]
