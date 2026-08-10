"""Async orchestration for the real-time, credential-free Research Paper layer."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from queue import Empty

import httpx

from btc_short_horizon.config import BtcProjectConfig
from btc_short_horizon.data import MarketWindow
from btc_short_horizon.data.collector import RawCollectorEvent
from btc_short_horizon.data.forward import AdmittedEventBuffer
from btc_short_horizon.execution_timing import CLOB_DELAYED_TAKER_SERVER_MS
from btc_short_horizon.data.gamma import GammaMarketClient
from btc_short_horizon.live.dashboard_state import DashboardSnapshotStore
from btc_short_horizon.live.paper_execution import PaperExecutionConfig, PaperMarketRules
from btc_short_horizon.live.research_paper import (
    PaperLedgerStore,
    ResearchPaperEngine,
    ResearchPaperPortfolio,
)
from btc_short_horizon.live.runtime import RuntimeStatus, RuntimeStatusStore
from btc_short_horizon.live.settlement_trades import (
    PublicSettlementTradesClient,
    SettlementTradeEvidenceStore,
)
from btc_short_horizon.models import ModelArtifactStore
from btc_short_horizon.research.binance_history import fetch_binance_spot_kline_history
from btc_short_horizon.research.opening_proxy import (
    CausalFeatureUnavailableError,
    opening_proxy_feature_schema,
    opening_proxy_protocol,
    validate_opening_proxy_protocol,
)
from btc_short_horizon.research.opening_runtime import build_opening_proxy_prediction
from btc_short_horizon.strategy import StagePolicyConfig


_CLOB_HOST = "https://clob.polymarket.com"
_RULES_RETRY_DELAY = timedelta(seconds=5)
_STATUS_PUBLISH_INTERVAL = timedelta(seconds=5)
_BOOTSTRAP_ANCHOR_TIMEOUT_SECONDS = 30.0
_DECISIONS_WITHOUT_PREDICTION = {
    "book_unavailable",
    "cancel_pending",
    "market_unavailable",
    "outside_entry_window",
    "placement_cycle_used",
}


class PublicPaperRulesClient:
    """Read one versioned CLOB market-rule snapshot without credentials."""

    def __init__(
        self,
        *,
        base_url: str = _CLOB_HOST,
        timeout_seconds: float = 10.0,
        enabled_taker_delay_ms: float = CLOB_DELAYED_TAKER_SERVER_MS,
        taker_delay_policy_id: str = "clob-itode-250ms-v1",
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("CLOB rules base_url must use https")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be finite and > 0")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        if not isfinite(enabled_taker_delay_ms) or enabled_taker_delay_ms <= 0.0:
            raise ValueError("enabled_taker_delay_ms must be finite and > 0")
        if not taker_delay_policy_id:
            raise ValueError("taker_delay_policy_id must not be empty")
        self.enabled_taker_delay_ms = enabled_taker_delay_ms
        self.taker_delay_policy_id = taker_delay_policy_id

    async def fetch(
        self,
        market: MarketWindow,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, PaperMarketRules]:
        owns_client = client is None
        active = client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await active.get(f"{self.base_url}/clob-markets/{market.condition_id}")
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns_client:
                await active.aclose()
        if not isinstance(payload, Mapping):
            raise ValueError("CLOB market rules must be a JSON object")
        tokens = payload.get("t")
        if not isinstance(tokens, list):
            raise ValueError("CLOB market rules require token metadata")
        token_ids = {
            str(item.get("t"))
            for item in tokens
            if isinstance(item, Mapping) and item.get("t") is not None
        }
        expected = {market.up_token_id, market.down_token_id}
        if token_ids != expected:
            raise ValueError("CLOB market-rule tokens do not match Gamma")
        fee_details = payload.get("fd")
        if not isinstance(fee_details, Mapping):
            raise ValueError("CLOB market rules require fee details")
        taker_fee_rate = _nonnegative_number(fee_details.get("r"), "fee rate")
        taker_fee_exponent = _nonnegative_integer(fee_details.get("e"), "fee exponent")
        taker_only = fee_details.get("to")
        if taker_only is not True:
            raise ValueError("Research Paper requires a verified taker-only fee schedule")
        taker_delay_enabled = payload.get("itode")
        if not isinstance(taker_delay_enabled, bool):
            raise ValueError("CLOB market rules require a boolean taker delay flag")
        taker_server_delay_ms = self.enabled_taker_delay_ms if taker_delay_enabled else 0.0
        tick_size = _decimal_text(payload.get("mts"), "minimum tick size")
        minimum_order_size = _positive_number(payload.get("mos"), "minimum order size")
        neg_risk = payload.get("nr", False)
        if neg_risk is None:
            neg_risk = False
        if not isinstance(neg_risk, bool):
            raise ValueError("CLOB neg risk must be bool")
        observed_at_ns = int(datetime.now(UTC).timestamp() * 1_000_000_000)
        return {
            token_id: PaperMarketRules(
                condition_id=market.condition_id,
                token_id=token_id,
                tick_size=tick_size,
                minimum_order_size=minimum_order_size,
                neg_risk=neg_risk,
                maker_fee_rate_bps=0,
                observed_at_ns=observed_at_ns,
                taker_fee_rate=taker_fee_rate,
                taker_fee_exponent=taker_fee_exponent,
                taker_only=taker_only,
                taker_server_delay_ms=taker_server_delay_ms,
                taker_delay_enabled=taker_delay_enabled,
                taker_delay_policy_id=self.taker_delay_policy_id,
            )
            for token_id in expected
        }


class ModelPaperPredictor:
    """Pinned model adapter implementing the Paper predictor boundary."""

    def __init__(self, *, project: BtcProjectConfig, model_directory: Path) -> None:
        schema = opening_proxy_feature_schema(1)
        self.model, self.metadata = ModelArtifactStore.load(
            directory=model_directory,
            expected_schema_hash=schema.hash,
        )
        self._stage_models: dict[str, tuple[object, object]] = {}
        for rule in project.stage_policy.rules:
            stage_directory = model_directory / rule.stage.value
            if not stage_directory.is_dir():
                continue
            stage_model, stage_metadata = ModelArtifactStore.load(
                directory=stage_directory,
                expected_schema_hash=schema.hash,
            )
            self._stage_models[rule.stage.value] = (stage_model, stage_metadata)
        self._stage_policy = project.stage_policy
        protocol = opening_proxy_protocol(
            entry_start_seconds=project.research_timing.entry_start_seconds,
            entry_end_seconds=project.research_timing.entry_end_seconds,
            snapshot_seconds=project.research_timing.training_snapshot_seconds,
        )
        validate_opening_proxy_protocol(self.metadata.config, expected=protocol)
        for _model, metadata in self._stage_models.values():
            validate_opening_proxy_protocol(metadata.config, expected=protocol)

    @property
    def model_id(self) -> str:
        return self.metadata.model_id

    @property
    def stage_model_ids(self) -> dict[str, str]:
        return {
            stage: metadata.model_id for stage, (_model, metadata) in self._stage_models.items()
        }

    def __call__(self, market, history, observation):  # type: ignore[no-untyped-def]
        if history is None:
            raise ValueError("Research Paper Binance bootstrap is unavailable")
        model, metadata = self.model, self.metadata
        elapsed = (
            observation.decision_ts_ns - int(market.t0.timestamp() * 1_000_000_000)
        ) / 1_000_000_000
        try:
            rule = self._stage_policy.rule_for(elapsed)
        except ValueError:
            rule = None
        if rule is not None and rule.stage.value in self._stage_models:
            model, metadata = self._stage_models[rule.stage.value]
        return build_opening_proxy_prediction(
            model=model,
            metadata=metadata,
            market=market,
            klines=history,
            market_observation=observation,
        )


def build_research_paper_portfolio(
    *,
    project: BtcProjectConfig,
    predictor: ModelPaperPredictor,
    runtime_root: Path,
    starting_balance: float,
    stage_policy: StagePolicyConfig | None = None,
) -> ResearchPaperPortfolio:
    """Build the one execution-policy portfolio shared by live and replay."""

    scenario = project.require_scenario("p99_half_volume_book_first").execution
    latency = scenario.latency_model
    execution_config = PaperExecutionConfig(
        insert_latency_ms=latency.base_latency_ms + latency.insert_latency_ms,
        cancel_latency_ms=latency.base_latency_ms + latency.cancel_latency_ms,
        taker_latency_ms=latency.base_latency_ms + latency.insert_latency_ms,
        trade_volume_multiplier=scenario.trade_execution_size_multiplier,
    )
    return ResearchPaperPortfolio(
        tuple(
            ResearchPaperEngine(
                predictor=predictor,
                model_id=predictor.model_id,
                maker_config=replace(
                    project.maker,
                    max_work_seconds=(
                        variant.maker_work_seconds
                        if variant.mode == "maker"
                        else project.maker.max_work_seconds
                    ),
                ),
                execution_config=execution_config,
                variant=variant,
                ledger_store=PaperLedgerStore(
                    runtime_root,
                    project.paper_execution_epoch,
                    variant.variant_id,
                ),
                starting_balance=starting_balance,
                stage_policy=stage_policy or project.stage_policy,
                clob_capture_end_seconds=project.collection.opening_handoff_delay_seconds,
            )
            for variant in project.paper_execution_variants
        )
    )


class ResearchPaperRuntime:
    """Keep Paper failures isolated while preserving real-time causal decisions."""

    def __init__(
        self,
        *,
        project: BtcProjectConfig,
        model_directory: Path,
        runtime_root: Path,
        rule_epoch: str,
        event_buffer: AdmittedEventBuffer,
        starting_balance: float = 1_000.0,
        rules_client: PublicPaperRulesClient | None = None,
        gamma_client: GammaMarketClient | None = None,
        settlement_trades_client: PublicSettlementTradesClient | None = None,
    ) -> None:
        predictor = ModelPaperPredictor(project=project, model_directory=model_directory)
        self.predictor = predictor
        self.engine = build_research_paper_portfolio(
            project=project,
            predictor=predictor,
            runtime_root=runtime_root,
            starting_balance=starting_balance,
            stage_policy=project.stage_policy,
        )
        self.project = project
        self.runtime_root = runtime_root
        self.rule_epoch = rule_epoch
        self.event_buffer = event_buffer
        self.rules_client = rules_client or PublicPaperRulesClient()
        self.gamma_client = gamma_client or GammaMarketClient()
        self.settlement_trades_client = settlement_trades_client or PublicSettlementTradesClient()
        self.settlement_trade_store = SettlementTradeEvidenceStore(
            runtime_root,
            project.paper_execution_epoch,
        )
        self._markets: dict[str, MarketWindow] = {}
        self._rules: dict[str, dict[str, PaperMarketRules]] = {}
        self._rule_tasks: dict[str, asyncio.Task[None]] = {}
        self._next_rule_retry: dict[str, datetime] = {}
        self._recent_events: dict[str, deque[RawCollectorEvent]] = defaultdict(
            lambda: deque(maxlen=20_000)
        )
        self._active_slug: str | None = None
        self._next_decision_ns: int | None = None
        self._next_resolution_check = datetime.min.replace(tzinfo=UTC)
        self._next_publish_at = datetime.min.replace(tzinfo=UTC)
        self._last_decision_result = "not_started"
        self._last_error: str | None = None
        self._recoverable_errors: dict[str, str] = {}
        self._prediction_errors = 0
        self._last_prediction_error: dict[str, str] | None = None
        self._resolution_errors = 0
        self._started_at = datetime.now(UTC)
        self._ready = False

    @property
    def model_id(self) -> str:
        return self.engine.model_id

    def register_markets(self, market: MarketWindow, lookahead: MarketWindow | None) -> None:
        now = datetime.now(UTC)
        registered = tuple(item for item in (market, lookahead) if item is not None)
        retained_slugs = {item.slug for item in registered}
        retired = tuple(item for slug, item in self._markets.items() if slug not in retained_slugs)
        for item in retired:
            self._markets.pop(item.slug, None)
            self._rules.pop(item.slug, None)
            self._next_rule_retry.pop(item.slug, None)
            self._recoverable_errors.pop(f"rules:{item.slug}", None)
            task = self._rule_tasks.pop(item.slug, None)
            if task is not None and not task.done():
                task.cancel()
            self._recent_events.pop(item.up_token_id, None)
            self._recent_events.pop(item.down_token_id, None)
        if self._active_slug not in retained_slugs:
            self._active_slug = None
            self._next_decision_ns = None
        for item in registered:
            self._markets[item.slug] = item
            self._schedule_rule_fetch(item, now=now)

    async def run(self, *, stop_event: asyncio.Event) -> None:
        try:
            if not await self._bootstrap_history(stop_event=stop_event):
                self._checkpoint_evaluations()
                self._publish(now=datetime.now(UTC), state="stopped", healthy=True)
                return
            self._ready = True
            while not stop_event.is_set():
                if self.event_buffer.overflowed:
                    self._last_error = "admitted_event_buffer_overflow"
                    self._checkpoint_evaluations()
                    self._publish(now=datetime.now(UTC), state="failed", healthy=False)
                    return
                self._drain_events()
                now = datetime.now(UTC)
                self._activate_current_market(now)
                self._run_due_decision(now)
                if now >= self._next_resolution_check:
                    await self._refresh_resolutions()
                    self._next_resolution_check = now + timedelta(seconds=30)
                self._publish_running_if_due(now)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.05)
                except TimeoutError:
                    continue
            self._checkpoint_evaluations()
            self._publish(now=datetime.now(UTC), state="stopped", healthy=True)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._checkpoint_evaluations()
            self._publish(now=datetime.now(UTC), state="failed", healthy=False)
            raise

    def close(self) -> None:
        for task in self._rule_tasks.values():
            if not task.done():
                task.cancel()
        self.engine.close()

    def _checkpoint_evaluations(self) -> None:
        checkpoint = getattr(getattr(self, "engine", None), "checkpoint_evaluations", None)
        if checkpoint is not None:
            checkpoint()

    async def _bootstrap_history(self, *, stop_event: asyncio.Event) -> bool:
        deferred: list[RawCollectorEvent] = []
        anchor: RawCollectorEvent | None = None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BOOTSTRAP_ANCHOR_TIMEOUT_SECONDS
        while anchor is None:
            if stop_event.is_set():
                return False
            if self.event_buffer.overflowed:
                raise RuntimeError("admitted_event_buffer_overflow")
            try:
                event = self.event_buffer.get_nowait()
            except Empty:
                if loop.time() >= deadline:
                    raise TimeoutError("timed out waiting for an admitted closed Binance kline")
                await asyncio.sleep(0.05)
                continue
            deferred.append(event)
            if _closed_binance_kline_open_ms(event) is not None:
                anchor = event

        anchor_open_ms = _closed_binance_kline_open_ms(anchor)
        if anchor_open_ms is None:
            raise RuntimeError("Binance bootstrap anchor disappeared")
        end = datetime.fromtimestamp(anchor_open_ms / 1_000, tz=UTC)
        start = end - timedelta(seconds=self.project.research_timing.max_feature_lookback_seconds)
        history = await fetch_binance_spot_kline_history(
            start_time=start,
            end_time=end,
        )
        self.engine.set_kline_history(history)
        if self.event_buffer.overflowed:
            raise RuntimeError("admitted_event_buffer_overflow")
        for event in deferred:
            self._consume_event(event)
        return True

    async def _fetch_rules(self, market: MarketWindow) -> None:
        try:
            self._rules[market.slug] = await self.rules_client.fetch(market)
        except Exception as exc:
            key = f"rules:{market.slug}"
            self._recoverable_errors[key] = f"{type(exc).__name__}: {exc}"
        else:
            self._recoverable_errors.pop(f"rules:{market.slug}", None)
            self._next_rule_retry.pop(market.slug, None)

    def _schedule_rule_fetch(self, market: MarketWindow, *, now: datetime) -> None:
        if market.slug in self._rules:
            return
        task = self._rule_tasks.get(market.slug)
        if task is not None and not task.done():
            return
        retry_at = self._next_rule_retry.get(market.slug)
        if retry_at is not None and now < retry_at:
            return
        self._next_rule_retry[market.slug] = now + _RULES_RETRY_DELAY
        self._rule_tasks[market.slug] = asyncio.create_task(
            self._fetch_rules(market),
            name=f"paper-rules-{market.slug}",
        )

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.event_buffer.get_nowait()
            except Empty:
                return
            self._consume_event(event)

    def _consume_event(self, event: RawCollectorEvent) -> None:
        self._recent_events[event.timing.instrument].append(event)
        self.engine.on_event(event)

    def _activate_current_market(self, now: datetime) -> None:
        candidates = tuple(
            market for market in self._markets.values() if market.t0 <= now < market.t1
        )
        if not candidates:
            return
        market = max(candidates, key=lambda item: item.t0)
        if market.slug == self._active_slug:
            return
        if self.engine.market is not None and self.engine.market.slug != market.slug:
            self.engine.advance(now_ts_ns=int(now.timestamp() * 1_000_000_000))
            if self.engine.has_unfinished_placement:
                return
        rules = self._rules.get(market.slug)
        if rules is None:
            self._schedule_rule_fetch(market, now=now)
            return
        self.engine.activate_market(market, rules=rules)
        events = [
            *self._recent_events[market.up_token_id],
            *self._recent_events[market.down_token_id],
        ]
        for event in sorted(
            events,
            key=lambda item: (
                item.timing.available_ts,
                item.admission_sequence,
                item.timing.instrument,
            ),
        ):
            self.engine.on_event(event)
        self._active_slug = market.slug
        cadence_ns = round(self.project.maker.signal_cadence_seconds * 1_000_000_000)
        start_ns = int(market.t0.timestamp() * 1_000_000_000)
        now_ns = int(now.timestamp() * 1_000_000_000)
        first_ns = start_ns + round(
            max(
                self.project.maker.entry_start_seconds,
                self.project.maker.signal_cadence_seconds,
            )
            * 1_000_000_000
        )
        if now_ns <= first_ns:
            self._next_decision_ns = first_ns
        else:
            steps = (now_ns - first_ns + cadence_ns - 1) // cadence_ns
            self._next_decision_ns = first_ns + steps * cadence_ns

    def _run_due_decision(self, now: datetime) -> None:
        if self.engine.market is None:
            return
        now_ns = int(now.timestamp() * 1_000_000_000)
        self.engine.advance(now_ts_ns=now_ns)
        if self._next_decision_ns is None or now_ns < self._next_decision_ns:
            return
        end_ns = int(self.engine.market.t0.timestamp() * 1_000_000_000) + round(
            (
                self.project.maker.entry_end_seconds
                + max(
                    variant.maker_work_seconds for variant in self.project.paper_execution_variants
                )
            )
            * 1_000_000_000
        )
        if self._next_decision_ns > end_ns:
            self._next_decision_ns = None
            return
        try:
            self._last_decision_result = self.engine.decide(now_ts_ns=now_ns)
        except CausalFeatureUnavailableError as exc:
            if "prediction" not in self._recoverable_errors:
                self._prediction_errors += 1
            self._last_decision_result = f"prediction_unavailable:{exc}"
            message = f"{type(exc).__name__}: {exc}"
            self._recoverable_errors["prediction"] = message
            self._last_prediction_error = {
                "market_slug": self.engine.market.slug,
                "observed_at": now.isoformat(),
                "message": message,
            }
        else:
            if self._last_decision_result not in _DECISIONS_WITHOUT_PREDICTION:
                self._recoverable_errors.pop("prediction", None)
                self._last_prediction_error = None
        self._next_decision_ns += round(self.project.maker.signal_cadence_seconds * 1_000_000_000)

    async def _settle_resolved_markets(self) -> None:
        unresolved = set(self.engine.unresolved_market_slugs())
        if not unresolved:
            return
        catalog = await self.gamma_client.discover_catalog(
            family=self.project.primary_family,
            rule_epoch=self.rule_epoch,
            closed=True,
            slugs=tuple(sorted(unresolved)),
        )
        for market in catalog.windows():
            if market.resolution is None or market.label_available_ts is None:
                continue
            if self.engine.requires_settlement_trade_evidence(market.slug):
                boundary_ns = self.engine.deferred_fill_start_ns(market.slug)
                if boundary_ns is None:
                    raise RuntimeError("deferred maker evidence boundary is unavailable")
                evidence = await self.settlement_trades_client.fetch(
                    market,
                    start_seconds=boundary_ns // 1_000_000_000 + 1,
                    end_seconds=int(market.t1.timestamp()),
                )
                self.settlement_trade_store.write(evidence)
                self.engine.apply_settlement_trade_evidence(
                    market_slug=market.slug,
                    trades=evidence.trades,
                    assessed_at_ns=int(evidence.fetched_at.timestamp() * 1_000_000_000),
                )
            self.engine.settle(
                market_slug=market.slug,
                outcome=market.resolution,
                label_available_ts_ns=int(market.label_available_ts.timestamp() * 1_000_000_000),
            )

    async def _refresh_resolutions(self) -> None:
        try:
            await self._settle_resolved_markets()
        except Exception as exc:
            self._resolution_errors += 1
            self._recoverable_errors["resolution"] = f"{type(exc).__name__}: {exc}"
        else:
            self._recoverable_errors.pop("resolution", None)

    def _publish_running_if_due(self, now: datetime) -> None:
        if now < self._next_publish_at:
            return
        self._publish(
            now=now,
            state="running",
            healthy=not self._recoverable_errors,
        )
        self._next_publish_at = now + _STATUS_PUBLISH_INTERVAL

    def _publish(self, *, now: datetime, state: str, healthy: bool) -> None:
        details = {
            "model_id": self.model_id,
            "default_model_id": self.predictor.metadata.model_id,
            "stage_model_ids": self.predictor.stage_model_ids,
            "paper_execution_epoch": self.project.paper_execution_epoch,
            "active_market": self._active_slug,
            "ready": self._ready,
            "last_decision_result": self._last_decision_result,
            "prediction_errors": self._prediction_errors,
            "last_prediction_error": self._last_prediction_error,
            "order_count": len(self.engine.records),
            "fill_count": sum(item.filled_shares > 0.0 for item in self.engine.records),
            "resolved_count": sum(item.realized_pnl is not None for item in self.engine.records),
            "execution_variants": [
                {
                    "id": engine.variant.variant_id,
                    "mode": engine.variant.mode,
                    "primary": engine.variant.primary,
                    "last_decision_result": self.engine.last_decisions.get(
                        engine.variant.variant_id,
                        "not_started",
                    ),
                    "order_count": len(engine.records),
                    "fill_count": sum(item.filled_shares > 0.0 for item in engine.records),
                }
                for engine in self.engine.engines
            ],
            "event_buffer_pending": self.event_buffer.pending_events,
            "event_buffer_overflowed": self.event_buffer.overflowed,
            "event_buffer_dropped": self.event_buffer.dropped_events,
            "execution_assumption": "p99_half_volume_book_first",
            "resolution_errors": self._resolution_errors,
            "recoverable_errors": dict(self._recoverable_errors),
            "last_error": (
                self._last_error or ("; ".join(self._recoverable_errors.values()) or None)
            ),
            "credentials_loaded": False,
            "real_orders_enabled": False,
        }
        RuntimeStatusStore(self.runtime_root).write(
            RuntimeStatus(
                service="research_paper",
                mode="paper",
                state=state,
                healthy=healthy,
                started_at=self._started_at,
                updated_at=now,
                details=details,
            )
        )
        DashboardSnapshotStore(self.runtime_root).write(self.engine.dashboard_snapshot(now=now))


def _positive_number(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be finite and > 0")
    return result


def _closed_binance_kline_open_ms(event: RawCollectorEvent) -> int | None:
    if (
        event.timing.source != "binance_spot"
        or event.timing.instrument != "BTCUSDT"
        or event.event_type != "kline_1s"
    ):
        return None
    data = event.payload.get("data")
    message = data if isinstance(data, Mapping) else event.payload
    kline = message.get("k")
    if not isinstance(kline, Mapping) or kline.get("x") is not True:
        return None
    open_ms = kline.get("t")
    if isinstance(open_ms, bool) or not isinstance(open_ms, int) or open_ms < 0:
        raise ValueError("Binance bootstrap kline open time must be a non-negative integer")
    if open_ms % 1_000 != 0:
        raise ValueError("Binance bootstrap kline open time must align to one second")
    return open_ms


def _nonnegative_number(value: object, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(result) or result < 0.0 or isinstance(value, bool):
        raise ValueError(f"{name} must be finite and non-negative")
    return result


def _nonnegative_integer(value: object, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 0 or isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def _decimal_text(value: object, name: str) -> str:
    result = _positive_number(value, name)
    if result >= 1.0:
        raise ValueError(f"{name} must be below 1")
    return format(result, ".10g")


__all__ = [
    "ModelPaperPredictor",
    "PublicPaperRulesClient",
    "ResearchPaperRuntime",
]
