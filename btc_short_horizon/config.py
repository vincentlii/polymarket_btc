"""Single-file runtime configuration for the independent BTC project."""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose, isfinite
from pathlib import Path
import tomllib
from typing import Mapping

from prediction_market_extensions.backtesting._execution_config import (
    ExecutionModelConfig,
    SameTimestampPriority,
    StaticLatencyConfig,
)

from btc_short_horizon.data import BtcMarketFamily, MarketCollectionMode
from btc_short_horizon.execution_timing import (
    CLOB_DELAYED_TAKER_SERVER_MS,
    paper_execution_lifecycle_tail_seconds,
)
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
    shutdown_flush_timeout_seconds: float
    max_pending_events: int
    max_pending_bytes: int
    rotation_poll_seconds: float
    polymarket_capture_lead_seconds: float
    opening_handoff_delay_seconds: float
    binance_spot_depth_snapshot_limit: int
    binance_futures_depth_snapshot_limit: int
    binance_depth_snapshot_retry_initial_seconds: float
    binance_depth_snapshot_retry_max_seconds: float
    polymarket_source_timestamp_regression_tolerance_seconds: float
    ingest_version: str


@dataclass(frozen=True, slots=True)
class ExecutionScenario:
    name: str
    execution: ExecutionModelConfig
    formal_grid_component: bool


@dataclass(frozen=True, slots=True)
class PaperExecutionVariantConfig:
    variant_id: str
    label: str
    mode: str
    maker_work_seconds: float
    primary: bool
    minimum_taker_net_edge: float
    slippage_buffer: float
    model_uncertainty_buffer: float
    opportunity_policy: str = "shared_maker"
    confirmation_policy: str = "side_only"
    confirmation_signals: int = 2
    maximum_edge_decay: float = 0.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.variant_id, str)
            or not self.variant_id
            or len(self.variant_id) > 64
            or not self.variant_id[0].isascii()
            or not self.variant_id[0].isalnum()
            or any(
                not character.isascii() or not (character.isalnum() or character in {"_", "-"})
                for character in self.variant_id
            )
        ):
            raise ValueError("paper execution variant ID must be a simple ASCII identifier")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("paper execution variant label must not be empty")
        if self.mode not in {"maker", "immediate_fak", "maker_then_fak"}:
            raise ValueError("paper execution mode must be maker, immediate_fak, or maker_then_fak")
        if self.opportunity_policy not in {"shared_maker", "independent_taker"}:
            raise ValueError("paper opportunity policy must be shared_maker or independent_taker")
        if self.confirmation_policy not in {"side_only", "edge_stable"}:
            raise ValueError("paper confirmation policy must be side_only or edge_stable")
        if self.opportunity_policy == "independent_taker" and self.mode != "immediate_fak":
            raise ValueError("independent taker opportunity policy requires immediate_fak mode")
        if self.opportunity_policy == "shared_maker" and self.confirmation_policy != "side_only":
            raise ValueError("shared maker opportunity policy requires side_only confirmation")
        if (
            isinstance(self.confirmation_signals, bool)
            or not isinstance(self.confirmation_signals, int)
            or self.confirmation_signals < 1
        ):
            raise ValueError("paper confirmation_signals must be an integer >= 1")
        if not isfinite(self.maximum_edge_decay) or self.maximum_edge_decay < 0.0:
            raise ValueError("paper maximum_edge_decay must be finite and >= 0")
        if self.confirmation_policy == "side_only" and self.maximum_edge_decay != 0.0:
            raise ValueError("side_only confirmation cannot configure maximum_edge_decay")
        for name, value in (
            ("maker_work_seconds", self.maker_work_seconds),
            ("minimum_taker_net_edge", self.minimum_taker_net_edge),
            ("slippage_buffer", self.slippage_buffer),
            ("model_uncertainty_buffer", self.model_uncertainty_buffer),
        ):
            if isinstance(value, bool) or not isfinite(value) or value < 0.0:
                raise ValueError(f"paper execution {name} must be finite and >= 0")
        if self.mode == "immediate_fak" and self.maker_work_seconds != 0.0:
            raise ValueError("immediate-FAK paper variants require maker_work_seconds=0")
        if self.mode != "immediate_fak" and self.maker_work_seconds <= 0.0:
            raise ValueError("maker paper variants require maker_work_seconds > 0")
        if not isinstance(self.primary, bool):
            raise ValueError("paper execution primary must be bool")
        if self.mode == "maker" and any(
            value > 0.0
            for value in (
                self.minimum_taker_net_edge,
                self.slippage_buffer,
                self.model_uncertainty_buffer,
            )
        ):
            raise ValueError("maker-only paper variants cannot configure taker buffers")
        if self.mode in {"immediate_fak", "maker_then_fak"} and any(
            value <= 0.0
            for value in (
                self.minimum_taker_net_edge,
                self.slippage_buffer,
                self.model_uncertainty_buffer,
            )
        ):
            raise ValueError("FAK paper variants require positive taker safety buffers")
        if (
            self.minimum_taker_net_edge + self.slippage_buffer + self.model_uncertainty_buffer
            >= 1.0
        ):
            raise ValueError("paper execution taker buffers must sum to less than 1")


