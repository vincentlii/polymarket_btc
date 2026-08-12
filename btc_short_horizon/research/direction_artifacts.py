"""Strict provenance contract for BTC direction probability artifacts."""

from __future__ import annotations

from dataclasses import asdict
from numbers import Integral
from typing import Mapping

from btc_short_horizon.models import FittedDirectionModel
from btc_short_horizon.research.opening_dataset import (
    MARKET_STAGE_WEIGHTING_PROTOCOL,
    market_stage_weighting_hash,
)
from btc_short_horizon.research.walk_forward import WalkForwardConfig


def build_direction_artifact_contract(
    *,
    model: FittedDirectionModel,
    rule_epoch: str,
    training_start_ns: int,
    training_end_ns: int,
    calibration_start_ns: int,
    calibration_end_ns: int,
    market_count: int,
    up_market_count: int,
    down_market_count: int,
    weighting_hash: str,
    split_config: WalkForwardConfig,
    source_hashes: Mapping[str, str],
) -> dict[str, object]:
    if not rule_epoch or rule_epoch.strip() != rule_epoch:
        raise ValueError("rule_epoch must be non-empty and trimmed")
    times = (training_start_ns, training_end_ns, calibration_start_ns, calibration_end_ns)
    if any(
        isinstance(value, bool) or not isinstance(value, Integral) or value < 0 for value in times
    ):
        raise ValueError("artifact time bounds must be non-negative integers")
    if not training_start_ns <= training_end_ns < calibration_start_ns <= calibration_end_ns:
        raise ValueError("artifact time bounds must be chronological and disjoint")
    counts = (market_count, up_market_count, down_market_count)
    if any(
        isinstance(value, bool) or not isinstance(value, Integral) or value < 1 for value in counts
    ):
        raise ValueError("artifact market counts must be positive integers")
    if up_market_count + down_market_count != market_count:
        raise ValueError("Up and Down market counts must equal market_count")
    expected_weighting_hash = market_stage_weighting_hash()
    if weighting_hash != expected_weighting_hash:
        raise ValueError("artifact weighting contract does not match the active protocol")
    if not source_hashes:
        raise ValueError("source_hashes must not be empty")
    normalized_hashes = dict(source_hashes)
    for name, digest in normalized_hashes.items():
        if not name or name.strip() != name:
            raise ValueError("source hash names must be non-empty and trimmed")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("source hashes must be lowercase SHA-256 digests")
    calibration = model.calibration_selection
    if model.config.calibration_method == "auto" and calibration is None:
        raise ValueError("auto calibration artifact is missing comparison evidence")
    return {
        "rule_epoch": rule_epoch,
        "feature_schema_hash": model.schema.hash,
        "time_range_ns": {
            "training_start": int(training_start_ns),
            "training_end": int(training_end_ns),
            "calibration_start": int(calibration_start_ns),
            "calibration_end": int(calibration_end_ns),
        },
        "market_counts": {
            "all": int(market_count),
            "up": int(up_market_count),
            "down": int(down_market_count),
        },
        "weighting": {
            "protocol": MARKET_STAGE_WEIGHTING_PROTOCOL,
            "hash": weighting_hash,
        },
        "calibration": None if calibration is None else asdict(calibration),
        "split_protocol": {
            "train_seconds": split_config.train_duration.total_seconds(),
            "calibration_seconds": split_config.calibration_duration.total_seconds(),
            "test_seconds": split_config.test_duration.total_seconds(),
            "step_seconds": split_config.step_duration.total_seconds(),
            "embargo_seconds": split_config.embargo_duration.total_seconds(),
            "sealed_holdout_seconds": split_config.sealed_holdout_duration.total_seconds(),
            "training_window": split_config.training_window,
        },
        "model_config": model.config_dict,
        "source_hashes": dict(sorted(normalized_hashes.items())),
    }


__all__ = ["build_direction_artifact_contract"]
