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
    result = {}
    for stage in OpeningStage:
        selected, selected_anchors, execution = _select_stage(anchored, stage, policy=stage_policy)
        plan = build_walk_forward_plan(selected.samples, config=split_config)
        candidate = np.full(len(selected.samples), np.nan)
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
                    probability_uncertainty_radius=0.03,
                    num_leaves=effective_model.lightgbm_num_leaves,
                    max_depth=effective_model.lightgbm_max_depth,
                    min_child_samples=effective_model.lightgbm_min_child_samples,
                    learning_rate=effective_model.lightgbm_learning_rate,
                    n_estimators=effective_model.lightgbm_n_estimators,
                    early_stopping_rounds=effective_model.lightgbm_early_stopping_rounds,
                    random_seed=effective_model.random_seed,
                    train_weights=selected.sample_weights[train],
                    validation_weights=selected.sample_weights[calibration],
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
            candidate[test] = model.predict_up_probability(
                selected.vectors[test], selected_anchors[test]
            )
            fold_number[test] = fold_index
        indices = np.flatnonzero(np.isfinite(candidate))
        if not len(indices):
            raise ValueError(f"stage {stage.value} produced no OOF predictions")
        labels = selected.labels[indices].astype(float)
        model_p = candidate[indices]
        market_p = selected_anchors[indices]
        scores = _paired_scores(
            labels=labels,
            model=model_p,
            market=market_p,
            cost=execution_cost_per_share,
            minimum_edge=minimum_trade_edge,
        )
        opportunities = _execution_scores(
            labels=labels,
            model=model_p,
            up_asks=execution[0][indices],
            down_asks=execution[1][indices],
            up_sizes=execution[2][indices],
            down_sizes=execution[3][indices],
            fee_rates=execution[4][indices],
            minimum_edge=minimum_trade_edge,
            execution_cost=execution_cost_per_share,
            minimum_order_size=minimum_order_size,
            tail_quarantine_price=tail_quarantine_price,
        )
        scores["net_ev"] = (opportunities[1], np.zeros(len(opportunities[1])))
        fold_metrics = _fold_metrics(
            fold_numbers=fold_number[indices],
            scores=scores,
            opportunity_mask=opportunities[0],
        )
        market_ids = tuple(selected.samples[index].group_id for index in indices)
        market_times = tuple(selected.samples[index].feature_ts for index in indices)
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
                "opportunity_count": int(np.count_nonzero(opportunities[0])),
                "up_count": int(np.count_nonzero(opportunities[2] == 1)),
                "down_count": int(np.count_nonzero(opportunities[2] == -1)),
                "tail_quarantined_count": opportunities[3],
            },
            "metrics": {
                name: _metric_summary(name, candidate=values[0], baseline=values[1])
                for name, values in scores.items()
            },
            "p_lower": {
                name: {
                    unit.value: asdict_lower_bound(
                        paired_block_lower_bound(
                            candidate=values[0],
                            baseline=values[1],
                            market_ids=(
                                tuple(
                                    value
                                    for value, keep in zip(
                                        market_ids, opportunities[0], strict=True
                                    )
                                    if keep
                                )
                                if name == "net_ev"
                                else market_ids
                            ),
                            market_times=(
                                tuple(
                                    value
                                    for value, keep in zip(
                                        market_times, opportunities[0], strict=True
                                    )
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
                    )
                    for unit in BlockUnit
                }
                for name, values in scores.items()
            },
        }
    return result


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
    *, labels: np.ndarray, model: np.ndarray, market: np.ndarray, cost: float, minimum_edge: float
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
    model,
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
    up_edge = model - up_asks - up_fees - execution_cost
    down_edge = (1.0 - model) - down_asks - down_fees - execution_cost
    up_ok = (
        (up_edge > minimum_edge)
        & (up_sizes >= minimum_order_size)
        & (up_asks >= tail_quarantine_price)
    )
    down_ok = (
        (down_edge > minimum_edge)
        & (down_sizes >= minimum_order_size)
        & (down_asks >= tail_quarantine_price)
    )
    sides = np.where(up_ok & (up_edge >= down_edge), 1, np.where(down_ok, -1, 0))
    mask = sides != 0
    realized = np.where(
        sides[mask] == 1,
        labels[mask] - up_asks[mask] - up_fees[mask] - execution_cost,
        (1.0 - labels[mask]) - down_asks[mask] - down_fees[mask] - execution_cost,
    )
    tail = int(
        np.count_nonzero(
            ((up_asks < tail_quarantine_price) & (up_edge > minimum_edge))
            | ((down_asks < tail_quarantine_price) & (down_edge > minimum_edge))
        )
    )
    return mask, realized, sides, tail


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


def _metric_summary(name: str, *, candidate: np.ndarray, baseline: np.ndarray) -> dict[str, float]:
    loss_sign = -1.0 if name in {"log_loss", "brier"} else 1.0
    return {
        "candidate": float(loss_sign * np.mean(candidate)),
        "q_pm_baseline": float(loss_sign * np.mean(baseline)),
        "improvement": float(np.mean(candidate - baseline)),
    }


def _fold_metrics(
    *,
    fold_numbers: np.ndarray,
    scores: dict[str, tuple[np.ndarray, np.ndarray]],
    opportunity_mask: np.ndarray,
) -> tuple[dict[str, float], ...]:
    net_values = np.full(len(fold_numbers), np.nan)
    net_values[opportunity_mask] = scores["net_ev"][0]
    rows = []
    for fold in sorted(set(int(value) for value in fold_numbers if value >= 0)):
        selected = fold_numbers == fold
        net_selected = net_values[selected]
        finite_net = net_selected[np.isfinite(net_selected)]
        net_ev = float(np.mean(finite_net)) if len(finite_net) else 0.0
        rows.append(
            {
                "fold": float(fold),
                "log_loss": float(
                    np.mean(scores["log_loss"][0][selected] - scores["log_loss"][1][selected])
                ),
                "brier": float(
                    np.mean(scores["brier"][0][selected] - scores["brier"][1][selected])
                ),
                "net_ev": net_ev,
                "execution_score": net_ev,
                "opportunity_count": float(len(finite_net)),
            }
        )
    return tuple(rows)


__all__ = ["AnchoredDirectionDataset", "run_market_relative_stage_oof"]
