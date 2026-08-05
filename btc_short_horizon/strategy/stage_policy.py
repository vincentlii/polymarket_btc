"""Explicit stage policy for the BTC 15m post-open taker challenger.

The policy is intentionally separate from order-book planning.  It is the
small seam where research can replace stage thresholds or model bundles
without changing the execution simulator or the runtime event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


class OpeningStage(StrEnum):
    EARLY = "early_3s_to_30s"
    PRICE_DISCOVERY = "price_discovery_35s_to_90s"
    MID_EARLY = "mid_early_95s_to_180s"


@dataclass(frozen=True, slots=True)
class StageRule:
    stage: OpeningStage
    start_seconds: float
    end_seconds: float
    minimum_net_edge: float
    minimum_price: float = 0.20
    maximum_price: float = 0.80
    confirmation_signals: int = 2
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.end_seconds < self.start_seconds:
            raise ValueError("stage end_seconds must be >= start_seconds")
        for name, value in (
            ("start_seconds", self.start_seconds),
            ("end_seconds", self.end_seconds),
            ("minimum_net_edge", self.minimum_net_edge),
            ("minimum_price", self.minimum_price),
            ("maximum_price", self.maximum_price),
        ):
            if isinstance(value, bool) or not isfinite(value):
                raise ValueError(f"stage {name} must be finite")
        if self.start_seconds < 0.0 or self.minimum_net_edge < 0.0:
            raise ValueError("stage timing and edge must be non-negative")
        if not 0.0 < self.minimum_price < self.maximum_price < 1.0:
            raise ValueError("stage price bounds must satisfy 0 < min < max < 1")
        if (
            isinstance(self.confirmation_signals, bool)
            or not isinstance(self.confirmation_signals, int)
            or self.confirmation_signals < 1
        ):
            raise ValueError("stage confirmation_signals must be an integer >= 1")
        if not isinstance(self.enabled, bool):
            raise ValueError("stage enabled must be bool")


@dataclass(frozen=True, slots=True)
class StagePolicyConfig:
    rules: tuple[StageRule, ...]
    tolerance_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError("stage policy requires at least one rule")
        stages = [rule.stage for rule in self.rules]
        if len(set(stages)) != len(stages):
            raise ValueError("stage policy stages must be unique")
        ordered = sorted(self.rules, key=lambda rule: rule.start_seconds)
        if tuple(ordered) != self.rules:
            raise ValueError("stage policy rules must be ordered by start_seconds")
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.start_seconds <= previous.end_seconds:
                raise ValueError("stage policy rules must not overlap")
        if isinstance(self.tolerance_seconds, bool) or not isfinite(self.tolerance_seconds):
            raise ValueError("stage tolerance_seconds must be finite")
        if self.tolerance_seconds < 0.0:
            raise ValueError("stage tolerance_seconds must be >= 0")

    @classmethod
    def default(cls) -> StagePolicyConfig:
        return cls(
            rules=(
                StageRule(
                    stage=OpeningStage.EARLY,
                    start_seconds=3.0,
                    end_seconds=30.0,
                    minimum_net_edge=0.04,
                    confirmation_signals=2,
                ),
                StageRule(
                    stage=OpeningStage.PRICE_DISCOVERY,
                    start_seconds=35.0,
                    end_seconds=90.0,
                    minimum_net_edge=0.035,
                    confirmation_signals=2,
                ),
                StageRule(
                    stage=OpeningStage.MID_EARLY,
                    start_seconds=95.0,
                    end_seconds=180.0,
                    minimum_net_edge=0.03,
                    confirmation_signals=3,
                ),
            )
        )

    def rule_for(self, elapsed_seconds: float) -> StageRule:
        if isinstance(elapsed_seconds, bool) or not isfinite(elapsed_seconds):
            raise ValueError("elapsed_seconds must be finite")
        for rule in self.rules:
            if rule.start_seconds <= elapsed_seconds <= rule.end_seconds:
                if not rule.enabled:
                    raise ValueError(f"stage {rule.stage.value} is disabled")
                return rule
        for rule in self.rules:
            nearest = min(
                abs(elapsed_seconds - rule.start_seconds), abs(elapsed_seconds - rule.end_seconds)
            )
            if nearest <= self.tolerance_seconds and rule.enabled:
                return rule
        raise ValueError("elapsed_seconds falls outside stage policy")


__all__ = ["OpeningStage", "StagePolicyConfig", "StageRule"]
