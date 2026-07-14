"""Leakage-resistant walk-forward research protocol."""

from btc_short_horizon.research.walk_forward import (
    ResearchSample,
    WalkForwardConfig,
    WalkForwardFold,
    WalkForwardPlan,
    build_walk_forward_plan,
    select_complete_group_indices,
)
from btc_short_horizon.research.pipeline import (
    DirectionDataset,
    HoldoutPrediction,
    OofPrediction,
    SealedHoldoutModelRun,
    WalkForwardModelRun,
    run_sealed_holdout_model,
    run_walk_forward_model,
)
from btc_short_horizon.research.gates import (
    CalibrationBinEvidence,
    DirectionGateEvidence,
    GateDecision,
    MakerGateEvidence,
    OpeningMispricingGateEvidence,
    evaluate_direction_gate,
    evaluate_maker_gate,
    evaluate_opening_mispricing_gate,
)

__all__ = [
    "CalibrationBinEvidence",
    "DirectionDataset",
    "DirectionGateEvidence",
    "GateDecision",
    "HoldoutPrediction",
    "MakerGateEvidence",
    "OofPrediction",
    "OpeningMispricingGateEvidence",
    "ResearchSample",
    "WalkForwardConfig",
    "WalkForwardFold",
    "WalkForwardPlan",
    "build_walk_forward_plan",
    "select_complete_group_indices",
    "WalkForwardModelRun",
    "SealedHoldoutModelRun",
    "run_sealed_holdout_model",
    "run_walk_forward_model",
    "evaluate_direction_gate",
    "evaluate_maker_gate",
    "evaluate_opening_mispricing_gate",
]
