from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY
from btc_short_horizon.features.schema import FeatureSchema
from btc_short_horizon.research.pipeline import DirectionDataset
from btc_short_horizon.research.stage_aware_models import (
    sample_elapsed_seconds,
    select_stage_dataset,
)
from btc_short_horizon.research.walk_forward import ResearchSample
from btc_short_horizon.strategy import OpeningStage


def test_sample_elapsed_seconds_uses_canonical_market_identity() -> None:
    t0 = datetime(2026, 7, 27, tzinfo=UTC)
    slug = BTC_15M_MARKET_FAMILY.slug_for(t0)
    decision_ns = int((t0 + timedelta(seconds=35)).timestamp() * 1_000_000_000)

    assert sample_elapsed_seconds(f"{slug}@{decision_ns}") == 35.0


def test_select_stage_dataset_preserves_group_identity() -> None:
    t0 = datetime(2026, 7, 27, tzinfo=UTC)
    slug = BTC_15M_MARKET_FAMILY.slug_for(t0)
    samples = tuple(
        ResearchSample(
            sample_id=f"{slug}@{int((t0 + timedelta(seconds=offset)).timestamp() * 1e9)}",
            feature_ts=t0 + timedelta(seconds=offset),
            label_available_ts=t0 + timedelta(minutes=15),
            label=offset % 2,
            group_id=slug,
        )
        for offset in (5, 35, 95)
    )
    dataset = DirectionDataset(
        samples=samples,
        vectors=np.ones((3, 1)),
        schema=FeatureSchema(version="test", names=("x",)),
    )

    selected = select_stage_dataset(dataset, stage=OpeningStage.PRICE_DISCOVERY)

    assert len(selected.samples) == 1
    assert selected.samples[0].group_id == slug
