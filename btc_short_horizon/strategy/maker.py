"""Causal dual-side passive order selection for Opening Mispricing."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite

from btc_short_horizon.strategy.types import (
    LayerStructure,
    MakerOrderLayer,
    OrderPlan,
    OutcomeBooks,
    SideBook,
    TokenSide,
)

_NANOS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class MakerStrategyConfig:
    """Frozen decision and lifecycle constraints shared by replay and live adapters."""

    structure: LayerStructure = LayerStructure.SINGLE
    max_shares: float = 1.0
    safety_buffer: float = 0.01
    minimum_edge: float = 0.0
    entry_start_seconds: float = 3.0
    entry_end_seconds: float = 180.0
    confirmation_signals: int = 2
    signal_cadence_seconds: float = 5.0
    signal_cadence_tolerance_seconds: float = 0.25
    max_work_seconds: float = 60.0
    stale_after_seconds: float = 1.0
    cancel_probability_drop: float = 0.03
    max_visible_depth_fraction: float = 0.05
    price_level_tick_offsets: tuple[int, ...] = (0,)

    def __post_init__(self) -> None:
        if not isinstance(self.structure, LayerStructure):
            object.__setattr__(self, "structure", LayerStructure(self.structure))
        for name, value in (
            ("max_shares", self.max_shares),
            ("safety_buffer", self.safety_buffer),
            ("minimum_edge", self.minimum_edge),
            ("entry_start_seconds", self.entry_start_seconds),
            ("entry_end_seconds", self.entry_end_seconds),
            ("signal_cadence_seconds", self.signal_cadence_seconds),
            ("signal_cadence_tolerance_seconds", self.signal_cadence_tolerance_seconds),
            ("max_work_seconds", self.max_work_seconds),
            ("stale_after_seconds", self.stale_after_seconds),
            ("cancel_probability_drop", self.cancel_probability_drop),
            ("max_visible_depth_fraction", self.max_visible_depth_fraction),
        ):
            if isinstance(value, bool) or not isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and >= 0")
        if self.max_shares <= 0.0 or self.entry_start_seconds <= 0.0:
            raise ValueError("max_shares and entry_start_seconds must be > 0")
        if self.entry_end_seconds <= self.entry_start_seconds:
            raise ValueError("entry_end_seconds must exceed entry_start_seconds")
        if self.max_work_seconds <= 0.0 or self.stale_after_seconds <= 0.0:
            raise ValueError("max_work_seconds and stale_after_seconds must be > 0")
        if self.safety_buffer + self.minimum_edge >= 1.0:
            raise ValueError("safety_buffer plus minimum_edge must be less than 1")
        if (
            isinstance(self.confirmation_signals, bool)
            or not isinstance(self.confirmation_signals, int)
            or self.confirmation_signals < 1
        ):
            raise ValueError("confirmation_signals must be an integer >= 1")
        if self.signal_cadence_seconds <= 0.0:
            raise ValueError("signal_cadence_seconds must be > 0")
        if self.signal_cadence_tolerance_seconds >= self.signal_cadence_seconds:
            raise ValueError("signal cadence tolerance must be below the cadence")
        if not 0.0 < self.max_visible_depth_fraction <= 1.0:
            raise ValueError("max_visible_depth_fraction must be within (0, 1]")
        if self.cancel_probability_drop >= 1.0:
            raise ValueError("cancel_probability_drop must be less than 1")
        if len(self.price_level_tick_offsets) != len(self.structure.allocations):
            raise ValueError("price_level_tick_offsets must match the configured layer count")
        if any(
            not isinstance(offset, int) or offset < 0 for offset in self.price_level_tick_offsets
        ):
            raise ValueError("price_level_tick_offsets must contain non-negative integers")
        if tuple(sorted(self.price_level_tick_offsets)) != self.price_level_tick_offsets:
            raise ValueError("price_level_tick_offsets must be non-decreasing")


@dataclass(frozen=True, slots=True)
class PlanDecision:
    plan: OrderPlan | None
    reason: str

    @property
    def accepted(self) -> bool:
        return self.plan is not None


@dataclass(frozen=True, slots=True)
class CancellationAssessment:
    should_cancel: bool
    reason: str


def plan_opening_mispricing_orders(
    *,
    market_slug: str,
    p_boundary_up: float,
    p_up: float,
    books: OutcomeBooks,
    decision_ts_ns: int,
    elapsed_seconds: float,
    config: MakerStrategyConfig,
) -> PlanDecision:
    """Select only the side with the highest viable passive executable edge."""

    if decision_ts_ns < 0:
        raise ValueError("decision_ts_ns must be non-negative")
    if not isfinite(p_up) or not 0.0 < p_up < 1.0:
        raise ValueError("p_up must be finite and in (0, 1)")
    if not isfinite(p_boundary_up) or not 0.0 < p_boundary_up < 1.0:
        raise ValueError("p_boundary_up must be finite and in (0, 1)")
    if not isfinite(elapsed_seconds) or elapsed_seconds < 0.0:
        raise ValueError("elapsed_seconds must be finite and >= 0")
    if not config.entry_start_seconds <= elapsed_seconds <= config.entry_end_seconds:
        return PlanDecision(plan=None, reason="outside_entry_window")

    candidates: list[OrderPlan] = []
    p_market_mid_up = books.implied_up_midpoint
    for side in (TokenSide.UP, TokenSide.DOWN):
        fair = p_up if side is TokenSide.UP else 1.0 - p_up
        boundary = p_boundary_up if side is TokenSide.UP else 1.0 - p_boundary_up
        market = p_market_mid_up if side is TokenSide.UP else 1.0 - p_market_mid_up
        book = books.for_side(side)
        layers = _build_layers(p_fair=fair, book=book, config=config)
        if not layers:
            continue
        candidates.append(
            OrderPlan(
                market_slug=market_slug,
                token_id=book.token_id,
                side=side,
                p_boundary=boundary,
                p_fair=fair,
                p_market=market,
                safety_buffer=config.safety_buffer,
                minimum_edge=config.minimum_edge,
                created_ts_ns=decision_ts_ns,
                expires_ts_ns=decision_ts_ns + round(config.max_work_seconds * _NANOS_PER_SECOND),
                layers=layers,
            )
        )
    if not candidates:
        return PlanDecision(plan=None, reason="no_passive_price_with_required_edge")
    return PlanDecision(
        plan=max(
            candidates,
            key=lambda plan: (
                plan.net_edge(plan.layers[0].price),
                plan.model_edge,
                plan.side.value,
            ),
        ),
        reason="accepted",
    )


def evaluate_cancellation(
    *,
    plan: OrderPlan,
    now_ts_ns: int,
    selected_probability: float,
    data_age_seconds: float,
    has_data_gap: bool,
    structure_valid: bool,
    tick_unchanged: bool,
    fee_unchanged: bool,
    latency_healthy: bool,
    config: MakerStrategyConfig,
) -> CancellationAssessment:
    if now_ts_ns < plan.created_ts_ns:
        raise ValueError("now_ts_ns cannot precede plan creation")
    if not isfinite(selected_probability) or not 0.0 < selected_probability < 1.0:
        raise ValueError("selected_probability must be finite and in (0, 1)")
    if not isfinite(data_age_seconds) or data_age_seconds < 0.0:
        raise ValueError("data_age_seconds must be finite and >= 0")
    if now_ts_ns >= plan.expires_ts_ns:
        return CancellationAssessment(True, "max_work_age")
    if has_data_gap:
        return CancellationAssessment(True, "data_gap")
    if data_age_seconds > config.stale_after_seconds:
        return CancellationAssessment(True, "data_stale")
    if not tick_unchanged:
        return CancellationAssessment(True, "tick_changed")
    if not fee_unchanged:
        return CancellationAssessment(True, "fee_changed")
    if not latency_healthy:
        return CancellationAssessment(True, "latency_unhealthy")
    if not structure_valid:
        return CancellationAssessment(True, "structure_invalid")
    if selected_probability <= plan.p_fair - config.cancel_probability_drop:
        return CancellationAssessment(True, "probability_drop")
    if all(
        selected_probability - layer.price - plan.safety_buffer < plan.minimum_edge
        for layer in plan.layers
    ):
        return CancellationAssessment(True, "edge_exhausted")
    return CancellationAssessment(False, "continue")


def _build_layers(
    *, p_fair: float, book: SideBook, config: MakerStrategyConfig
) -> tuple[MakerOrderLayer, ...]:
    maximum_price = p_fair - config.safety_buffer - config.minimum_edge
    layers: list[MakerOrderLayer] = []
    for index, allocation in enumerate(config.structure.allocations):
        offset = config.price_level_tick_offsets[index]
        price = _snap_down(book.best_bid - offset * book.tick_size, book.tick_size)
        if not 0.0 < price < book.best_ask or price > maximum_price + 1e-12:
            return ()
        visible_size = book.visible_bid_size_at(price)
        if visible_size <= 0.0:
            return ()
        size = min(
            config.max_shares * allocation,
            visible_size * config.max_visible_depth_fraction,
        )
        if size + 1e-12 < book.minimum_order_size:
            return ()
        layers.append(MakerOrderLayer(price=price, size=size, visible_size=visible_size))
    return tuple(layers)


def _snap_down(value: float, tick_size: float) -> float:
    snapped = floor((value + 1e-12) / tick_size) * tick_size
    return round(snapped, 12)