@dataclass(frozen=True, slots=True)
class BtcProjectConfig:
    paths: ProjectPaths
    primary_family: BtcMarketFamily
    collection_only_family: BtcMarketFamily
    research_timing: ResearchTimingConfig
    collection: ForwardCollectionConfig
    maker: MakerStrategyConfig
    paper_execution_epoch: str
    paper_execution_variants: tuple[PaperExecutionVariantConfig, ...]
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
        entry_start_seconds=_positive_float(maker_section, "entry_start_seconds"),
        entry_end_seconds=_positive_float(maker_section, "entry_end_seconds"),
        confirmation_signals=_positive_int(maker_section, "confirmation_signals"),
        signal_cadence_seconds=_positive_float(maker_section, "signal_cadence_seconds"),
        signal_cadence_tolerance_seconds=_nonnegative_float(
            maker_section, "signal_cadence_tolerance_seconds"
        ),
        max_work_seconds=_positive_float(maker_section, "max_work_seconds"),
        stale_after_seconds=_positive_float(maker_section, "stale_after_seconds"),
        cancel_probability_drop=_positive_float(maker_section, "cancel_probability_drop"),
        max_visible_depth_fraction=_probability_excluding_zero(
            maker_section, "max_visible_depth_fraction"
        ),
        price_level_tick_offsets=tuple(
            _nonnegative_int_list(maker_section, "price_level_tick_offsets")
        ),
        improve_inside_spread=_bool(maker_section, "improve_inside_spread"),
    )
    if maker.entry_start_seconds != float(
        timing.entry_start_seconds
    ) or maker.entry_end_seconds != float(timing.entry_end_seconds):
        raise ValueError("maker entry window must match research entry window")
    if not isclose(
        maker.signal_cadence_seconds * 1_000,
        timing.model_cadence_ms,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError("maker signal cadence must match research model cadence")
    if collection.opening_handoff_delay_seconds < timing.entry_end_seconds:
        raise ValueError("opening handoff must cover the complete research entry window")
    paper_variants = tuple(
        _paper_execution_variant(item) for item in _mapping_list(raw, "paper_execution_variants")
    )
    if not paper_variants:
        raise ValueError("paper_execution_variants must not be empty")
    variant_ids = {variant.variant_id for variant in paper_variants}
    if len(variant_ids) != len(paper_variants):
        raise ValueError("paper execution variant IDs must be unique")
    if sum(variant.primary for variant in paper_variants) != 1:
        raise ValueError("exactly one paper execution variant must be primary")
    paper_execution_epoch = _simple_ascii_identifier(
        raw.get("paper_execution_epoch"),
        "paper_execution_epoch",
    )
    sources = _data_sources(root, raw)
    scenarios = tuple(_scenario(item) for item in _mapping_list(raw, "execution_scenarios"))
    if not scenarios:
        raise ValueError("execution_scenarios must not be empty")
    if len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ValueError("execution scenario names must be unique")
    _validate_formal_scenario_grid(scenarios)
    maximum_lifecycle_tail_seconds = max(
        paper_execution_lifecycle_tail_seconds(
            mode=variant.mode,
            maker_work_seconds=variant.maker_work_seconds,
            cancel_latency_ms=(
                scenario.execution.latency_model.base_latency_ms
                + scenario.execution.latency_model.cancel_latency_ms
            ),
            taker_latency_ms=(
                scenario.execution.latency_model.base_latency_ms
                + scenario.execution.latency_model.insert_latency_ms
            ),
            taker_server_delay_ms=CLOB_DELAYED_TAKER_SERVER_MS,
        )
        for scenario in scenarios
        for variant in paper_variants
    )
    required_handoff_seconds = maker.entry_end_seconds + maximum_lifecycle_tail_seconds
    if collection.opening_handoff_delay_seconds < required_handoff_seconds:
        raise ValueError(
            "opening handoff must cover the entry window, order lifecycle, and cancel latency"
        )
    return BtcProjectConfig(
        paths=paths,
        primary_family=primary_family,
        collection_only_family=collection_only_family,
        research_timing=timing,
        collection=collection,
        maker=maker,
        paper_execution_epoch=paper_execution_epoch,
        paper_execution_variants=paper_variants,
        data_sources=sources,
        scenarios=scenarios,
    )


def _paper_execution_variant(section: Mapping[str, object]) -> PaperExecutionVariantConfig:
    primary = section.get("primary")
    if not isinstance(primary, bool):
        raise ValueError("paper execution variant primary must be bool")
    return PaperExecutionVariantConfig(
        variant_id=_text(section, "id"),
        label=_text(section, "label"),
        mode=_text(section, "mode"),
        maker_work_seconds=_nonnegative_float(section, "maker_work_seconds"),
        primary=primary,
        minimum_taker_net_edge=_nonnegative_float(section, "minimum_taker_net_edge"),
        slippage_buffer=_nonnegative_float(section, "slippage_buffer"),
        model_uncertainty_buffer=_nonnegative_float(section, "model_uncertainty_buffer"),
        opportunity_policy=_text(section, "opportunity_policy"),
        confirmation_policy=_text(section, "confirmation_policy"),
        confirmation_signals=_positive_int(section, "confirmation_signals"),
        maximum_edge_decay=_nonnegative_float(section, "maximum_edge_decay"),
    )


def _simple_ascii_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError(f"{name} must be a non-empty string")
    result = value.strip()
    if (
        not result[0].isascii()
        or not result[0].isalnum()
        or any(
            not character.isascii() or not (character.isalnum() or character in {"_", "-"})
            for character in result
        )
    ):
        raise ValueError(f"{name} must be a simple ASCII identifier")
    return result


def _scenario(section: Mapping[str, object]) -> ExecutionScenario:
    name = _text(section, "name")
    latency = StaticLatencyConfig(
        base_latency_ms=_nonnegative_float(section, "base_latency_ms"),
        insert_latency_ms=_nonnegative_float(section, "insert_latency_ms"),
        update_latency_ms=_nonnegative_float(section, "update_latency_ms"),
        cancel_latency_ms=_nonnegative_float(section, "cancel_latency_ms"),
    )
    formal_grid_component = _bool(section, "formal_grid_component")
    execution = ExecutionModelConfig(
        queue_position=_bool(section, "queue_position"),
        latency_model=latency,
        maker_rebates_enabled=_bool(section, "maker_rebates_enabled"),
        trade_execution_size_multiplier=_probability_excluding_zero(
            section, "trade_execution_size_multiplier"
        ),
        same_timestamp_priority=SameTimestampPriority(_text(section, "same_timestamp_priority")),
    )
    if formal_grid_component:
        if not execution.queue_position:
            raise ValueError("a conclusion-eligible scenario requires queue_position=true")
        if execution.maker_rebates_enabled:
            raise ValueError("a conclusion-eligible scenario must disable maker rebates")
        if latency.cancel_latency_ms <= 0.0 or latency.insert_latency_ms <= 0.0:
            raise ValueError(
                "a conclusion-eligible scenario requires positive insert and cancel latency"
            )
    return ExecutionScenario(
        name=name,
        execution=execution,
        formal_grid_component=formal_grid_component,
    )


def _validate_formal_scenario_grid(scenarios: tuple[ExecutionScenario, ...]) -> None:
    formal = tuple(scenario for scenario in scenarios if scenario.formal_grid_component)
    if not formal:
        return
    multipliers = {scenario.execution.trade_execution_size_multiplier for scenario in formal}
    if multipliers != {0.5, 1.0}:
        raise ValueError(
            "conclusion-eligible scenarios must contain exactly the 0.5 and 1.0 trade-volume grid"
        )
    required_priorities = set(SameTimestampPriority)
    for multiplier in multipliers:
        priorities = {
            scenario.execution.same_timestamp_priority
            for scenario in formal
            if scenario.execution.trade_execution_size_multiplier == multiplier
        }
        if priorities != required_priorities:
            raise ValueError(
                "each conclusion-eligible trade-volume setting must cover both tie orderings"
            )
    if len(formal) != 4:
        raise ValueError("conclusion-eligible execution grid must contain exactly four scenarios")
    latency_signatures = {
        (
            scenario.execution.latency_model.base_latency_ms,
            scenario.execution.latency_model.insert_latency_ms,
            scenario.execution.latency_model.update_latency_ms,
            scenario.execution.latency_model.cancel_latency_ms,
        )
        for scenario in formal
    }
    if len(latency_signatures) != 1:
        raise ValueError(
            "conclusion-eligible scenario comparisons must use one identical latency profile"
        )


def _forward_collection(section: Mapping[str, object]) -> ForwardCollectionConfig:
    config = ForwardCollectionConfig(
        flush_size=_positive_int(section, "flush_size"),
        flush_interval_seconds=_positive_float(section, "flush_interval_seconds"),
        shutdown_flush_timeout_seconds=_positive_float(
            section,
            "shutdown_flush_timeout_seconds",
        ),
        max_pending_events=_positive_int(section, "max_pending_events"),
        max_pending_bytes=_positive_int(section, "max_pending_bytes"),
        rotation_poll_seconds=_positive_float(section, "rotation_poll_seconds"),
        polymarket_capture_lead_seconds=_positive_float(section, "polymarket_capture_lead_seconds"),
        opening_handoff_delay_seconds=_positive_float(section, "opening_handoff_delay_seconds"),
        binance_spot_depth_snapshot_limit=_positive_int(
            section,
            "binance_spot_depth_snapshot_limit",
        ),
        binance_futures_depth_snapshot_limit=_positive_int(
            section,
            "binance_futures_depth_snapshot_limit",
        ),
        binance_depth_snapshot_retry_initial_seconds=_positive_float(
            section,
            "binance_depth_snapshot_retry_initial_seconds",
        ),
        binance_depth_snapshot_retry_max_seconds=_positive_float(
            section,
            "binance_depth_snapshot_retry_max_seconds",
        ),
        polymarket_source_timestamp_regression_tolerance_seconds=_nonnegative_float(
            section,
            "polymarket_source_timestamp_regression_tolerance_seconds",
        ),
        ingest_version=_text(section, "ingest_version"),
    )
    if config.max_pending_events < config.flush_size:
        raise ValueError("collection.max_pending_events must be >= flush_size")
    if config.binance_spot_depth_snapshot_limit > 5_000:
        raise ValueError("collection.binance_spot_depth_snapshot_limit must be <= 5000")
    if config.binance_futures_depth_snapshot_limit > 1_000:
        raise ValueError("collection.binance_futures_depth_snapshot_limit must be <= 1000")
    if (
        config.binance_depth_snapshot_retry_max_seconds
        < config.binance_depth_snapshot_retry_initial_seconds
    ):
        raise ValueError(
            "collection.binance_depth_snapshot_retry_max_seconds must be >= "
            "binance_depth_snapshot_retry_initial_seconds"
        )
    return config


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
    raw_value = section.get(name)
    if isinstance(raw_value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and >= 0")
    return value


def _probability(section: Mapping[str, object], name: str) -> float:
    value = _nonnegative_float(section, name)
    if value > 1.0:
        raise ValueError(f"{name} must be <= 1")
    return value


def _probability_excluding_zero(section: Mapping[str, object], name: str) -> float:
    value = _probability(section, name)
    if value == 0.0:
        raise ValueError(f"{name} must be > 0")
    return value


def _bool(section: Mapping[str, object], name: str) -> bool:
    value = section.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()
