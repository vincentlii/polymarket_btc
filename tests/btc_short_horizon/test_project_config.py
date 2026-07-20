from __future__ import annotations

from pathlib import Path

import pytest

from btc_short_horizon.config import load_btc_project_config


def test_baseline_config_is_path_relative_and_has_explicit_queue_scenarios() -> None:
    path = Path("configs/btc_short_horizon/baseline.toml")
    config = load_btc_project_config(path)

    assert config.primary_family.window_seconds == 900
    assert config.collection_only_family.is_collection_only
    assert config.research_timing.feature_cadence_ms == 250
    assert config.research_timing.model_cadence_ms == 5_000
    assert config.research_timing.entry_start_seconds == 3
    assert config.research_timing.entry_end_seconds == 180
    assert config.research_timing.max_feature_lookback_seconds == 3_600
    assert config.maker.minimum_edge == 0.10
    assert config.maker.entry_end_seconds == 180.0
    assert config.maker.edge_persistence_seconds == 5.0
    assert config.maker.max_work_seconds == 15.0
    assert config.collection.flush_size == 50_000
    assert config.collection.flush_interval_seconds == 60.0
    assert config.collection.shutdown_flush_timeout_seconds == 30.0
    assert config.collection.binance_spot_depth_snapshot_limit == 1_000
    assert config.collection.binance_futures_depth_snapshot_limit == 1_000
    assert config.collection.binance_depth_snapshot_retry_initial_seconds == 0.5
    assert config.collection.binance_depth_snapshot_retry_max_seconds == 30.0
    assert config.collection.polymarket_source_timestamp_regression_tolerance_seconds == 1.0
    assert config.collection.max_pending_events == 100_000
    assert config.collection.max_pending_bytes == 67_108_864
    assert config.collection.opening_handoff_delay_seconds == 180.0
    assert config.collection.ingest_version == "btc-short-horizon-v8"
    assert config.require_scenario("p99_pessimistic").execution.queue_position
    assert config.paths.raw_data_root.is_absolute()
    assert config.paths.raw_data_root.name == "btc_short_horizon"
    assert config.data_sources[0].startswith("local:")
    assert Path(config.data_sources[0].removeprefix("local:")).is_absolute()
    assert Path(config.data_sources[0].removeprefix("local:")).name == "pmxt_raw"


def test_project_config_rejects_primary_collection_only_family(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(
        """
[paths]
raw_data_root = "raw"
model_root = "models"
artifact_root = "output"
[primary_market]
name = "btc"
slug_prefix = "btc-updown"
window_seconds = 900
collection_mode = "collection_only"
[collection_only_market]
name = "btc5"
slug_prefix = "btc-updown"
window_seconds = 300
collection_mode = "collection_only"
[research]
feature_cadence_ms = 250
model_cadence_ms = 1000
entry_start_seconds = 3
entry_end_seconds = 180
training_snapshot_seconds = 5
max_feature_lookback_seconds = 60
[maker]
structure = "single"
max_shares = 1
safety_buffer = 0.01
minimum_edge = 0
maker_fee_per_share = 0
entry_start_seconds = 3
entry_end_seconds = 180
edge_persistence_seconds = 2
max_work_seconds = 60
stale_after_seconds = 1
cancel_probability_drop = 0.03
price_level_tick_offsets = [0]
data_sources = ["archive:r2v2.pmxt.dev"]
[[execution_scenarios]]
name = "base"
queue_position = true
base_latency_ms = 0
insert_latency_ms = 0
update_latency_ms = 0
cancel_latency_ms = 0
prob_fill_on_limit = 0.25
min_synthetic_book_size = 10
synthetic_book_depth_multiplier = 1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="primary_market"):
        load_btc_project_config(path)
