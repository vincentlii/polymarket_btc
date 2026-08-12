from __future__ import annotations

from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from btc_short_horizon.research.market_relative_shadow import (
    load_market_relative_shadow_dataset,
)
from btc_short_horizon.strategy import OpeningStage


def _write_shadow(root: Path, *, epoch: int, invalidate_early: bool = False) -> str:
    slug = f"btc-updown-15m-{epoch}"
    directory = root / f"{slug}-artifact"
    directory.mkdir(parents=True)
    rows = []
    for index, seconds in enumerate(range(5, 181, 5)):
        stage = (
            OpeningStage.EARLY
            if seconds <= 30
            else OpeningStage.PRICE_DISCOVERY
            if seconds <= 90
            else OpeningStage.MID_EARLY
        )
        first_in_stage = seconds in {5, 35, 95}
        age = 0.25 if first_in_stage else 2.0
        if invalidate_early and stage is OpeningStage.EARLY:
            age = 2.0
        rows.append(
            {
                "market_slug": slug,
                "model_version": "direction-v1",
                "feature_schema_hash": "a" * 64,
                "market_window_start_ts_ns": epoch * 1_000_000_000,
                "trigger_ts_ns": (epoch + seconds) * 1_000_000_000,
                "p_up": 0.55 + index / 10_000,
                "p_boundary_up": 0.52,
                "p_market_mid_up": 0.50,
                "data_age_seconds": age,
                "has_data_gap": False,
                "structure_valid": True,
                "tick_unchanged": True,
                "fee_unchanged": True,
                "latency_healthy": True,
            }
        )
    pq.write_table(pa.Table.from_pylist(rows), directory / "predictions.parquet")
    return slug


def test_shadow_loader_keeps_only_causal_rows_and_balances_complete_stages(tmp_path) -> None:
    included = _write_shadow(tmp_path, epoch=1_000)
    excluded = _write_shadow(tmp_path, epoch=2_000, invalidate_early=True)

    result = load_market_relative_shadow_dataset(
        shadow_root=tmp_path,
        labels_by_market={included: 1, excluded: 0},
        end_before_epoch_seconds=3_000,
        maximum_pair_age_seconds=1.0,
    )

    assert result.dataset.market_count == 1
    assert result.excluded_missing_stage_markets == 1
    assert result.dataset.market_slugs == (included, included, included)
    assert np.sum(result.dataset.sample_weights) == 1.0
    assert result.dataset.sample_weights == pytest.approx((1 / 3, 1 / 3, 1 / 3))
