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
    assert config.maker.confirmation_signals == 2
    assert config.maker.signal_cadence_seconds == 5.0
    assert config.maker.max_visible_depth_fraction == 0.05
    assert config.maker.improve_inside_spread is True
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
    assert config.collection.polymarket_capture_lead_seconds == 90.0
    assert config.collection.opening_handoff_delay_seconds == 215.0
    assert config.collection.ingest_version == "btc-short-horizon-v15"
    assert config.paper_execution_epoch == "paper-v4-stage-aware-taker-v2"
    assert [variant.variant_id for variant in config.paper_execution_variants] == [
        "maker_15s",
        "immediate_fak",
        "maker_5s_then_fak",
        "independent_fak_2x5s",
        "independent_fak_stable_3x5s",
    ]
    assert config.paper_execution_variants[0].enabled is False
    assert config.paper_execution_variants[0].primary is False
    assert config.paper_execution_variants[1].mode == "immediate_fak"
    assert config.paper_execution_variants[1].maker_work_seconds == 0.0
    assert config.paper_execution_variants[3].opportunity_policy == "independent_taker"
    assert config.paper_execution_variants[3].confirmation_signals == 2
    assert config.paper_execution_variants[4].confirmation_policy == "edge_stable"
    assert config.paper_execution_variants[4].confirmation_signals == 3
    assert config.paper_execution_variants[4].maximum_edge_decay == 0.01
    assert config.paper_execution_variants[4].enabled is True
    assert config.paper_execution_variants[4].primary is True
    assert [rule.stage.value for rule in config.stage_policy.rules] == [
        "early_3s_to_30s",
        "price_discovery_35s_to_90s",
        "mid_early_95s_to_180s",
    ]
    formal = tuple(scenario for scenario in config.scenarios if scenario.formal_grid_component)
    assert len(formal) == 4
    assert all(scenario.execution.queue_position for scenario in formal)
    assert all(not scenario.execution.maker_rebates_enabled for scenario in formal)
    assert {
        (
            scenario.execution.trade_execution_size_multiplier,
            scenario.execution.same_timestamp_priority.value,
        )
        for scenario in formal
    } == {
        (0.5, "book_before_trade"),
        (0.5, "trade_before_book"),
        (1.0, "book_before_trade"),
        (1.0, "trade_before_book"),
    }
    assert config.paths.raw_data_root.is_absolute()
    assert config.paths.raw_data_root.name == "btc_short_horizon"
    assert config.data_sources[0].startswith("local:")
    assert Path(config.data_sources[0].removeprefix("local:")).is_absolute()
    assert Path(config.data_sources[0].removeprefix("local:")).name == "pmxt_raw"


def test_formal_execution_grid_cannot_omit_a_tie_ordering(tmp_path: Path) -> None:
    baseline = Path("configs/btc_short_horizon/baseline.toml").read_text(encoding="utf-8")
    incomplete = baseline.replace(
        'name = "p99_half_volume_book_first"\nformal_grid_component = true',
        'name = "p99_half_volume_book_first"\nformal_grid_component = false',
        1,
    )
    path = tmp_path / "incomplete-grid.toml"
    path.write_text(incomplete, encoding="utf-8")

    with pytest.raises(ValueError, match="both tie orderings"):
        load_btc_project_config(path)


def test_formal_execution_grid_rejects_unregistered_trade_volume_stress(tmp_path: Path) -> None:
    baseline = Path("configs/btc_short_horizon/baseline.toml").read_text(encoding="utf-8")
    changed = baseline.replace(
        "trade_execution_size_multiplier = 0.5", "trade_execution_size_multiplier = 0.25"
    )
    path = tmp_path / "changed-grid.toml"
    path.write_text(changed, encoding="utf-8")

    with pytest.raises(ValueError, match="exactly the 0.5 and 1.0"):
        load_btc_project_config(path)


def test_collection_window_covers_cancel_race_latency(tmp_path: Path) -> None:
    baseline = Path("configs/btc_short_horizon/baseline.toml").read_text(encoding="utf-8")
    path = tmp_path / "short-capture.toml"
    path.write_text(
        baseline.replace(
            "opening_handoff_delay_seconds = 215.0", "opening_handoff_delay_seconds = 195.0"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cancel latency"):
        load_btc_project_config(path)


def test_paper_execution_variant_ids_cannot_escape_the_ledger_root(tmp_path: Path) -> None:
    baseline = Path("configs/btc_short_horizon/baseline.toml").read_text(encoding="utf-8")
    path = tmp_path / "unsafe-variant.toml"
    path.write_text(baseline.replace('id = "maker_15s"', 'id = "../maker_15s"'), encoding="utf-8")

    with pytest.raises(ValueError, match="ASCII identifier"):
        load_btc_project_config(path)


def test_paper_execution_epoch_cannot_escape_the_ledger_root(tmp_path: Path) -> None:
    baseline = Path("configs/btc_short_horizon/baseline.toml").read_text(encoding="utf-8")
    path = tmp_path / "unsafe-epoch.toml"
    path.write_text(
        baseline.replace(
            'paper_execution_epoch = "paper-v4-stage-aware-taker-v2"',
            'paper_execution_epoch = "../paper-v3-independent-fak"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ASCII identifier"):
        load_btc_project_config(path)


def test_immediate_fak_variant_cannot_hide_a_maker_wait(tmp_path: Path) -> None:
    baseline = Path("configs/btc_short_horizon/baseline.toml").read_text(encoding="utf-8")
    path = tmp_path / "delayed-immediate-fak.toml"
    path.write_text(
        baseline.replace(
            'mode = "immediate_fak"\nmaker_work_seconds = 0.0',
            'mode = "immediate_fak"\nmaker_work_seconds = 5.0',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="maker_work_seconds=0"):
        load_btc_project_config(path)


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
entry_start_seconds = 3
entry_end_seconds = 180
confirmation_signals = 2
signal_cadence_seconds = 1
signal_cadence_tolerance_seconds = 0.1
max_work_seconds = 60
stale_after_seconds = 1
cancel_probability_drop = 0.03
max_visible_depth_fraction = 0.05
price_level_tick_offsets = [0]
data_sources = ["archive:r2v2.pmxt.dev"]
[[execution_scenarios]]
name = "base"
formal_grid_component = false
queue_position = true
maker_rebates_enabled = false
trade_execution_size_multiplier = 1
same_timestamp_priority = "book_before_trade"
base_latency_ms = 0
insert_latency_ms = 0
update_latency_ms = 0
cancel_latency_ms = 0
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="primary_market"):
        load_btc_project_config(path)
