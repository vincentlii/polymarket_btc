"""Market-level probability evidence and rolling direction diagnostics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
import json
from math import isfinite, sqrt
from numbers import Integral
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from btc_short_horizon.strategy import OpeningStage


Direction = Literal["up", "down"]
TradeOutcome = Literal["win", "loss"]


@dataclass(frozen=True, slots=True)
class DirectionMarketEvidence:
    """One explicit market/stage row; no field is inferred from another in reports."""

    market_slug: str
    t0_ns: int
    stage: OpeningStage
    actual_label: int
    raw_p_up: float
    calibrated_p_up: float
    hard_model_direction: Direction
    causal_pm_direction: Direction | None
    selected_trade_side: Direction | None
    filled: bool
    trade_outcome: TradeOutcome | None

    def __post_init__(self) -> None:
        if not self.market_slug or self.market_slug.strip() != self.market_slug:
            raise ValueError("market_slug must be non-empty and trimmed")
        if isinstance(self.t0_ns, bool) or not isinstance(self.t0_ns, Integral) or self.t0_ns < 0:
            raise ValueError("t0_ns must be a non-negative integer")
        object.__setattr__(self, "stage", OpeningStage(self.stage))
        if isinstance(self.actual_label, bool) or self.actual_label not in {0, 1}:
            raise ValueError("actual_label must be 0 or 1")
        for name in ("raw_p_up", "calibrated_p_up"):
            value = getattr(self, name)
            if not isfinite(value) or not 0.0 < value < 1.0:
                raise ValueError(f"{name} must be finite and in (0, 1)")
        expected_hard = "up" if self.calibrated_p_up >= 0.5 else "down"
        if self.hard_model_direction != expected_hard:
            raise ValueError("hard_model_direction conflicts with calibrated_p_up")
        for name in ("causal_pm_direction", "selected_trade_side"):
            value = getattr(self, name)
            if value is not None and value not in {"up", "down"}:
                raise ValueError(f"{name} must be 'up', 'down', or None")
        if not isinstance(self.filled, bool):
            raise ValueError("filled must be bool")
        if self.filled and self.selected_trade_side is None:
            raise ValueError("a fill requires an explicit selected_trade_side")
        if not self.filled and self.trade_outcome is not None:
            raise ValueError("an unfilled row cannot have a trade_outcome")
        if self.trade_outcome is not None:
            winning_side = "up" if self.actual_label == 1 else "down"
            expected_outcome = "win" if self.selected_trade_side == winning_side else "loss"
            if self.trade_outcome != expected_outcome:
                raise ValueError("trade_outcome conflicts with selected side and actual label")


@dataclass(frozen=True, slots=True)
class RollingDirectionDiagnostic:
    stage: OpeningStage
    window_markets: int
    sample_count: int
    first_t0_ns: int
    last_t0_ns: int
    actual_up_rate: float
    mean_p_up: float
    hard_up_ratio: float
    bias: float
    standard_error: float
    calibration_z: float | None


def rolling_direction_diagnostics(
    evidence: Sequence[DirectionMarketEvidence],
    *,
    windows: tuple[int, ...] = (96, 384),
) -> tuple[RollingDirectionDiagnostic, ...]:
    """Summarize latest non-independent rolling windows per stage."""

    if not evidence:
        return ()
    if not windows or any(isinstance(value, bool) or value < 1 for value in windows):
        raise ValueError("rolling windows must contain positive integers")
    keys = [(item.market_slug, item.stage) for item in evidence]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate market/stage evidence")
    by_stage: dict[OpeningStage, list[DirectionMarketEvidence]] = defaultdict(list)
    for item in evidence:
        by_stage[item.stage].append(item)
    summaries: list[RollingDirectionDiagnostic] = []
    for stage in OpeningStage:
        ordered = sorted(by_stage.get(stage, ()), key=lambda item: (item.t0_ns, item.market_slug))
        for window in sorted(set(windows)):
            if len(ordered) < window:
                continue
            selected = ordered[-window:]
            probabilities = [item.calibrated_p_up for item in selected]
            actual_up = sum(item.actual_label for item in selected)
            hard_up = sum(item.hard_model_direction == "up" for item in selected)
            residual = sum(item.actual_label - item.calibrated_p_up for item in selected)
            variance = sum(value * (1.0 - value) for value in probabilities)
            standard_error = sqrt(variance) / window
            summaries.append(
                RollingDirectionDiagnostic(
                    stage=stage,
                    window_markets=window,
                    sample_count=window,
                    first_t0_ns=selected[0].t0_ns,
                    last_t0_ns=selected[-1].t0_ns,
                    actual_up_rate=actual_up / window,
                    mean_p_up=sum(probabilities) / window,
                    hard_up_ratio=hard_up / window,
                    bias=residual / window,
                    standard_error=standard_error,
                    calibration_z=None if variance <= 0.0 else residual / sqrt(variance),
                )
            )
    return tuple(summaries)


def write_direction_evidence(
    path: Path,
    evidence: Sequence[DirectionMarketEvidence],
) -> None:
    """Atomically persist the explicit evidence rows as JSON Lines."""

    rolling_direction_diagnostics(evidence, windows=(1,))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for item in sorted(
                evidence, key=lambda value: (value.t0_ns, value.market_slug, value.stage)
            ):
                handle.write(json.dumps(asdict(item), sort_keys=True, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "DirectionMarketEvidence",
    "RollingDirectionDiagnostic",
    "rolling_direction_diagnostics",
    "write_direction_evidence",
]
