"""Single-file runtime configuration for the independent BTC project."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from btc_short_horizon.data.disk_pressure import DiskProtectionPolicy
from btc_short_horizon.execution_timing import (
    CLOB_DELAYED_TAKER_SERVER_MS,
    paper_execution_lifecycle_tail_seconds,
)
from btc_short_horizon.strategy import (
    LayerStructure,
    MakerStrategyConfig,
    OpeningStage,
    StagePolicyConfig,
    StageRule,
)


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
class OkxPublicSubscriptionConfig:
    channel: str
    instrument: str


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
    binance_spot_streams: tuple[str, ...]
    binance_futures_market_streams: tuple[str, ...]
    binance_futures_public_streams: tuple[str, ...]
    okx_subscriptions: tuple[OkxPublicSubscriptionConfig, ...]
    disk_protection: DiskProtectionPolicy
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
    enabled: bool = True
    maker_expiry_policy: str = "fixed_duration"
    maker_fill_evidence_policy: str = "live_stream"

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
        if self.opportunity_policy not in {
            "shared_maker",
            "independent_taker",
            "robust_independent_taker",
        }:
            raise ValueError(
                "paper opportunity policy must be shared_maker, independent_taker, "
                "or robust_independent_taker"
            )
        if self.confirmation_policy not in {"side_only", "edge_stable"}:
            raise ValueError("paper confirmation policy must be side_only or edge_stable")
        if self.opportunity_policy in {
            "independent_taker",
            "robust_independent_taker",
        } and self.mode not in {
            "maker",
            "immediate_fak",
        }:
            raise ValueError("independent taker opportunity policy requires maker or immediate_fak")
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
        if self.maker_expiry_policy not in {"fixed_duration", "market_end"}:
            raise ValueError("maker expiry policy must be fixed_duration or market_end")
        if self.mode == "immediate_fak" and self.maker_expiry_policy != "fixed_duration":
            raise ValueError("immediate-FAK variants require fixed_duration expiry")
        if self.maker_expiry_policy == "market_end" and self.mode != "maker":
            raise ValueError("market_end expiry requires maker mode")
        if (
            self.mode != "immediate_fak"
            and self.maker_expiry_policy == "fixed_duration"
            and self.maker_work_seconds <= 0.0
        ):
            raise ValueError("maker paper variants require maker_work_seconds > 0")
        if self.maker_expiry_policy == "market_end" and self.maker_work_seconds != 0.0:
            raise ValueError("market_end maker variants require maker_work_seconds=0")
        if self.maker_fill_evidence_policy not in {"live_stream", "settlement_trades"}:
            raise ValueError("maker fill evidence policy must be live_stream or settlement_trades")
        if self.maker_fill_evidence_policy == "settlement_trades" and (
            self.mode != "maker" or self.maker_expiry_policy != "market_end"
        ):
            raise ValueError("settlement trade evidence requires a market_end maker variant")
        if not isinstance(self.primary, bool):
            raise ValueError("paper execution primary must be bool")
        if not isinstance(self.enabled, bool):
            raise ValueError("paper execution enabled must be bool")
        if (
            self.mode == "maker"
            and self.opportunity_policy == "shared_maker"
            and any(
                value > 0.0
                for value in (
                    self.minimum_taker_net_edge,
                    self.slippage_buffer,
                    self.model_uncertainty_buffer,
                )
            )
        ):
            raise ValueError("maker-only paper variants cannot configure taker buffers")
        if self.mode in {"immediate_fak", "maker_then_fak"}:
            required_buffers = [self.minimum_taker_net_edge, self.slippage_buffer]
            if self.opportunity_policy != "robust_independent_taker":
                required_buffers.append(self.model_uncertainty_buffer)
            if any(value <= 0.0 for value in required_buffers):
                raise ValueError("FAK paper variants require positive taker safety buffers")
        if (
            self.minimum_taker_net_edge + self.slippage_buffer + self.model_uncertainty_buffer
            >= 1.0
        ):
            raise ValueError("paper execution taker buffers must sum to less than 1")


@dataclass(frozen=True, slots=True)
class PaperResearchConfig:
    tail_entry_price_threshold: float
    evidence_target_markets: int
    market_relative_feature_profile: str
    sealed_forward_start: datetime


@dataclass(frozen=True, slots=True)
class BtcProjectConfig:
    paths: ProjectPaths
    primary_family: BtcMarketFamily
    collection_only_family: BtcMarketFamily
    research_timing: ResearchTimingConfig
    collection: ForwardCollectionConfig
    maker: MakerStrategyConfig
    rule_epoch: str
    model_rule_epoch: str
    allow_rule_epoch_transition_proxy: bool
    paper_execution_epoch: str
    paper_research: PaperResearchConfig
    paper_execution_variants: tuple[PaperExecutionVariantConfig, ...]
    data_sources: tuple[str, ...]
    scenarios: tuple[ExecutionScenario, ...]
    stage_policy: StagePolicyConfig = StagePolicyConfig.default()

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
    if any(variant.primary and not variant.enabled for variant in paper_variants):
        raise ValueError("paper execution primary variant must be enabled")
    if sum(variant.primary and variant.enabled for variant in paper_variants) != 1:
        raise ValueError("exactly one enabled primary paper execution variant is required")
    if (
        any(
            variant.enabled and variant.maker_fill_evidence_policy == "settlement_trades"
            for variant in paper_variants
        )
        and maker.structure is not LayerStructure.SINGLE
    ):
        raise ValueError("settlement trade evidence currently requires a single maker layer")
    stage_policy = _stage_policy(raw.get("stage_policy"))
    rule_epoch = _simple_ascii_identifier(raw.get("rule_epoch"), "rule_epoch")
    model_rule_epoch = _simple_ascii_identifier(raw.get("model_rule_epoch"), "model_rule_epoch")
    allow_rule_epoch_transition_proxy = _bool_default(
        raw,
        "allow_rule_epoch_transition_proxy",
        False,
    )
    if model_rule_epoch != rule_epoch and not allow_rule_epoch_transition_proxy:
        raise ValueError("cross-epoch model requires explicit Research Paper transition proxy")
    paper_execution_epoch = _simple_ascii_identifier(
        raw.get("paper_execution_epoch"),
        "paper_execution_epoch",
    )
    paper_research_section = _mapping(raw, "paper_research")
    paper_research = PaperResearchConfig(
        tail_entry_price_threshold=_probability_excluding_zero(
            paper_research_section,
            "tail_entry_price_threshold",
        ),
        evidence_target_markets=_positive_int(
            paper_research_section,
            "evidence_target_markets",
        ),
        market_relative_feature_profile=_market_relative_feature_profile(paper_research_section),
        sealed_forward_start=_aware_datetime(
            paper_research_section,
            "sealed_forward_start",
        ),
    )
    sources = _data_sources(root, raw)
    scenarios = tuple(_scenario(item) for item in _mapping_list(raw, "execution_scenarios"))
    if not scenarios:
        raise ValueError("execution_scenarios must not be empty")
    if len({scenario.name for scenario in scenarios}) != len(scenarios):
        raise ValueError("execution scenario names must be unique")
    _validate_formal_scenario_grid(scenarios)
    required_handoff_seconds = max(
        (
            maker.entry_end_seconds
            + (
                (
                    scenario.execution.latency_model.base_latency_ms
                    + scenario.execution.latency_model.insert_latency_ms
                )
                / 1_000.0
                if variant.maker_fill_evidence_policy == "settlement_trades"
                else paper_execution_lifecycle_tail_seconds(
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
            )
            for scenario in scenarios
            for variant in paper_variants
            if variant.enabled
        ),
        default=maker.entry_end_seconds,
    )
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
        rule_epoch=rule_epoch,
        model_rule_epoch=model_rule_epoch,
        allow_rule_epoch_transition_proxy=allow_rule_epoch_transition_proxy,
        paper_execution_epoch=paper_execution_epoch,
        paper_research=paper_research,
        paper_execution_variants=paper_variants,
        data_sources=sources,
        scenarios=scenarios,
        stage_policy=stage_policy,
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
        enabled=_bool_default(section, "enabled", True),
        maker_expiry_policy=str(section.get("maker_expiry_policy", "fixed_duration")),
        maker_fill_evidence_policy=str(section.get("maker_fill_evidence_policy", "live_stream")),
    )


def _stage_policy(value: object) -> StagePolicyConfig:
    if value is None:
        return StagePolicyConfig.default()
    if not isinstance(value, Mapping):
        raise ValueError("stage_policy must be a table")
    section = value
    tolerance = _nonnegative_float_default(section, "tolerance_seconds", 0.25)
    raw_rules = section.get("rules")
    if not isinstance(raw_rules, list):
        raise ValueError("stage_policy.rules must be an array")
    rules: list[StageRule] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, Mapping):
            raise ValueError("stage policy rule must be a table")
        item = raw_rule
        stage_id = _text(item, "id")
        try:
            stage = OpeningStage(stage_id)
        except ValueError as exc:
            raise ValueError(f"unknown stage policy id: {stage_id}") from exc
        rules.append(
            StageRule(
                stage=stage,
                start_seconds=_nonnegative_float(item, "start_seconds"),
                end_seconds=_nonnegative_float(item, "end_seconds"),
                minimum_net_edge=_nonnegative_float(item, "minimum_net_edge"),
                minimum_price=_probability_excluding_zero_default(item, "minimum_price", 0.20),
                maximum_price=_probability_excluding_zero_default(item, "maximum_price", 0.80),
                enabled=_bool_default(item, "enabled", True),
            )
        )
    return StagePolicyConfig(tuple(rules), tolerance_seconds=tolerance)


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
        binance_spot_streams=_stream_names(section, "binance_spot_streams"),
        binance_futures_market_streams=_stream_names(section, "binance_futures_market_streams"),
        binance_futures_public_streams=_stream_names(section, "binance_futures_public_streams"),
        okx_subscriptions=tuple(
            OkxPublicSubscriptionConfig(
                channel=_text(item, "channel"),
                instrument=_text(item, "inst_id"),
            )
            for item in _mapping_list(section, "okx_subscriptions")
        ),
        disk_protection=DiskProtectionPolicy(
            warning_free_gib=_positive_float(section, "disk_warning_free_gib"),
            optional_feeds_free_gib=_positive_float(section, "disk_optional_feeds_free_gib"),
            extended_capture_free_gib=_positive_float(section, "disk_extended_capture_free_gib"),
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


def _stream_names(section: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = section.get(name)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty array")
    streams: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item.strip() != item:
            raise ValueError(f"{name} must contain non-empty stream names without whitespace")
        streams.append(item)
    if len(set(streams)) != len(streams):
        raise ValueError(f"{name} must not contain duplicate stream names")
    return tuple(streams)


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


def _market_relative_feature_profile(section: Mapping[str, object]) -> str:
    profile = _text(section, "market_relative_feature_profile")
    if profile not in {"core", "trade_flow", "flow", "enriched"}:
        raise ValueError(
            "paper_research.market_relative_feature_profile must be 'core', 'trade_flow', "
            "'flow', or 'enriched'"
        )
    return profile


def _aware_datetime(section: Mapping[str, object], name: str) -> datetime:
    value = section.get(name)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware TOML datetime")
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


def _nonnegative_float_default(section: Mapping[str, object], name: str, default: float) -> float:
    return default if name not in section else _nonnegative_float(section, name)


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


def _probability_excluding_zero_default(
    section: Mapping[str, object], name: str, default: float
) -> float:
    return default if name not in section else _probability_excluding_zero(section, name)


def _bool(section: Mapping[str, object], name: str) -> bool:
    value = section.get(name)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool")
    return value


def _bool_default(section: Mapping[str, object], name: str, default: bool) -> bool:
    return default if name not in section else _bool(section, name)


def _positive_int_default(section: Mapping[str, object], name: str, default: int) -> int:
    return default if name not in section else _positive_int(section, name)


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()
