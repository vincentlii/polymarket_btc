"""Single-file runtime configuration for the independent BTC project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Mapping

from prediction_market_extensions.backtesting._execution_config import (
    ExecutionModelConfig,
    StaticLatencyConfig,
)

from btc_short_horizon.data import BtcMarketFamily, MarketCollectionMode
from btc_short_horizon.strategy import LayerStructure, MakerStrategyConfig


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    raw_data_root: Path
    model_root: Path
    artifact_root: Path


@dataclass(frozen=True, slots=True)
class ResearchTimingConfig:
    feature_cadence_ms: int
    model_cadence_ms: int
    entry_start_seconds: int
    entry_end_seconds: int
    training_snapshot_seconds: int
    max_feature_lookback_seconds: int


@dataclass(frozen=True, slots=True)
class ForwardCollectionConfig:
    flush_size: int
    flush_interval_seconds: float
    rotation_poll_seconds: float
    opening_handoff_delay_seconds: float
    ingest_version: str


@dataclass(frozen=True, slots=True)
class ExecutionScenario:
    name: str
    execution: ExecutionModelConfig


@dataclass(frozen=True, slots=True)
class BtcProjectConfig:
    paths: ProjectPaths
    primary_family: BtcMarketFamily
    collection_only_family: BtcMarketFamily
    research_timing: ResearchTimingConfig
    collection: ForwardCollectionConfig
    maker: MakerStrategyConfig
    data_sources: tuple[str, ...]
    scenarios: tuple[ExecutionScenario, ...]

    def require_scenario(self, name: str) -> ExecutionScenario:
        for scenario in self.scenarios:
            if scenario.name == name:
                return scenario
        raise KeyError(name)


def load_btc_project_config(path: Path) -> BtcProjectConfig:
    """Load one TOML file without making directories or connecting to any venue."""

    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    root = path.parent.resolve()
    paths_section = _mapping(raw, "paths")
    paths = ProjectPaths(
        raw_data_root=_resolve_path(root, _text(paths_section, "raw_data_root")),
        model_root=_resolve_path(root, _text(paths_section, "model_root")),
        artifact_root=_resolve_path(root, _text(paths_section, "artifact_root")),
    )
    primary_family = _family(_mapping(raw, "primary_market"))
    collection_only_family = _family(_mapping(raw, "collection_only_market"))
    if primary_family.is_collection_only:
        raise ValueError("primary_market must not be collection-only")
    if not collection_only_family.is_collection_only:
        raise ValueError("collection_only_market must be collection-only")
    collection = _forward_collection(_mapping(raw, "collection"))
    research = _mapping(raw, "research")
    timing = ResearchTimingConfig(
        feature_cadence_ms=_positive_int(research, "feature_cadence_ms"),
        model_cadence_ms=_positive_int(research, "model_cadence_ms"),
        entry_start_seconds=_positive_int(research, "entry_start_seconds"),
        entry_end_seconds=_positive_int(research, "entry_end_seconds"),
        training_snapshot_seconds=_positive_int(research, "training_snapshot_seconds"),
        max_feature_lookback_seconds=_positive_int(research, "max_feature_lookback_seconds"),
    )
    if timing.model_cadence_ms < timing.feature_cadence_ms:
        raise ValueError("research.model_cadence_ms must not be below feature_cadence_ms")
    if timing.entry_end_seconds <= timing.entry_start_seconds:
        raise ValueError("research.entry_end_seconds must exceed entry_start_seconds")
    if timing.model_cadence_ms != timing.training_snapshot_seconds * 1_000:
        raise ValueError("research model cadence must match training_snapshot_seconds")
    maker_section = _mapping(raw, "maker")
    maker = MakerStrategyConfig(
        structure=LayerStructure(_text(maker_section, "structure")),
        max_shares=_positive_float(maker_section, "max_shares"),
        safety_buffer=_positive_float(maker_section, "safety_buffer"),
        minimum_edge=_nonnegative_float(maker_section, "minimum_edge"),
        maker_fee_per_share=_nonnegative_float(maker_section, "maker_fee_per_share"),
        entry_start_seconds=_positive_float(maker_section, "entry_start_seconds"),
        entry_end_seconds=_positive_float(maker_section, "entry_end_seconds"),
        edge_persistence_seconds=_nonnegative_float(maker_section, "edge_persistence_seconds"),
        max_work_seconds=_positive_float(maker_section, "max_work_seconds"),
        stale_after_seconds=_positive_float(maker_section, "stale_after_seconds"),
        cancel_probability_drop=_positive_float(maker_section, "cancel_probability_drop"),
        price_level_tick_offsets=tuple(
            _nonnegative_int_list(maker_section, "price_level_tick_offsets")
        ),
    )
    if maker.entry_start_seconds != float(
        timing.entry_start_seconds
    ) or maker.entry_end_seconds != float(timing.entry_end_seconds):
        raise ValueError("maker entry window must match research entry window")
    if collection.opening_handoff_delay_seconds < timing.entry_end_seconds:
        raise ValueError("opening handoff must cover the complete research entry window")
    sources = _data_sources(root, raw)
    scenarios = tuple(_scenario(item) for item in _mapping_list(raw, "execution_scenarios"))
    if not scenarios:
        raise ValueError("execution_scenarios must not be empty")
    if len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ValueError("execution scenario names must be unique")
    return BtcProjectConfig(
        paths=paths,
        primary_family=primary_family,
        collection_only_family=collection_only_family,
        research_timing=timing,
        collection=collection,
        maker=maker,
        data_sources=sources,
        scenarios=scenarios,
    )


def _scenario(section: Mapping[str, object]) -> ExecutionScenario:
    name = _text(section, "name")
    latency = StaticLatencyConfig(
        base_latency_ms=_nonnegative_float(section, "base_latency_ms"),
        insert_latency_ms=_nonnegative_float(section, "insert_latency_ms"),
        update_latency_ms=_nonnegative_float(section, "update_latency_ms"),
        cancel_latency_ms=_nonnegative_float(section, "cancel_latency_ms"),
    )
    return ExecutionScenario(
        name=name,
        execution=ExecutionModelConfig(
            queue_position=_bool(section, "queue_position"),
            latency_model=latency,
            prob_fill_on_limit=_probability(section, "prob_fill_on_limit"),
            min_synthetic_book_size=_positive_float(section, "min_synthetic_book_size"),
            synthetic_book_depth_multiplier=_positive_float(
                section, "synthetic_book_depth_multiplier"
            ),
        ),
    )


def _forward_collection(section: Mapping[str, object]) -> ForwardCollectionConfig:
    return ForwardCollectionConfig(
        flush_size=_positive_int(section, "flush_size"),
        flush_interval_seconds=_positive_float(section, "flush_interval_seconds"),
        rotation_poll_seconds=_positive_float(section, "rotation_poll_seconds"),
        opening_handoff_delay_seconds=_positive_float(section, "opening_handoff_delay_seconds"),
        ingest_version=_text(section, "ingest_version"),
    )


def _family(section: Mapping[str, object]) -> BtcMarketFamily:
    return BtcMarketFamily(
        name=_text(section, "name"),
        slug_prefix=_text(section, "slug_prefix"),
        window_seconds=_positive_int(section, "window_seconds"),
        collection_mode=MarketCollectionMode(_text(section, "collection_mode")),
    )


def _mapping(raw: Mapping[str, object], name: str) -> Mapping[str, object]:
    value = raw.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"[{name}] is required")
    return value


def _mapping_list(raw: Mapping[str, object], name: str) -> tuple[Mapping[str, object], ...]:
    value = raw.get(name)
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{name} must be an array of tables")
    return tuple(value)


def _text(section: Mapping[str, object], name: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _text_list(raw: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = raw.get(name)
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a string list")
    return tuple(_text({"value": item}, "value") for item in value)


def _data_sources(root: Path, raw: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(_resolve_data_source(root, value) for value in _text_list(raw, "data_sources"))


def _resolve_data_source(root: Path, value: str) -> str:
    if not value.casefold().startswith("local:"):
        return value
    local_path = value.removeprefix("local:").strip()
    if not local_path:
        raise ValueError("local data source must include a path")
    return f"local:{_resolve_path(root, local_path)}"


def _int(section: Mapping[str, object], name: str) -> int:
    value = section.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _positive_int(section: Mapping[str, object], name: str) -> int:
    value = _int(section, name)
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _int_list(section: Mapping[str, object], name: str) -> tuple[int, ...]:
    value = section.get(name)
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an integer list")
    return tuple(_int({"value": item}, "value") for item in value)


def _nonnegative_int_list(section: Mapping[str, object], name: str) -> tuple[int, ...]:
    values = _int_list(section, name)
    if any(value < 0 for value in values):
        raise ValueError(f"{name} must contain non-negative integers")
    return values


def _positive_float(section: Mapping[str, object], name: str) -> float:
    value = _nonnegative_float(section, name)
    if value <= 0.0:
        raise ValueError(f"{name} must be > 0")
    return value


def _nonnegative_float(section: Mapping[str, object], name: str) -> float:
    try:
        value = float(section.get(name))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value < 0.0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _probability(section: Mapping[str, object], name: str) -> float:
    value = _nonnegative_float(section, name)
    if value > 1.0:
        raise ValueError(f"{name} must be <= 1")
    return value


def _bool(section: Mapping[str, object], name: str) -> bool:
    value = section.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()
