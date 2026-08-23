"""Three-stage OOF research for logit(q_pm) plus a calibrated residual model."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from typing import Callable

import numpy as np
from nautilus_trader.model.enums import LiquiditySide
from prediction_market_extensions.adapters.polymarket.parsing import calculate_commission

from btc_short_horizon.models import DirectionModelConfig
from btc_short_horizon.models.market_relative import fit_market_relative_offset_model
from btc_short_horizon.models.market_relative_lightgbm import (
    fit_market_relative_lightgbm,
    minimum_leaf_unique_market_count,
)
from btc_short_horizon.research.market_relative_uncertainty import (
    BlockUnit,
    paired_block_lower_bound,
)
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.stage_aware_models import select_stage_dataset
from btc_short_horizon.research.walk_forward import WalkForwardConfig, build_walk_forward_plan
from btc_short_horizon.strategy import OpeningStage, StagePolicyConfig


@dataclass(frozen=True, slots=True)
class AnchoredDirectionDataset:
    dataset: DirectionDataset
    market_up_probabilities: np.ndarray
    up_best_asks: np.ndarray
    down_best_asks: np.ndarray
    up_best_ask_sizes: np.ndarray
    down_best_ask_sizes: np.ndarray
    fee_rates: np.ndarray
    fee_rule_hash: str

    def __post_init__(self) -> None:
        anchors = np.array(self.market_up_probabilities, dtype=float, copy=True)
        if anchors.ndim != 1 or len(anchors) != len(self.dataset.samples):
            raise ValueError("market anchors must align one-to-one with dataset samples")
        if not np.isfinite(anchors).all() or np.any((anchors <= 0.0) | (anchors >= 1.0)):
            raise ValueError("market anchors must be finite and in (0, 1)")
        anchors.setflags(write=False)
        object.__setattr__(self, "market_up_probabilities", anchors)
        for name in (
            "up_best_asks",
            "down_best_asks",
            "up_best_ask_sizes",
            "down_best_ask_sizes",
            "fee_rates",
        ):
            values = np.array(getattr(self, name), dtype=float, copy=True)
            if values.ndim != 1 or len(values) != len(self.dataset.samples):
                raise ValueError(f"{name} must align with dataset samples")
            if not np.isfinite(values).all() or np.any(values < 0.0):
                raise ValueError(f"{name} must be finite and non-negative")
            values.setflags(write=False)
            object.__setattr__(self, name, values)
        if not self.fee_rule_hash:
            raise ValueError("fee_rule_hash is required")


def run_market_relative_stage_oof(
    anchored: AnchoredDirectionDataset,
    *,
    split_config: WalkForwardConfig | None = None,
    logistic_c: float = 0.1,
    calibration_method: str = "auto",
    execution_cost_per_share: float = 0.01,
    minimum_trade_edge: float = 0.0,
    minimum_order_size: float = 5.0,
    tail_quarantine_price: float = 0.35,
    minimum_side_opportunities: int = 30,
    maximum_side_share: float = 0.80,
    confidence: float = 0.95,
    resamples: int = 5_000,
    seed: int = 17,
    fit_model: Callable[..., object] = fit_market_relative_offset_model,
    model_config: DirectionModelConfig | None = None,
    stage_policy: StagePolicyConfig | None = None,
) -> dict[str, object]:
    """Fit and calibrate one residual Logistic per stage, always against q_pm."""

    if not isfinite(execution_cost_per_share) or execution_cost_per_share < 0.0:
        raise ValueError("execution_cost_per_share must be finite and non-negative")
    if not isfinite(minimum_trade_edge) or minimum_trade_edge < 0.0:
        raise ValueError("minimum_trade_edge must be finite and non-negative")
    if minimum_side_opportunities < 1:
        raise ValueError("minimum_side_opportunities must be >= 1")
    if not isfinite(maximum_side_share) or not 0.5 < maximum_side_share < 1.0:
        raise ValueError("maximum_side_share must be in (0.5, 1)")
    effective_policy = stage_policy or StagePolicyConfig.default()
    result = {}
    for stage in OpeningStage:
        stage_rule = next(
            (rule for rule in effective_policy.rules if rule.stage is stage),
            None,
        )
        if stage_rule is None:
            raise ValueError(f"stage {stage.value} is not present in policy")
        effective_minimum_edge = max(minimum_trade_edge, stage_rule.minimum_net_edge)
        selected, selected_anchors, execution = _select_stage(
            anchored, stage, policy=effective_policy
        )
        plan = build_walk_forward_plan(selected.samples, config=split_config)
        candidate = np.full(len(selected.samples), np.nan)
        candidate_lower = np.full(len(selected.samples), np.nan)
        candidate_upper = np.full(len(selected.samples), np.nan)
        robust_interval_available = True
        fold_number = np.full(len(selected.samples), -1, dtype=int)
        leaf_minimums: list[int] = []
        effective_model = model_config or DirectionModelConfig(
            kind="logistic",
            logistic_c=logistic_c,
            calibration_method=calibration_method,
            random_seed=seed,
        )
        for fold_index, fold in enumerate(plan.folds):
            train = np.asarray(fold.train_indices, dtype=int)
            calibration = np.asarray(fold.calibration_indices, dtype=int)
            test = np.asarray(fold.test_indices, dtype=int)
            if effective_model.kind == "lightgbm":
                model = fit_market_relative_lightgbm(
                    train_vectors=selected.vectors[train],
                    train_labels=selected.labels[train],
                    train_market_up_probability=selected_anchors[train],
                    validation_vectors=selected.vectors[calibration],
                    validation_labels=selected.labels[calibration],
                    validation_market_up_probability=selected_anchors[calibration],
                    schema=selected.schema,
                    probability_uncertainty_radius=None,
                    num_leaves=effective_model.lightgbm_num_leaves,
                    max_depth=effective_model.lightgbm_max_depth,
                    min_child_samples=effective_model.lightgbm_min_child_samples,
                    learning_rate=effective_model.lightgbm_learning_rate,
                    n_estimators=effective_model.lightgbm_n_estimators,
                    early_stopping_rounds=effective_model.lightgbm_early_stopping_rounds,
                    random_seed=effective_model.random_seed,
                    train_weights=selected.sample_weights[train],
                    validation_weights=selected.sample_weights[calibration],
                    validation_market_ids=tuple(
                        selected.samples[index].group_id for index in calibration
                    ),
                )
                leaf_minimums.append(
                    minimum_leaf_unique_market_count(
                        leaf_indices=np.asarray(
                            model.booster.predict(
                                selected.vectors[train],
                                pred_leaf=True,
                                num_iteration=model.booster.best_iteration,
                            )
                        ),
                        market_slugs=tuple(selected.samples[index].group_id for index in train),
                    )
                )
            else:
                model = fit_model(
                    train_vectors=selected.vectors[train],
                    train_labels=selected.labels[train],
                    train_market_up_probability=selected_anchors[train],
                    calibration_vectors=selected.vectors[calibration],
                    calibration_labels=selected.labels[calibration],
                    calibration_market_up_probability=selected_anchors[calibration],
                    schema=selected.schema,
                    logistic_c=effective_model.logistic_c,
                    train_weights=selected.sample_weights[train],
                    calibration_weights=selected.sample_weights[calibration],
                    random_seed=effective_model.random_seed,
                    calibration_method=effective_model.calibration_method,
                    calibration_independent_market_count=len(
                        {selected.samples[index].group_id for index in calibration}
                    ),
                )
            point = model.predict_up_probability(selected.vectors[test], selected_anchors[test])
            candidate[test] = point
            interval_predictor = getattr(model, "predict_probability_intervals", None)
            if interval_predictor is None:
                robust_interval_available = False
                candidate_lower[test] = point
                candidate_upper[test] = point
            else:
                intervals = interval_predictor(selected.vectors[test], selected_anchors[test])
                candidate_lower[test] = [value.up_lower for value in intervals]
                candidate_upper[test] = [value.up_upper for value in intervals]
            fold_number[test] = fold_index
        indices = np.flatnonzero(np.isfinite(candidate))
        if not len(indices):
            raise ValueError(f"stage {stage.value} produced no OOF predictions")
        labels = selected.labels[indices].astype(float)
        model_p = candidate[indices]
        model_lower = candidate_lower[indices]
        model_upper = candidate_upper[indices]
        market_p = selected_anchors[indices]
        scores = _paired_scores(
            labels=labels,
            model=model_p,
            market=market_p,
        )
        opportunities = _execution_scores(
            labels=labels,
            up_fair=model_lower,
            down_fair=1.0 - model_upper,
            up_asks=execution[0][indices],
            down_asks=execution[1][indices],
            up_sizes=execution[2][indices],
            down_sizes=execution[3][indices],
            fee_rates=execution[4][indices],
            minimum_edge=effective_minimum_edge,
            execution_cost=execution_cost_per_share,
            minimum_order_size=minimum_order_size,
            tail_quarantine_price=tail_quarantine_price,
        )
        point_opportunities = _execution_scores(
            labels=labels,
            up_fair=model_p,
            down_fair=1.0 - model_p,
            up_asks=execution[0][indices],
            down_asks=execution[1][indices],
            up_sizes=execution[2][indices],
            down_sizes=execution[3][indices],
            fee_rates=execution[4][indices],
            minimum_edge=effective_minimum_edge,
            execution_cost=execution_cost_per_share,
            minimum_order_size=minimum_order_size,
            tail_quarantine_price=tail_quarantine_price,
        )
        market_ids = tuple(selected.samples[index].group_id for index in indices)
        market_times = tuple(selected.samples[index].feature_ts for index in indices)
        opportunity_mask = _earliest_market_opportunity_mask(
            candidate_mask=opportunities[0],
            market_ids=market_ids,
            market_times=market_times,
        )
        point_opportunity_mask = _earliest_market_opportunity_mask(
            candidate_mask=point_opportunities[0],
            market_ids=market_ids,
            market_times=market_times,
        )
        realized_net_ev = opportunities[1][opportunity_mask]
        point_realized_net_ev = point_opportunities[1][point_opportunity_mask]
        scores["net_ev"] = (realized_net_ev, np.zeros(len(realized_net_ev)))
        fold_metrics = _fold_metrics(
            fold_numbers=fold_number[indices],
            scores=scores,
            opportunity_mask=opportunity_mask,
            market_ids=market_ids,
        )
        direction_balance = _direction_balance(
            labels=labels,
            model=model_p,
            market=market_p,
            market_ids=market_ids,
            opportunity_mask=opportunity_mask,
            selected_sides=opportunities[2],
            realized_net_ev=realized_net_ev,
            minimum_side_opportunities=minimum_side_opportunities,
            maximum_side_share=maximum_side_share,
        )
        point_direction_balance = _direction_balance(
            labels=labels,
            model=model_p,
            market=market_p,
            market_ids=market_ids,
            opportunity_mask=point_opportunity_mask,
            selected_sides=point_opportunities[2],
            realized_net_ev=point_realized_net_ev,
            minimum_side_opportunities=minimum_side_opportunities,
            maximum_side_share=maximum_side_share,
        )
        point_market_ids = tuple(
            value for value, keep in zip(market_ids, point_opportunity_mask, strict=True) if keep
        )
        point_market_times = tuple(
            value for value, keep in zip(market_times, point_opportunity_mask, strict=True) if keep
        )
        result[stage.value] = {
            "oof_prediction_count": len(indices),
            "oof_market_count": len(set(market_ids)),
            "calibration_method": calibration_method,
            "calibration_selection": _calibration_selection(model),
            "model_kind": effective_model.kind,
            "minimum_leaf_unique_market_count": min(leaf_minimums) if leaf_minimums else None,
            "fold_metrics": fold_metrics,
            "execution": {
                "evidence_level": "bbo_size_proxy_requires_full_depth_exit_replay",
                "fee_rule_hash": anchored.fee_rule_hash,
                "minimum_net_edge": effective_minimum_edge,
                "probability_basis": (
                    "robust_interval" if robust_interval_available else "point_test_double_fallback"
                ),
                "mean_probability_interval_width": float(np.mean(model_upper - model_lower)),
                "opportunity_count": int(np.count_nonzero(opportunity_mask)),
                "point_probability_opportunity_count": int(
                    np.count_nonzero(point_opportunity_mask)
                ),
                "uncertainty_filtered_market_count": int(
                    np.count_nonzero(point_opportunity_mask & ~opportunity_mask)
                ),
                "up_count": int(np.count_nonzero(opportunities[2][opportunity_mask] == 1)),
                "down_count": int(np.count_nonzero(opportunities[2][opportunity_mask] == -1)),
                "tail_quarantined_market_count": len(
                    {
                        market_id
                        for market_id, is_tail in zip(market_ids, opportunities[3], strict=True)
                        if is_tail
                    }
                ),
            },
            "point_probability_diagnostic": {
                "status": "diagnostic_not_execution_evidence",
                "direction_balance": point_direction_balance,
                "net_ev": _metric_summary(
                    "net_ev",
                    candidate=point_realized_net_ev,
                    baseline=np.zeros(len(point_realized_net_ev)),
                    market_ids=point_market_ids,
                ),
                "p_lower": {
                    unit.value: _paired_lower_bound_receipt(
                        candidate=point_realized_net_ev,
                        baseline=np.zeros(len(point_realized_net_ev)),
                        market_ids=point_market_ids,
                        market_times=point_market_times,
                        unit=unit,
                        confidence=confidence,
                        resamples=resamples,
                        seed=seed,
                    )
                    for unit in BlockUnit
                },
            },
            "direction_balance": direction_balance,
            "metrics": {
                name: _metric_summary(
                    name,
                    candidate=values[0],
                    baseline=values[1],
                    market_ids=(
                        tuple(
                            value
                            for value, keep in zip(market_ids, opportunity_mask, strict=True)
                            if keep
                        )
                        if name == "net_ev"
                        else market_ids
                    ),
                )
                for name, values in scores.items()
            },
            "p_lower": {
                name: {
                    unit.value: _paired_lower_bound_receipt(
                        candidate=values[0],
                        baseline=values[1],
                        market_ids=(
                            tuple(
                                value
                                for value, keep in zip(market_ids, opportunity_mask, strict=True)
                                if keep
                            )
                            if name == "net_ev"
                            else market_ids
                        ),
                        market_times=(
                            tuple(
                                value
                                for value, keep in zip(market_times, opportunity_mask, strict=True)
                                if keep
                            )
                            if name == "net_ev"
                            else market_times
                        ),
                        unit=unit,
                        confidence=confidence,
                        resamples=resamples,
                        seed=seed,
                    )
                    for unit in BlockUnit
                }
                for name, values in scores.items()
            },
        }
    return result


def _direction_balance(
    *,
    labels: np.ndarray,
    model: np.ndarray,
    market: np.ndarray,
    market_ids: tuple[str, ...],
    opportunity_mask: np.ndarray,
    selected_sides: np.ndarray,
    realized_net_ev: np.ndarray,
    minimum_side_opportunities: int,
    maximum_side_share: float,
) -> dict[str, object]:
    selected = selected_sides[opportunity_mask]
    up = selected == 1
    down = selected == -1
    up_count = int(np.count_nonzero(up))
    down_count = int(np.count_nonzero(down))
    total = up_count + down_count
    selected_labels = labels[opportunity_mask]
    wins = np.where(up, selected_labels == 1.0, selected_labels == 0.0)
    up_ev = realized_net_ev[up]
    down_ev = realized_net_ev[down]
    selected_up_share = float(up_count / total) if total else 0.0
    gate = (
        up_count >= minimum_side_opportunities
        and down_count >= minimum_side_opportunities
        and 1.0 - maximum_side_share <= selected_up_share <= maximum_side_share
    )
    return {
        "outcome_up_rate": _market_first_mean(labels, market_ids),
        "model_up_rate": _market_first_mean(model >= 0.5, market_ids),
        "market_up_rate": _market_first_mean(market >= 0.5, market_ids),
        "opportunity_count": total,
        "selected_up_count": up_count,
        "selected_down_count": down_count,
        "selected_up_share": selected_up_share,
        "selected_accuracy": float(np.mean(wins)) if total else None,
        "selected_up_net_ev": float(np.mean(up_ev)) if up_count else None,
        "selected_down_net_ev": float(np.mean(down_ev)) if down_count else None,
        "minimum_side_opportunities": minimum_side_opportunities,
        "maximum_side_share": maximum_side_share,
        "gate_passed": gate,
    }


def asdict_lower_bound(value) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return {
        "lower_bound": value.lower_bound,
        "p_value": value.p_value,
        "point_estimate": value.point_estimate,
        "independent_market_count": value.independent_market_count,
        "block_count": value.block_count,
        "aggregation": value.aggregation,
    }


def _select_stage(
    anchored: AnchoredDirectionDataset,
    stage: OpeningStage,
    *,
    policy: StagePolicyConfig | None = None,
) -> tuple[DirectionDataset, np.ndarray, tuple[np.ndarray, ...]]:
    selected = select_stage_dataset(anchored.dataset, stage=stage, policy=policy)
    indices = {sample.sample_id: index for index, sample in enumerate(anchored.dataset.samples)}
    selected_indices = [indices[sample.sample_id] for sample in selected.samples]
    return (
        selected,
        anchored.market_up_probabilities[selected_indices],
        tuple(
            getattr(anchored, name)[selected_indices]
            for name in (
                "up_best_asks",
                "down_best_asks",
                "up_best_ask_sizes",
                "down_best_ask_sizes",
                "fee_rates",
            )
        ),
    )


def _paired_scores(
    *, labels: np.ndarray, model: np.ndarray, market: np.ndarray
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    clipped_model = np.clip(model, 1e-6, 1.0 - 1e-6)
    clipped_market = np.clip(market, 1e-6, 1.0 - 1e-6)
    model_log = labels * np.log(clipped_model) + (1.0 - labels) * np.log1p(-clipped_model)
    market_log = labels * np.log(clipped_market) + (1.0 - labels) * np.log1p(-clipped_market)
    model_brier = -((labels - clipped_model) ** 2)
    market_brier = -((labels - clipped_market) ** 2)
    return {
        "log_loss": (model_log, market_log),
        "brier": (model_brier, market_brier),
    }


def _execution_scores(
    *,
    labels,
    up_fair,
    down_fair,
    up_asks,
    down_asks,
    up_sizes,
    down_sizes,
    fee_rates,
    minimum_edge,
    execution_cost,
    minimum_order_size,
    tail_quarantine_price,
):  # type: ignore[no-untyped-def]
    up_fees = np.asarray(
        [_fee_per_share(price, rate) for price, rate in zip(up_asks, fee_rates, strict=True)]
    )
    down_fees = np.asarray(
        [_fee_per_share(price, rate) for price, rate in zip(down_asks, fee_rates, strict=True)]
    )
    up_edge = up_fair - up_asks - up_fees - execution_cost
    down_edge = down_fair - down_asks - down_fees - execution_cost
    up_ok = (
        (up_edge + 1e-12 >= minimum_edge)
        & (up_sizes >= minimum_order_size)
        & (up_asks >= tail_quarantine_price)
    )
    down_ok = (
        (down_edge + 1e-12 >= minimum_edge)
        & (down_sizes >= minimum_order_size)
        & (down_asks >= tail_quarantine_price)
    )
    sides = np.where(up_ok & (up_edge >= down_edge), 1, np.where(down_ok, -1, 0))
    mask = sides != 0
    realized = np.where(
        sides == 1,
        labels - up_asks - up_fees - execution_cost,
        np.where(
            sides == -1,
            (1.0 - labels) - down_asks - down_fees - execution_cost,
            np.nan,
        ),
    )
    tail = ((up_asks < tail_quarantine_price) & (up_edge + 1e-12 >= minimum_edge)) | (
        (down_asks < tail_quarantine_price) & (down_edge + 1e-12 >= minimum_edge)
    )
    return mask, realized, sides, tail


def _earliest_market_opportunity_mask(
    *,
    candidate_mask: np.ndarray,
    market_ids: tuple[str, ...],
    market_times: tuple[object, ...],
) -> np.ndarray:
    """Keep one executable decision per market, matching the 1x5 Paper contract."""

    selected = np.zeros(len(candidate_mask), dtype=bool)
    earliest: dict[str, tuple[object, int]] = {}
    for index, (eligible, market_id, feature_ts) in enumerate(
        zip(candidate_mask, market_ids, market_times, strict=True)
    ):
        if not eligible:
            continue
        current = earliest.get(market_id)
        if current is None or feature_ts < current[0]:
            earliest[market_id] = (feature_ts, index)
    for _, index in earliest.values():
        selected[index] = True
    return selected


def _fee_per_share(price: float, rate: float) -> float:
    return calculate_commission(
        quantity=Decimal("1"),
        price=Decimal(str(price)),
        fee_rate=Decimal(str(rate)),
        liquidity_side=LiquiditySide.TAKER,
    )


def _calibration_selection(model: object) -> object:
    selection = getattr(model, "calibration_selection", None)
    return None if selection is None else getattr(selection, "selected_method", str(selection))


def _metric_summary(
    name: str,
    *,
    candidate: np.ndarray,
    baseline: np.ndarray,
    market_ids: tuple[str, ...],
) -> dict[str, float]:
    if not len(candidate):
        return {"candidate": 0.0, "q_pm_baseline": 0.0, "improvement": 0.0}
    loss_sign = -1.0 if name in {"log_loss", "brier"} else 1.0
    candidate_mean = _market_first_mean(candidate, market_ids)
    baseline_mean = _market_first_mean(baseline, market_ids)
    return {
        "candidate": loss_sign * candidate_mean,
        "q_pm_baseline": loss_sign * baseline_mean,
        "improvement": _market_first_mean(candidate - baseline, market_ids),
    }


def _fold_metrics(
    *,
    fold_numbers: np.ndarray,
    scores: dict[str, tuple[np.ndarray, np.ndarray]],
    opportunity_mask: np.ndarray,
    market_ids: tuple[str, ...],
) -> tuple[dict[str, float], ...]:
    net_values = np.full(len(fold_numbers), np.nan)
    net_values[opportunity_mask] = scores["net_ev"][0]
    rows = []
    for fold in sorted(set(int(value) for value in fold_numbers if value >= 0)):
        selected = fold_numbers == fold
        net_selected = net_values[selected]
        finite_net = net_selected[np.isfinite(net_selected)]
        conditional_net_ev = float(np.mean(finite_net)) if len(finite_net) else 0.0
        eligible_market_count = len(
            {market_id for market_id, keep in zip(market_ids, selected, strict=True) if keep}
        )
        execution_score = (
            float(np.sum(finite_net) / eligible_market_count) if eligible_market_count else 0.0
        )
        rows.append(
            {
                "fold": float(fold),
                "log_loss": _market_first_mean(
                    scores["log_loss"][0][selected] - scores["log_loss"][1][selected],
                    tuple(
                        market_id
                        for market_id, keep in zip(market_ids, selected, strict=True)
                        if keep
                    ),
                ),
                "brier": _market_first_mean(
                    scores["brier"][0][selected] - scores["brier"][1][selected],
                    tuple(
                        market_id
                        for market_id, keep in zip(market_ids, selected, strict=True)
                        if keep
                    ),
                ),
                "net_ev": conditional_net_ev,
                "execution_score": execution_score,
                "opportunity_count": float(len(finite_net)),
                "eligible_market_count": float(eligible_market_count),
            }
        )
    return tuple(rows)


def _market_first_mean(values: np.ndarray, market_ids: tuple[str, ...]) -> float:
    numeric = np.asarray(values, dtype=float)
    if numeric.ndim != 1 or len(numeric) != len(market_ids) or not len(numeric):
        raise ValueError("market-first mean requires aligned non-empty values")
    grouped: dict[str, list[float]] = {}
    for market_id, value in zip(market_ids, numeric, strict=True):
        grouped.setdefault(market_id, []).append(float(value))
    return float(np.mean([np.mean(items) for items in grouped.values()]))


def _paired_lower_bound_receipt(
    *,
    candidate: np.ndarray,
    baseline: np.ndarray,
    market_ids: tuple[str, ...],
    market_times: tuple[object, ...],
    unit: BlockUnit,
    confidence: float,
    resamples: int,
    seed: int,
) -> dict[str, object]:
    try:
        return asdict_lower_bound(
            paired_block_lower_bound(
                candidate=candidate,
                baseline=baseline,
                market_ids=market_ids,
                market_times=market_times,
                unit=unit,
                confidence=confidence,
                resamples=resamples,
                seed=seed,
            )
        )
    except ValueError as exc:
        message = str(exc)
        if (
            "at least two independent markets" not in message
            and "requires at least two blocks" not in message
        ):
            raise
    differences = np.asarray(candidate, dtype=float) - np.asarray(baseline, dtype=float)
    point = _market_first_mean(differences, market_ids) if len(differences) else 0.0
    return {
        "lower_bound": min(0.0, point),
        "p_value": 1.0,
        "point_estimate": point,
        "independent_market_count": len(set(market_ids)),
        "block_count": _block_count(market_ids, market_times, unit),
        "aggregation": "insufficient_independent_blocks_fail_closed",
    }


def _block_count(
    market_ids: tuple[str, ...],
    market_times: tuple[object, ...],
    unit: BlockUnit,
) -> int:
    if unit is BlockUnit.MARKET:
        return len(set(market_ids))
    blocks = set()
    for value in market_times:
        if not hasattr(value, "date"):
            raise ValueError("market_times must contain datetimes")
        if unit is BlockUnit.DAY:
            blocks.add(value.date().isoformat())
        else:
            year, week, _weekday = value.isocalendar()
            blocks.add((year, week))
    return len(blocks)


__all__ = ["AnchoredDirectionDataset", "run_market_relative_stage_oof"]
