# Codebase UML Inventory

This file is generated from Python AST metadata and excludes `tests/` plus git-ignored private strategy/research directories.
Generated: 2026-07-20T03:28:31+00:00
Modules: 196 | Classes: 328 | Functions/methods: 2377

## Backtesting Data Flow

```mermaid
flowchart TD
    Main[main.py / runner scripts] --> Experiment[ReplayExperiment or ParameterSearchExperiment]
    Experiment --> Backtest[PredictionMarketBacktest]
    Backtest --> Registry[data_sources.registry]
    Registry --> Adapter[HistoricalReplayAdapter]
    Adapter --> Loader[Vendor loader: Kalshi / Polymarket / PMXT / Telonex]
    Loader --> Records[LoadedReplay records + instrument]
    Records --> Engine[Nautilus BacktestEngine]
    Engine --> Strategy[Strategy configs / LongOnlyPredictionMarketStrategy]
    Engine --> Artifacts[Artifacts, reports, summary series]
    Artifacts --> Optimizer[Optimizer score and leaderboard]
```

## Module Inventory

### `backtests/__init__.py`
- Imports: none

### `backtests/_beffer45_trade_data.py`
- Imports: `__future__`

### `backtests/_script_helpers.py`
- Imports: `__future__, importlib, pathlib, sys`
- Function L8: `ensure_repo_root(script_path: str | Path) -> Path`
- Function L23: `parse_csv_env(raw: str) -> list[str]`
- Function L27: `parse_bool_env(raw: str, *, default: bool = True) -> bool`

### `backtests/polymarket_beffer45_trade_replay_telonex.py`
- Imports: `__future__, collections, csv, datetime, decimal, pathlib, re, typing`
- Function L44: `_decimal(value: object) -> Decimal`
- Function L48: `_trade_notional(trade: Mapping[str, object]) -> Decimal`
- Function L52: `_trade_timestamp(trade: Mapping[str, object]) -> datetime`
- Function L56: `_iso(timestamp: datetime) -> str`
- Function L60: `_group_trades_by_instrument(trades: Iterable[Mapping[str, object]]) -> dict[tuple[str, int], tuple[dict[str, object], ...]]`
- Function L72: `_replay_window(*, slug: str, trades: Sequence[Mapping[str, object]]) -> tuple[str, str]`
- Function L91: `_ledger_cash_pnl(trades: Sequence[Mapping[str, object]]) -> Decimal`
- Function L102: `_ledger_open_quantity(trades: Sequence[Mapping[str, object]]) -> Decimal`
- Function L113: `_ledger_settlement_pnl(trades: Sequence[Mapping[str, object]], realized_outcome: object) -> Decimal | None`
- Function L121: `_sum_notional(trades: Iterable[Mapping[str, object]], *, side: str) -> Decimal`
- Function L128: `_build_replays() -> tuple[Any, ...]`
- Function L160: `_print_ledger_header() -> None`
- Function L187: `_write_comparison_csv(rows: Sequence[Mapping[str, object]]) -> None`
- Function L209: `_print_backtest_comparison(results: Sequence[Mapping[str, Any]]) -> None`
- Function L277: `run() -> None`

### `backtests/polymarket_book_ema_crossover.py`
- Imports: `__future__, decimal`
- Function L18: `run() -> None`

### `backtests/polymarket_book_ema_optimizer.py`
- Imports: `__future__, decimal`
- Function L18: `run() -> None`

### `backtests/polymarket_book_joint_portfolio_runner.py`
- Imports: `__future__, decimal`
- Function L18: `run() -> None`

### `backtests/polymarket_btc_15m_opening_mispricing_maker.py`
- Imports: `__future__, argparse, btc_short_horizon, collections, dataclasses, datetime, hashlib, json, pathlib, prediction_market_extensions, typing`
- Function L45: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L76: `load_runner_inputs(args: argparse.Namespace) -> RunnerInputs`
- Function L87: `build_backtest_from_args(args: argparse.Namespace) -> Any`
- Function L93: `run(argv: Sequence[str] | None = None) -> list[dict[str, Any]]`
- Function L117: `build_run_artifacts(*, args: argparse.Namespace, inputs: RunnerInputs, results: Sequence[Mapping[str, object]], order_events: Sequence[Mapping[str, object]] = ()) -> BtcRunArtifacts`
- Function L174: `_build_backtest(*, args: argparse.Namespace, inputs: RunnerInputs) -> Any`
- Function L201: `_load_market(*, args: argparse.Namespace, config: BtcProjectConfig) -> MarketWindow`
- Function L222: `_read_metadata(path: Path) -> dict[str, object]`
- Function L232: `_market_input_hashes(args: argparse.Namespace) -> dict[str, str]`
- Function L239: `_prediction_record(signal: BtcOpeningMispricingSignal) -> dict[str, object]`
- Function L259: `_flatten_results(*, market_slug: str, results: Sequence[Mapping[str, object]]) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...], tuple[dict[str, object], ...]]`
- Function L304: `_result_metrics(results: Sequence[Mapping[str, object]]) -> dict[str, object]`
- Function L314: `_price_points(value: object) -> tuple[tuple[object, object], ...]`
- Function L322: `_mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]`
- Function L328: `_as_int(value: object) -> int`
- Function L335: `_as_float(value: object) -> float`
- Function L342: `_sha256_file(path: Path) -> str`
- Function L350: `_require_sha256(value: object, name: str) -> str`
- Function L357: `_parse_utc_datetime(value: str) -> datetime`
- Function L367: `_require_text(value: object, name: str) -> str`
- Class L39: `RunnerInputs`

### `backtests/polymarket_btc_5m_late_favorite_taker_hold.py`
- Imports: `__future__, datetime, decimal`
- Function L26: `_utc_iso(value: datetime) -> str`
- Function L30: `_btc_5m_windows() -> tuple[tuple[str, datetime, datetime], ...]`
- Function L42: `_btc_5m_replays() -> Any`
- Function L65: `run() -> None`

### `backtests/polymarket_btc_5m_pair_arbitrage.py`
- Imports: `__future__, datetime, decimal`
- Function L24: `_utc_iso(value: datetime) -> str`
- Function L28: `_btc_5m_windows() -> tuple[tuple[str, str, str], ...]`
- Function L40: `_btc_5m_replays() -> Any`
- Function L56: `run() -> None`

### `backtests/polymarket_pmxt_book_100_replay_runner.py`
- Imports: `__future__, decimal`
- Function L126: `run() -> None`

### `backtests/polymarket_telonex_book_100_replay_runner.py`
- Imports: `__future__, decimal`
- Function L126: `run() -> None`

### `backtests/polymarket_telonex_book_joint_portfolio_runner.py`
- Imports: `__future__, decimal`
- Function L18: `run() -> None`

### `backtests/sitecustomize.py`
- Imports: `__future__, importlib, pathlib, sys`

### `btc_short_horizon/__init__.py`
- Imports: none

### `btc_short_horizon/backtest/__init__.py`
- Imports: `__future__, btc_short_horizon, importlib, typing`
- Function L52: `__getattr__(name: str) -> Any`

### `btc_short_horizon/backtest/audit.py`
- Imports: `__future__, typing`
- Function L40: `_json_ready_details(details: Mapping[str, object]) -> dict[str, object]`
- Class L8: `OrderAuditTrail`
  - Method L11: `__init__(self) -> None`
  - Method L15: `records(self) -> tuple[dict[str, object], ...]`
  - Method L18: `record(self, *, event_type: str, ts_ns: int, client_order_id: str | None = None, **details: object) -> None`

### `btc_short_horizon/backtest/joint.py`
- Imports: `__future__, btc_short_horizon, dataclasses, datetime, decimal, prediction_market_extensions, typing`
- Function L33: `_as_utc(value: datetime, name: str) -> datetime`
- Function L83: `build_btc_joint_backtest(*, name: str, data: MarketDataConfig, config: BtcJointReplayConfig) -> PredictionMarketBacktest`
- Function L162: `collect_btc_order_events(backtest: PredictionMarketBacktest) -> tuple[dict[str, object], ...]`
- Class L40: `BtcJointReplayConfig`
  - Method L54: `__post_init__(self) -> None`

### `btc_short_horizon/backtest/signal_io.py`
- Imports: `__future__, btc_short_horizon, pathlib, pyarrow, typing`
- Function L36: `write_opening_mispricing_signals(path: Path, signals: Sequence[BtcOpeningMispricingSignal]) -> None`
- Function L47: `read_opening_mispricing_signals(path: Path, *, market_slug: str | None = None) -> tuple[BtcOpeningMispricingSignal, ...]`
- Function L64: `_signal_record(signal: BtcOpeningMispricingSignal) -> dict[str, object]`
- Function L68: `_signal_from_record(record: dict[str, object]) -> BtcOpeningMispricingSignal`
- Function L93: `_signal_sort_key(signal: BtcOpeningMispricingSignal) -> tuple[int, int, str, str, str]`

### `btc_short_horizon/backtest/signals.py`
- Imports: `btc_short_horizon, math, nautilus_trader`
- Function L30: `to_opening_mispricing_signal(prediction: OpeningMispricingPrediction) -> BtcOpeningMispricingSignal`
- Function L52: `validate_opening_mispricing_signal(signal: BtcOpeningMispricingSignal) -> None`
- Function L74: `opening_signal_data_age_seconds(signal: BtcOpeningMispricingSignal, *, now_ts_ns: int) -> float`
- Function L83: `opening_signal_entry_rejection_reason(signal: BtcOpeningMispricingSignal, *, now_ts_ns: int, stale_after_seconds: float) -> str | None`
- Class L12: `BtcOpeningMispricingSignal(Data)`

### `btc_short_horizon/backtest/strategy.py`
- Imports: `__future__, btc_short_horizon, dataclasses, decimal, nautilus_trader, typing`
- Function L37: `_as_float(value: object | None) -> float | None`
- Function L49: `_event_ts_ns(event: object) -> int`
- Class L60: `BtcOpeningMispricingConfig(StrategyConfig)`
  - Method L79: `__post_init__(self) -> None`
  - Method L88: `maker_config(self) -> MakerStrategyConfig`
- Class L105: `BtcOpeningMispricingStrategy(Strategy)`
  - Method L108: `__init__(self, config: BtcOpeningMispricingConfig) -> None`
  - Method L121: `order_audit_events(self) -> tuple[dict[str, object], ...]`
  - Method L124: `on_start(self) -> None`
  - Method L138: `on_data(self, data) -> None`
  - Method L152: `on_order_book_deltas(self, deltas) -> None`
  - Method L163: `on_order_filled(self, event) -> None`
  - Method L184: `on_order_canceled(self, event) -> None`
  - Method L187: `on_order_expired(self, event) -> None`
  - Method L190: `on_order_rejected(self, event) -> None`
  - Method L193: `on_order_denied(self, event) -> None`
  - Method L196: `on_stop(self) -> None`
  - Method L200: `on_reset(self) -> None`
  - Method L209: `_process_signal(self, *, now_ts_ns: int) -> None`
  - Method L218: `_submit_plan_if_actionable(self, *, signal: BtcOpeningMispricingSignal, now_ts_ns: int) -> None`
  - Method L274: `_evaluate_working_orders(self, *, signal: BtcOpeningMispricingSignal, now_ts_ns: int) -> None`
  - Method L298: `_outcome_books(self) -> OutcomeBooks | None`
  - Method L308: `_side_book(self, side: TokenSide) -> SideBook | None`
  - Method L328: `_materialize_plan(self, plan: OrderPlan) -> OrderPlan | None`
  - Method L349: `_submit_orders(self, plan: OrderPlan) -> None`
  - Method L397: `_request_cancel(self, reason: str, *, now_ts_ns: int) -> None`
  - Method L411: `_close_order_event(self, event, *, terminal_event: str, rejected: bool) -> None`
  - Method L432: `_side_for_instrument(self, instrument_id: object) -> TokenSide | None`
  - Method L439: `_instrument_id_for(self, side: TokenSide) -> InstrumentId`

### `btc_short_horizon/config.py`
- Imports: `__future__, btc_short_horizon, dataclasses, pathlib, prediction_market_extensions, tomllib, typing`
- Function L69: `load_btc_project_config(path: Path) -> BtcProjectConfig`
- Function L144: `_scenario(section: Mapping[str, object]) -> ExecutionScenario`
- Function L166: `_forward_collection(section: Mapping[str, object]) -> ForwardCollectionConfig`
- Function L176: `_family(section: Mapping[str, object]) -> BtcMarketFamily`
- Function L185: `_mapping(raw: Mapping[str, object], name: str) -> Mapping[str, object]`
- Function L192: `_mapping_list(raw: Mapping[str, object], name: str) -> tuple[Mapping[str, object], ...]`
- Function L199: `_text(section: Mapping[str, object], name: str) -> str`
- Function L206: `_text_list(raw: Mapping[str, object], name: str) -> tuple[str, ...]`
- Function L213: `_data_sources(root: Path, raw: Mapping[str, object]) -> tuple[str, ...]`
- Function L217: `_resolve_data_source(root: Path, value: str) -> str`
- Function L226: `_int(section: Mapping[str, object], name: str) -> int`
- Function L233: `_positive_int(section: Mapping[str, object], name: str) -> int`
- Function L240: `_int_list(section: Mapping[str, object], name: str) -> tuple[int, ...]`
- Function L247: `_nonnegative_int_list(section: Mapping[str, object], name: str) -> tuple[int, ...]`
- Function L254: `_positive_float(section: Mapping[str, object], name: str) -> float`
- Function L261: `_nonnegative_float(section: Mapping[str, object], name: str) -> float`
- Function L271: `_probability(section: Mapping[str, object], name: str) -> float`
- Function L278: `_bool(section: Mapping[str, object], name: str) -> bool`
- Function L285: `_resolve_path(root: Path, value: str) -> Path`
- Class L20: `ProjectPaths`
- Class L27: `ResearchTimingConfig`
- Class L37: `ForwardCollectionConfig`
- Class L46: `ExecutionScenario`
- Class L52: `BtcProjectConfig`
  - Method L62: `require_scenario(self, name: str) -> ExecutionScenario`

### `btc_short_horizon/data/__init__.py`
- Imports: `btc_short_horizon`

### `btc_short_horizon/data/binance.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime, enum, math, typing`
- Function L37: `normalize_binance_trade(payload: Mapping[str, object], *, collector_receive_ts: datetime | None, source: str = 'binance_spot', availability_delay: timedelta = timedelta(0)) -> BinanceTradeRecord`
- Function L80: `normalize_binance_kline(payload: Mapping[str, object], *, collector_receive_ts: datetime, source: str = 'binance_spot') -> TimedMarketEvent`
- Function L133: `normalize_binance_depth_update(payload: Mapping[str, object], *, collector_receive_ts: datetime | None, source: str = 'binance_spot', availability_delay: timedelta = timedelta(0)) -> TimedMarketEvent`
- Function L161: `normalize_binance_depth_snapshot(payload: Mapping[str, object], *, instrument: str, collector_receive_ts: datetime, source: str = 'binance_spot') -> TimedMarketEvent`
- Function L190: `normalize_binance_book_ticker(payload: Mapping[str, object], *, collector_receive_ts: datetime, source: str = 'binance_spot') -> TimedMarketEvent`
- Function L481: `_required_text(payload: Mapping[str, object], key: str) -> str`
- Function L488: `_positive_float(payload: Mapping[str, object], key: str) -> float`
- Function L495: `_nonnegative_float(payload: Mapping[str, object], key: str) -> float`
- Function L502: `_float(value: object, key: str) -> float`
- Function L512: `_bool(value: object, key: str) -> bool`
- Function L518: `_nonnegative_int(payload: Mapping[str, object], key: str) -> int`
- Function L528: `_levels(value: object) -> tuple[tuple[float, float], ...]`
- Function L543: `_first_present(payload: Mapping[str, object], *keys: str) -> object | None`
- Function L550: `_timestamp_from_millis(value: object | None, field_name: str, *, default: datetime | None = None) -> datetime`
- Function L568: `_normalize_receive_ts(value: datetime | None) -> datetime | None`
- Function L576: `_available_time(source_ts: datetime, receive_ts: datetime | None, delay: timedelta) -> datetime`
- Function L583: `_datetime_to_ns(value: datetime) -> int`
- Class L16: `DepthUpdateStatus(StrEnum)`
- Class L24: `BinanceTradeRecord`
- Class L30: `DepthApplyResult`
- Class L220: `BinanceDiffDepthBook`
  - Method L223: `__init__(self, *, instrument: str, source: str = 'binance_spot', availability_delay: timedelta = timedelta(0)) -> None`
  - Method L243: `last_update_id(self) -> int | None`
  - Method L246: `apply_snapshot(self, payload: Mapping[str, object], *, collector_receive_ts: datetime) -> DepthApplyResult`
  - Method L269: `apply_delta(self, payload: Mapping[str, object], *, collector_receive_ts: datetime | None) -> DepthApplyResult`
  - Method L308: `_gap_result(self, reason: str) -> DepthApplyResult`
  - Method L318: `_apply_levels(levels: dict[float, float], changes: Sequence[tuple[float, float]]) -> None`
  - Method L325: `_book_top(self, *, source_ts: datetime, receive_ts: datetime | None) -> BtcBookTop | None`
- Class L346: `_BufferedDepthUpdate`
- Class L351: `BinanceDepthSynchronizer`
  - Method L354: `__init__(self, *, instrument: str, source: str = 'binance_spot', availability_delay: timedelta = timedelta(0), max_buffered_events: int = 10000) -> None`
  - Method L373: `synchronized(self) -> bool`
  - Method L377: `needs_snapshot(self) -> bool`
  - Method L381: `buffered_event_count(self) -> int`
  - Method L384: `invalidate(self) -> None`
  - Method L391: `observe_delta(self, payload: Mapping[str, object], *, collector_receive_ts: datetime | None) -> DepthApplyResult`
  - Method L409: `apply_snapshot(self, payload: Mapping[str, object], *, collector_receive_ts: datetime) -> DepthApplyResult`
  - Method L452: `_buffer_update(self, payload: Mapping[str, object], *, collector_receive_ts: datetime | None) -> DepthApplyResult`
  - Method L473: `_new_book(self) -> BinanceDiffDepthBook`

### `btc_short_horizon/data/catalog_io.py`
- Imports: `__future__, btc_short_horizon, collections, datetime, json, pathlib, uuid`
- Function L24: `write_market_catalog(*, path: Path, catalog: MarketCatalog, collected_at: datetime | None = None) -> None`
- Function L40: `market_catalog_payload(*, catalog: MarketCatalog, collected_at: datetime) -> dict[str, object]`
- Function L53: `read_market_catalog(path: Path) -> MarketCatalog`
- Function L81: `_family_record(family: BtcMarketFamily) -> dict[str, object]`
- Function L90: `_market_record(window: MarketWindow) -> dict[str, object]`
- Function L108: `_family_from_record(value: object) -> BtcMarketFamily`
- Function L122: `_market_from_record(value: object, *, families_by_name: Mapping[str, BtcMarketFamily]) -> MarketWindow`
- Function L162: `_require_value(value: Mapping[str, object], name: str) -> object`
- Function L169: `_require_text(value: Mapping[str, object], name: str) -> str`
- Function L176: `_require_int(value: Mapping[str, object], name: str) -> int`
- Function L183: `_normalize_timestamp(value: datetime, name: str) -> datetime`
- Function L189: `_parse_timestamp(value: object, name: str) -> datetime`

### `btc_short_horizon/data/collector.py`
- Imports: `__future__, asyncio, btc_short_horizon, collections, dataclasses, datetime, inspect, json, pathlib, pyarrow, typing, websockets`
- Function L184: `async _maybe_await(value: object) -> None`
- Function L189: `_message_payloads(raw: str | bytes) -> tuple[Mapping[str, object], ...]`
- Class L25: `RawCollectorEvent`
  - Method L31: `__post_init__(self) -> None`
  - Method L37: `as_row(self) -> dict[str, object]`
- Class L59: `PartitionedRawEventWriter`
  - Method L62: `__init__(self, root: Path) -> None`
  - Method L65: `write(self, events: Sequence[RawCollectorEvent], *, quality_counts: Mapping[RawPartitionKey, tuple[int, int]] | None = None) -> tuple[DataPartitionManifest, ...]`
- Class L114: `WebSocketSubscription`
  - Method L121: `__post_init__(self) -> None`
- Class L128: `JsonWebSocketCollector`
  - Method L131: `__init__(self, subscription: WebSocketSubscription) -> None`
  - Method L134: `async collect_forever(self, *, stop_event: asyncio.Event, on_payload: Callable[[Mapping[str, object], datetime], object | Awaitable[object]], on_error: Callable[[Exception], object | Awaitable[object]] | None = None) -> None`
  - Method L161: `async _collect_connection(self, *, socket, stop_event: asyncio.Event, on_payload: Callable[[Mapping[str, object], datetime], object | Awaitable[object]]) -> None`

### `btc_short_horizon/data/contracts.py`
- Imports: `__future__, dataclasses, datetime, enum, re`
- Function L33: `_normalize_utc(value: datetime, *, field_name: str) -> datetime`
- Function L41: `_normalize_window_time(value: datetime, *, field_name: str) -> datetime`
- Function L48: `_require_text(value: str, *, field_name: str) -> str`
- Function L57: `_epoch_seconds(value: datetime) -> int`
- Class L14: `MarketValidationError(ValueError)`
- Class L18: `MarketCollectionMode(str, Enum)`
- Class L25: `MarketOutcome(str, Enum)`
- Class L62: `BtcMarketFamily`
  - Method L70: `__post_init__(self) -> None`
  - Method L95: `window_minutes(self) -> int`
  - Method L100: `window_seconds_as_timedelta(self) -> timedelta`
  - Method L105: `is_collection_only(self) -> bool`
  - Method L109: `slug_for(self, t0: datetime) -> str`
  - Method L119: `parse_slug(self, slug: str) -> datetime`
- Class L158: `MarketWindow`
  - Method L173: `__post_init__(self) -> None`
  - Method L220: `_normalize_resolution(value: MarketOutcome | str | None) -> MarketOutcome | None`
  - Method L229: `_normalize_label_available_ts(value: datetime | None, *, resolution: MarketOutcome | None, t1: datetime) -> datetime | None`
  - Method L248: `winning_token_id(self) -> str | None`
  - Method L257: `is_resolved(self) -> bool`
- Class L263: `TimedMarketEvent`
  - Method L275: `__post_init__(self) -> None`
  - Method L310: `is_event_time_only(self) -> bool`

### `btc_short_horizon/data/forward.py`
- Imports: `__future__, asyncio, btc_short_horizon, collections, dataclasses, datetime, httpx, math, pathlib, threading`
- Function L1144: `_rejected(reason: str) -> CollectorIngressResult`
- Function L1148: `_with_depth_status(outcome: CollectorIngressResult, status: DepthUpdateStatus, reason: str | None) -> CollectorIngressResult`
- Function L1158: `_okx_source_for_instrument(instrument: str) -> str | None`
- Function L1166: `_okx_item_payload(payload: Mapping[str, object], item: Mapping[str, object]) -> Mapping[str, object]`
- Function L1177: `_positive_number(value: object, name: str) -> float`
- Function L1187: `_binance_instruments(streams: Sequence[str]) -> tuple[str, ...]`
- Function L1199: `_binance_depth_instruments(streams: Sequence[str]) -> tuple[str, ...]`
- Function L1211: `_binance_stream_ids(streams: Sequence[str]) -> tuple[str, ...]`
- Function L1230: `_binance_feed_keys(source: str, streams: Sequence[str]) -> set[_QualityStreamKey]`
- Function L1242: `_okx_stream_id(channel: object) -> str | None`
- Function L1253: `_partition_key(timing: TimedMarketEvent) -> _RawPartitionKey`
- Function L1265: `_add_quality_counts(counts_by_partition: dict[_RawPartitionKey, tuple[int, int]], key: _RawPartitionKey, *, duplicate_count: int = 0, gap_count: int = 0) -> None`
- Class L69: `CollectorIngressResult`
  - Method L75: `merged(self, other: CollectorIngressResult) -> CollectorIngressResult`
- Class L85: `RequiredFeedHealth`
- Class L91: `BtcForwardCollector`
  - Method L94: `__init__(self, *, raw_data_root: Path, polymarket_token_ids: Sequence[str], flush_size: int = 10000, flush_interval_seconds: float = 60.0, ingest_version: str = 'btc-short-horizon-v1', epoch_id_offset: int = 0, binance_depth_snapshot_url: str = _BINANCE_DEPTH_SNAPSHOT_URL, binance_futures_depth_snapshot_url: str = _BINANCE_FUTURES_DEPTH_SNAPSHOT_URL, binance_depth_snapshot_limit: int = 1000, binance_depth_snapshot_timeout_seconds: float = 10.0, okx_instruments_url: str = _OKX_PUBLIC_INSTRUMENTS_URL, okx_swap_contract_value: float | None = None) -> None`
  - Method L170: `pending_event_count(self) -> int`
  - Method L175: `flush_required(self) -> bool`
  - Method L179: `quality_stats(self) -> dict[_QualityStreamKey, DataQualityStats]`
  - Method L182: `configure_required_feeds(self, *, binance_streams: Sequence[str] = DEFAULT_BINANCE_STREAMS, binance_futures_market_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_MARKET_STREAMS, binance_futures_public_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS, okx_subscriptions: Sequence[Mapping[str, str]] = DEFAULT_OKX_SUBSCRIPTIONS) -> None`
  - Method L206: `configure_required_polymarket_tokens(self, token_ids: Sequence[str]) -> None`
  - Method L218: `feed_health(self, *, now: datetime, stale_after_seconds: float) -> RequiredFeedHealth`
  - Method L268: `handle_polymarket(self, payload: Mapping[str, object], *, collector_receive_ts: datetime) -> CollectorIngressResult`
  - Method L291: `handle_chainlink_rtds(self, payload: Mapping[str, object], *, collector_receive_ts: datetime) -> CollectorIngressResult`
  - Method L305: `invalidate_polymarket_token(self, token_id: str) -> None`
  - Method L312: `handle_binance(self, payload: Mapping[str, object], *, collector_receive_ts: datetime, source: str = 'binance_spot') -> CollectorIngressResult`
  - Method L355: `handle_binance_depth_snapshot(self, *, instrument: str, payload: Mapping[str, object], collector_receive_ts: datetime, source: str = 'binance_spot') -> CollectorIngressResult`
  - Method L393: `async refresh_binance_depth_snapshot(self, *, instrument: str, client: httpx.AsyncClient | None = None, source: str = 'binance_spot') -> CollectorIngressResult`
  - Method L427: `binance_depth_needs_snapshot(self, instrument: str, *, source: str = 'binance_spot') -> bool`
  - Method L432: `invalidate_binance_depth(self, instrument: str, *, source: str = 'binance_spot') -> None`
  - Method L435: `_handle_binance_trade(self, message: Mapping[str, object], *, payload: Mapping[str, object], collector_receive_ts: datetime, source: str) -> CollectorIngressResult`
  - Method L458: `_handle_binance_kline(self, message: Mapping[str, object], *, payload: Mapping[str, object], collector_receive_ts: datetime, source: str) -> CollectorIngressResult`
  - Method L484: `_handle_binance_depth_update(self, message: Mapping[str, object], *, payload: Mapping[str, object], collector_receive_ts: datetime, source: str) -> CollectorIngressResult`
  - Method L521: `_handle_binance_book_ticker(self, message: Mapping[str, object], *, payload: Mapping[str, object], collector_receive_ts: datetime, source: str) -> CollectorIngressResult`
  - Method L544: `handle_okx(self, payload: Mapping[str, object], *, collector_receive_ts: datetime) -> CollectorIngressResult`
  - Method L587: `async refresh_okx_swap_contract_value(self, *, client: httpx.AsyncClient | None = None) -> float`
  - Method L636: `_handle_okx_trades(self, *, data: Sequence[object], payload: Mapping[str, object], collector_receive_ts: datetime, source: str) -> CollectorIngressResult`
  - Method L672: `_handle_okx_books(self, *, data: Sequence[object], payload: Mapping[str, object], action: str, instrument: str, collector_receive_ts: datetime, source: str) -> CollectorIngressResult`
  - Method L725: `mark_gap(self, *, source: str, instrument: str, stream_id: str, reason: str) -> None`
  - Method L737: `flush(self) -> tuple[DataPartitionManifest, ...]`
  - Method L767: `async collect_forever(self, *, stop_event: asyncio.Event, binance_streams: Sequence[str] = DEFAULT_BINANCE_STREAMS, binance_futures_market_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_MARKET_STREAMS, binance_futures_public_streams: Sequence[str] = DEFAULT_BINANCE_FUTURES_PUBLIC_STREAMS, okx_subscriptions: Sequence[Mapping[str, str]] = DEFAULT_OKX_SUBSCRIPTIONS) -> None`
  - Method L1019: `async _flush_if_required(self) -> None`
  - Method L1023: `async _flush_periodically(self, *, stop_event: asyncio.Event) -> None`
  - Method L1031: `_ingest(self, *, timing: TimedMarketEvent, event_type: str, payload: Mapping[str, object], stream_id: str) -> CollectorIngressResult`
  - Method L1079: `_record_partition_quality(self, timing: TimedMarketEvent, *, duplicate_count: int = 0, gap_count: int = 0) -> None`
  - Method L1094: `_record_unattributed_gap(self, *, source: str, instrument: str, stream_id: str) -> None`
  - Method L1099: `_validator(self, *, source: str, instrument: str, stream_id: str) -> EventQualityValidator`
  - Method L1107: `_binance_depth_snapshot_url(self, source: str) -> str`
  - Method L1114: `_depth_synchronizer(self, *, source: str, instrument: str) -> BinanceDepthSynchronizer`
  - Method L1123: `_okx_book_synchronizer(self, *, source: str, instrument: str) -> OkxBookSynchronizer`
  - Method L1131: `_invalidate_state_after_quality_gap(self, timing: TimedMarketEvent, *, stream_id: str) -> None`

### `btc_short_horizon/data/gamma.py`
- Imports: `__future__, btc_short_horizon, collections, datetime, hashlib, httpx, json`
- Function L103: `gamma_market_to_window(payload: Mapping[str, object], *, family: BtcMarketFamily, rule_epoch: str) -> MarketWindow`
- Function L148: `_closed_states(closed: bool | None) -> tuple[bool, ...]`
- Function L154: `_requested_slugs(family: BtcMarketFamily, slugs: Sequence[str] | None) -> tuple[str, ...]`
- Function L163: `_keyset_page(value: object) -> tuple[Sequence[object], str | None]`
- Function L177: `gamma_rule_hash(payload: Mapping[str, object]) -> str`
- Function L197: `_resolution_from_payload(payload: Mapping[str, object], *, outcomes: Sequence[str]) -> MarketOutcome`
- Function L215: `_label_available_time(payload: Mapping[str, object], *, minimum: datetime) -> datetime`
- Function L225: `_string_list(value: object, name: str) -> tuple[str, ...]`
- Function L237: `_timestamp(value: object) -> datetime`
- Function L252: `_text(value: object, name: str) -> str`
- Function L258: `_float(value: object, name: str) -> float`
- Function L265: `_bool(value: object, name: str) -> bool`
- Function L273: `_first_present(payload: Mapping[str, object], *keys: str) -> object | None`
- Class L22: `GammaMarketClient`
  - Method L25: `__init__(self, *, base_url: str = _GAMMA_MARKETS_KEYSET_URL, timeout_seconds: float = 20.0) -> None`
  - Method L35: `async discover_catalog(self, *, family: BtcMarketFamily, rule_epoch: str, closed: bool | None = None, page_size: int = 100, slugs: Sequence[str] | None = None, client: httpx.AsyncClient | None = None) -> MarketCatalog`

### `btc_short_horizon/data/market_catalog.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime`
- Class L19: `ParsedMarketSlug`
- Class L26: `MarketCatalog`
  - Method L29: `__init__(self, *, families: Iterable[BtcMarketFamily] = (BTC_15M_MARKET_FAMILY, BTC_5M_MARKET_FAMILY), windows: Iterable[MarketWindow] = ()) -> None`
  - Method L63: `families(self) -> tuple[BtcMarketFamily, ...]`
  - Method L67: `parse_slug(self, slug: str) -> ParsedMarketSlug`
  - Method L81: `register(self, window: MarketWindow) -> None`
  - Method L100: `get(self, slug: str) -> MarketWindow | None`
  - Method L104: `require(self, slug: str) -> MarketWindow`
  - Method L111: `windows(self, *, family_name: str | None = None) -> tuple[MarketWindow, ...]`
  - Method L124: `__len__(self) -> int`

### `btc_short_horizon/data/okx.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime, math`
- Function L21: `normalize_okx_trade(payload: Mapping[str, object], *, collector_receive_ts: datetime | None, source: str, quantity_multiplier: float = 1.0, availability_delay: timedelta = timedelta(0)) -> OkxTradeRecord`
- Function L67: `normalize_okx_book_update(payload: Mapping[str, object], *, action: str, instrument: str, collector_receive_ts: datetime | None, source: str) -> TimedMarketEvent`
- Function L201: `_book_top(*, bids: Mapping[float, float], asks: Mapping[float, float], source_ts: datetime, receive_ts: datetime | None, source: str, instrument: str) -> BtcBookTop | None`
- Function L229: `_apply_levels(levels: dict[float, float], changes: Sequence[tuple[float, float]]) -> None`
- Function L237: `_levels(value: object, name: str) -> tuple[tuple[float, float], ...]`
- Function L250: `_require_source(source: str) -> None`
- Function L255: `_text(payload: Mapping[str, object], field: str) -> str`
- Function L262: `_finite(value: object, name: str, *, positive: bool) -> float`
- Function L273: `_positive(payload: Mapping[str, object], field: str) -> float`
- Function L277: `_nonnegative(payload: Mapping[str, object], field: str) -> int`
- Function L284: `_signed_integer(payload: Mapping[str, object], field: str) -> int`
- Function L293: `_timestamp_from_millis(value: object, field: str) -> datetime`
- Function L298: `_receive_time(value: datetime | None) -> datetime | None`
- Function L306: `_available_time(source_ts: datetime, receive_ts: datetime | None, availability_delay: timedelta) -> datetime`
- Function L312: `_datetime_to_ns(value: datetime) -> int`
- Class L16: `OkxTradeRecord`
- Class L100: `OkxBookSynchronizer`
  - Method L103: `__init__(self, *, instrument: str, source: str) -> None`
  - Method L115: `synchronized(self) -> bool`
  - Method L118: `reset(self) -> None`
  - Method L124: `apply(self, *, action: str, payload: Mapping[str, object], collector_receive_ts: datetime | None) -> DepthApplyResult`
  - Method L186: `_applied(self, *, source_ts: datetime, receive_ts: datetime | None) -> DepthApplyResult`

### `btc_short_horizon/data/polymarket.py`
- Imports: `__future__, btc_short_horizon, dataclasses, datetime, enum, hashlib, json, math, typing`
- Function L297: `_levels(value: object) -> tuple[tuple[float, float], ...]`
- Function L313: `_metadata_references_token(payload: Mapping[str, object], token_id: str) -> bool`
- Function L325: `_payload_hash(payload: Mapping[str, object]) -> str`
- Function L330: `_text(value: object, name: str) -> str`
- Function L336: `_number(value: object, name: str) -> float`
- Function L346: `_probability(value: object, name: str) -> float`
- Function L353: `_positive_float(value: object, name: str) -> float`
- Function L360: `_nonnegative_float(value: object, name: str) -> float`
- Function L367: `_timestamp_millis(value: object, name: str) -> datetime`
- Function L377: `_as_utc(value: datetime, name: str) -> datetime`
- Function L383: `_datetime_to_ns(value: datetime) -> int`
- Class L17: `PolymarketL2Status(StrEnum)`
- Class L25: `PolymarketL2Result`
- Class L36: `PolymarketL2Normalizer`
  - Method L39: `__init__(self, *, token_id: str, source: str = 'polymarket_clob', source_timestamp_regression_tolerance: timedelta = timedelta(seconds=1)) -> None`
  - Method L61: `tick_size(self) -> float | None`
  - Method L64: `reset(self) -> None`
  - Method L74: `apply(self, payload: Mapping[str, object], *, collector_receive_ts: datetime) -> PolymarketL2Result`
  - Method L100: `_apply_book(self, payload: Mapping[str, object], *, receive_ts: datetime) -> PolymarketL2Result`
  - Method L127: `_apply_price_change(self, payload: Mapping[str, object], *, receive_ts: datetime) -> PolymarketL2Result`
  - Method L178: `_apply_trade(self, payload: Mapping[str, object], *, receive_ts: datetime) -> PolymarketL2Result`
  - Method L204: `_apply_tick_size_change(self, payload: Mapping[str, object], *, receive_ts: datetime) -> PolymarketL2Result`
  - Method L223: `_apply_market_metadata(self, payload: Mapping[str, object], *, receive_ts: datetime, event_type: str) -> PolymarketL2Result`
  - Method L236: `_ignored(self, payload: Mapping[str, object], receive_ts: datetime, reason: str) -> PolymarketL2Result`
  - Method L246: `_timing(self, payload: Mapping[str, object], *, receive_ts: datetime) -> TimedMarketEvent`
  - Method L267: `_accept_timing(self, timing: TimedMarketEvent) -> None`
  - Method L273: `_has_material_source_timestamp_regression(self, source_ts: datetime) -> bool`
  - Method L278: `_book_top(self, timing: TimedMarketEvent) -> BtcBookTop | None`

### `btc_short_horizon/data/quality.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime`
- Class L13: `DataQualityDecision`
- Class L21: `DataQualityStats`
- Class L31: `EventQualityValidator`
  - Method L34: `__init__(self, *, max_seen_identifiers: int = 100000, max_transport_delay: timedelta = timedelta(seconds=1)) -> None`
  - Method L58: `epoch_id(self) -> int`
  - Method L62: `stats(self) -> DataQualityStats`
  - Method L73: `mark_gap(self, *, reason: str) -> DataQualityDecision`
  - Method L85: `observe(self, event: TimedMarketEvent) -> DataQualityDecision`
  - Method L116: `_remember(self, identifier: tuple[str, str, str, str]) -> None`

### `btc_short_horizon/data/rtds.py`
- Imports: `__future__, btc_short_horizon, dataclasses, datetime, hashlib, json, math, typing`
- Function L22: `normalize_chainlink_btc_usd(message: Mapping[str, object], *, collector_receive_ts: datetime) -> RtdsReferenceRecord`
- Function L64: `_text(value: object, name: str) -> str`
- Function L70: `_timestamp(value: object, name: str) -> datetime`
- Function L80: `_positive_float(value: object, name: str) -> float`
- Function L90: `_as_utc(value: datetime) -> datetime`
- Function L96: `_datetime_to_ns(value: datetime) -> int`
- Class L17: `RtdsReferenceRecord`

### `btc_short_horizon/data/storage.py`
- Imports: `__future__, dataclasses, datetime, hashlib, json, pathlib, pyarrow, re, typing, urllib, uuid`
- Function L128: `_require_safe_path_part(value: str) -> None`
- Function L133: `instrument_directory_name(value: str) -> str`
- Function L148: `_ensure_instrument_identity(directory: Path, instrument: str) -> None`
- Function L168: `_instrument_digest(instrument: str) -> str`
- Function L172: `_column_min(table: pa.Table, name: str) -> int | None`
- Function L179: `_column_max(table: pa.Table, name: str) -> int | None`
- Function L186: `_atomic_write_json(path: Path, payload: object) -> None`
- Function L196: `_content_id(content_hash: str) -> str`
- Function L200: `_sha256_file(path: Path) -> str`
- Class L24: `DataPartitionManifest`
- Class L42: `ImmutableParquetStore`
  - Method L45: `__init__(self, root: Path) -> None`
  - Method L48: `write(self, *, table: pa.Table, source: str, instrument: str, partition_date: str, partition_hour: str, schema_version: str, ingest_version: str, duplicate_count: int = 0, gap_count: int = 0, attributes: Mapping[str, str] | None = None) -> DataPartitionManifest`
  - Method L122: `read_manifest(self, relative_path: str) -> DataPartitionManifest`

### `btc_short_horizon/data/subscriptions.py`
- Imports: `__future__, btc_short_horizon, json, urllib`
- Function L18: `polymarket_market_subscription(token_ids: tuple[str, ...]) -> WebSocketSubscription`
- Function L33: `polymarket_rtds_chainlink_btc_subscription() -> WebSocketSubscription`
- Function L51: `binance_combined_stream_subscription(streams: tuple[str, ...]) -> WebSocketSubscription`
- Function L58: `binance_futures_market_stream_subscription(streams: tuple[str, ...]) -> WebSocketSubscription`
- Function L67: `binance_futures_public_stream_subscription(streams: tuple[str, ...]) -> WebSocketSubscription`
- Function L76: `_binance_combined_stream_subscription(*, endpoint: str, streams: tuple[str, ...]) -> WebSocketSubscription`
- Function L91: `okx_public_subscription(arguments: tuple[dict[str, str], ...]) -> WebSocketSubscription`

### `btc_short_horizon/features/__init__.py`
- Imports: `events, opening, schema`

### `btc_short_horizon/features/events.py`
- Imports: `__future__, dataclasses, math, typing`
- Function L10: `_require_timestamp(name: str, value: int) -> None`
- Function L15: `_require_positive(name: str, value: float) -> None`
- Class L21: `BtcTrade`
  - Method L30: `__post_init__(self) -> None`
- Class L42: `BtcBookTop`
  - Method L52: `__post_init__(self) -> None`
- Class L66: `BtcReferencePrice`
  - Method L73: `__post_init__(self) -> None`

### `btc_short_horizon/features/opening.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, math, statistics, typing`
- Function L20: `opening_feature_schema() -> FeatureSchema`
- Function L358: `build_opening_feature_observations(*, state: OpeningFeatureState, events: Iterable[BtcTrade | BtcBookTop | BtcReferencePrice], decision_times_ns: Iterable[int], market_window_start_ns: int) -> tuple[OpeningFeatureObservation, ...]`
- Function L385: `_trades_since(trades: deque[BtcTrade], cutoff_ns: int) -> tuple[BtcTrade, ...]`
- Function L389: `_trade_window_values(window: tuple[BtcTrade, ...], latest_price: float | None) -> tuple[float, float, float]`
- Function L413: `_book_values(book: BtcBookTop | None) -> tuple[float, float, float]`
- Function L426: `_dispersion_bps(prices: list[float], consensus: float | None) -> float`
- Class L55: `OpeningFeatureObservation`
  - Method L66: `eligible(self) -> bool`
- Class L71: `_VenueState`
- Class L78: `OpeningFeatureState`
  - Method L81: `__init__(self, *, up_token_id: str, down_token_id: str, schema: FeatureSchema | None = None, required_venue_sources: tuple[str, ...] = _VENUE_SOURCES, venue_stale_seconds: float = 1.0, chainlink_stale_seconds: float = 10.0) -> None`
  - Method L116: `mark_gap(self, *, source: str, instrument: str) -> None`
  - Method L130: `update(self, event: BtcTrade | BtcBookTop | BtcReferencePrice) -> None`
  - Method L148: `snapshot(self, *, decision_ts_ns: int, market_window_start_ns: int) -> OpeningFeatureObservation`
  - Method L233: `_state(self, *, source: str, instrument: str) -> _VenueState`
  - Method L244: `_venue_values(self, *, source: str, decision_ts_ns: int) -> tuple[dict[str, float], set[str], float | None, float, list[float]]`
  - Method L296: `_reference_prices(self, *, decision_ts_ns: int, market_window_start_ns: int) -> tuple[BtcReferencePrice | None, BtcReferencePrice | None]`
  - Method L307: `_market_probability(self, *, decision_ts_ns: int) -> tuple[float, set[str]]`
  - Method L324: `_boundary_probability(*, consensus: float | None, opening_reference: float | None, rv_300: float, remaining_seconds: float) -> tuple[float, float, float, float]`
  - Method L347: `_prune_trades(state: _VenueState, now_ns: int) -> None`
  - Method L352: `_prune_references(self, now_ns: int) -> None`

### `btc_short_horizon/features/schema.py`
- Imports: `__future__, dataclasses, hashlib, json, math, typing`
- Class L11: `FeatureSchema`
  - Method L15: `__post_init__(self) -> None`
  - Method L26: `hash(self) -> str`
  - Method L34: `vector_from(self, values: Mapping[str, float]) -> tuple[float, ...]`
  - Method L46: `mapping_from(self, vector: Sequence[float]) -> dict[str, float]`

### `btc_short_horizon/live/__init__.py`
- Imports: `btc_short_horizon`

### `btc_short_horizon/live/dashboard.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime, http, json, pathlib, urllib`
- Function L38: `build_dashboard_payload(config: DashboardConfig, *, now: datetime | None = None) -> dict[str, object]`
- Function L95: `_shadow_projection(status: RuntimeStatus | None, errors: list[str]) -> dict[str, object] | None`
- Function L113: `create_dashboard_server(config: DashboardConfig, *, host: str, port: int, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> ThreadingHTTPServer`
- Function L179: `serve_dashboard(config: DashboardConfig, *, host: str, port: int) -> None`
- Function L186: `_status_payload(status: RuntimeStatus, now: datetime, max_age_seconds: float) -> dict[str, object]`
- Class L26: `DashboardConfig`
  - Method L31: `__post_init__(self) -> None`

### `btc_short_horizon/live/dashboard_page.py`
- Imports: `__future__`
- Function L6: `dashboard_html() -> str`

### `btc_short_horizon/live/dashboard_state.py`
- Imports: `__future__, collections, dataclasses, datetime, enum, json, math, os, pathlib, uuid`
- Function L485: `_mapping(value: object, name: str) -> Mapping[str, object]`
- Function L491: `_sequence(value: object, name: str) -> Sequence[object]`
- Function L497: `_require_identifier(value: object, name: str) -> None`
- Function L503: `_require_text(value: object, name: str) -> None`
- Function L507: `_text(value: object, name: str) -> str`
- Function L513: `_optional_text(value: object, name: str) -> str | None`
- Function L517: `_finite(value: float, name: str) -> None`
- Function L522: `_float(value: object, name: str) -> float`
- Function L528: `_optional_float(value: object, name: str) -> float | None`
- Function L532: `_nonnegative(value: float, name: str) -> None`
- Function L538: `_optional_finite(value: float | None, name: str) -> None`
- Function L543: `_optional_nonnegative(value: float | None, name: str) -> None`
- Function L548: `_integer(value: object, name: str) -> int`
- Function L554: `_optional_integer(value: object, name: str) -> int | None`
- Function L558: `_as_utc(value: datetime, name: str) -> datetime`
- Function L564: `_timestamp(value: object, name: str) -> datetime`
- Function L574: `_optional_timestamp(value: object, name: str) -> datetime | None`
- Function L578: `_optional_iso(value: datetime | None) -> str | None`
- Class L19: `HealthState(StrEnum)`
- Class L26: `StrategyStage(StrEnum)`
- Class L34: `GateState(StrEnum)`
- Class L43: `HealthIndicator`
  - Method L51: `__post_init__(self) -> None`
  - Method L60: `to_json(self) -> dict[str, object]`
  - Method L71: `from_json(cls, raw: object) -> HealthIndicator`
- Class L84: `EquityPoint`
  - Method L88: `__post_init__(self) -> None`
  - Method L94: `to_json(self) -> dict[str, object]`
  - Method L98: `from_json(cls, raw: object) -> EquityPoint`
- Class L107: `TradePerformance`
  - Method L122: `__post_init__(self) -> None`
  - Method L144: `to_json(self) -> dict[str, object]`
  - Method L162: `from_json(cls, raw: object) -> TradePerformance`
- Class L182: `PerformanceSnapshot`
  - Method L198: `__post_init__(self) -> None`
  - Method L228: `to_json(self) -> dict[str, object]`
  - Method L247: `from_json(cls, raw: object) -> PerformanceSnapshot`
- Class L274: `StrategyCycle`
  - Method L290: `__post_init__(self) -> None`
  - Method L322: `to_json(self) -> dict[str, object]`
  - Method L341: `from_json(cls, raw: object) -> StrategyCycle`
- Class L370: `DashboardAlert`
  - Method L375: `__post_init__(self) -> None`
  - Method L383: `to_json(self) -> dict[str, object]`
  - Method L391: `from_json(cls, raw: object) -> DashboardAlert`
- Class L401: `BotDashboardSnapshot`
  - Method L409: `__post_init__(self) -> None`
  - Method L418: `to_json(self) -> dict[str, object]`
  - Method L430: `from_json(cls, raw: object) -> BotDashboardSnapshot`
- Class L453: `DashboardSnapshotStore`
  - Method L456: `__init__(self, runtime_root: Path) -> None`
  - Method L459: `write(self, snapshot: BotDashboardSnapshot) -> Path`
  - Method L475: `read(self) -> BotDashboardSnapshot | None`

### `btc_short_horizon/live/forward_runtime.py`
- Imports: `__future__, asyncio, btc_short_horizon, collections, dataclasses, datetime, math, pathlib`
- Function L36: `async run_forward_collector_runtime(*, config: ForwardCollectorRuntimeConfig, collect: CollectorEntrypoint, status_details: StatusDetailsProvider = lambda: {}, status_health: StatusHealthProvider = lambda: (True, 'ok'), stop_event: asyncio.Event | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None`
- Function L165: `async _wait_for_activity(*, collector_task: asyncio.Task[None], stop_event: asyncio.Event, timeout_seconds: float) -> None`
- Function L183: `async _await_shutdown(collector_task: asyncio.Task[None]) -> None`
- Function L192: `_write_status(*, store: RuntimeStatusStore, config: ForwardCollectorRuntimeConfig, state: str, healthy: bool, started_at: datetime, now: Callable[[], datetime], status_details: StatusDetailsProvider, status_health: StatusHealthProvider, stop_reason: str | None, error: str | None = None, apply_status_health: bool = False) -> None`
- Function L229: `_utc(value: datetime) -> datetime`
- Class L21: `ForwardCollectorRuntimeConfig`
  - Method L27: `__post_init__(self) -> None`

### `btc_short_horizon/live/gateway.py`
- Imports: `__future__, dataclasses, httpx, time, typing`
- Function L225: `_is_ambiguous_request_error(exc: Exception) -> bool`
- Class L13: `LiveOrderRequest`
  - Method L21: `__post_init__(self) -> None`
- Class L33: `GatewayOrderResponse`
- Class L40: `GatewaySubmissionUnknownError(RuntimeError)`
- Class L44: `GatewayCancellationUnknownError(RuntimeError)`
- Class L49: `PreparedPostOnlyOrder`
- Class L55: `LiveOrderGateway(Protocol)`
  - Method L56: `submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse`
  - Method L58: `cancel_order(self, venue_order_id: str) -> None`
  - Method L60: `cancel_market(self, condition_id: str, token_id: str | None = None) -> None`
  - Method L62: `cancel_all(self) -> None`
  - Method L64: `send_heartbeat(self, heartbeat_id: str = '') -> str`
- Class L67: `PaperOrderGateway`
  - Method L70: `__init__(self) -> None`
  - Method L75: `open_orders(self) -> dict[str, LiveOrderRequest]`
  - Method L78: `submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse`
  - Method L84: `cancel_order(self, venue_order_id: str) -> None`
  - Method L87: `cancel_market(self, condition_id: str, token_id: str | None = None) -> None`
  - Method L95: `cancel_all(self) -> None`
  - Method L98: `send_heartbeat(self, heartbeat_id: str = '') -> str`
- Class L102: `PyClobV2Gateway`
  - Method L105: `__init__(self, client) -> None`
  - Method L108: `submit_post_only_buy(self, request: LiveOrderRequest) -> GatewayOrderResponse`
  - Method L112: `prepare_post_only_buy(self, request: LiveOrderRequest) -> PreparedPostOnlyOrder`
  - Method L139: `submit_prepared_post_only_buy(self, prepared: PreparedPostOnlyOrder) -> GatewayOrderResponse`
  - Method L171: `cancel_order(self, venue_order_id: str) -> None`
  - Method L187: `cancel_market(self, condition_id: str, token_id: str | None = None) -> None`
  - Method L205: `cancel_all(self) -> None`
  - Method L215: `send_heartbeat(self, heartbeat_id: str = '') -> str`

### `btc_short_horizon/live/risk.py`
- Imports: `__future__, dataclasses, math`
- Function L65: `evaluate_order_risk(*, config: TradingSafetyConfig, account: AccountSnapshot, order_notional: float) -> RiskDecision`
- Class L8: `TradingSafetyConfig`
  - Method L18: `__post_init__(self) -> None`
- Class L36: `AccountSnapshot`
  - Method L45: `__post_init__(self) -> None`
- Class L60: `RiskDecision`

### `btc_short_horizon/live/runtime.py`
- Imports: `__future__, collections, dataclasses, datetime, hashlib, json, os, pathlib, shutil, subprocess, uuid`
- Function L177: `build_runtime_identity(*, config_path: Path, ingest_version: str, model_sha256: str | None = None) -> dict[str, object]`
- Function L196: `filesystem_usage(path: Path) -> dict[str, object]`
- Function L213: `check_runtime_health(status: RuntimeStatus | None, *, now: datetime, max_age_seconds: float) -> RuntimeHealth`
- Function L238: `_read_status(path: Path) -> RuntimeStatus`
- Function L242: `_git_revision(config_path: Path) -> str`
- Function L257: `_read_stop_request(path: Path) -> StopRequest`
- Function L261: `_read_json(path: Path) -> object`
- Function L268: `_atomic_write_json(path: Path, value: Mapping[str, object]) -> None`
- Function L282: `_as_utc(value: datetime, name: str) -> datetime`
- Function L288: `_parse_timestamp(value: object, name: str) -> datetime`
- Function L298: `_require_identifier(value: object, name: str) -> None`
- Function L304: `_text(value: object, name: str) -> str`
- Function L310: `_bool(value: object, name: str) -> bool`
- Function L316: `_json_mapping(value: object, name: str) -> dict[str, object]`
- Function L322: `_json_value(value: object, name: str) -> object`
- Class L22: `RuntimeStatus`
  - Method L33: `__post_init__(self) -> None`
  - Method L48: `to_json(self) -> dict[str, object]`
  - Method L61: `from_json(cls, raw: object) -> RuntimeStatus`
- Class L78: `StopRequest`
  - Method L82: `__post_init__(self) -> None`
  - Method L87: `to_json(self) -> dict[str, object]`
  - Method L95: `from_json(cls, raw: object) -> StopRequest`
- Class L107: `RuntimeHealth`
- Class L113: `RuntimeStatusStore`
  - Method L116: `__init__(self, root: Path) -> None`
  - Method L119: `write(self, status: RuntimeStatus) -> Path`
  - Method L124: `read(self, service: str) -> RuntimeStatus | None`
  - Method L131: `all(self) -> tuple[RuntimeStatus, ...]`
  - Method L143: `status_path(self, service: str) -> Path`
- Class L148: `RuntimeControl`
  - Method L151: `__init__(self, root: Path) -> None`
  - Method L155: `stop_path(self) -> Path`
  - Method L158: `request_stop(self, *, reason: str, requested_at: datetime) -> StopRequest`
  - Method L163: `stop_request(self) -> StopRequest | None`
  - Method L169: `clear_stop(self) -> bool`

### `btc_short_horizon/live/service.py`
- Imports: `__future__, btc_short_horizon, dataclasses, enum, math, typing, uuid`
- Function L402: `_mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]`
- Function L408: `_nonnegative_float(value: object, name: str) -> float`
- Function L418: `_positive_float(value: object, name: str) -> float`
- Function L425: `_live_order_from_json(value: Mapping[str, object]) -> LiveOrder`
- Function L440: `_live_trade_from_json(value: Mapping[str, object]) -> LiveTrade`
- Class L23: `LiveMode(StrEnum)`
- Class L30: `LiveExecutionConfig`
  - Method L36: `__post_init__(self) -> None`
- Class L46: `SubmitResult`
- Class L54: `CanaryProgress`
  - Method L59: `ready_for_extended_canary(self) -> bool`
  - Method L63: `ready_for_scale_review(self) -> bool`
- Class L67: `LiveExecutionService`
  - Method L70: `__init__(self, *, config: LiveExecutionConfig, wal: JsonlWriteAheadLog, gateway: LiveOrderGateway | None = None) -> None`
  - Method L95: `canary_progress(self) -> CanaryProgress`
  - Method L99: `halted(self) -> bool`
  - Method L102: `submit(self, *, request: LiveOrderRequest, account: AccountSnapshot, ts_ns: int) -> SubmitResult`
  - Method L174: `request_cancel(self, *, client_order_id: str, ts_ns: int) -> None`
  - Method L195: `cancel_all(self, *, ts_ns: int, reason: str) -> None`
  - Method L205: `emergency_stop(self, *, ts_ns: int, reason: str) -> bool`
  - Method L222: `record_heartbeat(self, *, ts_ns: int) -> None`
  - Method L229: `send_venue_heartbeat(self, *, ts_ns: int) -> str`
  - Method L241: `enforce_heartbeat_timeout(self, *, now_ts_ns: int) -> bool`
  - Method L255: `reconcile_user_event(self, event: Mapping[str, object], *, ts_ns: int) -> None`
  - Method L262: `restore_from_wal(self) -> int`
  - Method L298: `resolve_submission_unknown(self, *, client_order_id: str, ts_ns: int, venue_order_id: str | None = None, confirmed_absent: bool = False) -> LiveOrder`
  - Method L327: `_reconcile_order(self, event: Mapping[str, object], *, ts_ns: int) -> None`
  - Method L349: `_reconcile_trade(self, event: Mapping[str, object], *, ts_ns: int) -> None`
  - Method L382: `_order_for_venue_event(self, event: Mapping[str, object]) -> LiveOrder | None`
  - Method L388: `_record_cumulative_match(order: LiveOrder, cumulative: float) -> LiveOrder`
  - Method L393: `_write(self, event_type: str, ts_ns: int, payload: Mapping[str, object]) -> None`
  - Method L396: `_block_order(self, order: LiveOrder, *, ts_ns: int, reason: str) -> None`

### `btc_short_horizon/live/shadow_scheduler.py`
- Imports: `__future__, btc_short_horizon, dataclasses, datetime, math, pathlib`
- Function L27: `scan_shadow_windows(*, catalog_directory: Path, output_root: Path, family: BtcMarketFamily, model_sha256: str, now: datetime, handoff_delay_seconds: float, flush_interval_seconds: float, lookback: timedelta) -> ShadowWindowScan`
- Function L93: `_as_utc(value: datetime) -> datetime`
- Class L14: `ShadowWindow`
- Class L21: `ShadowWindowScan`

### `btc_short_horizon/live/state.py`
- Imports: `__future__, dataclasses, enum, math`
- Function L70: `_require_nonempty(name: str, value: str) -> None`
- Function L75: `_require_nonnegative(name: str, value: float) -> None`
- Class L8: `LiveOrderStatus(StrEnum)`
- Class L20: `LiveTradeStatus(StrEnum)`
- Class L81: `LiveOrder`
  - Method L91: `__post_init__(self) -> None`
  - Method L108: `remaining_size(self) -> float`
  - Method L111: `transition(self, status: LiveOrderStatus, *, venue_order_id: str | None = None) -> LiveOrder`
  - Method L120: `record_match(self, matched_size: float) -> LiveOrder`
- Class L129: `LiveTrade`
  - Method L137: `__post_init__(self) -> None`
  - Method L146: `is_terminal(self) -> bool`
  - Method L149: `transition(self, status: LiveTradeStatus, *, transaction_hash: str | None = None) -> LiveTrade`

### `btc_short_horizon/live/wal.py`
- Imports: `__future__, collections, dataclasses, enum, json, pathlib, typing`
- Function L55: `_json_payload(payload: object) -> object`
- Class L11: `JsonlWriteAheadLog`
  - Method L14: `__init__(self, path: Path) -> None`
  - Method L17: `append(self, *, event_type: str, ts_ns: int, payload: object) -> None`
  - Method L37: `read(self) -> tuple[dict[str, Any], ...]`

### `btc_short_horizon/models/__init__.py`
- Imports: `artifacts, direction, opening_mispricing`

### `btc_short_horizon/models/artifacts.py`
- Imports: `__future__, dataclasses, direction, hashlib, joblib, json, pathlib`
- Function L93: `_sha256_file(path: Path) -> str`
- Class L14: `ModelArtifactMetadata`
  - Method L26: `__post_init__(self) -> None`
- Class L41: `ModelArtifactStore`
  - Method L45: `save(*, directory: Path, model: FittedDirectionModel, metadata: ModelArtifactMetadata) -> ModelArtifactMetadata`
  - Method L71: `load(*, directory: Path, expected_schema_hash: str) -> tuple[FittedDirectionModel, ModelArtifactMetadata]`

### `btc_short_horizon/models/direction.py`
- Imports: `__future__, btc_short_horizon, dataclasses, math, numpy, pandas, sklearn, typing`
- Function L123: `fit_direction_model(*, train_vectors: np.ndarray, train_labels: np.ndarray, calibration_vectors: np.ndarray, calibration_labels: np.ndarray, schema: FeatureSchema, config: DirectionModelConfig | None = None, train_weights: np.ndarray | None = None, calibration_weights: np.ndarray | None = None, early_stopping_vectors: np.ndarray | None = None, early_stopping_labels: np.ndarray | None = None, early_stopping_weights: np.ndarray | None = None) -> FittedDirectionModel`
- Function L195: `_fit_estimator(matrix: np.ndarray, labels: np.ndarray, config: DirectionModelConfig, *, schema: FeatureSchema, sample_weights: np.ndarray | None, early_stopping_matrix: np.ndarray | None, early_stopping_target: np.ndarray | None, early_stopping_weights: np.ndarray | None) -> object`
- Function L262: `_fit_calibrator(*, raw_probabilities: np.ndarray, labels: np.ndarray, sample_weights: np.ndarray | None, method: CalibrationMethod, random_seed: int, min_isotonic_calibration_samples: int, temperature_grid: tuple[float, ...]) -> _Calibrator`
- Function L301: `_fit_temperature_calibrator(*, raw_probabilities: np.ndarray, labels: np.ndarray, sample_weights: np.ndarray | None, grid: tuple[float, ...]) -> _Calibrator`
- Function L330: `_proper_scores(probabilities: np.ndarray, labels: np.ndarray, sample_weights: np.ndarray | None) -> tuple[float, float]`
- Function L344: `_predict_positive_probability(estimator: object, matrix: np.ndarray) -> np.ndarray`
- Function L354: `_estimator_matrix(*, matrix: np.ndarray, schema: FeatureSchema, config: DirectionModelConfig) -> np.ndarray | pd.DataFrame`
- Function L362: `_feature_frame(*, matrix: np.ndarray, schema: FeatureSchema) -> pd.DataFrame`
- Function L366: `_probability_logits(probabilities: np.ndarray) -> np.ndarray`
- Function L373: `_validate_matrix(vectors: np.ndarray, *, schema: FeatureSchema) -> np.ndarray`
- Function L388: `_validate_binary_labels(labels: np.ndarray, *, expected_rows: int, name: str) -> np.ndarray`
- Function L398: `_validate_weights(weights: np.ndarray | None, *, expected_rows: int, name: str) -> np.ndarray | None`
- Function L411: `_validate_early_stopping_data(*, vectors: np.ndarray | None, labels: np.ndarray | None, weights: np.ndarray | None, schema: FeatureSchema) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]`
- Class L22: `_Calibrator(Protocol)`
  - Method L23: `transform(self, probabilities: np.ndarray) -> np.ndarray`
- Class L27: `DirectionModelConfig`
  - Method L41: `__post_init__(self) -> None`
- Class L69: `FittedDirectionModel`
  - Method L75: `predict_up_probability(self, vectors: np.ndarray) -> np.ndarray`
  - Method L87: `config_dict(self) -> dict[str, object]`
- Class L92: `_IdentityCalibrator`
  - Method L93: `transform(self, probabilities: np.ndarray) -> np.ndarray`
- Class L98: `_SigmoidCalibrator`
  - Method L101: `transform(self, probabilities: np.ndarray) -> np.ndarray`
- Class L107: `_IsotonicCalibrator`
  - Method L110: `transform(self, probabilities: np.ndarray) -> np.ndarray`
- Class L115: `_TemperatureCalibrator`
  - Method L118: `transform(self, probabilities: np.ndarray) -> np.ndarray`

### `btc_short_horizon/models/opening_mispricing.py`
- Imports: `__future__, btc_short_horizon, dataclasses, math, numpy, typing`
- Function L19: `_probability(name: str, value: float) -> None`
- Function L112: `fit_opening_mispricing_model(*, train_vectors: np.ndarray, train_labels: np.ndarray, calibration_vectors: np.ndarray, calibration_labels: np.ndarray, schema: FeatureSchema, config: DirectionModelConfig | None = None, train_weights: np.ndarray | None = None, calibration_weights: np.ndarray | None = None, early_stopping_vectors: np.ndarray | None = None, early_stopping_labels: np.ndarray | None = None, early_stopping_weights: np.ndarray | None = None) -> FittedOpeningMispricingModel`
- Class L25: `OpeningMispricingPrediction`
  - Method L43: `__post_init__(self) -> None`
  - Method L61: `elapsed_seconds(self) -> float`
- Class L66: `FittedOpeningMispricingModel`
  - Method L72: `schema(self) -> FeatureSchema`
  - Method L75: `predict(self, *, market_slug: str, model_version: str, market_window_start_ts_ns: int, trigger_ts_ns: int, feature_values: Mapping[str, float], p_boundary_up: float, p_market_mid_up: float, data_age_seconds: float, has_data_gap: bool = False, structure_valid: bool = True, tick_unchanged: bool = True, fee_unchanged: bool = True, latency_healthy: bool = True) -> OpeningMispricingPrediction`

### `btc_short_horizon/reporting/__init__.py`
- Imports: `btc_short_horizon`

### `btc_short_horizon/reporting/artifacts.py`
- Imports: `__future__, dataclasses, html, json, pathlib, pyarrow, typing, uuid`
- Function L69: `_atomic_json(path: Path, value: object) -> None`
- Function L73: `_atomic_text(path: Path, content: str) -> None`
- Function L82: `_atomic_parquet(path: Path, records: Sequence[Mapping[str, object]]) -> None`
- Function L93: `_jsonable(value: object) -> object`
- Function L107: `_html_report(*, title: str, metrics: Mapping[str, object], data_quality: Mapping[str, object]) -> str`
- Class L17: `RunManifest`
- Class L27: `BtcRunArtifacts`
- Class L39: `RunArtifactWriter`
  - Method L52: `write(cls, *, directory: Path, artifacts: BtcRunArtifacts, title: str) -> None`

### `btc_short_horizon/reporting/metrics.py`
- Imports: `__future__, dataclasses, math, numpy, sklearn, typing`
- Function L15: `_require_probability(name: str, value: float) -> None`
- Function L133: `evaluate_probabilities(evaluations: Sequence[ProbabilityEvaluation], *, bin_edges: Sequence[float] | None = None) -> ProbabilityMetrics`
- Function L162: `evaluate_maker_fills(evaluations: Sequence[MakerFillEvaluation]) -> MakerFillSummary`
- Function L182: `_normalize_bin_edges(edges: Sequence[float] | None) -> tuple[float, ...]`
- Function L193: `_calibration_bins(probabilities: np.ndarray, outcomes: np.ndarray, edges: tuple[float, ...]) -> tuple[CalibrationBin, ...]`
- Function L221: `_calibration_regression(probabilities: np.ndarray, outcomes: np.ndarray) -> tuple[float | None, float | None]`
- Class L21: `ProbabilityEvaluation`
  - Method L26: `__post_init__(self) -> None`
- Class L35: `CalibrationBin`
- Class L45: `ProbabilityMetrics`
- Class L56: `FairProbabilityAttribution`
  - Method L65: `total(self) -> float`
- Class L70: `MakerFillEvaluation`
  - Method L82: `__post_init__(self) -> None`
  - Method L100: `attribution(self) -> FairProbabilityAttribution`
  - Method L109: `expected_edge_per_share(self) -> float`
  - Method L113: `net_expected_value(self) -> float`
  - Method L117: `realized_pnl(self) -> float`
- Class L122: `MakerFillSummary`

### `btc_short_horizon/research/__init__.py`
- Imports: `btc_short_horizon`

### `btc_short_horizon/research/binance_history.py`
- Imports: `__future__, dataclasses, datetime, hashlib, httpx, math, numpy, pandas, pathlib, typing, zipfile`
- Function L70: `binance_spot_kline_url(*, day: str, symbol: str = 'BTCUSDT', interval: str = '1m') -> str`
- Function L79: `load_binance_kline_archives(paths: Sequence[Path], *, interval: str = '1m') -> BinanceKlineHistory`
- Function L124: `async fetch_binance_spot_kline_history(*, start_time: datetime, end_time: datetime, symbol: str = 'BTCUSDT', interval: str = '1s', maximum_bars: int = _MAX_BOOTSTRAP_BARS, timeout_seconds: float = 10.0, client: httpx.AsyncClient | None = None) -> BinanceKlineHistory`
- Function L224: `_parse_rest_klines(*, rows: Sequence[Sequence[object]], start_ms: int, end_ms: int, interval_ms: int) -> list[tuple[int, float, float, float, float]]`
- Function L251: `_integer_field(value: object, *, name: str) -> int`
- Function L263: `_finite_float(value: object, *, name: str, positive: bool = False) -> float`
- Function L274: `_epoch_to_ns(values: np.ndarray) -> np.ndarray`
- Class L26: `BinanceKlineHistory`
  - Method L36: `__post_init__(self) -> None`
  - Method L57: `source_hash(self) -> str`

### `btc_short_horizon/research/gates.py`
- Imports: `__future__, dataclasses, math`
- Function L109: `evaluate_direction_gate(evidence: DirectionGateEvidence) -> GateDecision`
- Function L135: `evaluate_opening_mispricing_gate(evidence: OpeningMispricingGateEvidence) -> GateDecision`
- Function L143: `evaluate_maker_gate(evidence: MakerGateEvidence) -> GateDecision`
- Function L174: `_decision(failures: list[str]) -> GateDecision`
- Function L178: `_require_finite(value: float, name: str) -> None`
- Function L183: `_require_finite_nonnegative(value: float, name: str) -> None`
- Class L25: `GateDecision`
- Class L31: `CalibrationBinEvidence`
  - Method L35: `__post_init__(self) -> None`
- Class L42: `DirectionGateEvidence`
  - Method L51: `__post_init__(self) -> None`
- Class L67: `OpeningMispricingGateEvidence`
  - Method L70: `__post_init__(self) -> None`
- Class L75: `MakerGateEvidence`
  - Method L90: `__post_init__(self) -> None`

### `btc_short_horizon/research/opening_dataset.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime, numpy`
- Function L34: `build_opening_direction_dataset(*, markets: Sequence[MarketWindow], observations_by_market: Mapping[str, Sequence[OpeningFeatureObservation]], snapshot_seconds: int = 5, entry_start_seconds: int = 3, entry_end_seconds: int = 180) -> OpeningDirectionDatasetBuild`
- Function L134: `_index_market_observations(*, market: MarketWindow, observations: Sequence[OpeningFeatureObservation], expected_schema_hash: str) -> dict[int, OpeningFeatureObservation]`
- Function L158: `_snapshot_offsets(*, snapshot_seconds: int, entry_start_seconds: int, entry_end_seconds: int) -> tuple[int, ...]`
- Function L175: `_datetime_to_ns(value: datetime) -> int`
- Function L179: `_datetime_from_ns(value: int) -> datetime`
- Class L21: `OpeningDirectionDatasetBuild`

### `btc_short_horizon/research/opening_evidence.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime, json, math, nautilus_trader, pathlib, pyarrow`
- Function L160: `load_forward_raw_events(*, raw_data_root: Path, source: str, instrument: str, start_time: datetime, end_time: datetime, ingest_version: str | None = None) -> ForwardRawEventLoad`
- Function L234: `load_forward_polymarket_book_events(*, raw_data_root: Path, token_id: str, start_time: datetime, end_time: datetime, ingest_version: str | None = None) -> ForwardBookEventLoad`
- Function L339: `build_opening_market_observations(*, market: MarketWindow, up_events: Sequence[TokenBookStateEvent], down_events: Sequence[TokenBookStateEvent], decision_ts_ns: Sequence[int], initial_data_gap: bool = False) -> tuple[OpeningMarketObservation, ...]`
- Function L419: `pmxt_order_book_state_events(*, token_id: str, records: Sequence[OrderBookDeltas], gap_hours: Sequence[object] = ()) -> PmxtBookEventLoad`
- Function L477: `_raw_part_paths(*, raw_data_root: Path, source: str, instrument: str, start_time: datetime, end_time: datetime) -> Iterator[Path]`
- Function L498: `_raw_rows(path: Path) -> Iterator[tuple[int, dict[str, object]]]`
- Function L514: `_validate_raw_identity(row: Mapping[str, object], *, expected_source: str, expected_instrument: str, path: Path, row_index: int) -> None`
- Function L530: `_payload_mapping(row: Mapping[str, object], *, path: Path, row_index: int) -> Mapping[str, object]`
- Function L543: `_validate_token_events(events: Sequence[TokenBookStateEvent], *, expected_token_id: str) -> None`
- Function L550: `_state_event_sort_key(event: TokenBookStateEvent) -> tuple[int, int, int, str, int]`
- Function L560: `_raw_payload_sort_key(row: ForwardRawEvent) -> tuple[int, int, int, str, str, int]`
- Function L571: `_window_ns(*, start_time: datetime, end_time: datetime) -> tuple[int, int]`
- Function L579: `_hours_between(*, start_time: datetime, end_time: datetime) -> Iterator[datetime]`
- Function L587: `_required_text(row: Mapping[str, object], name: str, path: Path, row_index: int) -> str`
- Function L594: `_required_int(row: Mapping[str, object], name: str, path: Path, row_index: int) -> int`
- Function L601: `_optional_int(row: Mapping[str, object], name: str, path: Path, row_index: int) -> int | None`
- Function L616: `_as_utc(value: datetime, name: str) -> datetime`
- Function L622: `_datetime_to_ns(value: datetime) -> int`
- Function L626: `_datetime_from_ns(value: int) -> datetime`
- Function L630: `_as_float(value: object | None) -> float | None`
- Class L39: `RawPayloadError(ValueError)`
- Class L44: `TokenBookStateEvent`
  - Method L56: `__post_init__(self) -> None`
- Class L78: `ForwardBookEventLoad`
- Class L90: `PmxtBookEventLoad`
- Class L100: `OpeningMarketObservation`
  - Method L115: `__post_init__(self) -> None`
- Class L131: `ForwardRawEvent`
- Class L149: `ForwardRawEventLoad`

### `btc_short_horizon/research/opening_features.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime, pathlib`
- Function L97: `build_forward_opening_feature_observations(*, raw_data_root: Path, market: MarketWindow, start_time: datetime, end_time: datetime, decision_ts_ns: Sequence[int], ingest_version: str, required_venue_sources: tuple[str, ...] = _SUPPORTED_VENUE_SOURCES) -> ForwardOpeningFeatureBuild`
- Function L201: `_load_clob_feature_events(*, raw_data_root: Path, market: MarketWindow, start_time: datetime, end_time: datetime, ingest_version: str) -> tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary]`
- Function L253: `_load_chainlink_feature_events(*, raw_data_root: Path, start_time: datetime, end_time: datetime, ingest_version: str) -> tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary]`
- Function L281: `_load_binance_feature_events(*, raw_data_root: Path, source: str, start_time: datetime, end_time: datetime, ingest_version: str) -> tuple[tuple[ForwardFeatureStateEvent, ...], ForwardFeatureSourceSummary]`
- Function L310: `_chainlink_state_events(raw_events: Sequence[ForwardRawEvent]) -> tuple[tuple[ForwardFeatureStateEvent, ...], int]`
- Function L353: `_binance_state_events(raw_events: Sequence[ForwardRawEvent], *, source: str) -> tuple[tuple[ForwardFeatureStateEvent, ...], int]`
- Function L430: `_binance_feature_stream_id(event_type: str) -> str | None`
- Function L438: `_epoch_gap(*, raw: ForwardRawEvent, current_epoch: int | None) -> bool`
- Function L447: `_gap_event(raw: ForwardRawEvent, *, state_source: str, state_instrument: str) -> ForwardFeatureStateEvent`
- Function L463: `_value_event(raw: ForwardRawEvent, *, state_source: str, state_instrument: str, value: _FeatureValue) -> ForwardFeatureStateEvent`
- Function L483: `_binance_message(raw: ForwardRawEvent) -> dict[str, object]`
- Function L490: `_collector_receive_time(raw: ForwardRawEvent) -> datetime`
- Function L494: `_raw_normalization_error(raw: ForwardRawEvent, exc: Exception) -> RawPayloadError`
- Function L498: `_feature_event_sort_key(event: ForwardFeatureStateEvent) -> tuple[int, int, int, int, str, str]`
- Function L509: `_datetime_to_ns(value: datetime) -> int`
- Function L515: `_datetime_from_ns(value: int) -> datetime`
- Class L42: `ForwardFeatureStateEvent`
  - Method L57: `__post_init__(self) -> None`
- Class L75: `ForwardFeatureSourceSummary`
- Class L84: `ForwardOpeningFeatureBuild`
  - Method L89: `source_event_count(self, raw_source: str) -> int`

### `btc_short_horizon/research/opening_model_gate.py`
- Imports: `__future__, btc_short_horizon, collections, dataclasses, datetime, math, numpy, sklearn`
- Function L63: `select_direction_candidate(candidates: Sequence[CandidateEvaluation]) -> CandidateSelectionDecision`
- Function L106: `paired_daily_block_bootstrap(*, candidate_predictions: Iterable[object], baseline_predictions: Iterable[object], weights: np.ndarray, baseline_name: str, iterations: int = 2000, seed: int = 17) -> CandidatePairedEvidence`
- Function L179: `weighted_calibration_error(*, labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray) -> float`
- Function L205: `calibration_slope(*, labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray) -> float`
- Function L220: `target_confidence_bands(*, predictions: Iterable[object], weights: np.ndarray) -> tuple[dict[str, object], ...]`
- Function L263: `build_direction_gate_artifact(*, sealed_holdout_markets: int, log_loss_improvement: float, log_loss_ci_lower: float, log_loss_ci_upper: float, brier_improvement: float, brier_ci_lower: float, brier_ci_upper: float, calibration_slope: float, target_bands: Sequence[Mapping[str, object]], protocol_eligible: bool, protocol_failures: Sequence[str] = ()) -> dict[str, object]`
- Function L321: `candidate_evaluation_dict(candidate: CandidateEvaluation) -> dict[str, object]`
- Function L327: `_binary_log_loss(labels: np.ndarray, probabilities: np.ndarray) -> np.ndarray`
- Function L331: `_validated_arrays(labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]`
- Function L348: `_market_id(sample_id: str) -> str`
- Class L22: `CandidatePairedEvidence`
- Class L35: `CandidateEvaluation`
  - Method L45: `__post_init__(self) -> None`
- Class L56: `CandidateSelectionDecision`

### `btc_short_horizon/research/opening_proxy.py`
- Imports: `__future__, btc_short_horizon, dataclasses, datetime, enum, math, numpy, typing`
- Function L35: `opening_regime_for_elapsed_seconds(elapsed_seconds: float) -> OpeningRegime`
- Function L47: `opening_proxy_protocol(*, entry_start_seconds: int, entry_end_seconds: int, snapshot_seconds: int) -> dict[str, object]`
- Function L86: `validate_opening_proxy_protocol(metadata_config: Mapping[str, object], *, expected: Mapping[str, object]) -> None`
- Function L108: `opening_proxy_feature_schema(interval_seconds: int) -> FeatureSchema`
- Function L135: `opening_proxy_decision_offsets_ms(*, cadence_ms: int, entry_start_seconds: int, entry_end_seconds: int) -> tuple[int, ...]`
- Function L156: `build_opening_proxy_dataset(*, markets: Sequence[MarketWindow], klines: BinanceKlineHistory, snapshot_seconds: int = 5, entry_start_seconds: int = 3, entry_end_seconds: int = 180, availability_delay: timedelta = timedelta(seconds=1)) -> OpeningProxyDatasetBuild`
- Function L285: `opening_proxy_feature_values_at(*, klines: BinanceKlineHistory, market_start: datetime, decision_time: datetime, availability_delay: timedelta = timedelta(seconds=1)) -> dict[str, float]`
- Function L345: `_feature_vector(*, klines: BinanceKlineHistory, available_ts_ns: np.ndarray, start: int, end: int, reference_index: int, decision_ns: int, elapsed_seconds: float, schema: FeatureSchema, windows: Sequence[int]) -> tuple[float, ...]`
- Function L417: `_has_full_history(*, available_ts_ns: np.ndarray, start: int, decision_ns: int, max_window_ns: int, interval_ns: int) -> bool`
- Function L428: `_snapshot_offsets(*, snapshot_seconds: int, entry_start_seconds: int, entry_end_seconds: int) -> tuple[int, ...]`
- Function L441: `_windows_for_interval(interval_seconds: int) -> tuple[int, ...]`
- Function L449: `_validate_timing(*, snapshot_seconds: int, entry_start_seconds: int, entry_end_seconds: int, interval_seconds: int, availability_delay: timedelta) -> None`
- Function L469: `_datetime_to_ns(value: datetime) -> int`
- Class L24: `OpeningRegime(StrEnum)`
- Class L97: `OpeningProxyDatasetBuild`

### `btc_short_horizon/research/opening_runtime.py`
- Imports: `__future__, btc_short_horizon, datetime`
- Function L19: `build_opening_proxy_prediction(*, model: FittedDirectionModel, metadata: ModelArtifactMetadata, market: MarketWindow, klines: BinanceKlineHistory, market_observation: OpeningMarketObservation, availability_delay: timedelta = timedelta(seconds=1), fee_unchanged: bool = True, latency_healthy: bool = True) -> OpeningMispricingPrediction`

### `btc_short_horizon/research/pipeline.py`
- Imports: `__future__, btc_short_horizon, dataclasses, math, numpy`
- Function L115: `run_walk_forward_model(*, dataset: DirectionDataset, split_config: WalkForwardConfig | None = None, model_config: DirectionModelConfig | None = None, early_stopping_fraction: float = 0.2) -> WalkForwardModelRun`
- Function L190: `run_sealed_holdout_model(*, dataset: DirectionDataset, split_config: WalkForwardConfig | None = None, model_config: DirectionModelConfig | None = None, early_stopping_fraction: float = 0.2) -> SealedHoldoutModelRun`
- Function L287: `_split_training_indices(*, indices: tuple[int, ...], labels: np.ndarray, samples: tuple[ResearchSample, ...], fraction: float, require_early_stopping: bool) -> tuple[tuple[int, ...], tuple[int, ...]]`
- Class L22: `DirectionDataset`
  - Method L30: `__post_init__(self) -> None`
  - Method L53: `labels(self) -> np.ndarray`
- Class L58: `OofPrediction`
  - Method L68: `__post_init__(self) -> None`
- Class L80: `WalkForwardModelRun`
  - Method L87: `__post_init__(self) -> None`
- Class L96: `HoldoutPrediction`
- Class L105: `SealedHoldoutModelRun`

### `btc_short_horizon/research/polymarket_price_history.py`
- Imports: `__future__, asyncio, collections, dataclasses, httpx, math`
- Function L30: `async fetch_polymarket_price_history(*, token_ids: Sequence[str], start_ts: int, end_ts: int, fidelity_minutes: int = 1, max_concurrency: int = 4, client: httpx.AsyncClient | None = None) -> tuple[TokenPricePoint, ...]`
- Function L82: `async _fetch_batch(*, client: httpx.AsyncClient, token_ids: tuple[str, ...], start_ts: int, end_ts: int, fidelity_minutes: int) -> tuple[TokenPricePoint, ...]`
- Function L110: `_parse_batch_response(payload: object, *, requested_tokens: set[str]) -> tuple[TokenPricePoint, ...]`
- Class L18: `TokenPricePoint`
  - Method L23: `__post_init__(self) -> None`

### `btc_short_horizon/research/walk_forward.py`
- Imports: `__future__, dataclasses, datetime, typing`
- Function L10: `_as_utc(value: datetime, name: str) -> datetime`
- Function L16: `_require_positive_duration(name: str, value: timedelta) -> None`
- Function L127: `build_walk_forward_plan(samples: Sequence[ResearchSample], *, config: WalkForwardConfig | None = None) -> WalkForwardPlan`
- Function L225: `_partition_indices(samples: Sequence[ResearchSample], candidates: Sequence[int], groups: Mapping[str, tuple[int, ...]], *, start: datetime, end: datetime, label_deadline: datetime | None) -> tuple[int, ...]`
- Function L246: `select_complete_group_indices(samples: Sequence[ResearchSample], candidates: Sequence[int], *, start: datetime, end: datetime, label_deadline: datetime | None) -> tuple[int, ...]`
- Function L266: `_grouped_indices(samples: Sequence[ResearchSample], candidates: Sequence[int]) -> dict[str, tuple[int, ...]]`
- Class L22: `ResearchSample`
  - Method L31: `__post_init__(self) -> None`
- Class L49: `WalkForwardConfig`
  - Method L59: `__post_init__(self) -> None`
- Class L72: `WalkForwardFold`
  - Method L86: `__post_init__(self) -> None`
- Class L106: `WalkForwardPlan`
  - Method L113: `__post_init__(self) -> None`

### `btc_short_horizon/strategy/__init__.py`
- Imports: `btc_short_horizon`

### `btc_short_horizon/strategy/lifecycle.py`
- Imports: `__future__, btc_short_horizon, dataclasses, enum, math`
- Class L12: `StrategyPhase(StrEnum)`
- Class L26: `MarketExecution`
  - Method L36: `__post_init__(self) -> None`
  - Method L49: `remaining_size(self) -> float`
  - Method L52: `begin_monitoring(self) -> MarketExecution`
  - Method L55: `submit_plan(self, plan: OrderPlan) -> MarketExecution`
  - Method L64: `request_cancel(self) -> MarketExecution`
  - Method L67: `reject_cancel(self) -> MarketExecution`
  - Method L70: `record_fill(self, size: float) -> MarketExecution`
  - Method L88: `acknowledge_cancel(self) -> MarketExecution`
  - Method L94: `reject_order(self) -> MarketExecution`
  - Method L99: `complete_working_orders(self) -> MarketExecution`
  - Method L106: `record_resolution(self) -> MarketExecution`
  - Method L116: `redeem(self) -> MarketExecution`
  - Method L123: `_transition(self, source: StrategyPhase, target: StrategyPhase) -> MarketExecution`

### `btc_short_horizon/strategy/maker.py`
- Imports: `__future__, btc_short_horizon, dataclasses, math`
- Function L88: `plan_opening_mispricing_orders(*, market_slug: str, p_up: float, p_market_mid_up: float, books: OutcomeBooks, decision_ts_ns: int, elapsed_seconds: float, config: MakerStrategyConfig) -> PlanDecision`
- Function L145: `evaluate_cancellation(*, plan: OrderPlan, now_ts_ns: int, selected_probability: float, data_age_seconds: float, has_data_gap: bool, structure_valid: bool, tick_unchanged: bool, fee_unchanged: bool, latency_healthy: bool, config: MakerStrategyConfig) -> CancellationAssessment`
- Function L189: `_build_layers(*, p_fair: float, book: SideBook, config: MakerStrategyConfig) -> tuple[MakerOrderLayer, ...]`
- Function L208: `_snap_down(value: float, tick_size: float) -> float`
- Class L21: `MakerStrategyConfig`
  - Method L37: `__post_init__(self) -> None`
- Class L73: `PlanDecision`
  - Method L78: `accepted(self) -> bool`
- Class L83: `CancellationAssessment`

### `btc_short_horizon/strategy/types.py`
- Imports: `__future__, dataclasses, enum, math`
- Function L29: `_require_probability(name: str, value: float) -> None`
- Function L34: `_require_nonnegative(name: str, value: float) -> None`
- Function L39: `_require_positive(name: str, value: float) -> None`
- Class L10: `TokenSide(StrEnum)`
- Class L15: `LayerStructure(StrEnum)`
  - Method L21: `allocations(self) -> tuple[float, ...]`
- Class L45: `SideBook`
  - Method L53: `__post_init__(self) -> None`
  - Method L63: `midpoint(self) -> float`
- Class L68: `OutcomeBooks`
  - Method L74: `__post_init__(self) -> None`
  - Method L78: `for_side(self, side: TokenSide) -> SideBook`
  - Method L82: `implied_up_midpoint(self) -> float`
- Class L87: `MakerOrderLayer`
  - Method L91: `__post_init__(self) -> None`
  - Method L96: `notional(self) -> float`
- Class L101: `OrderPlan`
  - Method L116: `__post_init__(self) -> None`
  - Method L132: `total_size(self) -> float`
  - Method L136: `total_notional(self) -> float`
  - Method L140: `model_edge(self) -> float`
  - Method L143: `net_edge(self, price: float) -> float`

### `live/btc_eth_sol_snapshot_model_sandbox.py`
- Imports: `__future__, asyncio, live, os, pathlib, sys, typing`
- Function L30: `_configure_env_defaults() -> None`
- Function L39: `async _main(argv: Sequence[str] | None = None, *, force_run: bool = False) -> None`
- Function L44: `run() -> None`

### `live/btc_snapshot_model_sandbox.py`
- Imports: `__future__, argparse, asyncio, decimal, dotenv, json, nautilus_trader, os, pathlib, prediction_market_extensions, sys, typing`
- Function L56: `_model_path() -> str`
- Function L60: `_trade_size() -> Decimal`
- Function L64: `_env_float(name: str, default: float) -> float`
- Function L68: `_env_bool(name: str, default: bool) -> bool`
- Function L75: `_diagnostics_path() -> str | None`
- Function L83: `_settlement_path() -> str | None`
- Function L92: `_split_csv(raw: str | None) -> tuple[str, ...]`
- Function L98: `_instrument_id_for_spot_prefix(prefix: str) -> InstrumentId`
- Function L103: `_model_extra_spot_prefixes(model_path: str) -> tuple[str, ...]`
- Function L120: `_extra_spot_instrument_ids(model_path: str) -> tuple[InstrumentId, ...]`
- Function L129: `_strategy_parameters() -> dict[str, object]`
- Function L189: `_build_strategy_config(*, instrument_ids: tuple[InstrumentId, ...], btc_instrument_id: InstrumentId, extra_spot_instrument_ids: tuple[InstrumentId, ...] = ()) -> ImportableStrategyConfig`
- Function L208: `_parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L281: `_resolve_btc_data_source(args: argparse.Namespace) -> str`
- Function L287: `_default_btc_instrument_id(btc_data_source: str) -> InstrumentId`
- Function L291: `_btc_data_source_label(btc_data_source: str) -> str`
- Function L297: `_policy_label(config: dict[str, object]) -> str`
- Function L311: `async _main(argv: Sequence[str] | None = None, *, force_run: bool = False) -> None`
- Function L406: `run() -> None`

### `main.py`
- Imports: `__future__, argparse, ast, asyncio, functools, importlib, inspect, json, os, pathlib, re, string, subprocess, sys, time, typing`
- Function L70: `_configure_mode(mode: str) -> None`
- Function L83: `_parse_args(argv: list[str] | tuple[str, ...]) -> argparse.Namespace`
- Function L94: `_env_flag_enabled(name: str) -> bool`
- Function L101: `_discoverable_backtest_paths(backtests_root: Path) -> list[Path]`
- Function L119: `_warn(message: str) -> None`
- Function L123: `_literal_string(node: ast.AST | None) -> str | None`
- Function L133: `_assignment_targets(node: ast.Assign | ast.AnnAssign) -> list[str]`
- Function L141: `_has_assignment(module_ast: ast.Module, target_name: str) -> bool`
- Function L150: `_call_name(node: ast.AST) -> str | None`
- Function L158: `_literal_runner_kwargs(call: ast.Call) -> dict[str, str]`
- Function L168: `_experiment_constructor_kwargs(module_ast: ast.Module) -> dict[str, str] | None`
- Function L191: `_has_run_entrypoint(module_ast: ast.Module) -> bool`
- Function L198: `_load_runner_metadata(path: Path) -> dict[str, Any] | None`
- Function L241: `_notebook_source_text(cell: dict[str, Any]) -> str`
- Function L248: `_notebook_description(cells: list[dict[str, Any]]) -> str`
- Function L263: `_load_notebook_metadata(path: Path, *, project_root: Path) -> dict[str, Any] | None`
- Function L305: `discover() -> list[dict]`
- Function L318: `_relative_parts(backtest: dict[str, Any]) -> tuple[str, ...]`
- Function L327: `_relative_runner_path(backtest: dict[str, Any]) -> Path`
- Function L331: `_runner_stem(backtest: dict[str, Any]) -> str`
- Function L335: `_menu_label(backtest: dict[str, Any]) -> str`
- Function L339: `_textual_menu_label(backtest: dict[str, Any], shortcut: str | None) -> str`
- Function L346: `_runner_search_text(backtest: dict[str, Any]) -> str`
- Function L358: `_filter_backtests(backtests: list[dict[str, Any]], query: str) -> list[int]`
- Function L369: `_shortcut_candidates(backtest: dict[str, Any]) -> list[str]`
- Function L400: `_assign_shortcuts(backtests: list[dict[str, Any]]) -> dict[str, str | None]`
- Function L418: `_runner_file_preview(path: Path) -> str`
- Function L425: `_runner_preview(backtest: dict[str, Any]) -> str`
- Function L429: `_runner_preview_lexer(backtest: dict[str, Any]) -> str`
- Function L438: `_runner_preview_renderable(backtest: dict[str, Any]) -> Any`
- Function L727: `_load_runner(backtest: dict[str, Any]) -> Any`
- Function L779: `_install_runtime_patches() -> None`
- Function L785: `_supports_textual_menu() -> bool`
- Function L806: `_show_basic_menu(backtests: list[dict[str, Any]]) -> int`
- Function L835: `_show_textual_menu(backtests: list[dict[str, Any]]) -> int`
- Function L845: `_build_menu_tree(backtests: list[dict[str, Any]]) -> dict[str, Any]`
- Function L856: `_render_menu_tree(node: dict[str, Any], *, prefix: str = '') -> list[str]`
- Function L883: `show_menu(backtests: list[dict]) -> int`
- Function L893: `main(argv: list[str] | tuple[str, ...] = ()) -> None`

### `prediction_market_extensions/__init__.py`
- Imports: `__future__`
- Function L8: `install_commission_patch() -> None`

### `prediction_market_extensions/_cache_writes.py`
- Imports: `__future__, collections, contextlib, dataclasses, os, pathlib, threading`
- Function L22: `cache_replace_slot(path: Path) -> Iterator[None]`
- Class L12: `_CacheReplaceLockState`

### `prediction_market_extensions/_native.py`
- Imports: `__future__, collections, importlib, nautilus_trader, os, pathlib, types, typing`
- Function L22: `_env_enabled(name: str) -> bool | None`
- Function L34: `_extension_module() -> ModuleType | None`
- Function L65: `_required_extension_module() -> ModuleType`
- Function L76: `native_available() -> bool`
- Function L83: `_required_native_function(module: ModuleType, name: str) -> Any`
- Function L93: `_validate_semantics(semantics: str) -> WindowSemantics`
- Function L100: `source_days_for_window_ns(start_ns: int, end_ns: int, *, semantics: str = 'inclusive') -> list[str]`
- Function L108: `telonex_source_days_for_window_ns(start_ns: int, end_ns: int) -> list[str]`
- Function L113: `telonex_day_window_ns(date: str, start_ns: int, end_ns: int) -> tuple[int, int] | None`
- Function L121: `telonex_flat_book_snapshot_diff_rows(*, timestamp_ns: Sequence[int], bid_prices: Sequence[Sequence[str]], bid_sizes: Sequence[Sequence[str]], ask_prices: Sequence[Sequence[str]], ask_sizes: Sequence[Sequence[str]], start_ns: int, end_ns: int) -> tuple[int | None, list[int], list[int], list[int], list[float], list[float], list[int], list[int], list[int], list[int]]`
- Function L177: `telonex_nested_book_snapshot_diff_rows(*, timestamp_ns: Sequence[int], bids: Sequence[object], asks: Sequence[object], start_ns: int, end_ns: int) -> tuple[int | None, list[int], list[int], list[int], list[float], list[float], list[int], list[int], list[int], list[int]]`
- Function L230: `telonex_parquet_book_snapshot_diff_rows(*, path: str, row_groups: Sequence[int], start_ns: int, end_ns: int) -> tuple[int | None, list[int], list[int], list[int], list[float], list[float], list[int], list[int], list[int], list[int]]`
- Function L281: `telonex_onchain_fill_trade_rows(*, timestamp_ns: Sequence[int], prices: Sequence[object], sizes: Sequence[object], sides: Sequence[object] | None, ids: Sequence[object] | None, start_ns: int, end_ns: int, token_suffix: str) -> tuple[list[float], list[float], list[int], list[str], list[int], list[int]]`
- Function L327: `decimal_seconds_to_ns(value: object) -> int`
- Function L333: `float_seconds_to_ms_string(value: float) -> str`
- Function L338: `fixed_raw_values(values: Sequence[object], precision: int) -> list[int]`
- Function L351: `pmxt_payload_sort_key(update_type: str, payload_text: str) -> tuple[int, int]`
- Function L357: `pmxt_sort_payload_columns(update_type_columns: Sequence[Sequence[str]], payload_text_columns: Sequence[Sequence[str]]) -> list[tuple[int, int, str, str]]`
- Function L372: `pmxt_payload_delta_rows(*, update_type_columns: Sequence[Sequence[str]], payload_text_columns: Sequence[Sequence[str]], token_id: str, start_ns: int, end_ns: int, has_snapshot: bool, last_payload_key: tuple[int, int] | None) -> tuple[bool, tuple[int, int] | None, dict[str, list[object]]]`
- Function L433: `pmxt_fixed_delta_rows(*, event_type_columns: Sequence[Sequence[str]], timestamp_ns_columns: Sequence[Sequence[int]], timestamp_received_ns_columns: Sequence[Sequence[int]], asset_id_columns: Sequence[Sequence[str]], bids_json_columns: Sequence[Sequence[object]], asks_json_columns: Sequence[Sequence[object]], price_columns: Sequence[Sequence[object]], size_columns: Sequence[Sequence[object]], side_columns: Sequence[Sequence[object]], token_id: str, start_ns: int, end_ns: int, has_snapshot: bool, last_payload_key: tuple[int, int] | None) -> tuple[bool, tuple[int, int] | None, dict[str, list[object]]]`
- Function L508: `polymarket_trade_sort_key(trade: Mapping[str, object]) -> tuple[int, str, str, str, str, str]`
- Function L521: `polymarket_trade_sort_keys(trades: Sequence[Mapping[str, object]]) -> list[tuple[int, str, str, str, str, str]]`
- Function L551: `polymarket_trade_id(transaction_hash: str, asset: str, sequence: int) -> str`
- Function L556: `polymarket_trade_ids(rows: Sequence[tuple[str, str, int]]) -> list[str]`
- Function L561: `polymarket_normalize_trade_side(side: str) -> str`
- Function L566: `polymarket_normalize_trade_sides(sides: Sequence[str]) -> list[str]`
- Function L571: `polymarket_is_tradable_probability_price(price: str) -> bool`
- Function L576: `polymarket_are_tradable_probability_prices(prices: Sequence[str]) -> list[bool]`
- Function L583: `polymarket_trade_event_timestamp_ns(base_timestamp_ns: int, occurrence_in_second: int) -> int`
- Function L591: `polymarket_trade_event_timestamp_ns_batch(rows: Sequence[tuple[int, int]]) -> list[int]`
- Function L598: `polymarket_public_trade_rows(trades: Sequence[Mapping[str, object]], *, token_id: str, sort: bool = False) -> tuple[list[float], list[float], list[int], list[str], list[int], list[int], list[tuple[int, str]], list[tuple[int, float]]]`
- Function L653: `replay_merge_plan(*, book_ts_events: Sequence[int], book_ts_inits: Sequence[int], trade_ts_events: Sequence[int], trade_ts_inits: Sequence[int]) -> list[tuple[int, int]]`
- Function L673: `pmxt_archive_hours_for_window_ns(start_ns: int, end_ns: int) -> list[int]`
- Function L678: `telonex_source_label_kind(source: str) -> str | None`
- Function L684: `telonex_stage_for_source(source: str) -> str`
- Function L689: `telonex_api_url(*, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> str`
- Function L702: `telonex_api_cache_relative_path(*, base_url_key: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> Path`
- Function L721: `telonex_deltas_cache_relative_path(*, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, instrument_key: str, start_ns: int, end_ns: int) -> Path`
- Function L749: `telonex_trade_ticks_cache_relative_path(*, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, instrument_key: str, start_ns: int, end_ns: int) -> Path`
- Function L777: `telonex_local_consolidated_candidate_paths(*, root: Path, channel: str, market_slug: str, token_index: int, outcome: str | None) -> tuple[Path, ...]`
- Function L794: `telonex_local_daily_candidate_paths(*, root: Path, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> tuple[Path, ...]`

### `prediction_market_extensions/_runtime_log.py`
- Imports: `__future__, collections, contextlib, dataclasses, datetime, inspect, json, os, pathlib, re, sys, threading, time, typing, urllib`
- Function L39: `format_utc_timestamp_ns(epoch_ns: int) -> str`
- Function L45: `_normalize_level(level: str) -> str`
- Function L52: `_caller_origin(*, stacklevel: int) -> str`
- Function L66: `_json_safe(value) -> Any`
- Function L80: `_env_flag_enabled(value: str | None, *, default: bool = True) -> bool`
- Function L86: `loader_progress_enabled(environ: Mapping[str, str] | None = None) -> bool`
- Function L91: `loader_progress_logs_enabled(environ: Mapping[str, str] | None = None) -> bool`
- Function L100: `_progress_log_interval_secs(environ: Mapping[str, str] | None = None) -> float`
- Function L204: `_format_status(value: str) -> str`
- Function L208: `_format_source_kind(event: LoaderEvent) -> str | None`
- Function L217: `_format_elapsed_ms(elapsed_ms: float | None) -> str | None`
- Function L223: `_format_int_count(value: int | None, label: str) -> str | None`
- Function L229: `_format_bytes(value: int | None) -> str | None`
- Function L244: `_format_progress_bytes(value: int | None) -> str`
- Function L249: `_progress_source_time_label(source: str) -> str | None`
- Function L263: `_infer_progress_source_kind(vendor: str, source: str, source_kind: str | None) -> str | None`
- Function L279: `_progress_source_kind_label(vendor: str, source_kind: str | None) -> str | None`
- Function L285: `_progress_source_label(vendor: str, source: str, source_kind: str | None) -> str`
- Function L298: `_progress_message(*, vendor: str, mode: str, source: str, source_kind: str | None, downloaded_bytes: int | None, total_bytes: int | None, scanned_batches: int | None, scanned_rows: int | None, matched_rows: int | None, finished: bool) -> str`
- Function L338: `emit_loader_progress_snapshot(*, owner: object, vendor: str, mode: str, source: str, source_kind: str | None = None, downloaded_bytes: int | None = None, total_bytes: int | None = None, scanned_batches: int | None = None, scanned_rows: int | None = None, matched_rows: int | None = None, finished: bool = False, clock: Callable[[], float] = time.monotonic) -> None`
- Function L416: `_event_time_label(event: LoaderEvent) -> str | None`
- Function L424: `_event_request_count(event: LoaderEvent) -> int | None`
- Function L436: `_event_error(event: LoaderEvent) -> str | None`
- Function L443: `_event_reason(event: LoaderEvent) -> str | None`
- Function L450: `_event_count_label(event: LoaderEvent) -> str | None`
- Function L462: `_event_location_label(event: LoaderEvent) -> str | None`
- Function L483: `_event_operation_label(event: LoaderEvent) -> str`
- Function L507: `_should_format_loader_event(event: LoaderEvent) -> bool`
- Function L529: `format_loader_event_message(event: LoaderEvent) -> str`
- Function L564: `_is_standard_stream(stream: TextIO) -> bool`
- Function L568: `_tqdm_write_line(line: str, *, stream: TextIO) -> bool`
- Function L579: `_write_console_line(line: str, *, stream: TextIO) -> None`
- Function L624: `loader_event_sinks_from_env(environ: Mapping[str, str] | None = None, *, include_console: bool = True) -> tuple[LoaderEventSink, ...]`
- Function L640: `format_log_line(message: object, *, level: str, origin: str, timestamp_ns: int) -> str`
- Function L655: `get_loader_event_sinks() -> tuple[LoaderEventSink, ...]`
- Function L660: `set_loader_event_sinks(sinks: Sequence[LoaderEventSink]) -> None`
- Function L666: `register_loader_event_sink(sink: LoaderEventSink) -> None`
- Function L671: `configure_loader_event_sinks_from_env(environ: Mapping[str, str] | None = None) -> None`
- Function L676: `loader_event_sinks(sinks: Sequence[LoaderEventSink]) -> Iterator[None]`
- Function L686: `capture_loader_events() -> Iterator[CaptureEventSink]`
- Function L692: `_emit_event(event: LoaderEvent, *, sinks: Sequence[LoaderEventSink] | None = None) -> None`
- Function L699: `emit_loader_event(message: object, *, level: LogLevel = 'INFO', stage: str = 'runtime', vendor: str = 'repo', status: str = 'complete', origin: str | None = None, clock_ns: Callable[[], int] = time.time_ns, stacklevel: int = 2, sinks: Sequence[LoaderEventSink] | None = None, **fields) -> None`
- Function L763: `log_message(message: object, *, level: LogLevel = 'INFO', origin: str | None = None, stream: TextIO | None = None, clock_ns: Callable[[], int] = time.time_ns, stacklevel: int = 2) -> None`
- Function L783: `log_debug(message: object, *, origin: str | None = None, stacklevel: int = 2) -> None`
- Function L787: `log_info(message: object, *, origin: str | None = None, stacklevel: int = 2) -> None`
- Function L791: `log_warning(message: object, *, origin: str | None = None, stacklevel: int = 2) -> None`
- Function L795: `log_error(message: object, *, origin: str | None = None, stacklevel: int = 2) -> None`
- Function L799: `clone_event(event: LoaderEvent, **changes) -> LoaderEvent`
- Class L112: `LoaderEvent`
  - Method L140: `__post_init__(self) -> None`
  - Method L149: `to_dict(self) -> dict[str, Any]`
- Class L188: `LoaderEventSink(Protocol)`
  - Method L189: `emit(self, event: LoaderEvent) -> None`
- Class L586: `ConsoleEventSink`
  - Method L589: `emit(self, event: LoaderEvent) -> None`
- Class L603: `JsonlEventSink`
  - Method L606: `emit(self, event: LoaderEvent) -> None`
- Class L614: `CaptureEventSink`
  - Method L617: `emit(self, event: LoaderEvent) -> None`

### `prediction_market_extensions/adapters/__init__.py`
- Imports: none

### `prediction_market_extensions/adapters/kalshi/__init__.py`
- Imports: none

### `prediction_market_extensions/adapters/kalshi/config.py`
- Imports: `__future__, nautilus_trader, os`
- Class L22: `KalshiDataClientConfig(LiveDataClientConfig)`
  - Method L58: `resolved_api_key_id(self) -> str | None`
  - Method L62: `resolved_private_key_pem(self) -> str | None`
  - Method L66: `has_credentials(self) -> bool`

### `prediction_market_extensions/adapters/kalshi/data.py`
- Imports: `__future__, asyncio, nautilus_trader, prediction_market_extensions, typing`
- Class L55: `KalshiDataClient(LiveMarketDataClient)`
  - Method L84: `__init__(self, loop: asyncio.AbstractEventLoop, msgbus: MessageBus, cache: Cache, clock: LiveClock, instrument_provider: KalshiInstrumentProvider, config: KalshiDataClientConfig, name: str | None) -> None`
  - Method L105: `async _connect(self) -> None`
  - Method L109: `async _disconnect(self) -> None`
  - Method L112: `_send_all_instruments_to_data_engine(self) -> None`
  - Method L119: `_log_unsupported(self, action: str) -> None`
  - Method L124: `async _subscribe_order_book_deltas(self, command: SubscribeOrderBook) -> None`
  - Method L127: `async _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None`
  - Method L130: `async _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None`
  - Method L133: `async _subscribe_bars(self, command: SubscribeBars) -> None`
  - Method L136: `async _subscribe_instrument_status(self, command: SubscribeInstrumentStatus) -> None`
  - Method L139: `async _subscribe_instrument_close(self, command: SubscribeInstrumentClose) -> None`
  - Method L142: `async _unsubscribe_order_book_deltas(self, command: UnsubscribeOrderBook) -> None`
  - Method L145: `async _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None`
  - Method L148: `async _unsubscribe_trade_ticks(self, command: UnsubscribeTradeTicks) -> None`
  - Method L151: `async _unsubscribe_bars(self, command: UnsubscribeBars) -> None`
  - Method L154: `async _unsubscribe_instrument_status(self, command: UnsubscribeInstrumentStatus) -> None`
  - Method L157: `async _unsubscribe_instrument_close(self, command: UnsubscribeInstrumentClose) -> None`
  - Method L160: `async _request_instrument(self, request: RequestInstrument) -> None`
  - Method L168: `async _request_instruments(self, request: RequestInstruments) -> None`
  - Method L178: `async _request_quote_ticks(self, request: RequestQuoteTicks) -> None`
  - Method L181: `async _request_trade_ticks(self, request: RequestTradeTicks) -> None`
  - Method L184: `async _request_bars(self, request: RequestBars) -> None`

### `prediction_market_extensions/adapters/kalshi/factories.py`
- Imports: `__future__, asyncio, nautilus_trader, prediction_market_extensions, typing`
- Class L31: `KalshiLiveDataClientFactory(LiveDataClientFactory)`
  - Method L37: `create(loop: asyncio.AbstractEventLoop, name: str, config: KalshiDataClientConfig, msgbus: MessageBus, cache: Cache, clock: LiveClock) -> KalshiDataClient`

### `prediction_market_extensions/adapters/kalshi/fee_model.py`
- Imports: `__future__, datetime, decimal, nautilus_trader, prediction_market_extensions`
- Class L31: `KalshiProportionalFeeModelConfig(FeeModelConfig)`
- Class L45: `KalshiProportionalFeeModel(FeeModel)`
  - Method L97: `__init__(self, fee_rate: Decimal = KALSHI_TAKER_FEE_RATE, config: KalshiProportionalFeeModelConfig | None = None) -> None`
  - Method L107: `_fee_rate_for_fill(order, instrument, default_fee_rate: Decimal) -> Decimal`
  - Method L139: `get_commission(self, order, fill_qty, fill_px, instrument) -> Money`

### `prediction_market_extensions/adapters/kalshi/loaders.py`
- Imports: `__future__, hashlib, msgspec, nautilus_trader, pandas, prediction_market_extensions, typing, warnings`
- Class L47: `KalshiDataLoader`
  - Method L82: `_normalize_price(raw: float | str) -> float`
  - Method L107: `_trade_timestamp_ns(trade: dict[str, Any]) -> int`
  - Method L114: `_trade_timestamp_seconds(cls, trade: dict[str, Any]) -> int`
  - Method L118: `_trade_sort_key(cls, trade: dict[str, Any]) -> tuple[int, str, str, str, str, str]`
  - Method L129: `_extract_yes_price(cls, trade: dict[str, Any]) -> float`
  - Method L139: `_extract_quantity(payload: dict[str, Any], *, fp_key: str, raw_key: str) -> str | int | float`
  - Method L147: `_extract_candle_price(price_payload: dict[str, Any], field: str) -> float | None`
  - Method L159: `_fallback_trade_id(ticker: str, trade: dict[str, Any], occurrence: int) -> TradeId`
  - Method L167: `__init__(self, instrument: BinaryOption, series_ticker: str, http_client: nautilus_pyo3.HttpClient | None = None, resolution_metadata: dict[str, Any] | None = None) -> None`
  - Method L180: `resolution_metadata(self) -> dict[str, Any]`
  - Method L190: `_create_http_client() -> nautilus_pyo3.HttpClient`
  - Method L196: `instrument(self) -> BinaryOption`
  - Method L201: `async from_market_ticker(cls, ticker: str, http_client: nautilus_pyo3.HttpClient | None = None) -> KalshiDataLoader`
  - Method L257: `async fetch_trades(self, min_ts: int | None = None, max_ts: int | None = None, limit: int = 1000) -> list[dict[str, Any]]`
  - Method L311: `async fetch_candlesticks(self, start_ts: int | None = None, end_ts: int | None = None, interval: str = 'Minutes1') -> list[dict[str, Any]]`
  - Method L363: `parse_trades(self, trades_data: list[dict[str, Any]]) -> list[TradeTick]`
  - Method L446: `parse_candlesticks(self, candlesticks_data: list[dict[str, Any]], interval: str = 'Minutes1') -> list[Bar]`
  - Method L516: `async load_bars(self, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None, interval: str = 'Minutes1') -> list[Bar]`
  - Method L549: `async load_trades(self, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> list[TradeTick]`

### `prediction_market_extensions/adapters/kalshi/market_selection.py`
- Imports: `__future__, collections, datetime, re, typing`
- Function L29: `_parse_datetime(raw) -> datetime | None`
- Function L39: `volume_24h(market: Mapping[str, Any]) -> float`
- Function L56: `yes_price(market: Mapping[str, Any]) -> float | None`
- Function L75: `end_date_utc(market: Mapping[str, Any]) -> datetime | None`
- Function L82: `market_close_time_ns(raw) -> int`
- Function L92: `days_since_close(raw, now: datetime) -> float | None`
- Function L103: `market_duration_days(market: Mapping[str, Any]) -> float | None`
- Function L119: `is_game_market(market: Mapping[str, Any]) -> bool`
- Function L132: `is_sports_market(market: Mapping[str, Any], *, now: datetime, max_hours_to_close: float, max_market_duration_days: float | None = None) -> bool`
- Function L161: `is_resolved_sports_market(market: Mapping[str, Any], *, now: datetime, max_days_since_close: float, max_market_duration_days: float | None = None) -> bool`

### `prediction_market_extensions/adapters/kalshi/providers.py`
- Imports: `__future__, datetime, decimal, logging, math, nautilus_trader, prediction_market_extensions`
- Function L52: `calculate_kalshi_commission(quantity: decimal.Decimal, price: decimal.Decimal, fee_rate: decimal.Decimal = KALSHI_TAKER_FEE_RATE) -> decimal.Decimal`
- Function L101: `_market_dict_to_instrument(market: dict) -> BinaryOption`
- Class L138: `_KalshiHttpClient`
  - Method L153: `__init__(self, base_url: str) -> None`
  - Method L164: `async get_markets(self, series_tickers: tuple[str, ...] = (), event_tickers: tuple[str, ...] = ()) -> list[dict]`
- Class L212: `KalshiInstrumentProvider(InstrumentProvider)`
  - Method L225: `__init__(self, config: KalshiDataClientConfig) -> None`
  - Method L231: `async load_all_async(self, filters: dict | None = None) -> None`
  - Method L241: `async _fetch_markets(self) -> list[dict]`
  - Method L247: `_market_to_instrument(self, market: dict) -> BinaryOption`

### `prediction_market_extensions/adapters/kalshi/research.py`
- Imports: `__future__, asyncio, collections, datetime, msgspec, nautilus_trader, pandas, prediction_market_extensions, typing`
- Function L49: `_passes_filters(market: Mapping[str, Any], *, min_volume_24h: float, yes_price_min: float | None, yes_price_max: float | None, min_expiry_dt: datetime | None, predicate: MarketPredicate | None) -> bool`
- Function L78: `_extend_with_event_markets(all_markets: list[dict[str, Any]], events: list[dict[str, Any]], *, exclude_ticker_prefixes: tuple[str, ...]) -> None`
- Function L98: `_default_http_client(*, quota_rate_per_second: int) -> nautilus_pyo3.HttpClient`
- Function L104: `async fetch_market_by_ticker(ticker: str, *, http_client: nautilus_pyo3.HttpClient | None = None, quota_rate_per_second: int = 10) -> dict[str, Any]`
- Function L128: `async discover_markets(*, http_client: nautilus_pyo3.HttpClient, candidate_limit: int, status: str = 'open', page_limit: int = 200, max_pages: int | None = None, include_nested_markets: bool = True, exclude_ticker_prefixes: tuple[str, ...] = ('KXMVE',), min_volume_24h: float = 0.0, yes_price_min: float | None = None, yes_price_max: float | None = None, min_days_to_expiry: int | None = None, predicate: MarketPredicate | None = None, sort_key: MarketSortKey = volume_24h, descending: bool = True) -> list[dict[str, Any]]`
- Function L202: `async discover_live_sports_markets(*, candidate_limit: int, http_client: nautilus_pyo3.HttpClient | None = None, quota_rate_per_second: int = 10, max_pages: int | None = None, page_limit: int = 200, min_volume: float = 0.0, max_hours_to_close: float, max_market_duration_days: float | None = None, games_only: bool = False) -> list[dict[str, Any]]`
- Function L238: `async discover_resolved_sports_markets(*, candidate_limit: int, http_client: nautilus_pyo3.HttpClient | None = None, quota_rate_per_second: int = 10, max_pages: int | None = None, page_limit: int = 200, min_volume: float = 0.0, max_days_since_close: float, max_market_duration_days: float | None = None, games_only: bool = False) -> list[dict[str, Any]]`
- Function L274: `_analysis_window_end(*, market: Mapping[str, Any], now: datetime) -> datetime | None`
- Function L281: `async analyze_market_trade_window(*, market: Mapping[str, Any], lookback_days: int, entry_price: float, now: datetime | None = None) -> dict[str, Any] | None`
- Function L332: `async select_breakout_markets_per_game(*, markets: list[dict[str, Any]], lookback_days: int, entry_price: float, now: datetime | None = None, max_results: int | None = None) -> list[dict[str, Any]]`
- Function L407: `async load_market_bars(*, market: Mapping[str, Any], start: pd.Timestamp, end: pd.Timestamp, http_client: nautilus_pyo3.HttpClient, interval: str = 'Minutes1', chunk_minutes: int = 5000, min_bars: int = 0, min_price_range: float = 0.0, max_retries: int = 4, retry_base_delay: float = 2.0) -> tuple[KalshiDataLoader, list[Bar]] | None`

### `prediction_market_extensions/adapters/polymarket/__init__.py`
- Imports: none

### `prediction_market_extensions/adapters/polymarket/execution.py`
- Imports: `asyncio, collections, json, msgspec, nautilus_trader, py_clob_client, typing`
- Class L122: `PolymarketExecutionClient(LiveExecutionClient)`
  - Method L149: `__init__(self, loop: asyncio.AbstractEventLoop, http_client: ClobClient, msgbus: MessageBus, cache: Cache, clock: LiveClock, instrument_provider: PolymarketInstrumentProvider, ws_auth: PolymarketWebSocketAuth, config: PolymarketExecClientConfig, name: str | None) -> None`
  - Method L247: `async _connect(self) -> None`
  - Method L269: `async _disconnect(self) -> None`
  - Method L272: `_stop(self) -> None`
  - Method L275: `async _maintain_active_market(self, instrument_id: InstrumentId) -> None`
  - Method L279: `async _update_account_state(self) -> None`
  - Method L298: `async _fetch_user_positions(self, *, limit: int = 100, size_threshold: int = 0) -> list[dict[str, Any]]`
  - Method L346: `async generate_order_status_reports(self, command: GenerateOrderStatusReports) -> list[OrderStatusReport]`
  - Method L517: `async generate_order_status_report(self, command: GenerateOrderStatusReport) -> OrderStatusReport | None`
  - Method L572: `async generate_fill_reports(self, command: GenerateFillReports) -> list[FillReport]`
  - Method L621: `async generate_position_status_reports(self, command: GeneratePositionStatusReports) -> list[PositionStatusReport]`
  - Method L660: `_parse_trades_response_object(self, command: GenerateFillReports, json_obj: JSON, parsed_fill_keys: set[tuple[TradeId, VenueOrderId]], reports: list[FillReport]) -> None`
  - Method L715: `async _fetch_quantities_from_gamma_api(self, instrument_ids: list[InstrumentId]) -> dict[InstrumentId, Quantity]`
  - Method L753: `async _fetch_quantities_from_clob_api(self, instrument_ids: list[InstrumentId]) -> dict[InstrumentId, Quantity]`
  - Method L781: `_generate_cancel_event(self, strategy_id, instrument_id, client_order_id, venue_order_id, reason: str, ts_event: int) -> None`
  - Method L807: `_get_neg_risk_for_instrument(self, instrument) -> bool`
  - Method L812: `async _query_account(self, _command: QueryAccount) -> None`
  - Method L816: `async _cancel_order(self, command: CancelOrder) -> None`
  - Method L863: `async _batch_cancel_orders(self, command: BatchCancelOrders) -> None`
  - Method L916: `async _cancel_all_orders(self, command: CancelAllOrders) -> None`
  - Method L964: `async _cancel_all_global(self) -> None`
  - Method L998: `async _cancel_market_orders(self, instrument_id: InstrumentId | None = None, asset_id: str = '') -> None`
  - Method L1052: `async _submit_order(self, command: SubmitOrder) -> None`
  - Method L1126: `_validate_order_for_batch(self, order: Order) -> str | None`
  - Method L1150: `async _submit_order_list(self, command: SubmitOrderList) -> None`
  - Method L1220: `async _sign_orders_for_batch(self, orders: list[Order]) -> tuple[list[Order], list[PostOrdersArgs]]`
  - Method L1281: `async _post_signed_orders_batch(self, orders: list[Order], signed_orders_args: list[PostOrdersArgs]) -> None`
  - Method L1310: `_reject_all_orders(self, orders: list[Order], reason: str) -> None`
  - Method L1323: `_process_batch_response(self, orders: list[Order], response: list) -> None`
  - Method L1374: `_deny_market_order_quantity(self, order: Order, reason: str) -> None`
  - Method L1386: `async _submit_market_order(self, command: SubmitOrder, instrument) -> None`
  - Method L1435: `async _submit_limit_order(self, command: SubmitOrder, instrument) -> None`
  - Method L1481: `async _post_signed_order(self, order: Order, signed_order, post_only: bool = False) -> None`
  - Method L1517: `_handle_ws_message(self, raw: bytes) -> None`
  - Method L1539: `_add_trade_to_cache(self, msg: PolymarketUserTrade, raw: bytes) -> None`
  - Method L1549: `async _wait_for_ack_order(self, msg: PolymarketUserOrder, venue_order_id: VenueOrderId) -> None`
  - Method L1572: `async _wait_for_ack_trade(self, msg: PolymarketUserTrade, venue_order_id: VenueOrderId) -> None`
  - Method L1599: `_handle_ws_order_msg(self, msg: PolymarketUserOrder, wait_for_ack: bool) -> Any`
  - Method L1671: `_truncate_ordered_dict(self, store: OrderedDict[Any, Any]) -> None`
  - Method L1675: `_record_processed_trade(self, trade_id: TradeId, status: PolymarketTradeStatus) -> None`
  - Method L1689: `_record_processed_fill(self, trade_id: TradeId, venue_order_id: VenueOrderId) -> None`
  - Method L1695: `_handle_ws_trade_msg(self, msg: PolymarketUserTrade, wait_for_ack: bool) -> Any`
  - Method L1737: `_handle_user_trade_in_ws_trade_msg(self, msg: PolymarketUserTrade, trade_id: TradeId, wait_for_ack: bool, order_id: str) -> Any`

### `prediction_market_extensions/adapters/polymarket/fee_model.py`
- Imports: `__future__, collections, decimal, nautilus_trader, prediction_market_extensions, typing`
- Function L66: `_normalize_label(value: object) -> str | None`
- Function L75: `_iter_tag_labels(tags: object) -> Iterable[str]`
- Function L97: `_market_labels(info: Mapping[str, Any] | None) -> set[str]`
- Function L125: `infer_maker_rebate_rate(*, market_info: Mapping[str, Any] | None, fee_rate_bps: Decimal) -> Decimal`
- Function L155: `calculate_maker_rebate(*, quantity: Decimal, price: Decimal, fee_rate_bps: Decimal, maker_rebate_rate: Decimal) -> float`
- Class L187: `PolymarketFeeModel(FeeModel)`
  - Method L211: `__init__(self, *, maker_rebates_enabled: bool = True) -> None`
  - Method L214: `get_commission(self, order, fill_qty, fill_px, instrument) -> Money`

### `prediction_market_extensions/adapters/polymarket/gamma_markets.py`
- Imports: `__future__, collections, math, msgspec, nautilus_trader, os, typing`
- Function L43: `_normalize_base_url(base_url: str | None) -> str`
- Function L48: `build_markets_query(filters: dict[str, Any] | None = None) -> dict[str, Any]`
- Function L105: `async _request_markets_page(http_client: HttpClient, base_url: str, params: dict[str, Any], offset: int, limit: int, timeout: float) -> list[dict[str, Any]]`
- Function L142: `async iter_markets(http_client: HttpClient, filters: dict[str, Any] | None = None, base_url: str | None = None, timeout: float = 10.0) -> AsyncGenerator[dict[str, Any]]`
- Function L174: `_decode_gamma_list(raw) -> list[Any]`
- Function L182: `_truthy_gamma_value(raw) -> bool`
- Function L192: `_gamma_market_allows_price_winner_inference(gamma_market: dict[str, Any]) -> bool`
- Function L217: `infer_gamma_token_winners(gamma_market: dict[str, Any]) -> tuple[dict[str, bool], bool]`
- Function L249: `normalize_gamma_market_to_clob_format(gamma_market: dict[str, Any]) -> dict[str, Any]`
- Function L339: `async list_markets(http_client: HttpClient, filters: dict[str, Any] | None = None, base_url: str | None = None, timeout: float = 10.0, max_results: int | None = None) -> list[dict[str, Any]]`

### `prediction_market_extensions/adapters/polymarket/loaders.py`
- Imports: `__future__, copy, decimal, hashlib, msgspec, nautilus_trader, numpy, os, pandas, pathlib, prediction_market_extensions, time, typing, warnings`
- Function L54: `_rounded_float64_array(values, precision: int) -> np.ndarray`
- Function L58: `_unique_tmp_path(path: Path) -> Path`
- Class L62: `PolymarketDataLoader`
  - Method L100: `__init__(self, instrument: BinaryOption, token_id: str | None = None, condition_id: str | None = None, http_client: nautilus_pyo3.HttpClient | None = None, resolution_metadata: dict[str, Any] | None = None) -> None`
  - Method L115: `resolution_metadata(self) -> dict[str, Any]`
  - Method L125: `_create_http_client() -> nautilus_pyo3.HttpClient`
  - Method L131: `clear_metadata_cache(cls) -> None`
  - Method L138: `_gamma_metadata_cache_key(cls) -> str`
  - Method L142: `_clob_metadata_cache_key(cls) -> str`
  - Method L146: `_env_flag_enabled(value: str | None) -> bool`
  - Method L150: `_metadata_cache_dir(cls) -> Path | None`
  - Method L168: `_metadata_cache_dir_from_default(cls) -> Path`
  - Method L174: `_metadata_cache_ttl_secs(cls, payload: dict[str, Any]) -> int`
  - Method L193: `_metadata_cache_path(cls, kind: str, base_key: str, identifier: str) -> Path | None`
  - Method L201: `_metadata_cache_event_fields(kind: str, identifier: str) -> dict[str, str]`
  - Method L211: `_emit_metadata_cache_event(cls, message: str, *, kind: str, identifier: str, cache_path: Path, level: str = 'INFO', stage: str, status: str, bytes_count: int | None = None, attrs: dict[str, Any] | None = None) -> None`
  - Method L245: `_read_metadata_disk_cache(cls, kind: str, base_key: str, identifier: str) -> object`
  - Method L307: `_write_metadata_disk_cache(cls, kind: str, base_key: str, identifier: str, payload: dict[str, Any]) -> None`
  - Method L355: `async _get_market_by_slug(cls, slug: str, http_client: nautilus_pyo3.HttpClient) -> dict[str, Any]`
  - Method L386: `async _get_market_details(cls, condition_id: str, http_client: nautilus_pyo3.HttpClient) -> dict[str, Any]`
  - Method L418: `async _get_event_by_slug(cls, slug: str, http_client: nautilus_pyo3.HttpClient) -> dict[str, Any]`
  - Method L438: `async _get_market_fee_rate_bps(cls, token_id: str, http_client: nautilus_pyo3.HttpClient) -> Decimal | None`
  - Method L466: `async _fetch_market_by_slug(slug: str, http_client: nautilus_pyo3.HttpClient) -> dict[str, Any]`
  - Method L546: `async _fetch_market_details(condition_id: str, http_client: nautilus_pyo3.HttpClient) -> dict[str, Any]`
  - Method L597: `_coerce_fee_rate_bps(value) -> Decimal | None`
  - Method L607: `async _fetch_market_fee_rate_bps(cls, token_id: str, http_client: nautilus_pyo3.HttpClient) -> Decimal | None`
  - Method L662: `async _enrich_market_details_with_fee_rate(cls, market_details: dict[str, Any], token_id: str, http_client: nautilus_pyo3.HttpClient) -> dict[str, Any]`
  - Method L688: `async _fetch_event_by_slug(slug: str, http_client: nautilus_pyo3.HttpClient) -> dict[str, Any]`
  - Method L773: `async from_market_slug(cls, slug: str, token_index: int = 0, http_client: nautilus_pyo3.HttpClient | None = None) -> PolymarketDataLoader`
  - Method L868: `async from_event_slug(cls, slug: str, token_index: int = 0, http_client: nautilus_pyo3.HttpClient | None = None) -> list[PolymarketDataLoader]`
  - Method L969: `async query_market_by_slug(slug: str, http_client: nautilus_pyo3.HttpClient | None = None) -> dict[str, Any]`
  - Method L999: `async query_market_details(condition_id: str, http_client: nautilus_pyo3.HttpClient | None = None) -> dict[str, Any]`
  - Method L1027: `async query_event_by_slug(slug: str, http_client: nautilus_pyo3.HttpClient | None = None) -> dict[str, Any]`
  - Method L1057: `instrument(self) -> BinaryOption`
  - Method L1064: `token_id(self) -> str | None`
  - Method L1071: `condition_id(self) -> str | None`
  - Method L1077: `async load_trades(self, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> list[TradeTick]`
  - Method L1132: `async fetch_event_by_slug(self, slug: str) -> dict[str, Any]`
  - Method L1159: `async fetch_events(self, active: bool = True, closed: bool = False, archived: bool = False, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]`
  - Method L1251: `async get_event_markets(self, slug: str) -> list[dict[str, Any]]`
  - Method L1276: `async fetch_markets(self, active: bool = True, closed: bool = False, archived: bool = False, limit: int = 100, offset: int = 0) -> list[dict]`
  - Method L1368: `async fetch_market_by_slug(self, slug: str) -> dict[str, Any]`
  - Method L1392: `async find_market_by_slug(self, slug: str) -> dict[str, Any]`
  - Method L1414: `async fetch_market_details(self, condition_id: str) -> dict[str, Any]`
  - Method L1431: `async fetch_trades(self, condition_id: str, limit: int = _TRADES_PAGE_LIMIT, start_ts: int | None = None, end_ts: int | None = None) -> list[dict[str, Any]]`
  - Method L1559: `parse_trades(self, trades_data: list[dict]) -> list[TradeTick]`
  - Method L1576: `_parse_public_trade_rows(self, trades_data: list[dict], *, sort: bool) -> list[TradeTick]`

### `prediction_market_extensions/adapters/polymarket/market_selection.py`
- Imports: `__future__, collections, datetime, msgspec, re, typing`
- Function L57: `_parse_datetime(raw) -> datetime | None`
- Function L67: `_event_payload(market: Mapping[str, Any]) -> Mapping[str, Any]`
- Function L77: `volume_24h(market: Mapping[str, Any]) -> float`
- Function L91: `yes_price(market: Mapping[str, Any]) -> float | None`
- Function L108: `end_date_utc(market: Mapping[str, Any]) -> datetime | None`
- Function L116: `event_start_utc(market: Mapping[str, Any]) -> datetime | None`
- Function L135: `closed_time_utc(market: Mapping[str, Any]) -> datetime | None`
- Function L155: `market_close_time_ns(raw) -> int`
- Function L165: `is_game_market(market: Mapping[str, Any]) -> bool`
- Function L191: `is_sports_market(market: Mapping[str, Any], *, now: datetime, max_hours_to_close: float) -> bool`
- Function L215: `is_resolved_sports_market(market: Mapping[str, Any], *, now: datetime, max_days_since_close: float) -> bool`

### `prediction_market_extensions/adapters/polymarket/parsing.py`
- Imports: `__future__, decimal, nautilus_trader`
- Function L32: `basis_points_as_decimal(basis_points: Decimal) -> Decimal`
- Function L50: `calculate_commission(quantity: Decimal, price: Decimal, fee_rate: Decimal, liquidity_side: LiquiditySide) -> float`

### `prediction_market_extensions/adapters/polymarket/pmxt.py`
- Imports: `__future__, collections, concurrent, contextlib, dataclasses, datetime, duckdb, hashlib, nautilus_trader, numpy, os, pandas, pathlib, prediction_market_extensions, pyarrow, re, shutil, tempfile, time, typing, urllib, warnings`
- Function L53: `_raw_fixed_values(values: Sequence[object], precision: int) -> list[int]`
- Function L57: `_unique_tmp_path(path: Path) -> Path`
- Class L62: `_PMXTOrderBookConversionState`
- Class L67: `PolymarketPMXTDataLoader(PolymarketDataLoader)`
  - Method L142: `__init__(self, *args, **kwargs) -> None`
  - Method L164: `last_load_gap_hours(self) -> tuple[pd.Timestamp, ...]`
  - Method L169: `_normalize_timestamp(value: pd.Timestamp | str | None) -> pd.Timestamp | None`
  - Method L178: `_archive_hours(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]`
  - Method L193: `_archive_filename_for_hour(cls, hour: pd.Timestamp) -> str`
  - Method L198: `_archive_url_for_hour(cls, hour: pd.Timestamp) -> str`
  - Method L202: `_archive_relative_path_for_hour(cls, hour: pd.Timestamp) -> str`
  - Method L210: `_env_flag_enabled(value: str | None) -> bool`
  - Method L216: `_default_cache_dir(cls) -> Path`
  - Method L222: `_resolve_cache_dir(cls) -> Path | None`
  - Method L238: `_resolve_local_archive_dir(cls) -> Path | None`
  - Method L249: `_resolve_prefetch_workers(cls) -> int`
  - Method L264: `_resolve_scan_batch_size(cls) -> int`
  - Method L279: `_write_materialized_cache_enabled(cls) -> bool`
  - Method L283: `_write_window_cache_enabled(cls) -> bool`
  - Method L287: `_market_cache_path_for_hour(cls, cache_dir: Path, condition_id: str, token_id: str, hour: pd.Timestamp) -> Path`
  - Method L298: `_cache_path_for_hour(self, hour: pd.Timestamp) -> Path | None`
  - Method L306: `_window_cache_path_for_range(self, start: pd.Timestamp, end: pd.Timestamp) -> Path | None`
  - Method L321: `_deltas_cache_path_for_range(self, start: pd.Timestamp, end: pd.Timestamp) -> Path | None`
  - Method L337: `_hour_label(hour: pd.Timestamp) -> str`
  - Method L343: `_emit_cache_write_event(self, *, hour: pd.Timestamp, cache_path: Path, table: pa.Table, level: str, status: str, message: str, error: str | None = None) -> None`
  - Method L375: `_write_market_cache_if_enabled(self, hour: pd.Timestamp, table: pa.Table) -> None`
  - Method L406: `_local_archive_candidate_paths_for_hour(cls, archive_dir: Path, hour: pd.Timestamp) -> tuple[Path, ...]`
  - Method L413: `_local_archive_paths_for_hour(self, hour: pd.Timestamp) -> tuple[Path, ...]`
  - Method L418: `_market_filter(self) -> Any`
  - Method L425: `_empty_market_table(cls) -> pa.Table`
  - Method L431: `_is_raw_payload_schema(cls, names: Sequence[str]) -> bool`
  - Method L435: `_is_fixed_schema(cls, names: Sequence[str]) -> bool`
  - Method L439: `_is_raw_fixed_schema(cls, names: Sequence[str]) -> bool`
  - Method L443: `_to_market_batch(cls, batch: pa.RecordBatch) -> pa.RecordBatch`
  - Method L457: `_filter_batch_to_token(self, batch: pa.RecordBatch) -> pa.RecordBatch`
  - Method L472: `_filter_raw_batch(self, batch: pa.RecordBatch) -> pa.RecordBatch`
  - Method L497: `_load_cached_market_table(self, hour: pd.Timestamp) -> pa.Table | None`
  - Method L514: `_load_cached_market_batches(self, hour: pd.Timestamp) -> list[pa.RecordBatch] | None`
  - Method L532: `_load_window_cache_batches(self, start: pd.Timestamp, end: pd.Timestamp) -> list[pa.RecordBatch] | None`
  - Method L570: `_load_deltas_cache_for_range(self, start: pd.Timestamp, end: pd.Timestamp) -> list[OrderBookDeltas] | None`
  - Method L610: `_deltas_records_to_table(records: Sequence[OrderBookDeltas]) -> pa.Table | None`
  - Method L648: `_write_deltas_cache_for_range(self, records: Sequence[OrderBookDeltas], start: pd.Timestamp, end: pd.Timestamp) -> None`
  - Method L710: `_write_market_cache(self, hour: pd.Timestamp, table: pa.Table) -> None`
  - Method L724: `_scan_raw_market_batches(self, dataset: ds.Dataset, *, batch_size: int, source: str | None = None, total_bytes: int | None = None) -> list[pa.RecordBatch]`
  - Method L780: `_market_stats_value(market_type: pa.DataType, condition_id: str) -> bytes | str`
  - Method L789: `_matching_raw_fixed_market_row_groups(self, parquet_file: pq.ParquetFile) -> list[int] | None`
  - Method L830: `_load_raw_fixed_market_batches_pyarrow(self, parquet_path: Path, *, batch_size: int, progress_source: str, total_bytes: int | None) -> list[pa.RecordBatch] | None`
  - Method L934: `_load_raw_market_batches_duckdb(self, parquet_path: Path, *, batch_size: int, progress_source: str, total_bytes: int | None) -> list[pa.RecordBatch] | None`
  - Method L1010: `_load_remote_market_table(self, hour: pd.Timestamp, *, batch_size: int) -> pa.Table | None`
  - Method L1018: `_load_remote_market_batches(self, hour: pd.Timestamp, *, batch_size: int) -> list[pa.RecordBatch] | None`
  - Method L1024: `_load_raw_market_batches_via_download(self, archive_url: str, *, batch_size: int) -> list[pa.RecordBatch] | None`
  - Method L1049: `_emit_remote_archive_load_error(self, archive_url: str, error: Exception) -> None`
  - Method L1066: `_load_local_archive_market_batches(self, hour: pd.Timestamp, *, batch_size: int) -> list[pa.RecordBatch] | None`
  - Method L1084: `_filter_table_to_token(self, table: pa.Table) -> pa.Table`
  - Method L1099: `_load_market_table(self, hour: pd.Timestamp, *, batch_size: int) -> pa.Table | None`
  - Method L1124: `_load_market_batches(self, hour: pd.Timestamp, *, batch_size: int) -> list[pa.RecordBatch] | None`
  - Method L1147: `_emit_download_progress(self, url: str, *, downloaded_bytes: int, total_bytes: int | None, finished: bool) -> None`
  - Method L1165: `_emit_scan_progress(self, source: str, *, scanned_batches: int, scanned_rows: int, matched_rows: int, total_bytes: int | None, finished: bool) -> None`
  - Method L1193: `_content_length_from_response(response: object) -> int | None`
  - Method L1205: `_progress_total_bytes(self, source: str) -> int | None`
  - Method L1242: `_download_to_file_with_progress(self, url: str, destination: Path) -> int | None`
  - Method L1295: `_download_payload_with_progress(self, url: str) -> bytes | None`
  - Method L1335: `_load_raw_market_batches_from_local_file(self, parquet_path: Path, *, batch_size: int, progress_source: str, total_bytes: int | None) -> list[pa.RecordBatch] | None`
  - Method L1371: `_temporary_download_filename(url: str) -> str`
  - Method L1376: `_pid_is_active(pid: int) -> bool`
  - Method L1388: `_temporary_download_path(self, url: str) -> Iterator[Path]`
  - Method L1400: `_cleanup_stale_temp_downloads(self) -> None`
  - Method L1429: `_iter_market_tables(self, hours: list[pd.Timestamp], *, batch_size: int) -> Iterator[tuple[pd.Timestamp, pa.Table | None]]`
  - Method L1460: `_iter_market_batches(self, hours: list[pd.Timestamp], *, batch_size: int) -> Iterator[tuple[pd.Timestamp, list[pa.RecordBatch] | None]]`
  - Method L1492: `_timestamp_to_ms_string(timestamp_secs: float) -> str`
  - Method L1496: `_event_sort_key(record: OrderBookDeltas) -> tuple[int, int]`
  - Method L1502: `_trade_sort_key(record: TradeTick) -> tuple[int, int, str]`
  - Method L1505: `_trade_ticks_from_fixed_batches(self, batches: Sequence[pa.RecordBatch], *, start_ns: int, end_ns: int) -> list[TradeTick]`
  - Method L1603: `load_pmxt_trade_ticks(self, start: pd.Timestamp, end: pd.Timestamp, *, batch_size: int | None = None) -> list[TradeTick]`
  - Method L1639: `_deltas_records_from_columns(self, data: dict[str, list[object]]) -> list[OrderBookDeltas]`
  - Method L1688: `_payload_sort_key(self, update_type: str, payload_text: str) -> tuple[int, int]`
  - Method L1692: `_batches_use_fixed_schema(cls, batches: Sequence[pa.RecordBatch]) -> bool`
  - Method L1696: `new_order_book_delta_state() -> _PMXTOrderBookConversionState`
  - Method L1699: `_order_book_deltas_from_hour_batches_with_state(self, *, start_ns: int, end_ns: int, hour_batches: Iterator[tuple[pd.Timestamp, list[pa.RecordBatch] | None]], include_order_book: bool, state: _PMXTOrderBookConversionState) -> tuple[list[OrderBookDeltas], list[pd.Timestamp]]`
  - Method L1771: `load_order_book_deltas_from_hour_batches_incremental(self, start: pd.Timestamp, end: pd.Timestamp, hour_batches: Sequence[tuple[pd.Timestamp, list[pa.RecordBatch] | None]], *, state: _PMXTOrderBookConversionState, include_order_book: bool = True, sort_events: bool = True) -> tuple[list[OrderBookDeltas], tuple[pd.Timestamp, ...]]`
  - Method L1797: `_order_book_deltas_from_hour_batches(self, *, start_ts: pd.Timestamp, end_ts: pd.Timestamp, hour_batches: Iterator[tuple[pd.Timestamp, list[pa.RecordBatch] | None]], include_order_book: bool) -> list[OrderBookDeltas]`
  - Method L1831: `load_order_book_deltas_from_hour_batches(self, start: pd.Timestamp, end: pd.Timestamp, hour_batches: Sequence[tuple[pd.Timestamp, list[pa.RecordBatch] | None]], *, include_order_book: bool = True) -> list[OrderBookDeltas]`
  - Method L1863: `load_order_book_deltas(self, start: pd.Timestamp, end: pd.Timestamp, *, batch_size: int | None = None, include_order_book: bool = True) -> list[OrderBookDeltas]`
  - Method L1928: `_timestamp_to_ns(value: object) -> int`

### `prediction_market_extensions/adapters/polymarket/research.py`
- Imports: `__future__, collections, datetime, msgspec, nautilus_trader, pandas, prediction_market_extensions, typing`
- Function L49: `_default_http_client(*, quota_rate_per_second: int) -> nautilus_pyo3.HttpClient`
- Function L55: `_passes_filters(market: Mapping[str, Any], *, min_volume_24h: float, yes_price_min: float | None, yes_price_max: float | None, min_expiry_dt: datetime | None, predicate: MarketPredicate | None) -> bool`
- Function L84: `_event_volume(market: Mapping[str, Any]) -> float`
- Function L88: `_main_market_from_event(event: Mapping[str, Any]) -> dict[str, Any] | None`
- Function L116: `async _discover_resolved_game_markets_from_events(*, candidate_limit: int, http_client: nautilus_pyo3.HttpClient | None = None, max_results: int, quota_rate_per_second: int, min_volume_24h: float, max_days_since_close: float) -> list[dict[str, Any]]`
- Function L199: `async discover_markets(*, candidate_limit: int, http_client: nautilus_pyo3.HttpClient | None = None, api_filters: dict[str, Any] | None = None, max_results: int = 200, quota_rate_per_second: int = 20, min_volume_24h: float = 0.0, yes_price_min: float | None = None, yes_price_max: float | None = None, min_days_to_expiry: int | None = None, predicate: MarketPredicate | None = None, sort_key: MarketSortKey = volume_24h, descending: bool = True) -> list[dict[str, Any]]`
- Function L258: `async fetch_market_by_slug(slug: str, *, http_client: nautilus_pyo3.HttpClient | None = None, quota_rate_per_second: int = 10) -> dict[str, Any]`
- Function L282: `async discover_live_sports_markets(*, candidate_limit: int, http_client: nautilus_pyo3.HttpClient | None = None, max_results: int = 200, quota_rate_per_second: int = 20, min_volume_24h: float = 0.0, max_hours_to_close: float, games_only: bool = False) -> list[dict[str, Any]]`
- Function L309: `async discover_resolved_sports_markets(*, candidate_limit: int, http_client: nautilus_pyo3.HttpClient | None = None, max_results: int = 200, quota_rate_per_second: int = 20, min_volume_24h: float = 0.0, max_days_since_close: float, games_only: bool = False) -> list[dict[str, Any]]`
- Function L351: `market_trade_window_bounds(market: Mapping[str, Any], *, active_window_hours: float, now: datetime | None = None) -> tuple[datetime | None, datetime | None]`
- Function L382: `async analyze_market_trade_window(*, market: Mapping[str, Any], lookback_days: int, entry_price: float, active_window_hours: float, now: datetime | None = None, http_client: nautilus_pyo3.HttpClient | None = None) -> dict[str, Any] | None`
- Function L489: `async load_market_trades(*, slug: str, start: pd.Timestamp, end: pd.Timestamp, min_trades: int = 0, min_price_range: float = 0.0) -> tuple[PolymarketDataLoader, list[TradeTick]] | None`

### `prediction_market_extensions/adapters/prediction_market/__init__.py`
- Imports: `prediction_market_extensions`

### `prediction_market_extensions/adapters/prediction_market/backtest_utils.py`
- Imports: `__future__, collections, datetime, nautilus_trader, pandas, warnings`
- Function L34: `_parse_numeric(value: object, default: float = 0.0) -> float`
- Function L54: `_parse_required_numeric(value: object) -> float | None`
- Function L61: `_book_midpoint(book: OrderBook) -> float | None`
- Function L69: `extract_realized_pnl(pos_report: pd.DataFrame) -> float`
- Function L81: `_timestamp_to_naive_utc_datetime(ts: pd.Timestamp) -> datetime`
- Function L92: `to_naive_utc(value: object) -> datetime | None`
- Function L122: `extract_price_points(records: Sequence[object], *, price_attr: str, ts_attrs: tuple[str, ...] = _DEFAULT_TS_ATTRS) -> list[PricePoint]`
- Function L173: `downsample_price_points(points: list[PricePoint], max_points: int = 5000) -> list[PricePoint]`
- Function L204: `_probability_frame(points: Sequence[PricePoint]) -> pd.DataFrame`
- Function L256: `_resolved_outcome_from_result(info: Mapping[object, object], outcome_name: str) -> float | None`
- Function L269: `_resolved_outcome_from_numeric_fields(info: Mapping[object, object]) -> float | None`
- Function L288: `_resolved_outcome_from_tokens(info: Mapping[object, object], outcome_name: str) -> float | None`
- Function L306: `infer_realized_outcome_from_metadata(metadata: Mapping[object, object] | None, outcome_name: str) -> float | None`
- Function L337: `infer_realized_outcome(source: object | None) -> float | None`
- Function L356: `compute_binary_settlement_pnl(fill_events: Sequence[Mapping[object, object]], resolved_outcome: float | None) -> float | None`
- Function L401: `build_brier_inputs(points: Sequence[PricePoint], window: int, realized_outcome: float | None = None, warnings_out: list[str] | None = None) -> tuple[pd.Series, pd.Series, pd.Series]`
- Function L447: `build_market_prices(points: Sequence[PricePoint], *, resample_rule: str | None = None) -> list[tuple[datetime, float]]`

### `prediction_market_extensions/adapters/prediction_market/fill_model.py`
- Imports: `__future__, decimal, nautilus_trader, prediction_market_extensions`
- Function L28: `effective_prediction_market_slippage_tick(instrument) -> float`
- Function L45: `_coerce_positive_float(value: object) -> float | None`
- Function L63: `_order_quantity(order) -> float | None`
- Function L71: `_is_entry_order(order) -> bool`
- Function L82: `_synthetic_book_size(order, *, min_synthetic_book_size: float, synthetic_book_depth_multiplier: float) -> float`
- Class L97: `PredictionMarketTakerFillModel(FillModel)`
  - Method L130: `__init__(self, *, slippage_ticks: int = 1, entry_slippage_pct: float = 0.0, exit_slippage_pct: float = 0.0, prob_fill_on_limit: float = _DEFAULT_LIMIT_FILL_PROBABILITY, min_synthetic_book_size: float = _DEFAULT_MIN_SYNTHETIC_BOOK_SIZE, synthetic_book_depth_multiplier: float = _DEFAULT_SYNTHETIC_BOOK_DEPTH_MULTIPLIER) -> None`
  - Method L169: `get_orderbook_for_fill_simulation(self, instrument, order, best_bid, best_ask) -> Any`

### `prediction_market_extensions/adapters/prediction_market/info_sanitization.py`
- Imports: `__future__, collections, typing`
- Function L40: `extract_resolution_metadata(info: Mapping[str, Any] | None) -> dict[str, Any]`
- Function L77: `sanitize_info_for_simulation(info: Mapping[str, Any] | None) -> dict[str, Any]`

### `prediction_market_extensions/adapters/prediction_market/order_tags.py`
- Imports: `__future__, decimal, typing`
- Function L10: `_coerce_positive_float(value: object) -> float | None`
- Function L22: `format_order_intent_tag(intent: str) -> str`
- Function L27: `parse_order_intent(tags: Iterable[str] | None) -> str | None`
- Function L37: `format_visible_liquidity_tag(size: object) -> str | None`
- Function L45: `parse_visible_liquidity(tags: Iterable[str] | None) -> float | None`

### `prediction_market_extensions/adapters/prediction_market/replay.py`
- Imports: `__future__, abc, collections, contextlib, dataclasses, nautilus_trader, typing`
- Class L31: `ReplayAdapterKey`
- Class L38: `ReplayWindow`
  - Method L42: `__post_init__(self) -> None`
- Class L54: `ReplayCoverageStats`
- Class L63: `ReplayLoadRequest`
- Class L73: `ReplayEngineProfile`
- Class L87: `LoadedReplay`
  - Method L101: `spec(self) -> Any`
  - Method L105: `count(self) -> int`
  - Method L109: `count_key(self) -> str`
  - Method L113: `market_key(self) -> str`
  - Method L117: `market_id(self) -> str`
  - Method L121: `prices(self) -> tuple[float, ...]`
- Class L125: `HistoricalReplayAdapter(ABC)`
  - Method L128: `key(self) -> ReplayAdapterKey`
  - Method L133: `replay_spec_type(self) -> type[Any]`
  - Method L136: `build_single_market_replay(self, *, field_values: Mapping[str, Any]) -> Any`
  - Method L142: `configure_sources(self, *, sources: Sequence[str]) -> AbstractContextManager[Any]`
  - Method L147: `engine_profile(self) -> ReplayEngineProfile`
  - Method L151: `async load_replay(self, replay, *, request: ReplayLoadRequest) -> LoadedReplay | None`

### `prediction_market_extensions/adapters/prediction_market/research.py`
- Imports: `__future__, collections, datetime, math, nautilus_trader, pandas, pathlib, prediction_market_extensions, re, typing`
- Function L72: `_extract_account_pnl_series(engine: BacktestEngine) -> pd.Series`
- Function L95: `_dense_account_series_from_engine(*, engine: BacktestEngine, market_id: str, market_prices: Sequence[tuple[datetime, float]], initial_cash: float) -> tuple[pd.Series, pd.Series]`
- Function L107: `_dense_account_series_from_engine_for_markets(*, engine: BacktestEngine, market_prices: Mapping[str, Sequence[tuple[datetime, float]]], initial_cash: float) -> tuple[pd.Series, pd.Series]`
- Function L146: `_dense_market_account_series_from_fill_events(*, market_id: str, market_prices: Sequence[tuple[datetime, float]], fill_events: Sequence[dict[str, Any]], initial_cash: float) -> tuple[pd.Series, pd.Series]`
- Function L223: `_pairs_to_series(pairs: Sequence[tuple[str, float]] | Sequence[tuple[Any, float]]) -> pd.Series`
- Function L238: `_fill_event_timestamp(event: Mapping[str, Any]) -> pd.Timestamp`
- Function L249: `_to_legacy_datetime(timestamp: pd.Timestamp) -> datetime`
- Function L253: `_series_to_iso_pairs(series: pd.Series) -> list[tuple[str, float]]`
- Function L260: `_align_series_to_timeline(series: pd.Series, timeline: pd.DatetimeIndex, *, before: float, after: float) -> pd.Series`
- Function L272: `_extend_active_range(active_ranges: dict[str, tuple[pd.Timestamp, pd.Timestamp]], label: str, start: pd.Timestamp, end: pd.Timestamp) -> None`
- Function L285: `_parse_float_like(value, default: float = 0.0) -> float`
- Function L305: `_serialize_fill_events(*, market_id: str, fills_report: pd.DataFrame) -> list[dict[str, Any]]`
- Function L383: `_deserialize_fill_events(*, market_id: str, fill_events: Sequence[dict[str, Any]], models_module) -> list[Any]`
- Function L426: `_aggregate_brier_frames(results: Sequence[dict[str, Any]]) -> dict[str, pd.DataFrame]`
- Function L454: `_aggregate_brier_unavailable_reason(results: Sequence[dict[str, Any]]) -> str | None`
- Function L476: `_summary_panels_need_market_prices(plot_panels: Sequence[str]) -> bool`
- Function L480: `_summary_panels_need_fill_events(plot_panels: Sequence[str]) -> bool`
- Function L486: `_summary_panels_need_overlay_series(plot_panels: Sequence[str]) -> bool`
- Function L493: `_yes_price_fill_marker_budget(max_points: int) -> int`
- Function L499: `_summary_yes_price_fill_marker_limit(fill_count: int, max_points: int) -> int | None`
- Function L510: `_configure_summary_report_downsampling(plotting_module, *, adaptive: bool = True, max_points: int = 5000) -> None`
- Function L557: `_build_summary_brier_panel(brier_frames: dict[str, pd.DataFrame], *, axis_label: str, max_points_per_market: int) -> Any | None`
- Function L571: `_build_total_summary_brier_frame(brier_frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame`
- Function L592: `_build_summary_brier_extra_panels(*, results: Sequence[dict[str, Any]], resolved_plot_panels: Sequence[str], max_points_per_market: int) -> dict[str, Any]`
- Function L637: `_apply_summary_layout_overrides(layout, *, initial_cash: float, max_yes_price_fill_markers: int | None) -> Any`
- Function L652: `run_market_backtest(*, market_id: str, instrument, data: Sequence[object], strategy: Strategy, strategy_name: str, output_prefix: str, platform: str, venue: Venue, base_currency: Currency, fee_model, fill_model: Any | None = None, apply_default_fill_model: bool = True, initial_cash: float, probability_window: int, price_attr: str, count_key: str, data_count: int | None = None, chart_resample_rule: str | None = None, market_key: str = 'market', open_browser: bool = False, return_summary_series: bool = False, book_type: BookType = BookType.L1_MBP, liquidity_consumption: bool = False, queue_position: bool = False, latency_model: Any | None = None) -> dict[str, Any]`
- Function L805: `save_combined_backtest_report(*, results: Sequence[dict[str, Any]], output_path: str | Path, title: str, market_key: str, pnl_label: str) -> str | None`
- Function L859: `save_aggregate_backtest_report(*, results: Sequence[dict[str, Any]], output_path: str | Path, title: str, market_key: str, pnl_label: str, max_points_per_market: int = 400, plot_panels: Sequence[str] | None = None) -> str | None`
- Function L1105: `save_joint_portfolio_backtest_report(*, results: Sequence[dict[str, Any]], output_path: str | Path, title: str, market_key: str, pnl_label: str, max_points_per_market: int = 400, plot_panels: Sequence[str] | None = None) -> str | None`
- Function L1347: `print_backtest_summary(*, results: list[dict[str, Any]], market_key: str, count_key: str, count_label: str, pnl_label: str, empty_message: str = 'No markets had sufficient data.') -> None`
- Function L1410: `_summary_stats_for_result(result: Mapping[str, Any]) -> dict[str, float | None]`
- Function L1432: `_summary_stats_total(*, rows: Sequence[Mapping[str, float | None]], results: Sequence[Mapping[str, Any]]) -> dict[str, float | None]`
- Function L1485: `_summary_fill_stats(fill_events: object) -> tuple[float, float, float | None]`
- Function L1503: `_summary_returns_from_pairs(pairs: object) -> dict[int, float]`
- Function L1508: `_summary_returns_from_series(series: pd.Series) -> dict[int, float]`
- Function L1528: `_summary_return_stats(returns: dict[int, float]) -> dict[str, float | None]`
- Function L1545: `_summary_total_return_pct(pairs: object) -> float | None`
- Function L1550: `_summary_total_return_pct_from_series(series: pd.Series) -> float | None`
- Function L1565: `_summary_reconciled_equity_series(pairs: object, *, final_pnl: object) -> pd.Series`
- Function L1595: `_summary_total_return_pct_for_portfolio(*, equity_series: object, total_pnl: float, portfolio_pnls: Mapping[str, Any], use_portfolio_stats: bool) -> float | None`
- Function L1627: `_summary_portfolio_stats(results: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]`
- Function L1634: `_summary_portfolio_return_stats(portfolio_stats: Mapping[str, Any]) -> Mapping[str, Any]`
- Function L1639: `_summary_portfolio_pnl_stats(portfolio_stats: Mapping[str, Any]) -> Mapping[str, Any]`
- Function L1651: `_summary_portfolio_pnl_matches(*, portfolio_pnls: Mapping[str, Any], total_pnl: float) -> bool`
- Function L1659: `_summary_prefer_stat(primary: object, fallback: float | None) -> float | None`
- Function L1664: `_safe_stat(func, returns: dict[int, float]) -> float | None`
- Function L1672: `_safe_stat_percent(func, returns: dict[int, float]) -> float | None`
- Function L1677: `_coerce_float(value: object) -> float | None`
- Function L1685: `_format_summary_float(value: object, decimals: int) -> str`
- Function L1692: `_format_summary_pct(value: object) -> str`
- Function L1699: `_print_portfolio_stats(results: Sequence[Mapping[str, Any]]) -> None`
- Function L1762: `_selected_named_stats(stats: Mapping[str, Any], names: Sequence[str]) -> list[str]`

### `prediction_market_extensions/analysis/__init__.py`
- Imports: none

### `prediction_market_extensions/analysis/config.py`
- Imports: `__future__, nautilus_trader`
- Class L29: `TearsheetPnLChart(TearsheetChart)`
  - Method L31: `name(self) -> str`
- Class L35: `TearsheetAllocationChart(TearsheetChart)`
  - Method L37: `name(self) -> str`
- Class L41: `TearsheetCumulativeBrierAdvantageChart(TearsheetChart)`
  - Method L43: `name(self) -> str`

### `prediction_market_extensions/analysis/legacy_backtesting/__init__.py`
- Imports: none

### `prediction_market_extensions/analysis/legacy_backtesting/models.py`
- Imports: `__future__, collections, dataclasses, datetime, enum, typing, uuid`
- Function L110: `normalize_plot_panels(panels: Sequence[str] | None, *, default: Sequence[str]) -> tuple[str, ...]`
- Class L22: `Platform(str, Enum)`
- Class L27: `Side(str, Enum)`
- Class L32: `OrderAction(str, Enum)`
- Class L37: `OrderStatus(str, Enum)`
- Class L43: `MarketStatus(str, Enum)`
- Class L134: `MarketInfo`
- Class L149: `TradeEvent`
- Class L163: `Order`
- Class L180: `Fill`
- Class L194: `Position`
- Class L208: `PortfolioSnapshot`
- Class L219: `BacktestResult`
  - Method L245: `plot(self, **kwargs) -> Any`

### `prediction_market_extensions/analysis/legacy_backtesting/plotting.py`
- Imports: `__future__, bokeh, collections, colorsys, functools, itertools, numpy, os, pandas, pathlib, prediction_market_extensions, random, sys, typing`
- Function L95: `_is_notebook() -> bool`
- Function L100: `set_bokeh_output(notebook: bool = False) -> None`
- Function L132: `_bokeh_reset(filename: str | None = None) -> None`
- Function L143: `colorgen() -> Any`
- Function L148: `lightness(color, light: float = 0.94) -> str`
- Function L156: `_series_from_pairs(values: pd.Series | Sequence[tuple[Any, float]] | None) -> pd.Series`
- Function L185: `_normalize_overlay_mapping(values: Mapping[str, pd.Series | Sequence[tuple[Any, float]]]) -> dict[str, pd.Series]`
- Function L197: `_align_overlay_series(series: pd.Series, datetimes: pd.Series | pd.DatetimeIndex) -> np.ndarray`
- Function L208: `_drawdown_array(values: np.ndarray) -> np.ndarray`
- Function L222: `_estimate_ticks_per_year(datetimes: pd.DatetimeIndex | None = None) -> float`
- Function L240: `_rolling_sharpe_array(values: np.ndarray, annualize: bool = True, annualization_factor: float | None = None, datetimes: pd.DatetimeIndex | None = None) -> tuple[np.ndarray, int | None]`
- Function L268: `_build_dataframes(result: BacktestResult, bar: PinnedProgress[None] | None = None, max_markets: int = 10) -> Any`
- Function L398: `_select_display_markets(market_df: pd.DataFrame, fills_df: pd.DataFrame, *, max_markets: int) -> list[str]`
- Function L426: `_finite_idxmax(series: pd.Series) -> int | None`
- Function L433: `_downsample(eq: pd.DataFrame, fills_df: pd.DataFrame, market_df: pd.DataFrame, max_points: int = 5000, alloc_df: pd.DataFrame | None = None, keep_indices: set[int] | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame | None]`
- Function L511: `_build_allocation_data(eq: pd.DataFrame, fills_df: pd.DataFrame, market_prices: dict[str, list[tuple]], top_n: int | None = None) -> pd.DataFrame`
- Function L662: `plot(result: BacktestResult, *, filename: str = '', plot_width: int | None = None, plot_equity: bool = True, plot_drawdown: bool = True, plot_pl: bool = True, plot_cash: bool = True, plot_market_prices: bool = True, plot_allocation: bool = True, show_legend: bool = True, open_browser: bool = True, relative_equity: bool = True, plot_monthly_returns: bool | None = None, max_markets: int = 30, progress: bool = True, plot_panels: Sequence[str] | None = None, extra_panels: Mapping[str, Any] | None = None) -> Any`

### `prediction_market_extensions/analysis/legacy_backtesting/progress.py`
- Imports: `__future__, collections, os, sys, time, typing`
- Function L30: `_term_width() -> int`
- Function L37: `_term_height() -> int`
- Class L44: `PinnedProgress(Generic[T])`
  - Method L52: `__init__(self, iterable: Iterable[T], total: int, desc: str = '', unit: str = ' it', refresh_interval: float = 0.05) -> Any`
  - Method L74: `_setup(self) -> None`
  - Method L85: `_teardown(self) -> None`
  - Method L97: `_refresh_bar(self) -> None`
  - Method L149: `_strip_ansi(s: str) -> str`
  - Method L155: `_fmt_time(seconds: float) -> str`
  - Method L164: `write(self, msg: str) -> None`
  - Method L174: `advance(self, n: int = 1) -> None`
  - Method L181: `set_desc(self, desc: str) -> None`
  - Method L188: `__enter__(self) -> PinnedProgress[T]`
  - Method L192: `__exit__(self, *exc: object) -> None`
  - Method L198: `__iter__(self) -> Iterator[T]`

### `prediction_market_extensions/analysis/legacy_plot_adapter.py`
- Imports: `__future__, collections, datetime, importlib, nautilus_trader, numpy, pandas, pathlib, prediction_market_extensions, re, typing`
- Function L46: `_parse_float(value, default: float = 0.0) -> float`
- Function L71: `_to_naive_utc(value) -> datetime | None`
- Function L102: `_timestamp_to_naive_utc_datetime(ts: pd.Timestamp) -> datetime`
- Function L113: `_first_value(row: pd.Series, *keys: str) -> Any`
- Function L122: `prepare_cumulative_brier_advantage(user_probabilities: pd.Series | None = None, market_probabilities: pd.Series | None = None, outcomes: pd.Series | None = None) -> pd.DataFrame`
- Function L169: `_load_legacy_modules(repo_path: Path | None = None) -> tuple[Any, Any]`
- Function L181: `_extract_account_report(engine) -> pd.DataFrame`
- Function L214: `_infer_market_side(models_module, market_id: str) -> Any`
- Function L221: `_signed_quantity(action: str, side: str, qty: float) -> float`
- Function L233: `_convert_fills(fills_report: pd.DataFrame, models_module) -> list[Any]`
- Function L292: `_position_count_by_snapshot(snapshot_times: list[datetime], fills: list[Any]) -> list[int]`
- Function L317: `_build_portfolio_snapshots(models_module, account_report: pd.DataFrame, fills: list[Any]) -> list[Any]`
- Function L343: `_build_dense_timeline(fills: list[Any], market_prices: Mapping[str, Sequence[tuple[datetime, float]]]) -> pd.DatetimeIndex`
- Function L353: `_dense_cash_series(sparse_snapshots: list[Any], dense_dt: pd.DatetimeIndex, initial_cash: float) -> np.ndarray`
- Function L370: `_fill_cash_delta(fill) -> float`
- Function L377: `_dense_cash_series_from_fills(fills: list[Any], dense_dts: np.ndarray, initial_cash: float) -> np.ndarray`
- Function L393: `_replay_fill_position_deltas(fills: list[Any], dense_dts: np.ndarray) -> tuple[dict[str, np.ndarray], dict[str, float]]`
- Function L417: `_aligned_market_prices(market_id: str, market_prices: Mapping[str, Sequence[tuple[datetime, float]]], dense_dts: np.ndarray, n_bars: int, fallback_price: float) -> tuple[np.ndarray, np.datetime64 | None]`
- Function L444: `_apply_resolution_cutoffs(pos_qty: dict[str, np.ndarray], pos_changes: Mapping[str, np.ndarray], market_last_ts: Mapping[str, np.datetime64 | None], dense_dts: np.ndarray) -> None`
- Function L466: `_mark_to_market(pos_qty: Mapping[str, np.ndarray], price_on_bar: Mapping[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]`
- Function L486: `_build_dense_portfolio_snapshots(models_module, sparse_snapshots: list[Any], fills: list[Any], market_prices: Mapping[str, Sequence[tuple[datetime, float]]], initial_cash: float) -> list[Any]`
- Function L559: `_normalize_market_prices(market_prices: Mapping[str, Sequence[tuple[Any, float]]] | None) -> dict[str, list[tuple[datetime, float]]]`
- Function L590: `_market_prices_from_fills(fills: list[Any]) -> dict[str, list[tuple[datetime, float]]]`
- Function L599: `_merge_market_price_sources(primary: Mapping[str, Sequence[tuple[Any, float]]] | None, secondary: Mapping[str, Sequence[tuple[Any, float]]] | None) -> dict[str, list[tuple[datetime, float]]]`
- Function L623: `_market_prices_with_fill_points(market_prices: Mapping[str, Sequence[tuple[Any, float]]] | None, fills: list[Any]) -> dict[str, list[tuple[datetime, float]]]`
- Function L634: `_build_metrics(snapshots: list[Any], initial_cash: float) -> dict[str, float]`
- Function L652: `_platform_enum(models_module, platform: str) -> Any`
- Function L659: `_mark_panel_figure(fig, panel_id: str) -> Any`
- Function L667: `_brier_unavailable_reason(*, user_probabilities: pd.Series | None, market_probabilities: pd.Series | None, outcomes: pd.Series | None) -> str | None`
- Function L686: `_build_brier_placeholder_panel(message: str) -> Any`
- Function L727: `_style_panel_legend(fig) -> None`
- Function L739: `_build_brier_timeseries_panel(brier_frame: pd.DataFrame, *, panel_id: str, axis_label: str, legend_label: str, line_color: str = '#2ca0f0') -> Any | None`
- Function L840: `_build_brier_panel(brier_frame: pd.DataFrame) -> Any | None`
- Function L849: `_build_total_brier_panel(brier_frame: pd.DataFrame) -> Any | None`
- Function L859: `_iter_layout_nodes(node) -> Any`
- Function L871: `_iter_figures(layout) -> Any`
- Function L877: `_field_name(spec) -> str | None`
- Function L888: `_filter_tool_container(container, tools_to_remove: set[Any]) -> None`
- Function L922: `_remove_tools_from_layout(layout, tools_to_remove: set[Any]) -> None`
- Function L931: `_remove_hover_tools(fig, *, layout: Any | None = None) -> set[Any]`
- Function L943: `_format_period_label(start, end) -> str`
- Function L956: `_find_figure_with_yaxis_label(layout, predicate) -> Any | None`
- Function L964: `_periodic_pnl_panel_source(target) -> tuple[dict[str, Any] | None, float | None]`
- Function L982: `_build_periodic_pnl_panel_source_data(source_data: dict[str, Any]) -> dict[str, Any] | None`
- Function L1002: `_resolve_periodic_pnl_bar_width(x_values: np.ndarray, bar_width: float | None) -> float`
- Function L1010: `_yes_price_line_renderers(target) -> list[Any]`
- Function L1032: `_remove_data_banner(layout) -> Any`
- Function L1051: `_legend_item_label_text(item) -> str`
- Function L1061: `_remove_yes_price_profitability_legend_items(fig) -> set[Any]`
- Function L1077: `_remove_yes_price_profitability_connectors(layout) -> None`
- Function L1103: `_limit_yes_price_fill_markers(layout, max_yes_price_fill_markers: int | None) -> None`
- Function L1146: `_subset_bokeh_source_values(values, indexes: np.ndarray) -> Any`
- Function L1156: `_limit_market_pnl_fill_markers(layout, max_market_pnl_fill_markers: int | None) -> None`
- Function L1200: `_standardize_periodic_pnl_panel(layout) -> None`
- Function L1248: `_relabel_market_pnl_panel(layout, axis_label: str = 'Market P&L') -> None`
- Function L1275: `_build_multi_market_brier_panel(brier_frames: Mapping[str, pd.DataFrame], *, axis_label: str = 'Cumulative Brier Advantage', color_by_market: Mapping[str, Any] | None = None) -> Any | None`
- Function L1409: `_standardize_yes_price_hover(layout) -> None`
- Function L1440: `_focus_allocation_panel(layout) -> None`
- Function L1477: `_apply_layout_overrides(layout, initial_cash: float, *, relabel_market_pnl: bool = False, max_yes_price_fill_markers: int | None = None, max_market_pnl_fill_markers: int | None = None) -> Any`
- Function L1498: `_save_layout(layout, output_path: Path, title: str) -> None`
- Function L1511: `save_legacy_backtest_layout(layout, output_path: str | Path, title: str) -> str`
- Function L1521: `build_legacy_backtest_layout(engine, output_path: str | Path, strategy_name: str, platform: str, initial_cash: float, market_prices: Mapping[str, Sequence[tuple[Any, float]]] | None = None, user_probabilities: pd.Series | None = None, market_probabilities: pd.Series | None = None, outcomes: pd.Series | None = None, legacy_repo_path: str | Path | None = None, open_browser: bool = False, max_markets: int = 30, progress: bool = False, plot_panels: Sequence[str] | None = None) -> tuple[Any, str]`

### `prediction_market_extensions/analysis/tearsheet.py`
- Imports: `__future__, collections, difflib, nautilus_trader, numbers, pandas, typing`
- Function L63: `_hex_to_rgba(hex_color: str, alpha: float = 1.0) -> str`
- Function L87: `_normalize_theme_config(theme_config: dict[str, Any]) -> dict[str, Any]`
- Function L123: `_calculate_drawdown(returns: pd.Series) -> pd.Series`
- Function L156: `_clone_config_with_charts(config, charts: list[TearsheetChart]) -> Any`
- Function L174: `_prepare_brier_advantage_data(user_probabilities: pd.Series | None = None, market_probabilities: pd.Series | None = None, outcomes: pd.Series | None = None) -> pd.DataFrame`
- Function L221: `_extract_account_equity_series(engine: BacktestEngine | None) -> tuple[pd.Series, str | None]`
- Function L271: `_build_allocation_from_fills(fills_df: pd.DataFrame) -> pd.DataFrame`
- Function L322: `register_chart(name: str, func: Callable | None = None) -> Callable | None`
- Function L384: `get_chart(name: str) -> Callable`
- Function L422: `list_charts() -> list[str]`
- Function L435: `create_tearsheet(engine: BacktestEngine, output_path: str | None = 'tearsheet.html', title: str = 'NautilusTrader Backtest Results', currency = None, config = None, benchmark_returns: pd.Series | None = None, benchmark_name: str = 'Benchmark', user_probabilities: pd.Series | None = None, market_probabilities: pd.Series | None = None, outcomes: pd.Series | None = None) -> str | None`
- Function L581: `create_tearsheet_from_stats(stats_pnls: dict[str, Any] | dict[str, dict[str, Any]], stats_returns: dict[str, Any], stats_general: dict[str, Any], returns: pd.Series, output_path: str | None = 'tearsheet.html', title: str = 'NautilusTrader Backtest Results', config = None, benchmark_returns: pd.Series | None = None, benchmark_name: str = 'Benchmark', run_info: dict[str, Any] | None = None, account_info: dict[str, Any] | None = None, user_probabilities: pd.Series | None = None, market_probabilities: pd.Series | None = None, outcomes: pd.Series | None = None, engine = None) -> str | None`
- Function L723: `create_equity_curve(returns: pd.Series, output_path: str | None = None, title: str = 'Equity Curve', benchmark_returns: pd.Series | None = None, benchmark_name: str = 'Benchmark') -> go.Figure`
- Function L807: `create_drawdown_chart(returns: pd.Series, output_path: str | None = None, title: str = 'Drawdown', theme: str = 'plotly_white') -> go.Figure`
- Function L876: `create_monthly_returns_heatmap(returns: pd.Series, output_path: str | None = None, title: str = 'Monthly Returns (%)') -> go.Figure`
- Function L965: `create_returns_distribution(returns: pd.Series, output_path: str | None = None, title: str = 'Returns Distribution') -> go.Figure`
- Function L1021: `create_rolling_sharpe(returns: pd.Series, window: int = 60, output_path: str | None = None, title: str = 'Rolling Sharpe Ratio (60-day)') -> go.Figure`
- Function L1103: `create_yearly_returns(returns: pd.Series, output_path: str | None = None, title: str = 'Yearly Returns') -> go.Figure`
- Function L1173: `create_pnl_chart(returns: pd.Series, output_path: str | None = None, title: str = 'PnL Over Time', theme: str = 'plotly_white') -> go.Figure`
- Function L1218: `create_cumulative_brier_advantage_chart(user_probabilities: pd.Series, market_probabilities: pd.Series, outcomes: pd.Series, output_path: str | None = None, title: str = 'Cumulative Brier Advantage', theme: str = 'plotly_white') -> go.Figure`
- Function L1276: `_create_tearsheet_figure(stats_returns: dict[str, Any], stats_general: dict[str, Any], stats_pnls: dict[str, Any] | dict[str, dict[str, Any]], returns: pd.Series, title: str, config = None, benchmark_returns: pd.Series | None = None, benchmark_name: str = 'Benchmark', run_info: dict[str, Any] | None = None, account_info: dict[str, Any] | None = None, brier_data: pd.DataFrame | None = None, engine = None) -> go.Figure`
- Function L1418: `_create_stats_table(stats_pnls: dict[str, Any] | dict[str, dict[str, Any]], stats_returns: dict[str, Any], stats_general: dict[str, Any], theme_config: dict[str, Any] | None = None, run_info: dict[str, Any] | None = None, account_info: dict[str, Any] | None = None) -> go.Table`
- Function L1529: `_render_run_info(fig: go.Figure, row: int, col: int, theme_config: dict[str, Any], run_info: dict[str, Any] | None = None, account_info: dict[str, Any] | None = None, **kwargs) -> None`
- Function L1594: `_render_stats_table(fig: go.Figure, row: int, col: int, stats_pnls: dict[str, dict[str, Any]], stats_returns: dict[str, Any], stats_general: dict[str, Any], theme_config: dict[str, Any], **kwargs) -> None`
- Function L1618: `_render_equity(fig: go.Figure, row: int, col: int, returns: pd.Series, theme_config: dict[str, Any], benchmark_returns: pd.Series | None = None, benchmark_name: str = 'Benchmark', **kwargs) -> None`
- Function L1676: `_render_pnl(fig: go.Figure, row: int, col: int, returns: pd.Series, theme_config: dict[str, Any], engine = None, **kwargs) -> None`
- Function L1748: `_render_allocation(fig: go.Figure, row: int, col: int, theme_config: dict[str, Any], engine = None, **kwargs) -> None`
- Function L1805: `_render_cumulative_brier_advantage(fig: go.Figure, row: int, col: int, brier_data: pd.DataFrame, theme_config: dict[str, Any], **kwargs) -> None`
- Function L1849: `_render_drawdown(fig: go.Figure, row: int, col: int, returns: pd.Series, theme_config: dict[str, Any], **kwargs) -> None`
- Function L1891: `_render_monthly_returns(fig: go.Figure, row: int, col: int, returns: pd.Series, **kwargs) -> None`
- Function L1954: `_render_distribution(fig: go.Figure, row: int, col: int, returns: pd.Series, theme_config: dict[str, Any], **kwargs) -> None`
- Function L1993: `_estimate_ticks_per_year(returns: pd.Series) -> float`
- Function L2007: `_render_rolling_sharpe(fig: go.Figure, row: int, col: int, returns: pd.Series, theme_config: dict[str, Any], window: int = 60, **kwargs) -> None`
- Function L2064: `_render_yearly_returns(fig: go.Figure, row: int, col: int, returns: pd.Series, theme_config: dict[str, Any], **kwargs) -> None`
- Function L2105: `create_bars_with_fills(engine: BacktestEngine, bar_type: BarType, title: str | None = None, theme: str = 'plotly_white', output_path: str | None = None) -> go.Figure`
- Function L2202: `_render_bars_with_fills(fig: go.Figure, row: int, col: int, engine = None, bar_type = None, title: str | None = None, theme_config: dict[str, Any] | None = None, show_rangeslider: bool = False, **kwargs) -> None`
- Function L2489: `_add_fill_scatter_trace(fig: go.Figure, fills_df: pd.DataFrame, row: int, col: int, marker_symbol: str, marker_color: str, name: str) -> None`
- Function L2538: `_register_tearsheet_chart(name: str, subplot_type: str, title: str, renderer: Callable) -> None`
- Function L2558: `_calculate_grid_layout(charts: list[TearsheetChart], custom_layout = None) -> tuple[int, int, list, list[str], list[float], float, float]`

### `prediction_market_extensions/backtesting/__init__.py`
- Imports: none

### `prediction_market_extensions/backtesting/_artifact_paths.py`
- Imports: `__future__`

### `prediction_market_extensions/backtesting/_backtest_runtime.py`
- Imports: `__future__, collections, nautilus_trader, pandas, prediction_market_extensions, typing`
- Function L36: `_record_timestamp_ns(record: object) -> int | None`
- Function L50: `_iso_from_nanos(timestamp_ns: int | None) -> str | None`
- Function L56: `_data_window_ns(data: Sequence[object]) -> tuple[int | None, int | None]`
- Function L70: `_coverage_ratio_for_window(*, start_ns: int | None, end_ns: int | None, simulated_through_ns: int | None) -> float | None`
- Function L84: `build_backtest_run_state(*, data: Sequence[object], backtest_end_ns: int | None, forced_stop: bool, requested_start_ns: int | None = None, requested_end_ns: int | None = None) -> dict[str, Any]`
- Function L130: `apply_backtest_run_state(*, result: dict[str, Any], run_state: dict[str, Any]) -> dict[str, Any]`
- Function L137: `print_backtest_result_warnings(*, results: Sequence[dict[str, Any]], market_key: str) -> None`
- Function L180: `add_engine_data_by_type(engine: BacktestEngine, records: Sequence[Any]) -> None`
- Function L190: `run_market_backtest(*, market_id: str, instrument, data: Sequence[object], strategy: Strategy, strategy_name: str, output_prefix: str, platform: str, venue: Venue, base_currency: Currency, fee_model, fill_model: Any | None = None, apply_default_fill_model: bool = True, slippage_ticks: int = 1, entry_slippage_pct: float = 0.0, exit_slippage_pct: float = 0.0, initial_cash: float, probability_window: int, price_attr: str, count_key: str, data_count: int | None = None, chart_resample_rule: str | None = None, market_key: str = 'market', return_summary_series: bool = False, book_type: BookType = BookType.L1_MBP, liquidity_consumption: bool = False, queue_position: bool = False, latency_model: Any | None = None, nautilus_log_level: str = 'INFO', requested_start_ns: int | None = None, requested_end_ns: int | None = None) -> dict[str, Any]`

### `prediction_market_extensions/backtesting/_execution_config.py`
- Imports: `__future__, dataclasses, math, nautilus_trader`
- Function L11: `_validate_milliseconds(*, name: str, value: float) -> None`
- Function L18: `_milliseconds_to_nanos(value: float) -> int`
- Class L23: `StaticLatencyConfig`
  - Method L29: `__post_init__(self) -> None`
  - Method L35: `build_latency_model(self) -> LatencyModel | None`
- Class L53: `ExecutionModelConfig`
  - Method L63: `__post_init__(self) -> None`
  - Method L88: `build_latency_model(self) -> LatencyModel | None`
  - Method L93: `build_fill_model_kwargs(self) -> dict[str, int | float]`

### `prediction_market_extensions/backtesting/_experiments.py`
- Imports: `__future__, asyncio, collections, dataclasses, datetime, pandas, prediction_market_extensions, typing`
- Function L76: `build_backtest_for_experiment(experiment: ReplayExperiment) -> PredictionMarketBacktest`
- Function L100: `build_replay_experiment(*, name: str, description: str, data: MarketDataConfig, replays: Sequence[ReplaySpec], strategy_configs: Sequence[StrategyConfigSpec] = (), strategy_factory: Callable[..., Any] | None = None, joint_strategy_factory: Callable[..., Any] | None = None, auxiliary_data_factory: Callable[..., Sequence[Any]] | None = None, initial_cash: float = 100.0, probability_window: int = 30, min_book_events: int = 0, min_price_range: float = 0.0, default_lookback_days: int | None = None, default_lookback_hours: float | None = None, default_start_time: pd.Timestamp | datetime | str | None = None, default_end_time: pd.Timestamp | datetime | str | None = None, nautilus_log_level: str = 'INFO', execution: ExecutionModelConfig | None = None, chart_resample_rule: str | None = None, return_summary_series: bool = False, report: MarketReportConfig | None = None, empty_message: str | None = None, partial_message: str | None = None, result_policy: ResultPolicy | None = None) -> ReplayExperiment`
- Function L155: `replay_experiment_from_backtest(*, backtest: PredictionMarketBacktest, description: str, report: MarketReportConfig | None = None, empty_message: str | None = None, partial_message: str | None = None, result_policy: ResultPolicy | None = None) -> ReplayExperiment`
- Function L192: `async run_replay_experiment_async(experiment: ReplayExperiment) -> list[dict[str, Any]]`
- Function L198: `_finalize_replay_results(experiment: ReplayExperiment, results: list[dict[str, Any]]) -> list[dict[str, Any]]`
- Function L229: `run_experiment(experiment: Experiment) -> list[dict[str, Any]] | ParameterSearchSummary`
- Function L247: `async run_experiment_async(experiment: Experiment) -> list[dict[str, Any]] | ParameterSearchSummary`
- Class L35: `ReplayExperiment`
- Class L63: `ParameterSearchExperiment`
  - Method L69: `optimization(self) -> ParameterSearchConfig`

### `prediction_market_extensions/backtesting/_isolated_replay_runner.py`
- Imports: `__future__, asyncio, contextlib, multiprocessing, pathlib, pickle, tempfile, traceback, typing`
- Function L13: `_single_replay_worker(backtest_kwargs: dict[str, Any], result_path: str, send_conn) -> None`
- Function L39: `run_single_replay_backtest_in_subprocess(*, backtest_kwargs: dict[str, Any]) -> dict[str, Any] | None`

### `prediction_market_extensions/backtesting/_market_data_config.py`
- Imports: `__future__, dataclasses, prediction_market_extensions`
- Function L28: `_normalize_name(value: str | MarketPlatform | MarketDataType | MarketDataVendor) -> str`
- Class L13: `MarketDataConfig`
  - Method L19: `__post_init__(self) -> None`

### `prediction_market_extensions/backtesting/_market_data_support.py`
- Imports: `prediction_market_extensions`

### `prediction_market_extensions/backtesting/_notebook_runner.py`
- Imports: `__future__, pathlib, prediction_market_extensions, typing`
- Function L18: `load_notebook_metadata(notebook_path: Path, *, project_root: Path) -> dict[str, Any] | None`
- Function L51: `execute_notebook_runner(notebook_path: Path, *, project_root: Path) -> None`
- Function L95: `_import_nbclient() -> Any`
- Function L103: `_import_nbformat() -> Any`
- Function L111: `_notebook_description(notebook) -> str`
- Function L127: `_auto_embed_html_enabled(notebook) -> bool`
- Function L133: `_remove_auto_embed_cells(notebook) -> None`
- Function L139: `_replace_auto_embed_cell(*, notebook, notebook_path: Path, html_artifacts: list[Path], nbformat) -> None`
- Function L153: `_auto_embed_cell_source(*, notebook_path: Path, html_artifacts: list[Path]) -> str`
- Function L183: `_relative_html_path(*, notebook_path: Path, html_path: Path) -> str`
- Function L187: `_write_notebook(*, notebook_path: Path, notebook, nbformat) -> None`

### `prediction_market_extensions/backtesting/_notebook_support.py`
- Imports: `__future__, collections, contextlib, dataclasses, importlib, os, pathlib, prediction_market_extensions, sys, typing`
- Function L15: `find_repo_root(start_path: str | Path | None = None) -> Path`
- Function L26: `ensure_notebook_repo_context(start_path: str | Path | None = None) -> Path`
- Function L38: `suppress_notebook_cell_output() -> Any`
- Function L76: `resolve_optimizer_config(module) -> Any`
- Function L86: `load_optimizer_handle(module_name: str) -> tuple[Any, Any]`
- Function L91: `build_research_parameter_search(optimizer_config, *, max_trials: int, holdout_top_k: int, name_suffix: str = '_research') -> Any`
- Function L102: `select_parameter_search_window(parameter_search) -> Any`
- Function L108: `snapshot_html_artifacts(output_root: Path) -> dict[Path, tuple[int, int]]`
- Function L122: `find_updated_html_artifacts(output_root: Path, before: Mapping[Path, tuple[int, int]]) -> list[Path]`
- Function L143: `partition_html_artifacts(html_artifacts: Sequence[Path]) -> tuple[list[Path], list[Path]]`
- Function L159: `_embed_html_as_iframe(html_text: str, *, height: int = 820) -> str`
- Function L170: `_display_html_suppressing_iframe_warning(html_text: str) -> None`
- Function L184: `display_html_artifacts(html_artifacts: Sequence[Path], *, repo_root: Path, iframe_height: int = 820) -> None`

### `prediction_market_extensions/backtesting/_optimizer.py`
- Imports: `__future__, collections, contextlib, csv, dataclasses, datetime, itertools, json, multiprocessing, pathlib, pickle, prediction_market_extensions, random, statistics, tempfile, traceback, types, typing, warnings`
- Function L256: `_validate_parameter_spec(name: str, spec) -> ParameterSpec`
- Function L291: `_collect_search_placeholders(value) -> set[str]`
- Function L304: `_replace_search_placeholders(value, params: Mapping[str, Any]) -> Any`
- Function L320: `_parameter_candidates(parameter_grid: Mapping[str, Sequence[Any]]) -> list[ParameterValues]`
- Function L335: `_sample_parameter_sets(config: ParameterSearchConfig) -> list[ParameterValues]`
- Function L345: `_windowed_replay(*, base_replay: ReplaySpec, window: ParameterSearchWindow) -> ReplaySpec`
- Function L361: `_windowed_replays(*, base_replays: Sequence[ReplaySpec], window: ParameterSearchWindow) -> tuple[ReplaySpec, ...]`
- Function L367: `_build_backtest(*, config: ParameterSearchConfig, trial_id: int, window: ParameterSearchWindow, params: ParameterValues) -> PredictionMarketBacktest`
- Function L388: `_coerce_parameter_values(*, config: ParameterSearchConfig, params: ParameterValues | Mapping[str, Any]) -> ParameterValues`
- Function L396: `build_parameter_search_window_backtest(*, config: ParameterSearchConfig, window: ParameterSearchWindow, params: ParameterValues | Mapping[str, Any], trial_id: int = 1, name: str | None = None, return_summary_series: bool | None = None) -> PredictionMarketBacktest`
- Function L420: `_build_backtest_kwargs(*, config: ParameterSearchConfig, trial_id: int, window: ParameterSearchWindow, params: ParameterValues) -> dict[str, Any]`
- Function L446: `_default_evaluation_worker(worker_kwargs: dict[str, Any], result_path: str, send_conn) -> None`
- Function L470: `_run_default_evaluator_in_subprocess(*, worker_kwargs: dict[str, Any]) -> object`
- Function L520: `_coerce_results(value: object) -> list[dict[str, Any]]`
- Function L535: `_series_values(series: object) -> list[float]`
- Function L550: `_max_drawdown_currency(equity_series: object) -> float`
- Function L563: `_joint_portfolio_drawdown(equity_series_list: Sequence[object]) -> float`
- Function L633: `_as_float(value: object, *, default: float = 0.0) -> float`
- Function L639: `_as_int(value: object, *, default: int = 0) -> int`
- Function L647: `_score_result(*, pnl: float, max_drawdown_currency: float, fills: int, requested_coverage_ratio: float, terminated_early: bool, initial_cash: float, min_fills_per_window: int) -> float`
- Function L665: `_evaluate_window(*, config: ParameterSearchConfig, evaluator: BacktestEvaluator | None, trial_id: int, params: ParameterValues, window: ParameterSearchWindow) -> _WindowEvaluation`
- Function L760: `_median_metric(values: Sequence[float]) -> float`
- Function L764: `_build_leaderboard_row(*, trial_id: int, params: ParameterValues, train_evaluations: Sequence[_WindowEvaluation], holdout_evaluations: Sequence[_WindowEvaluation] = ()) -> ParameterSearchLeaderboardRow`
- Function L815: `_train_row_sort_key(row: ParameterSearchLeaderboardRow) -> tuple[float, int]`
- Function L819: `_final_row_sort_key(row: ParameterSearchLeaderboardRow) -> tuple[int, float, float, int]`
- Function L827: `_params_dict(params: ParameterValues) -> dict[str, Any]`
- Function L831: `_json_safe(value) -> Any`
- Function L845: `_write_leaderboard_csv(*, rows: Sequence[ParameterSearchLeaderboardRow], output_path: Path) -> str`
- Function L908: `_summary_payload(*, config: ParameterSearchConfig, summary: ParameterSearchSummary) -> dict[str, Any]`
- Function L947: `_write_summary_json(*, config: ParameterSearchConfig, summary: ParameterSearchSummary, output_path: Path) -> str`
- Function L960: `_format_score(value: float | None) -> str`
- Function L966: `_print_top_candidates(*, rows: Sequence[ParameterSearchLeaderboardRow], holdout_enabled: bool) -> None`
- Function L988: `_evaluate_train_windows(*, config: ParameterSearchConfig, evaluator: BacktestEvaluator | None, trial_id: int, params: ParameterValues) -> tuple[_WindowEvaluation, ...]`
- Function L1003: `_run_random_trials(config: ParameterSearchConfig, *, evaluator: BacktestEvaluator | None) -> tuple[dict[int, tuple[_WindowEvaluation, ...]], dict[int, ParameterSearchLeaderboardRow], int, int]`
- Function L1028: `_suggest_params_from_trial(trial, parameter_space: Mapping[str, ParameterSpec]) -> ParameterValues`
- Function L1063: `_run_tpe_trials(config: ParameterSearchConfig, *, evaluator: BacktestEvaluator | None) -> tuple[dict[int, tuple[_WindowEvaluation, ...]], dict[int, ParameterSearchLeaderboardRow], int, int]`
- Function L1103: `run_parameter_search(config: ParameterSearchConfig, *, evaluator: BacktestEvaluator | None = None) -> ParameterSearchSummary`
- Class L52: `ParameterSearchWindow`
- Class L59: `ParameterSearchConfig`
  - Method L85: `optimizer_type(self) -> str`
  - Method L88: `__post_init__(self) -> None`
- Class L207: `ParameterSearchLeaderboardRow`
- Class L225: `ParameterSearchSummary`
  - Method L239: `optimizer_type(self) -> str`
- Class L244: `_WindowEvaluation`

### `prediction_market_extensions/backtesting/_prediction_market_backtest.py`
- Imports: `__future__, asyncio, collections, contextlib, datetime, nautilus_trader, os, pandas, prediction_market_extensions, typing, warnings`
- Function L82: `_record_ts_event(record) -> int | None`
- Function L92: `_largest_record_gap_ns(records: Sequence[Any]) -> int | None`
- Function L106: `_resolve_replay_load_workers(replay_count: int) -> int`
- Function L123: `_loader_progress_env_for_workers(workers: int) -> Iterator[None]`
- Function L128: `_warn_on_large_loaded_gap(loaded_sim: LoadedReplay) -> None`
- Function L142: `_emit_engine_status(engine: BacktestEngine, message: str) -> None`
- Function L154: `_serialize_engine_result_stats(engine_result) -> dict[str, Any]`
- Function L604: `_LoadedMarketSim(*, spec: ReplaySpec, instrument, records: Sequence[Any], count: int, count_key: str, market_key: str, market_id: str, outcome: str, realized_outcome: float | None, prices: Sequence[float], metadata: Mapping[str, Any] | None, requested_start_ns: int | None, requested_end_ns: int | None) -> LoadedReplay`
- Class L166: `PredictionMarketBacktest`
  - Method L167: `__init__(self, *, name: str, data: MarketDataConfig, replays: Sequence[ReplaySpec], strategy_configs: Sequence[StrategyConfigSpec] = (), strategy_factory: StrategyFactory | None = None, joint_strategy_factory: JointStrategyFactory | None = None, auxiliary_data_factory: AuxiliaryDataFactory | None = None, initial_cash: float, probability_window: int, min_book_events: int = 0, min_price_range: float = 0.0, default_lookback_days: int | None = None, default_lookback_hours: float | None = None, default_start_time: pd.Timestamp | datetime | str | None = None, default_end_time: pd.Timestamp | datetime | str | None = None, nautilus_log_level: str = 'INFO', execution: ExecutionModelConfig | None = None, chart_resample_rule: str | None = None, return_summary_series: bool = False) -> None`
  - Method L226: `_strategy_summary_label(self) -> str`
  - Method L235: `run(self) -> list[dict[str, Any]]`
  - Method L245: `run_backtest(self) -> list[dict[str, Any]]`
  - Method L248: `async run_async(self) -> list[dict[str, Any]]`
  - Method L327: `async run_backtest_async(self) -> list[dict[str, Any]]`
  - Method L330: `_create_artifact_builder(self) -> PredictionMarketArtifactBuilder`
  - Method L342: `_build_result(self, *, loaded_sim: LoadedReplay, fills_report: pd.DataFrame, positions_report: pd.DataFrame, market_artifacts: Mapping[str, Any] | None = None, joint_portfolio_artifacts: Mapping[str, Any] | None = None, run_state: dict[str, Any] | None = None) -> dict[str, Any]`
  - Method L361: `_build_market_artifacts(self, *, engine: BacktestEngine, loaded_sims: Sequence[LoadedReplay], fills_report: pd.DataFrame) -> dict[str, dict[str, Any]]`
  - Method L372: `_build_joint_portfolio_artifacts(self, *, engine: BacktestEngine, loaded_sims: Sequence[LoadedReplay]) -> dict[str, Any]`
  - Method L379: `_normalize_replays(self, replays: Sequence[ReplaySpec]) -> tuple[ReplaySpec, ...]`
  - Method L392: `_load_request(self) -> ReplayLoadRequest`
  - Method L402: `async _load_sims_async(self) -> list[LoadedReplay]`
  - Method L453: `_build_engine(self) -> BacktestEngine`
  - Method L488: `_build_importable_strategy_configs(self, loaded_sims: Sequence[LoadedReplay]) -> list[Any]`
  - Method L510: `_is_batch_strategy_config(self, strategy_spec: StrategyConfigSpec) -> bool`
  - Method L519: `_contains_value(self, value, target: str) -> bool`
  - Method L528: `_bind_strategy_spec(self, *, strategy_spec: StrategyConfigSpec, loaded_sim: LoadedReplay, all_instrument_ids: Sequence[InstrumentId]) -> StrategyConfigSpec`
  - Method L556: `_bind_value(self, value, *, instrument_id: InstrumentId, all_instrument_ids: Sequence[InstrumentId], metadata: Mapping[str, Any]) -> Any`

### `prediction_market_extensions/backtesting/_prediction_market_runner.py`
- Imports: `__future__, collections, datetime, nautilus_trader, pandas, prediction_market_extensions, typing`
- Function L28: `async run_single_market_backtest(*, name: str, data: MarketDataConfig, probability_window: int, strategy_factory: StrategyFactory | None = None, strategy_configs: Sequence[StrategyConfigSpec] | None = None, market_slug: str | None = None, market_ticker: str | None = None, token_index: int = 0, lookback_days: int | None = None, lookback_hours: float | None = None, min_book_events: int = 0, min_price_range: float = 0.0, initial_cash: float = 100.0, nautilus_log_level: str = 'INFO', chart_resample_rule: str | None = None, emit_summary: bool = True, return_summary_series: bool = False, report: MarketReportConfig | None = None, empty_message: str | None = None, partial_message: str | None = None, result_policy: ResultPolicy | None = None, start_time: pd.Timestamp | datetime | str | None = None, end_time: pd.Timestamp | datetime | str | None = None, execution: ExecutionModelConfig | None = None) -> dict[str, Any] | None`

### `prediction_market_extensions/backtesting/_replay_specs.py`
- Imports: `__future__, collections, dataclasses, pandas, typing`
- Class L13: `BookReplay`

### `prediction_market_extensions/backtesting/_result_policies.py`
- Imports: `__future__, collections, dataclasses, pandas, prediction_market_extensions, typing`
- Function L26: `_timestamp_ns(value: object | None) -> int | None`
- Function L50: `_timestamp_utc(value: object | None) -> pd.Timestamp | None`
- Function L75: `_coerce_float(value: object | None) -> float | None`
- Function L85: `_pairs_to_series(pairs: object) -> pd.Series`
- Function L109: `_series_to_pairs(series: pd.Series) -> list[tuple[str, float]]`
- Function L117: `_series_value_at_or_before(series: pd.Series, timestamp: pd.Timestamp) -> float | None`
- Function L126: `_fill_event_timestamp(event: Mapping[object, object]) -> pd.Timestamp | None`
- Function L134: `_binary_mark_to_market_pnl_at_settlement(*, fill_events: object, price_series: object, timestamp: pd.Timestamp) -> tuple[float, float] | None`
- Function L191: `_series_bounds(*series_values: object) -> tuple[pd.Timestamp | None, pd.Timestamp | None]`
- Function L202: `_set_series_value_at_and_after(series: pd.Series, *, timestamp: pd.Timestamp, value: float) -> pd.Series`
- Function L215: `_add_series_delta_at_and_after(series: pd.Series, *, timestamp: pd.Timestamp, delta: float) -> pd.Series`
- Function L231: `_add_settlement_delta_to_equity_like_series(series: pd.Series, *, timestamp: pd.Timestamp, settlement_delta: float, post_settlement_delta: float) -> pd.Series`
- Function L256: `_settlement_timestamp(result: Mapping[str, Any], *, settlement_observable_ns_key: str, settlement_observable_time_key: str) -> pd.Timestamp | None`
- Function L289: `_apply_settlement_to_summary_series(result: dict[str, Any], *, settlement_pnl: float, settlement_observable_ns_key: str, settlement_observable_time_key: str) -> None`
- Function L366: `append_result_warning(result: dict[str, Any], message: str) -> None`
- Function L375: `apply_repo_research_disclosures(results: Results) -> Results`
- Function L388: `apply_binary_settlement_pnl(result: dict[str, Any], *, settlement_pnl_fn: SettlementPnlFn = compute_binary_settlement_pnl, pnl_key: str = 'pnl', market_exit_pnl_key: str = 'market_exit_pnl', fill_events_key: str = 'fill_events', realized_outcome_key: str = 'realized_outcome', settlement_observable_ns_key: str = 'settlement_observable_ns', settlement_observable_time_key: str = 'settlement_observable_time', simulated_through_key: str = 'simulated_through') -> dict[str, Any]`
- Function L449: `apply_joint_portfolio_settlement_pnl(results: Results) -> Results`
- Class L384: `ResultPolicy(Protocol)`
  - Method L385: `apply(self, results: Results) -> Results | None`
- Class L521: `BinarySettlementPnlPolicy`
  - Method L528: `apply(self, results: Results) -> Results`

### `prediction_market_extensions/backtesting/_strategy_configs.py`
- Imports: `__future__, collections, copy, importlib, nautilus_trader, typing`
- Function L18: `_is_primary_instrument_sentinel(value) -> bool`
- Function L22: `_import_symbol(import_path: str) -> Any`
- Function L31: `_config_field_names(config_path: str) -> set[str]`
- Function L36: `_normalized_config(*, raw_config: Mapping[str, Any], config_path: str, instrument_id: InstrumentId) -> dict[str, Any]`
- Function L74: `build_importable_strategy_configs(*, strategy_configs: Sequence[StrategyConfigSpec], instrument_id: InstrumentId) -> list[ImportableStrategyConfig]`
- Function L100: `build_strategies_from_configs(*, strategy_configs: Sequence[StrategyConfigSpec], instrument_id: InstrumentId) -> list[Strategy]`

### `prediction_market_extensions/backtesting/_timing_harness.py`
- Imports: `__future__, collections, functools, inspect, os, typing`
- Function L15: `_timing_enabled() -> bool`
- Function L22: `install_timing_harness() -> None`
- Function L31: `timing_harness(func: Callable[P, T] | Callable[P, Awaitable[T]] | None = None) -> Callable[[Callable[P, T] | Callable[P, Awaitable[T]]], Callable[P, T] | Callable[P, Awaitable[T]]] | Callable[P, T] | Callable[P, Awaitable[T]]`

### `prediction_market_extensions/backtesting/_timing_test.py`
- Imports: `__future__, asyncio, importlib, os, pathlib, sys, threading, time, urllib`
- Function L40: `_env_flag_enabled(value: str | None, *, default: bool = True) -> bool`
- Function L46: `_loader_progress_enabled() -> bool`
- Function L50: `_loader_progress_lines_enabled() -> bool`
- Function L54: `_loader_progress_log_interval_secs() -> float`
- Function L64: `_hour_label(source: str) -> str`
- Function L73: `_filename_label(source: str) -> str`
- Function L79: `_format_bytes(size: int | None) -> str`
- Function L92: `_transfer_label(source: str) -> str`
- Function L120: `_hour_progress_key(hour) -> str`
- Function L127: `_text_progress_bar(position: float, total: int, *, width: int = 24) -> str`
- Function L135: `_progress_bar_position(*, total_hours: int, completed_hours: int, active_hours_progress: float = 0.0) -> float`
- Function L145: `_hour_label_from_hour(hour) -> str`
- Function L152: `_is_local_scan_source(source: str | None) -> bool`
- Function L161: `_transfer_progress_fraction(*, mode: str | None, source: str | None = None, downloaded_bytes: int, total_bytes: int | None, scanned_batches: int) -> float`
- Function L189: `_active_transfer_progress(downloads: dict[str, dict[str, object]]) -> tuple[int, float]`
- Function L210: `install_timing() -> None`
- Function L774: `_load_backtest_module(path_str: str) -> Any`

### `prediction_market_extensions/backtesting/data_sources/__init__.py`
- Imports: `prediction_market_extensions`

### `prediction_market_extensions/backtesting/data_sources/_common.py`
- Imports: `__future__, pathlib, re, urllib`
- Function L12: `env_value(raw: str | None) -> str | None`
- Function L19: `is_disabled(raw: str | None) -> bool`
- Function L26: `looks_like_local_path(value: str) -> bool`
- Function L39: `normalize_local_path(value: str) -> str`
- Function L43: `normalize_urlish(value: str) -> str`
- Function L55: `trim_url_suffix(url: str, suffixes: tuple[str, ...]) -> str`

### `prediction_market_extensions/backtesting/data_sources/data_types.py`
- Imports: `__future__, dataclasses`
- Class L7: `MarketDataType`
  - Method L10: `__post_init__(self) -> None`
  - Method L13: `__str__(self) -> str`

### `prediction_market_extensions/backtesting/data_sources/kalshi_native.py`
- Imports: `__future__, collections, contextlib, contextvars, dataclasses, msgspec, os, prediction_market_extensions, typing`
- Function L41: `_current_loader_config() -> KalshiNativeLoaderConfig | None`
- Function L157: `_summary_from_rest_base_url(rest_base_url: str | None) -> str`
- Function L165: `_parse_named_source(raw_source: str) -> str | None`
- Function L180: `_resolve_explicit_sources(sources: Sequence[str]) -> tuple[KalshiNativeDataSourceSelection, KalshiNativeLoaderConfig]`
- Function L208: `resolve_kalshi_native_loader_config(sources: Sequence[str] | None = None) -> tuple[KalshiNativeDataSourceSelection, KalshiNativeLoaderConfig]`
- Function L226: `resolve_kalshi_native_data_source_selection(sources: Sequence[str] | None = None) -> tuple[KalshiNativeDataSourceSelection, dict[str, str | None]]`
- Function L236: `configured_kalshi_native_data_source(*, sources: Sequence[str] | None = None) -> Iterator[KalshiNativeDataSourceSelection]`
- Class L27: `KalshiNativeDataSourceSelection`
- Class L32: `KalshiNativeLoaderConfig`
- Class L45: `RunnerKalshiDataLoader(KalshiDataLoader)`
  - Method L47: `_configured_rest_base_url(cls) -> str`
  - Method L60: `async from_market_ticker(cls, ticker: str, http_client = None) -> RunnerKalshiDataLoader`
  - Method L92: `async fetch_trades(self, min_ts: int | None = None, max_ts: int | None = None, limit: int = 1000) -> list[dict[str, Any]]`
  - Method L128: `async fetch_candlesticks(self, start_ts: int | None = None, end_ts: int | None = None, interval: str = 'Minutes1') -> list[dict[str, Any]]`

### `prediction_market_extensions/backtesting/data_sources/platforms.py`
- Imports: `__future__, dataclasses`
- Class L7: `MarketPlatform`
  - Method L10: `__post_init__(self) -> None`
  - Method L13: `__str__(self) -> str`

### `prediction_market_extensions/backtesting/data_sources/pmxt.py`
- Imports: `__future__, collections, contextlib, contextvars, dataclasses, duckdb, os, pathlib, prediction_market_extensions, pyarrow, re, threading, time, urllib`
- Function L106: `_current_loader_config() -> PMXTLoaderConfig | None`
- Function L110: `_release_arrow_memory() -> None`
- Function L117: `_resolve_positive_int_env(name: str, *, default: int) -> int`
- Function L127: `_pmxt_row_group_scan_semaphore(workers: int) -> threading.BoundedSemaphore`
- Function L143: `_bounded_pmxt_row_group_scan(workers: int) -> Iterator[None]`
- Function L1705: `_normalize_mode(value: str | None) -> str`
- Function L1719: `_env_value(name: str) -> str | None`
- Function L1727: `_env_enabled(name: str) -> bool`
- Function L1734: `_resolve_prefetch_workers_override(*, default_when_unset: int | None) -> int | None`
- Function L1744: `_resolve_source_priority_override() -> tuple[str, ...]`
- Function L1764: `_resolve_existing_remote_url() -> str | None`
- Function L1769: `_resolve_existing_remote_urls() -> tuple[str, ...]`
- Function L1785: `_resolve_required_directory(env_name: str, *, label: str) -> Path`
- Function L1798: `_strip_prefixed_local_source(source: str, *, prefixes: Sequence[str]) -> str | None`
- Function L1808: `_strip_prefixed_remote_source(source: str, *, prefixes: Sequence[str]) -> str | None`
- Function L1818: `_classify_explicit_pmxt_sources(sources: Sequence[str]) -> tuple[str | None, tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]`
- Function L1894: `_explicit_source_summary(*, ordered_sources: Sequence[str], ordered_entries: Sequence[tuple[str, str]] = ()) -> str`
- Function L1912: `resolve_pmxt_loader_config(*, sources: Sequence[str] | None = None) -> tuple[PMXTDataSourceSelection, PMXTLoaderConfig]`
- Function L2050: `_loader_config_to_env_updates(config: PMXTLoaderConfig) -> dict[str, str | None]`
- Function L2064: `resolve_pmxt_data_source_selection(*, sources: Sequence[str] | None = None) -> tuple[PMXTDataSourceSelection, dict[str, str | None]]`
- Function L2074: `configured_pmxt_data_source(*, sources: Sequence[str] | None = None) -> Iterator[PMXTDataSourceSelection]`
- Class L61: `_RawDownloadLockEntry`
- Class L87: `PMXTLoaderConfig`
  - Method L97: `remote_base_url(self) -> str | None`
- Class L152: `RunnerPolymarketPMXTDataLoader(PolymarketPMXTDataLoader)`
  - Method L159: `__init__(self, *args, **kwargs) -> None`
  - Method L178: `_row_count_from_batches(batches: Sequence[object]) -> int`
  - Method L182: `_hour_label(hour) -> str`
  - Method L188: `_pmxt_source_attrs(self, hour, extra_attrs: dict[str, object] | None = None) -> dict[str, object]`
  - Method L197: `_source_kind_for_stage(stage: str) -> str`
  - Method L201: `_source_label_for_stage(stage: str, target: str | None) -> str | None`
  - Method L211: `_resolve_raw_root(cls) -> Path | None`
  - Method L227: `_resolve_remote_base_url(cls) -> str | None`
  - Method L232: `_resolve_remote_base_urls(cls) -> tuple[str, ...]`
  - Method L252: `_archive_url_for_hour(self, hour) -> Any`
  - Method L263: `_archive_urls_for_hour(self, hour) -> Any`
  - Method L271: `_raw_path_for_hour(self, hour) -> Path | None`
  - Method L277: `_raw_path_for_hour_at_root(self, raw_root: Path, hour) -> Path`
  - Method L287: `_raw_paths_for_hour_at_root(self, raw_root: Path, hour) -> tuple[Path, ...]`
  - Method L290: `_load_local_raw_market_batches_from_root(self, raw_root: Path, hour, *, batch_size: int) -> Any`
  - Method L312: `_load_local_raw_market_batches(self, hour, *, batch_size: int) -> Any`
  - Method L322: `_load_local_archive_market_batches(self, hour, *, batch_size: int) -> Any`
  - Method L328: `_load_remote_market_batches(self, hour, *, batch_size: int) -> Any`
  - Method L349: `_archive_url_for_base_url(self, base_url: str, hour) -> str`
  - Method L352: `_load_remote_market_batches_from_base_url(self, base_url: str, hour, *, batch_size: int) -> Any`
  - Method L369: `_raw_persistence_root(self) -> Path | None`
  - Method L380: `_raw_root_can_persist(raw_root: Path) -> bool`
  - Method L393: `_emit_raw_persistence_skip(self, archive_url: str, raw_path: Path, hour) -> None`
  - Method L417: `_raw_download_lock(raw_path: Path) -> Iterator[None]`
  - Method L434: `_load_remote_market_batches_via_raw_root(self, archive_url: str, hour, *, batch_size: int) -> Any`
  - Method L482: `_download_remote_raw_to_local_root(self, archive_url: str, raw_path: Path, hour) -> Path | None`
  - Method L561: `_resolve_source_priority(cls) -> tuple[str, ...]`
  - Method L585: `_resolve_prefetch_workers(cls) -> int`
  - Method L592: `_resolve_cache_prefetch_workers(cls) -> int`
  - Method L599: `_resolve_row_group_chunk_size(cls) -> int`
  - Method L606: `_resolve_row_group_scan_workers(cls) -> int`
  - Method L612: `_load_ordered_entry_batches(self, kind: str, target: str, hour, *, batch_size: int) -> Any`
  - Method L634: `_raw_path_for_ordered_entry(self, kind: str, target: str, hour) -> Path | None`
  - Method L662: `_raw_path_for_source_stage(self, stage: str, hour) -> Path | None`
  - Method L702: `_split_shared_fixed_table(self, table: pa.Table, *, requests: Sequence[tuple[int, str, str]], batch_size: int) -> dict[int, list[pa.RecordBatch]]`
  - Method L738: `_split_shared_fixed_table_one_pass(self, table: pa.Table, *, requests: Sequence[tuple[int, str, str]], batch_size: int) -> dict[int, list[pa.RecordBatch]]`
  - Method L780: `_split_shared_payload_table(self, table: pa.Table, *, requests: Sequence[tuple[int, str, str]], batch_size: int) -> dict[int, list[pa.RecordBatch]]`
  - Method L801: `_matching_shared_raw_fixed_market_row_group_requests(self, parquet_file: pq.ParquetFile, requests: Sequence[tuple[int, str, str]]) -> list[tuple[int, tuple[tuple[int, str, str], ...]]] | None`
  - Method L850: `_load_shared_raw_fixed_market_batches_pyarrow(self, raw_path: Path, *, requests: Sequence[tuple[int, str, str]], batch_size: int) -> dict[int, list[pa.RecordBatch]] | None`
  - Method L978: `_load_shared_market_batches_from_raw_file(self, raw_path: Path, *, requests: Sequence[tuple[int, str, str]], batch_size: int) -> dict[int, list[pa.RecordBatch]] | None`
  - Method L1051: `_load_shared_market_batches_from_remote_base_url(self, base_url: str, hour, *, requests: Sequence[tuple[int, str, str]], batch_size: int) -> dict[int, list[pa.RecordBatch]] | None`
  - Method L1114: `load_shared_market_batches_for_hour(self, hour, *, requests: Sequence[tuple[int, str, str]], batch_size: int) -> dict[int, list[pa.RecordBatch] | None]`
  - Method L1239: `_write_cache_if_enabled(self, hour, table) -> None`
  - Method L1281: `_load_market_table(self, hour, *, batch_size: int) -> Any`
  - Method L1339: `_load_market_batches(self, hour, *, batch_size: int) -> Any`
  - Method L1572: `_download_to_file_with_progress(self, url: str, destination: Path) -> int | None`
  - Method L1625: `_download_payload_with_progress(self, url: str) -> bytes | None`
  - Method L1665: `_progress_total_bytes(self, source: str) -> int | None`
- Class L1700: `PMXTDataSourceSelection`

### `prediction_market_extensions/backtesting/data_sources/polymarket_native.py`
- Imports: `__future__, collections, contextlib, contextvars, dataclasses, msgspec, os, prediction_market_extensions, typing, urllib, warnings`
- Function L56: `_current_loader_config() -> PolymarketNativeLoaderConfig | None`
- Function L277: `_summary_from_overrides(*, gamma_base_url: str | None, clob_base_url: str | None, trade_api_base_url: str | None) -> str`
- Function L295: `_normalized_override(value: str | None, *, env_name: str, suffixes: tuple[str, ...]) -> str | None`
- Function L304: `_parse_named_source(raw_source: str) -> tuple[str | None, str]`
- Function L321: `_infer_env_name_from_url(url: str) -> str`
- Function L346: `_normalized_env_updates(*, gamma_base_url: str | None, clob_base_url: str | None, trade_api_base_url: str | None) -> dict[str, str | None]`
- Function L368: `_resolve_explicit_sources(sources: Sequence[str]) -> tuple[PolymarketNativeDataSourceSelection, PolymarketNativeLoaderConfig]`
- Function L407: `resolve_polymarket_native_loader_config(sources: Sequence[str] | None = None) -> tuple[PolymarketNativeDataSourceSelection, PolymarketNativeLoaderConfig]`
- Function L444: `resolve_polymarket_native_data_source_selection(sources: Sequence[str] | None = None) -> tuple[PolymarketNativeDataSourceSelection, dict[str, str | None]]`
- Function L461: `configured_polymarket_native_data_source(*, sources: Sequence[str] | None = None) -> Iterator[PolymarketNativeDataSourceSelection]`
- Class L40: `PolymarketNativeDataSourceSelection`
- Class L45: `PolymarketNativeLoaderConfig`
- Class L60: `RunnerPolymarketDataLoader(PolymarketDataLoader)`
  - Method L62: `_gamma_metadata_cache_key(cls) -> str`
  - Method L66: `_clob_metadata_cache_key(cls) -> str`
  - Method L70: `_configured_gamma_base_url(cls) -> str`
  - Method L83: `_configured_clob_base_url(cls) -> str`
  - Method L96: `_configured_trade_api_base_url(cls) -> str`
  - Method L110: `async _fetch_market_by_slug(cls, slug: str, http_client) -> dict[str, Any]`
  - Method L135: `async _fetch_market_details(cls, condition_id: str, http_client) -> dict[str, Any]`
  - Method L145: `async _fetch_market_fee_rate_bps(cls, token_id: str, http_client) -> Any`
  - Method L163: `async _fetch_event_by_slug(cls, slug: str, http_client) -> dict[str, Any]`
  - Method L178: `async fetch_events(self, active: bool = True, closed: bool = False, archived: bool = False, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]`
  - Method L201: `async fetch_markets(self, active: bool = True, closed: bool = False, archived: bool = False, limit: int = 100, offset: int = 0) -> list[dict]`
  - Method L224: `async fetch_trades(self, condition_id: str, limit: int = PolymarketDataLoader._TRADES_PAGE_LIMIT, start_ts: int | None = None, end_ts: int | None = None) -> list[dict[str, Any]]`

### `prediction_market_extensions/backtesting/data_sources/registry.py`
- Imports: `__future__, dataclasses, prediction_market_extensions, typing`
- Function L21: `_normalize_key_part(value: object) -> str`
- Function L31: `_normalize_lookup_key(*, platform: object, data_type: object, vendor: object) -> MarketDataKey`
- Function L39: `_support_from_adapter(adapter: HistoricalReplayAdapter) -> MarketDataSupport`
- Function L53: `register_market_data_support(support: MarketDataSupport) -> None`
- Function L57: `unregister_market_data_support(key: MarketDataKey) -> MarketDataSupport | None`
- Function L61: `resolve_market_data_support(*, platform: object, data_type: object, vendor: object) -> MarketDataSupport`
- Function L78: `resolve_replay_adapter(*, platform: object, data_type: object, vendor: object) -> HistoricalReplayAdapter`
- Function L86: `supported_market_data_keys() -> tuple[MarketDataKey, ...]`
- Function L90: `build_single_market_replay(*, support: MarketDataSupport, field_values: dict[str, Any]) -> ReplaySpec`
- Class L16: `MarketDataSupport`

### `prediction_market_extensions/backtesting/data_sources/replay_adapters.py`
- Imports: `__future__, asyncio, collections, contextlib, dataclasses, datetime, gc, importlib, nautilus_trader, numpy, os, pandas, pathlib, prediction_market_extensions, pyarrow, time, typing, warnings`
- Function L65: `_release_arrow_memory() -> None`
- Function L72: `_unique_tmp_path(path: Path) -> Path`
- Function L76: `_resolve_backtest_compat_symbol(name: str, default) -> Any`
- Function L86: `_loader_realized_outcome(loader) -> float | None`
- Function L94: `_normalize_timestamp(value: object | None, *, default_now: bool = False) -> pd.Timestamp`
- Function L110: `_loaded_window(records: tuple[object, ...]) -> ReplayWindow | None`
- Function L126: `_requested_window(start: pd.Timestamp, end: pd.Timestamp) -> ReplayWindow`
- Function L130: `_price_range(prices: tuple[float, ...]) -> float`
- Function L136: `_best_book_midpoint(book: OrderBook) -> float | None`
- Function L144: `_book_event_count_and_midpoints(*, instrument, records: tuple[object, ...], deltas_type: type[Any]) -> tuple[int, tuple[float, ...]]`
- Function L161: `_book_event_count(records: tuple[object, ...], *, deltas_type: type[Any]) -> int`
- Function L165: `_book_event_count_and_prices_for_request(*, instrument, records: tuple[object, ...], deltas_type: type[Any], request: ReplayLoadRequest) -> tuple[int, tuple[float, ...]]`
- Function L181: `_validate_replay_window(*, market_label: str, count_label: str, count: int, min_record_count: int, prices: tuple[float, ...], min_price_range: float) -> bool`
- Function L210: `_cache_home() -> Path`
- Function L215: `_trade_cache_path(*, loader, date: pd.Timestamp) -> Path | None`
- Function L230: `_trade_record_sort_key(record: TradeTick) -> tuple[int, int]`
- Function L234: `_serialize_trade_ticks(trades: tuple[TradeTick, ...]) -> pd.DataFrame`
- Function L249: `_trade_ticks_from_native_columns(*, loader, data: tuple[list[float], list[float], list[int], list[str], list[int], list[int]]) -> tuple[TradeTick, ...]`
- Function L271: `_trade_ticks_from_cache_frame_native(*, loader, frame: pd.DataFrame) -> tuple[TradeTick, ...]`
- Function L308: `_rounded_float64_array(values, precision: int) -> np.ndarray`
- Function L312: `_deserialize_trade_ticks(*, loader, frame: pd.DataFrame) -> tuple[TradeTick, ...]`
- Function L318: `_write_trade_cache(*, path: Path, trades: tuple[TradeTick, ...], market_label: str, day: pd.Timestamp) -> None`
- Function L370: `_trade_day_label(day: pd.Timestamp) -> str`
- Function L374: `_print_trade_progress_header(*, market_label: str, start: pd.Timestamp, end: pd.Timestamp) -> None`
- Function L391: `_trade_source_label(source: str) -> str`
- Function L414: `_print_trade_progress_line(*, day: pd.Timestamp, elapsed_secs: float, rows: int, source: str) -> None`
- Function L437: `_polymarket_ceiling_warning(caught_warnings: list[warnings.WarningMessage]) -> str | None`
- Function L445: `_disable_polymarket_trade_fallback() -> bool`
- Function L450: `_trade_days_for_window(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Timestamp, ...]`
- Function L463: `async _load_trade_ticks(loader, *, start: pd.Timestamp, end: pd.Timestamp, market_label: str) -> tuple[TradeTick, ...]`
- Function L571: `_merge_records(*, book_records: tuple[OrderBookDeltas, ...], trade_records: tuple[TradeTick, ...]) -> tuple[object, ...]`
- Function L620: `async _gather_bounded(values: Sequence[Any], *, workers: int, func: Callable[[Any], Any]) -> list[Any]`
- Function L639: `_resolve_materialize_workers(source_workers: int) -> int`
- Function L651: `_resolve_pmxt_grouped_market_chunk_size() -> int`
- Function L661: `_pmxt_cache_disabled_for_all(prepared: Sequence[_PreparedBookReplay]) -> bool`
- Function L667: `_emit_materialize_worker_event(*, vendor: str, materialize_workers: int, source_workers: int) -> None`
- Function L691: `_call_int_method(obj, name: str, default: int) -> int`
- Function L701: `_prepared_book_day_count(item: _PreparedBookReplay) -> int`
- Function L711: `_telonex_materialized_cache_complete(prepared: Sequence[_PreparedBookReplay]) -> bool`
- Function L733: `_resolve_telonex_book_workers(prepared: Sequence[_PreparedBookReplay], *, requested_workers: int) -> int`
- Class L600: `_ResolvedBookReplay`
- Class L607: `_PreparedBookReplay`
- Class L614: `_LoadedBookReplay`
- Class L784: `_BaseReplayAdapter(HistoricalReplayAdapter)`
  - Method L794: `key(self) -> ReplayAdapterKey`
  - Method L798: `replay_spec_type(self) -> type[Any]`
  - Method L801: `configure_sources(self, *, sources: tuple[str, ...] | list[str]) -> AbstractContextManager[Any]`
  - Method L807: `engine_profile(self) -> ReplayEngineProfile`
  - Method L810: `build_single_market_replay(self, *, field_values: Mapping[str, Any]) -> Any`
  - Method L822: `_resolve_book_replay_window(self, replay: BookReplay, *, request: ReplayLoadRequest, source_label: str) -> _ResolvedBookReplay`
  - Method L850: `_emit_book_replay_start(*, resolved: _ResolvedBookReplay, vendor: str) -> None`
  - Method L869: `_emit_book_replay_fetch_error(*, replay: BookReplay, vendor: str, source_label: str, error: Exception) -> None`
  - Method L883: `_build_loaded_book_replay_or_none(self, *, prepared: _PreparedBookReplay, records: tuple[object, ...], book_event_count: int | None = None, request: ReplayLoadRequest, vendor: str, source_label: str) -> LoadedReplay | None`
  - Method L942: `_build_loaded_replay(self, *, replay, instrument, records: tuple[Any, ...], count: int, count_key: str, market_key: str, market_id: str, prices: tuple[float, ...], outcome: str, realized_outcome: float | None, metadata: dict[str, Any], requested_window: ReplayWindow) -> LoadedReplay`
- Class L978: `PolymarketPMXTBookReplayAdapter(_BaseReplayAdapter)`
  - Method L979: `__init__(self) -> None`
  - Method L1006: `async load_replay(self, replay: BookReplay, *, request: ReplayLoadRequest) -> LoadedReplay | None`
  - Method L1116: `async load_replays(self, replays: Sequence[BookReplay], *, request: ReplayLoadRequest, workers: int) -> list[LoadedReplay]`
- Class L1846: `PolymarketTelonexBookReplayAdapter(_BaseReplayAdapter)`
  - Method L1847: `__init__(self) -> None`
  - Method L1877: `async load_replay(self, replay: BookReplay, *, request: ReplayLoadRequest) -> LoadedReplay | None`
  - Method L1997: `async load_replays(self, replays: Sequence[BookReplay], *, request: ReplayLoadRequest, workers: int) -> list[LoadedReplay]`

### `prediction_market_extensions/backtesting/data_sources/telonex.py`
- Imports: `__future__, collections, concurrent, contextlib, contextvars, dataclasses, datetime, duckdb, hashlib, io, nautilus_trader, numpy, os, pandas, pathlib, prediction_market_extensions, pyarrow, re, tempfile, threading, time, urllib, warnings`
- Function L139: `_raw_fixed_values(values: Sequence[object], precision: int) -> list[int]`
- Function L143: `_unique_tmp_path(path: Path) -> Path`
- Function L184: `_current_loader_config() -> TelonexLoaderConfig | None`
- Function L188: `_env_value(name: str) -> str | None`
- Function L198: `_resolve_api_workers() -> int`
- Function L208: `_resolve_file_workers() -> int`
- Function L218: `_soft_open_file_limit() -> int | None`
- Function L230: `_default_file_workers() -> int`
- Function L239: `_release_arrow_memory() -> None`
- Function L246: `_max_blob_part_bytes() -> int`
- Function L256: `_blob_scan_batch_size() -> int`
- Function L266: `_telonex_api_semaphore() -> threading.BoundedSemaphore`
- Function L275: `_telonex_file_semaphore() -> threading.BoundedSemaphore`
- Function L285: `_telonex_api_slot() -> Iterator[None]`
- Function L295: `_telonex_file_slot() -> Iterator[None]`
- Function L304: `_blob_file_cache_key(path: str) -> tuple[str, int, int]`
- Function L309: `_cached_blob_parquet_file(path: str, cache_key: tuple[str, int, int]) -> pq.ParquetFile`
- Function L329: `_parquet_stat_string(value: object) -> str`
- Function L335: `_parquet_row_group_exact_string(row_group: pq.RowGroupMetaData, column_index: int) -> str | None`
- Function L346: `_parquet_row_group_day_range(row_group: pq.RowGroupMetaData, *, timestamp_us_index: int | None, timestamp_ms_index: int | None) -> tuple[object, object] | None`
- Function L369: `_iter_days_inclusive(start_day: object, end_day: object) -> Iterator[object]`
- Function L377: `_list_struct_field_column(column: pa.ChunkedArray, field_name: str) -> pa.ChunkedArray | None`
- Function L400: `_flatten_nested_book_side_columns(table: pa.Table) -> pa.Table`
- Function L422: `_resolve_channel(channel: str | None = None) -> str`
- Function L426: `_default_cache_root() -> Path`
- Function L432: `_resolve_api_cache_root() -> Path | None`
- Function L442: `_normalize_api_base_url(value: str | None) -> str`
- Function L451: `_expand_source_vars(source: str) -> str`
- Function L461: `_classify_telonex_sources(sources: Sequence[str]) -> tuple[TelonexSourceEntry, ...]`
- Function L504: `_default_telonex_sources_from_env() -> tuple[TelonexSourceEntry, ...]`
- Function L525: `_source_summary_parts(entries: Sequence[TelonexSourceEntry]) -> list[str]`
- Function L536: `_source_summary_line(label: str, parts: Sequence[str]) -> str`
- Function L540: `_trade_source_summary_parts(entries: Sequence[TelonexSourceEntry]) -> list[str]`
- Function L563: `_source_summary(entries: Sequence[TelonexSourceEntry]) -> str`
- Function L574: `resolve_telonex_loader_config(*, sources: Sequence[str] | None = None, channel: str | None = None) -> tuple[TelonexDataSourceSelection, TelonexLoaderConfig]`
- Function L594: `resolve_telonex_data_source_selection(*, sources: Sequence[str] | None = None) -> tuple[TelonexDataSourceSelection, dict[str, str | None]]`
- Function L602: `configured_telonex_data_source(*, sources: Sequence[str] | None = None, channel: str | None = None) -> Iterator[TelonexDataSourceSelection]`
- Class L148: `TelonexSourceEntry`
- Class L155: `TelonexLoaderConfig`
- Class L161: `TelonexDataSourceSelection`
- Class L167: `_TelonexDayResult`
- Class L174: `_TelonexBlobRowGroupIndex`
- Class L613: `RunnerPolymarketTelonexBookDataLoader(PolymarketDataLoader)`
  - Method L614: `__init__(self, *args, **kwargs) -> None`
  - Method L619: `_ensure_blob_scan_caches(self) -> None`
  - Method L649: `_forget_blob_ts_cache_key(self, cache_key: tuple[str, str, str, int, str | None, int, int]) -> None`
  - Method L659: `async from_market_slug(cls, slug: str, token_index: int = 0, http_client = None) -> 'RunnerPolymarketTelonexBookDataLoader'`
  - Method L672: `_download_progress(self, url: str, downloaded_bytes: int, total_bytes: int | None, finished: bool) -> None`
  - Method L690: `_telonex_source_kind(source: str) -> str | None`
  - Method L694: `_telonex_stage_for_source(source: str) -> str`
  - Method L697: `_day_progress(self, date: str, event: str, source: str, rows: int) -> None`
  - Method L730: `_emit_cache_write_event(self, *, cache_kind: str, cache_path: Path, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, level: str, status: str, message: str, rows: int | None = None, bytes_count: int | None = None, book_events: int | None = None, trade_ticks: int | None = None, error: str | None = None) -> None`
  - Method L779: `_resolve_api_cache_root(cls) -> Path | None`
  - Method L783: `_resolve_prefetch_workers(cls) -> int`
  - Method L793: `_resolve_local_prefetch_workers(cls) -> int`
  - Method L803: `_resolve_cache_prefetch_workers(cls) -> int`
  - Method L813: `_resolve_api_worker_limit(cls) -> int`
  - Method L817: `_resolve_file_worker_limit(cls) -> int`
  - Method L820: `_config(self) -> TelonexLoaderConfig`
  - Method L827: `_date_range(start: pd.Timestamp, end: pd.Timestamp) -> list[str]`
  - Method L833: `_outcome_segments(*, token_index: int, outcome: str | None) -> tuple[str, ...]`
  - Method L840: `_local_blob_root(root: Path) -> Path | None`
  - Method L854: `_outcome_segment_candidates(*, token_index: int, outcome: str | None) -> tuple[str, ...]`
  - Method L861: `_month_partition_dirs(*, channel_dir: Path, start: pd.Timestamp, end: pd.Timestamp) -> tuple[Path, ...]`
  - Method L872: `_readable_blob_part_paths(self, *, channel_dir: Path, start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[str], bool]`
  - Method L909: `_scan_readable_blob_part_paths(self, partition_dir: Path) -> tuple[tuple[str, ...], bool]`
  - Method L949: `_manifest_blob_part_paths(self, *, store_root: Path, channel: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[str], bool] | None`
  - Method L1026: `_manifest_completed_row_count(self, *, store_root: Path, channel: str, market_slug: str, token_index: int, outcome: str | None, date: str) -> int | None`
  - Method L1065: `_manifest_empty_day_exists(self, *, store_root: Path, channel: str, market_slug: str, token_index: int, outcome: str | None, date: str) -> bool`
  - Method L1102: `_blob_row_group_index(self, path: str) -> _TelonexBlobRowGroupIndex | None`
  - Method L1126: `_build_blob_row_group_index(parquet_file: pq.ParquetFile) -> _TelonexBlobRowGroupIndex`
  - Method L1184: `_load_blob_range_row_groups(self, *, part_paths: Sequence[str], market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None | object`
  - Method L1248: `_blob_row_groups_by_part(self, *, part_paths: Sequence[str], market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> dict[str, list[int]] | object`
  - Method L1276: `_load_blob_range(self, *, store_root: Path, channel: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame | None`
  - Method L1501: `_try_load_deltas_day_from_local_blob_native(self, *, entry: TelonexSourceEntry, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[OrderBookDeltas], Mapping[str, Sequence[object]], str] | None`
  - Method L1633: `_download_api_day_to_cache(self, *, presigned_url: str, progress_url: str, cache_path: Path, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> int | None`
  - Method L1719: `_download_api_day_to_temp_file(self, *, entry: TelonexSourceEntry, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> tuple[Path, str] | None`
  - Method L1788: `_ensure_api_day_cache_path(self, *, entry: TelonexSourceEntry, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> tuple[Path | None, str]`
  - Method L1851: `_try_load_deltas_day_from_api_native(self, *, entry: TelonexSourceEntry, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[OrderBookDeltas], Mapping[str, Sequence[object]], str] | None`
  - Method L1942: `_cached_ts_ns_for_frame(self, frame: pd.DataFrame, column_name: str) -> np.ndarray | None`
  - Method L1953: `_local_consolidated_candidates(cls, *, root: Path, channel: str, market_slug: str, token_index: int, outcome: str | None) -> tuple[Path, ...]`
  - Method L1971: `_local_daily_candidates(cls, *, root: Path, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> tuple[Path, ...]`
  - Method L1990: `_local_consolidated_path(self, *, root: Path, channel: str, market_slug: str, token_index: int, outcome: str | None) -> Path | None`
  - Method L2010: `_local_path_for_day(self, *, root: Path, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> Path | None`
  - Method L2033: `_safe_read_parquet(path: Path) -> pd.DataFrame | None`
  - Method L2044: `_load_local_day(self, *, root: Path, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> pd.DataFrame | None`
  - Method L2067: `_api_url(*, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> str`
  - Method L2086: `_api_cache_path(cls, *, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> Path | None`
  - Method L2110: `_load_api_cache_day(self, *, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> pd.DataFrame | None`
  - Method L2139: `_write_api_cache_day(self, *, payload: bytes, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> None`
  - Method L2201: `_fast_api_cache_path(cls, *, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> Path | None`
  - Method L2223: `_load_fast_cache_day(self, *, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> pd.DataFrame | None`
  - Method L2252: `_write_fast_cache_day(self, *, frame: pd.DataFrame, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> None`
  - Method L2350: `_load_api_day_cached(self, *, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> tuple[pd.DataFrame | None, str]`
  - Method L2425: `_deltas_cache_path(cls, *, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, instrument_id: object, start: pd.Timestamp, end: pd.Timestamp) -> Path | None`
  - Method L2454: `has_complete_materialized_deltas_cache(self, *, start: pd.Timestamp, end: pd.Timestamp, market_slug: str, token_index: int, outcome: str | None) -> bool`
  - Method L2485: `_load_deltas_cache_day(self, *, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[OrderBookDeltas] | None, str]`
  - Method L2531: `_write_deltas_cache_day(self, *, records: Sequence[OrderBookDeltas], delta_columns: Mapping[str, Sequence[object]] | None = None, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> None`
  - Method L2610: `_trade_ticks_cache_path(cls, *, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, instrument_id: object, start: pd.Timestamp, end: pd.Timestamp) -> Path | None`
  - Method L2639: `_load_trade_ticks_cache_day(self, *, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> tuple[tuple[TradeTick, ...] | None, str]`
  - Method L2695: `_write_trade_ticks_cache_day(self, *, records: Sequence[TradeTick], channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp) -> None`
  - Method L2771: `_trade_ticks_to_cache_table(records: Sequence[TradeTick]) -> pa.Table`
  - Method L2798: `_trade_ticks_from_cache_table(self, table: pa.Table) -> tuple[TradeTick, ...]`
  - Method L2801: `_trade_ticks_from_cache_frame(self, frame: pd.DataFrame) -> tuple[TradeTick, ...]`
  - Method L2837: `_deltas_columns_to_table(data: Mapping[str, Sequence[object]]) -> pa.Table`
  - Method L2853: `_deltas_records_to_table(records: Sequence[OrderBookDeltas]) -> pa.Table`
  - Method L2889: `_numeric_table_column(table: pa.Table, name: str) -> np.ndarray`
  - Method L2892: `_deltas_records_from_table(self, table: pa.Table) -> list[OrderBookDeltas]`
  - Method L2907: `_deltas_records_from_columns(self, data: dict[str, Sequence[object]]) -> list[OrderBookDeltas]`
  - Method L2959: `_resolve_presigned_url(*, url: str, api_key: str) -> str`
  - Method L2986: `_load_api_day(self, *, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, api_key: str | None = None) -> pd.DataFrame | None`
  - Method L3079: `_column_to_ns(column: pd.Series, column_name: str) -> np.ndarray`
  - Method L3091: `_normalize_to_utc(value: pd.Timestamp) -> pd.Timestamp`
  - Method L3096: `_day_window(self, date: str, *, start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp] | None`
  - Method L3110: `_first_present_column(frame: pd.DataFrame, names: Sequence[str], *, label: str) -> str`
  - Method L3116: `_book_events_from_frame(self, frame: pd.DataFrame, *, start: pd.Timestamp, end: pd.Timestamp, include_order_book: bool = True) -> list[OrderBookDeltas]`
  - Method L3132: `_book_events_and_delta_columns_from_frame(self, frame: pd.DataFrame, *, start: pd.Timestamp, end: pd.Timestamp, include_order_book: bool = True) -> tuple[list[OrderBookDeltas], Mapping[str, Sequence[object]] | None]`
  - Method L3225: `_optional_column(frame: pd.DataFrame, names: Sequence[str]) -> str | None`
  - Method L3231: `_onchain_fill_trade_ticks_from_frame(self, frame: pd.DataFrame, *, start: pd.Timestamp, end: pd.Timestamp) -> list[TradeTick]`
  - Method L3306: `_trade_ticks_from_native_columns(self, data: tuple[list[float], list[float], list[int], list[str], list[int], list[int]]) -> list[TradeTick]`
  - Method L3335: `_rounded_float64_array(values: object, precision: int) -> np.ndarray`
  - Method L3338: `_empty_local_blob_day_frame(self, *, entry: TelonexSourceEntry, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> pd.DataFrame | None`
  - Method L3374: `_parse_telonex_trade_frame(self, frame: pd.DataFrame, *, channel: str, source: str, start: pd.Timestamp, end: pd.Timestamp, market_slug: str, token_index: int) -> tuple[TradeTick, ...] | None`
  - Method L3399: `load_telonex_onchain_fill_ticks(self, start: pd.Timestamp, end: pd.Timestamp, *, market_slug: str | None = None, token_index: int | None = None, outcome: str | None = None) -> tuple[TradeTick, ...] | None`
  - Method L3574: `_try_load_day_from_local(self, *, entry: TelonexSourceEntry, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None, start: pd.Timestamp, end: pd.Timestamp, range_cache: dict[Path, pd.DataFrame | None]) -> pd.DataFrame | None`
  - Method L3642: `_try_load_day_from_api_entry(self, *, entry: TelonexSourceEntry, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> tuple[pd.DataFrame | None, str]`
  - Method L3684: `_telonex_api_source_label(self, *, base_url: str, channel: str, date: str, market_slug: str, token_index: int, outcome: str | None) -> str`
  - Method L3703: `_load_order_book_deltas_day(self, *, date: str, config: TelonexLoaderConfig, start: pd.Timestamp, end: pd.Timestamp, market_slug: str, token_index: int, outcome: str | None, include_order_book: bool, range_cache: dict[Path, pd.DataFrame | None]) -> _TelonexDayResult`
  - Method L3880: `_iter_loaded_telonex_days(self, *, dates: list[str], config: TelonexLoaderConfig, api_entries: Sequence[TelonexSourceEntry], start: pd.Timestamp, end: pd.Timestamp, market_slug: str, token_index: int, outcome: str | None, include_order_book: bool) -> Iterator[_TelonexDayResult]`
  - Method L3953: `load_order_book_deltas(self, start: pd.Timestamp, end: pd.Timestamp, *, market_slug: str, token_index: int, outcome: str | None, include_order_book: bool = True) -> list[OrderBookDeltas]`

### `prediction_market_extensions/backtesting/data_sources/vendors.py`
- Imports: `__future__, dataclasses`
- Class L7: `MarketDataVendor`
  - Method L10: `__post_init__(self) -> None`
  - Method L13: `__str__(self) -> str`

### `prediction_market_extensions/backtesting/optimizers/__init__.py`
- Imports: `prediction_market_extensions`

### `prediction_market_extensions/backtesting/prediction_market/__init__.py`
- Imports: `prediction_market_extensions`

### `prediction_market_extensions/backtesting/prediction_market/artifacts.py`
- Imports: `__future__, collections, dataclasses, datetime, nautilus_trader, pandas, pathlib, prediction_market_extensions, typing`
- Function L31: `resolve_repo_relative_path(path_like: str | Path) -> Path`
- Class L39: `PredictionMarketArtifactBuilder`
  - Method L49: `build_result(self, *, loaded_sim: LoadedReplay, fills_report: pd.DataFrame, positions_report: pd.DataFrame, market_artifacts: Mapping[str, Any] | None = None, joint_portfolio_artifacts: Mapping[str, Any] | None = None, run_state: dict[str, Any] | None = None) -> dict[str, Any]`
  - Method L106: `build_market_artifacts(self, *, engine: BacktestEngine, loaded_sims: Sequence[LoadedReplay], fills_report: pd.DataFrame) -> dict[str, dict[str, Any]]`
  - Method L126: `build_joint_portfolio_artifacts(self, *, engine: BacktestEngine, loaded_sims: Sequence[LoadedReplay]) -> dict[str, Any]`
  - Method L173: `_build_market_artifacts_for_loaded_sim(self, *, engine: BacktestEngine, loaded_sim: LoadedReplay, fills_report: pd.DataFrame, include_portfolio_series: bool) -> dict[str, Any]`
  - Method L216: `_build_market_summary_series(self, *, engine: BacktestEngine, loaded_sim: LoadedReplay, fills_report: pd.DataFrame, market_prices, user_probabilities: pd.Series, market_probabilities: pd.Series, outcomes: pd.Series, include_portfolio_series: bool) -> dict[str, Any]`
  - Method L293: `_filter_report_rows(report: pd.DataFrame, *, instrument_id: str) -> pd.DataFrame`

### `prediction_market_extensions/backtesting/prediction_market/reporting.py`
- Imports: `__future__, collections, dataclasses, prediction_market_extensions, typing`
- Function L42: `finalize_market_results(*, name: str, results: Sequence[dict[str, object]], report: MarketReportConfig) -> None`
- Function L89: `run_reported_backtest(*, backtest: PredictionMarketBacktest, report: MarketReportConfig, empty_message: str | None = None) -> list[dict[str, object]]`
- Function L105: `_resolve_report_market_key(*, results: Sequence[dict[str, object]], configured_key: str) -> str`
- Class L32: `MarketReportConfig`

### `prediction_market_extensions/live/__init__.py`
- Imports: none

### `prediction_market_extensions/live/btc_5m.py`
- Imports: `__future__, datetime, logging, nautilus_trader, os, time, typing`
- Function L21: `floor_to_btc_5m_start(timestamp: int | None = None) -> int`
- Function L26: `btc_5m_market_slug(market_start_ts: int) -> str`
- Function L30: `upcoming_btc_5m_event_slugs(*, market_count: int = DEFAULT_MARKET_COUNT, include_current: bool = True, timestamp: int | None = None) -> list[str]`
- Function L42: `configured_btc_5m_event_slugs() -> list[str]`
- Function L58: `upcoming_btc_5m_window_label(*, timestamp: int | None = None) -> str`
- Function L66: `async load_btc_5m_instrument_ids(*, market_count: int = DEFAULT_MARKET_COUNT, include_current: bool = True, event_slugs: Sequence[str] | None = None, http_client: HttpClient | None = None, min_loaded_markets: int = 1) -> tuple[InstrumentId, ...]`

### `prediction_market_extensions/live/btc_features.py`
- Imports: `__future__, bisect, math`
- Class L9: `LiveBtcFeatureStore`
  - Method L12: `__init__(self, *, buffer_seconds: int, book_prefix: str = 'btc') -> None`
  - Method L21: `record_trade(self, *, ts_ns: int, price: float, size: float) -> None`
  - Method L35: `record_book(self, *, ts_ns: int, mid: float, spread: float, bid_size: float, ask_size: float, bid_depth: float, ask_depth: float, book_imbalance: float, microprice: float) -> None`
  - Method L85: `_prune(self, *, current_second: int) -> None`
  - Method L102: `price_at(self, ts: int) -> float`
  - Method L110: `observation_second_at(self, ts: int) -> int | None`
  - Method L118: `observation_age_seconds(self, ts: int) -> float`
  - Method L124: `book_observation_second_at(self, ts: int) -> int | None`
  - Method L132: `book_observation_age_seconds(self, ts: int) -> float`
  - Method L138: `book_features_at(self, ts: int) -> dict[str, float] | None`
  - Method L150: `momentum(self, ts: int, seconds: int) -> float`
  - Method L157: `volume(self, ts: int, seconds: int) -> float`
  - Method L168: `volatility(self, ts: int, seconds: int) -> float`

### `prediction_market_extensions/live/sandbox.py`
- Imports: `__future__, asyncio, datetime, decimal, nautilus_trader, py_clob_client_v2, traceback, typing`
- Function L52: `is_duplicate_tick_size_change(instrument: BinaryOption, ws_message: PolymarketTickSizeChange) -> bool`
- Function L59: `_parse_iso8601_ns(value: object) -> int | None`
- Function L74: `_tick_size_change_ts_ns(ws_message: PolymarketTickSizeChange) -> int | None`
- Function L81: `is_post_expiry_tick_size_change(instrument: BinaryOption, ws_message: PolymarketTickSizeChange) -> bool`
- Function L327: `build_polymarket_binance_sandbox_config(*, strategies: Sequence[ImportableStrategyConfig], event_slug_builder: str, binance_instrument_ids: frozenset[InstrumentId] | None = None, btc_instrument_ids: frozenset[InstrumentId] | None = None, starting_balance: Decimal | str = Decimal('20'), trader_id: str = 'SANDBOX-001', log_level: str = 'INFO', polymarket_update_interval_mins: int | None = None, binance_us: bool = True, risk_submit_rate: str = '20/00:00:01') -> TradingNodeConfig`
- Function L384: `build_polymarket_binance_sandbox_node(*, config: TradingNodeConfig) -> TradingNode`
- Class L96: `PublicPolymarketInstrumentProvider(PolymarketInstrumentProvider)`
  - Method L99: `async _load_from_event_slugs(self) -> None`
  - Method L141: `_prune_loaded_event_slug_instruments(self, event_slugs: Sequence[str]) -> int`
  - Method L155: `_instrument_market_slug(instrument: object) -> str`
  - Method L161: `async _load_event_instruments_with_clob_constraints(self, event: dict[str, Any]) -> int`
  - Method L185: `async _overlay_clob_trading_constraints(self, market_info: dict[str, Any]) -> None`
- Class L224: `PublicPolymarketDataClient(PolymarketDataClient)`
  - Method L227: `async _unsubscribe_order_book_deltas(self, command) -> None`
  - Method L233: `_handle_instrument_update(self, instrument: BinaryOption, ws_message: PolymarketTickSizeChange) -> None`
  - Method L258: `_apply_tick_size_change(self, instrument: BinaryOption, ws_message: PolymarketTickSizeChange, *, post_expiry: bool) -> None`
- Class L294: `PublicPolymarketLiveDataClientFactory(LiveDataClientFactory)`
  - Method L298: `create(loop: asyncio.AbstractEventLoop, name: str, config: PolymarketDataClientConfig, msgbus: MessageBus, cache: Cache, clock: LiveClock) -> PolymarketDataClient`

### `prediction_market_extensions/live/settlement.py`
- Imports: `__future__, dataclasses, decimal, json, typing, urllib`
- Function L21: `split_polymarket_instrument_id(instrument_id: object) -> tuple[str, str]`
- Function L29: `_decimal_or_none(value: object) -> Decimal | None`
- Function L36: `_token_id(token: Mapping[str, Any]) -> str`
- Function L40: `settlement_from_clob_market(market: Mapping[str, Any], *, token_id: str) -> PolymarketTokenSettlement | None`
- Function L72: `fetch_clob_market(*, condition_id: str, base_url: str = 'https://clob.polymarket.com', timeout_seconds: float = 5.0) -> dict[str, Any]`
- Function L94: `fetch_clob_token_settlement(*, condition_id: str, token_id: str, base_url: str = 'https://clob.polymarket.com', timeout_seconds: float = 5.0) -> PolymarketTokenSettlement | None`
- Class L12: `PolymarketTokenSettlement`

### `scripts/__init__.py`
- Imports: none

### `scripts/_cache_clear_guard.py`
- Imports: `__future__, argparse, pathlib, sys`
- Function L8: `_resolved(path: str) -> Path | None`
- Function L15: `_is_same_or_nested(a: Path, b: Path) -> bool`
- Function L19: `main() -> int`

### `scripts/_pmxt_raw_download.py`
- Imports: `__future__, collections, dataclasses, datetime, os, pathlib, pyarrow, re, time, tqdm, urllib`
- Function L65: `extract_archive_filenames(html: str) -> list[str]`
- Function L76: `fetch_archive_page(archive_listing_url: str, page: int, timeout_secs: int) -> str`
- Function L85: `floor_utc_hour(value: datetime) -> datetime`
- Function L89: `archive_filename_for_hour(hour: datetime) -> str`
- Function L94: `parse_archive_hour(filename: str) -> datetime`
- Function L101: `raw_relative_path(filename: str) -> Path`
- Function L106: `_parse_hour_bound(value: str | None) -> datetime | None`
- Function L124: `discover_archive_filenames(*, archive_listing_url: str = _DEFAULT_ARCHIVE_LISTING_URL, timeout_secs: int = 60, stale_pages: int = 1, max_pages: int | None = None) -> list[str]`
- Function L158: `discover_archive_hours(*, archive_listing_url: str = _DEFAULT_ARCHIVE_LISTING_URL, timeout_secs: int = 60, stale_pages: int = 1, max_pages: int | None = None) -> list[datetime]`
- Function L176: `_filter_filenames_to_window(filenames: list[str], *, start_hour: datetime | None, end_hour: datetime | None) -> list[str]`
- Function L190: `_sort_filenames_newest_first(filenames: list[str]) -> list[str]`
- Function L194: `_filename_for_hour(hour: datetime) -> str`
- Function L198: `_hour_range_filenames(*, start_hour: datetime, end_hour: datetime) -> list[str]`
- Function L207: `_archive_url(base_url: str, filename: str) -> str`
- Function L211: `_archive_sources_from_args(*, archive_sources: list[tuple[str, str]] | None, archive_listing_url: str, archive_base_url: str) -> list[ArchiveSource]`
- Function L241: `_archive_candidate_urls(*, filename: str, archive_sources: list[ArchiveSource], discovered_archive_base_urls: dict[str, str]) -> list[tuple[str, str]]`
- Function L261: `_ranked_archive_candidate_urls(*, filename: str, archive_sources: list[ArchiveSource], discovered_archive_base_urls: dict[str, str], timeout_secs: int) -> list[tuple[str, str]]`
- Function L288: `_candidate_urls(*, source: str, filename: str, archive_sources: list[ArchiveSource], discovered_archive_base_urls: dict[str, str], timeout_secs: int | None = None) -> list[tuple[str, str]]`
- Function L312: `_content_length_from_headers(headers) -> int | None`
- Function L332: `_remote_content_length(*, url: str, timeout_secs: int) -> int | None`
- Function L355: `_hour_label_for_filename(filename: str) -> str`
- Function L361: `_progress_bar_description(*, total_hours: int, completed_hours: int, active_hours: int) -> str`
- Function L374: `_format_mib(size_bytes: int) -> str`
- Function L378: `_active_status_text(*, source: str, hour_label: str, written_bytes: int, total_bytes: int | None, elapsed_secs: float) -> str`
- Function L393: `_hour_result_text(*, hour_label: str, elapsed_secs: float, detail: str, source: str) -> str`
- Function L397: `_format_download_error(exc: Exception) -> str`
- Function L405: `_source_priority_summary(*, source_sequence: list[str], archive_sources: list[ArchiveSource]) -> str`
- Function L416: `_window_label_from_filenames(filenames: list[str]) -> tuple[str | None, str | None]`
- Function L423: `_read_parquet_row_count(path: Path) -> int | None`
- Function L430: `_validate_local_raw_hours(*, destination: Path, filenames: list[str]) -> tuple[list[str], list[str], list[str], list[str]]`
- Function L442: `_pid_is_active(pid: int) -> bool`
- Function L454: `_stale_tmp_download_paths(destination: Path) -> list[Path]`
- Function L466: `_is_stale_tmp_download_path(tmp_path: Path, *, destination_exists: bool) -> bool`
- Function L482: `_cleanup_stale_tmp_downloads(destination: Path) -> int`
- Function L498: `_set_status(progress_bar: tqdm | None, *, total_hours: int, completed_hours: int, active_hours: int, status: str, force: bool = False) -> None`
- Function L531: `_write_progress_line(progress_bar: tqdm | None, line: str) -> None`
- Function L537: `_download_one(*, url: str, destination: Path, timeout_secs: int, progress_bar: tqdm | None, total_hours: int, completed_hours: int, source: str, hour_label: str) -> int`
- Function L598: `download_raw_hours(*, destination: Path, archive_listing_url: str = _DEFAULT_ARCHIVE_LISTING_URL, archive_base_url: str = _DEFAULT_ARCHIVE_BASE_URL, archive_sources: list[tuple[str, str]] | None = None, source_order: list[str] | None = None, start_time: str | None = None, end_time: str | None = None, overwrite: bool = False, timeout_secs: int = 60, show_progress: bool = True, discovery_stale_pages: int = 1, discovery_max_pages: int | None = None) -> RawDownloadSummary`
- Class L36: `ArchiveSource`
- Class L42: `RawDownloadSummary`
  - Method L61: `as_dict(self) -> dict[str, object]`

### `scripts/_profile_telonex.py`
- Imports: `__future__, concurrent, datetime, dotenv, httpx, io, os, pandas, pathlib, time, urllib`
- Function L42: `_parse_d(v) -> Any`
- Function L71: `build_url(slug: str, date: str, channel: str = CHANNEL) -> str`
- Function L76: `urllib_fetch(slug: str, date: str) -> tuple[float, float, int]`
- Function L113: `httpx_fetch(slug: str, date: str) -> tuple[float, float, int]`
- Function L125: `bench(label: str, fn, workers: int) -> Any`
- Function L166: `httpx_fetch_and_parse(slug: str, date: str) -> tuple[float, float, int]`

### `scripts/_runtime_helpers.py`
- Imports: `__future__, pathlib`
- Function L8: `resolve_runtime_root(*, config_path: Path, runtime_root: Path | None) -> Path`

### `scripts/_script_helpers.py`
- Imports: `__future__, pathlib, sys`
- Function L12: `ensure_repo_root(script_path: str | Path) -> Path`

### `scripts/_telonex_data_download.py`
- Imports: `__future__, asyncio, collections, concurrent, dataclasses, datetime, duckdb, gc, httpx, io, itertools, os, pandas, pathlib, pyarrow, queue, random, signal, socket, sys, threading, time, tqdm, urllib`
- Function L105: `_format_bytes(size: int | None) -> str`
- Function L118: `_get_rss_mb() -> float | None`
- Function L153: `_release_arrow_memory() -> None`
- Function L161: `_get_arrow_allocated_mb() -> float | None`
- Function L169: `_parse_date_bound(value: str | None) -> date | None`
- Function L187: `_date_range(start: date, end: date) -> list[date]`
- Function L196: `_api_url(*, base_url: str, channel: str, market_slug: str, outcome: str | None, outcome_id: int | None, day: date) -> str`
- Function L321: `_is_nullish_type(value_type: pa.DataType) -> bool`
- Function L329: `_normalize_telonex_table(table: pa.Table) -> pa.Table`
- Function L368: `_merge_promotable_schema(base: pa.Schema, incoming: pa.Schema) -> pa.Schema | None`
- Function L395: `_align_table_to_schema(table: pa.Table, schema: pa.Schema) -> pa.Table | None`
- Function L960: `_fetch_markets_dataset(base_url: str, timeout_secs: int, *, show_progress: bool = False) -> pd.DataFrame`
- Function L994: `_iter_days_for_market_tuple(row, *, from_idx: int, to_idx: int, window_start: date | None, window_end: date | None) -> list[date]`
- Function L1027: `_iter_jobs_from_catalog(*, markets: pd.DataFrame, channels: list[str], outcomes: list[int], window_start: date | None, window_end: date | None, status_filter: str | None, slug_filter: set[str] | None, show_progress: bool) -> _CatalogJobIterable`
- Function L1116: `_build_jobs_from_explicit(*, channels: list[str], market_slugs: list[str], outcome: str | None, outcome_id: int | None, start: date, end: date) -> list[_Job]`
- Function L1153: `_is_transient(exc: BaseException) -> bool`
- Function L1174: `_resolve_parse_worker_count(value: int | None) -> int`
- Function L1186: `_resolve_positive_int(value: int | None, *, env_name: str, default: int) -> int`
- Function L1198: `async _download_day_bytes_with_retry_async(*, client: httpx.AsyncClient, timeout_secs: int, url: str, api_key: str, stop_event: asyncio.Event, progress_cb, max_retries: int, total_timeout_secs: float | None = None) -> bytes`
- Function L1265: `async _download_day_bytes_async(*, client: httpx.AsyncClient, timeout_secs: int, url: str, api_key: str, stop_event: asyncio.Event, progress_cb) -> bytes`
- Function L1354: `_postfix_text(*, downloaded_days: int, missing: int, failed: int, bytes_total: int, active: list[_ActiveDownload]) -> str`
- Function L1385: `_prune_jobs_against_manifest(*, jobs: Iterable[_Job], store: _TelonexParquetStore, overwrite: bool, show_progress: bool, channels_hint: set[str] | None = None, recheck_empty_after_days: int | None = _DEFAULT_EMPTY_RECHECK_AFTER_DAYS) -> tuple[Iterator[_Job], list[int]]`
- Function L1447: `_run_jobs(jobs: Iterable[_Job], *, store: _TelonexParquetStore, api_key: str, base_url: str, timeout_secs: int, workers: int, show_progress: bool, total_jobs: int | None = None, commit_batch_rows: int | None = None, commit_batch_secs: float | None = None, parse_workers: int | None = None, writer_queue_items: int | None = None, pending_commit_items: int | None = None) -> tuple[int, int, int, int, int, bool, list[str]]`
- Function L2129: `download_telonex_days(*, destination: Path, market_slugs: list[str] | None = None, outcome: str | None = None, outcome_id: int | None = None, channel: str | None = None, channels: list[str] | None = None, base_url: str = _DEFAULT_API_BASE_URL, start_date: str | None = None, end_date: str | None = None, all_markets: bool = False, status_filter: str | None = None, outcomes_for_all: list[int] | None = None, overwrite: bool = False, timeout_secs: int = 60, workers: int = 16, show_progress: bool = True, db_filename: str = _MANIFEST_FILENAME, recheck_empty_after_days: int | None = _DEFAULT_EMPTY_RECHECK_AFTER_DAYS, parse_workers: int | None = None, writer_queue_items: int | None = None, pending_commit_items: int | None = None, max_days: int | None = None) -> TelonexDownloadSummary`
- Class L82: `TelonexDownloadSummary`
  - Method L101: `as_dict(self) -> dict[str, object]`
- Class L218: `_Job`
- Class L228: `_CatalogJobIterable`
  - Method L236: `__iter__(self) -> Iterator[_Job]`
- Class L274: `_DownloadResult`
- Class L287: `_FlushWriterQueue`
- Class L298: `_CancelledError(Exception)`
- Class L303: `_OpenPart`
- Class L421: `_TelonexParquetStore`
  - Method L439: `__init__(self, root: Path, *, manifest_name: str = _MANIFEST_FILENAME) -> None`
  - Method L456: `manifest_path(self) -> Path`
  - Method L460: `data_root(self) -> Path`
  - Method L464: `open_writer_count(self) -> int`
  - Method L469: `_open_part_stats_locked(self) -> tuple[int, int]`
  - Method L475: `open_part_stats(self) -> tuple[int, int]`
  - Method L480: `close(self, *, progress_label: str | None = None) -> None`
  - Method L522: `_init_schema(self) -> None`
  - Method L553: `completed_keys(self, channel: str) -> set[tuple[str, str, date]]`
  - Method L561: `empty_keys(self, channel: str, *, recheck_after_days: int | None = None) -> set[tuple[str, str, date]]`
  - Method L577: `mark_empty(self, job: _Job, *, status: str) -> None`
  - Method L580: `mark_empty_batch(self, entries: list[tuple[_Job, str]]) -> None`
  - Method L603: `_partition_dir(self, channel: str, year: int, month: int) -> Path`
  - Method L609: `_next_part_number(partition_dir: Path) -> int`
  - Method L620: `_open_part(self, key: tuple[str, int, int], schema: pa.Schema) -> _OpenPart`
  - Method L641: `_flush_open_part_locked(self, key: tuple[str, int, int]) -> None`
  - Method L703: `_append_to_partition(self, key: tuple[str, int, int], entries: list[_DownloadResult]) -> int`
  - Method L740: `_write_partition_table_locked(self, key: tuple[str, int, int], table: pa.Table, pending: list[tuple[_DownloadResult, int]]) -> int`
  - Method L806: `ingest_batch(self, results: list[_DownloadResult]) -> int`
  - Method L905: `flush_all(self) -> int`
  - Method L914: `size_bytes(self) -> int`
  - Method L927: `_remove_orphan_parts(self) -> int`
- Class L1144: `_FakeHTTPError(Exception)`
  - Method L1148: `__init__(self, code: int, message: str) -> None`
- Class L1311: `_ActiveDownload`
- Class L1318: `_ActiveRegistry`
  - Method L1319: `__init__(self) -> None`
  - Method L1324: `start(self, job: _Job) -> int`
  - Method L1336: `update(self, token: int, downloaded: int, total: int | None) -> None`
  - Method L1345: `finish(self, token: int) -> None`
  - Method L1349: `snapshot(self) -> list[_ActiveDownload]`

### `scripts/benchmark_100_replay_loading.py`
- Imports: `__future__, argparse, asyncio, collections, contextlib, dotenv, gc, json, os, pathlib, psutil, threading, time, typing`
- Function L19: `_ensure_repo_root() -> None`
- Function L26: `_set_env(name: str, value: int | str | None) -> None`
- Function L32: `_source_tuple(vendor: str, source: str) -> tuple[str, ...]`
- Function L48: `_replays_for_vendor(vendor: str, *, limit: int | None = None, offset: int = 0) -> tuple[Any, ...]`
- Function L94: `_build_backtest(*, vendor: str, source: str, source_limit: int | None = None, source_offset: int = 0) -> Any`
- Function L204: `_apply_worker_env(args: argparse.Namespace) -> None`
- Function L229: `async _load_once(args: argparse.Namespace) -> dict[str, Any]`
- Function L295: `_parser() -> argparse.ArgumentParser`
- Function L322: `main(argv: Sequence[str] | None = None) -> int`
- Class L136: `MemorySampler`
  - Method L137: `__init__(self, *, interval_secs: float, limit_gb: float | None, time_limit_secs: float | None) -> None`
  - Method L153: `start(self) -> None`
  - Method L157: `stop(self) -> None`
  - Method L161: `_sample_rss(self) -> int`
  - Method L168: `_run(self) -> None`

### `scripts/benchmark_native_loader_helpers.py`
- Imports: `__future__, argparse, collections, importlib, nautilus_trader, os, pandas, pathlib, prediction_market_extensions, statistics, time`
- Function L28: `_configure_native(enabled: bool) -> Any`
- Function L34: `_load_extension_from_path(path: Path) -> Any`
- Function L50: `_configure_native_extension(enabled: bool, extension_path: Path | None) -> Any`
- Function L67: `_time_call(fn: Callable[[], object], repeats: int) -> list[float]`
- Function L76: `_payloads(items: int) -> list[tuple[str, str]]`
- Function L107: `_public_trades(items: int) -> list[dict[str, object]]`
- Function L123: `_telonex_inputs(items: int) -> list[tuple[str, str, str, int, str | None]]`
- Function L138: `_merge_inputs(items: int) -> tuple[list[int], list[int], list[int], list[int]]`
- Function L148: `_make_telonex_loader() -> RunnerPolymarketTelonexBookDataLoader`
- Function L170: `_make_pmxt_loader() -> PolymarketPMXTDataLoader`
- Function L190: `_make_polymarket_trade_loader() -> PolymarketDataLoader`
- Function L212: `_telonex_flat_frame(items: int) -> pd.DataFrame`
- Function L234: `_telonex_nested_frame(items: int) -> pd.DataFrame`
- Function L262: `_telonex_trade_frame(items: int) -> pd.DataFrame`
- Function L275: `_bench_native_mode(*, enabled: bool, items: int, telonex_events: int, repeats: int, pmxt_rows: list[tuple[str, str]], public_trade_rows: list[dict[str, object]], telonex_rows: list[tuple[str, str, str, int, str | None]], merge_inputs: tuple[list[int], list[int], list[int], list[int]], telonex_frame: pd.DataFrame, telonex_nested_frame: pd.DataFrame, telonex_trade_frame: pd.DataFrame, native_extension_path: Path | None) -> dict[str, float | bool]`
- Function L513: `main() -> None`

### `scripts/btc_forward_collector.py`
- Imports: `__future__, argparse, asyncio, btc_short_horizon, collections, datetime, httpx, math, pathlib`
- Function L38: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L127: `build_collector(args: argparse.Namespace, *, config: BtcProjectConfig | None = None) -> BtcForwardCollector`
- Function L144: `_token_ids(args: argparse.Namespace) -> tuple[str, ...]`
- Function L162: `async collect(args: argparse.Namespace) -> None`
- Function L208: `async collect_current_market_windows(*, family: BtcMarketFamily, rule_epoch: str, raw_data_root: Path, catalog_directory: Path, flush_size: int, flush_interval_seconds: float, ingest_version: str, binance_streams: Sequence[str], binance_futures_market_streams: Sequence[str], binance_futures_public_streams: Sequence[str], rotation_poll_seconds: float, opening_handoff_delay_seconds: float, stop_event: asyncio.Event, gamma_client: GammaMarketClient | None = None, collector_factory: Callable[[Path, tuple[str, ...], int, float, str, int], BtcForwardCollector] | None = None, on_market_active: Callable[[MarketWindow, MarketWindow | None, BtcForwardCollector], None] | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None`
- Function L323: `current_market_slug(family: BtcMarketFamily, now: datetime) -> str`
- Function L330: `next_market_slug(family: BtcMarketFamily, now: datetime) -> str`
- Function L337: `_write_single_market_catalog(*, directory: Path, family: BtcMarketFamily, market: MarketWindow) -> Path`
- Function L355: `_validate_follow_current_args(args: argparse.Namespace) -> None`
- Function L364: `_follow_family(config: BtcProjectConfig, name: str) -> BtcMarketFamily`
- Function L372: `_collection_settings(args: argparse.Namespace, config: BtcProjectConfig) -> tuple[int, float, float]`
- Function L390: `_build_window_collector(raw_data_root: Path, token_ids: tuple[str, ...], flush_size: int, flush_interval_seconds: float, ingest_version: str, epoch_id_offset: int) -> BtcForwardCollector`
- Function L408: `async _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> None`
- Function L415: `async _wait_for_market_rotation(*, stop_event: asyncio.Event, worker: asyncio.Task[None], market: MarketWindow, handoff_delay_seconds: float, now: Callable[[], datetime]) -> None`
- Function L443: `_as_utc(value: datetime) -> datetime`
- Function L449: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_forward_runtime.py`
- Imports: `__future__, argparse, asyncio, btc_short_horizon, collections, dataclasses, datetime, math, pathlib, scripts, signal`
- Function L49: `_effective_market(window: _ActiveWindow, *, now: datetime) -> MarketWindow`
- Function L55: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L78: `async run_async(args: argparse.Namespace) -> None`
- Function L235: `main(argv: Sequence[str] | None = None) -> int`
- Class L43: `_ActiveWindow`

### `scripts/btc_gamma_catalog.py`
- Imports: `__future__, argparse, asyncio, btc_short_horizon, collections, pathlib`
- Function L22: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L54: `select_family(config: BtcProjectConfig, name: str) -> Any`
- Function L62: `closed_filter(value: str) -> bool | None`
- Function L66: `async discover(args: argparse.Namespace) -> int`
- Function L81: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_opening_feature_audit.py`
- Imports: `__future__, argparse, btc_short_horizon, collections, dataclasses, datetime, json, pathlib, pyarrow`
- Function L36: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L59: `run(args: argparse.Namespace) -> dict[str, object]`
- Function L144: `_observation_row(observation) -> dict[str, object]`
- Function L157: `_validate_inputs(*, lookback_seconds: int, entry_start_seconds: int, entry_end_seconds: int, cadence_milliseconds: int, market_window_seconds: int) -> None`
- Function L175: `_ns(value) -> int`
- Function L179: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_opening_market_audit.py`
- Imports: `__future__, argparse, btc_short_horizon, collections, dataclasses, datetime, json, pathlib, pyarrow`
- Function L34: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L62: `run(args: argparse.Namespace) -> dict[str, object]`
- Function L158: `_load_summary(load: ForwardBookEventLoad) -> dict[str, int]`
- Function L168: `_validate_inputs(*, lookback_seconds: int, entry_start_seconds: int, entry_end_seconds: int, cadence_milliseconds: int, market_window_seconds: int) -> None`
- Function L186: `_seconds(value: int) -> timedelta`
- Function L190: `_ns(value: datetime) -> int`
- Function L194: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_opening_mispricing_proxy.py`
- Imports: `__future__, argparse, asyncio, btc_short_horizon, collections, dataclasses, datetime, hashlib, httpx, json, numpy, pandas, pathlib, pyarrow, subprocess`
- Function L68: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L112: `async discover_closed_markets(*, start: datetime, end: datetime, rule_epoch: str) -> MarketCatalog`
- Function L129: `download_binance_archives(*, directory: Path, start: date, end: date, interval: str) -> tuple[Path, ...]`
- Function L155: `existing_binance_archives(*, directory: Path, start: date, end: date, interval: str) -> tuple[Path, ...]`
- Function L171: `run(args: argparse.Namespace) -> dict[str, object]`
- Function L519: `load_materialized_opening_proxy_dataset(*, path: Path, interval_seconds: int, snapshot_seconds: int, entry_start_seconds: int, entry_end_seconds: int, market_stride: int = 1) -> DirectionDataset`
- Function L579: `_materialized_selected_row_count(*, parquet: pq.ParquetFile, expected_offsets: tuple[int, ...], market_stride: int) -> int`
- Function L608: `_load_materialized_proxy_batches(*, parquet: pq.ParquetFile, schema: FeatureSchema, expected_offsets: tuple[int, ...], market_stride: int, vectors: np.ndarray) -> tuple[list[ResearchSample], list[float]]`
- Function L701: `_sample_group_id(sample_id: str) -> str`
- Function L708: `_candidate_configs() -> tuple[tuple[str, DirectionModelConfig], ...]`
- Function L733: `_catalog_for_study(*, catalog_path: Path | None, start: datetime, end: datetime, rule_epoch: str) -> tuple[MarketCatalog, int]`
- Function L750: `_split_config(profile: str) -> WalkForwardConfig`
- Function L763: `_prediction_metrics(predictions: Iterable[object], *, weights: np.ndarray) -> dict[str, object]`
- Function L773: `_prediction_metrics_by_regime(predictions: Iterable[object], *, dataset: DirectionDataset) -> dict[str, dict[str, object]]`
- Function L785: `_constant_probability_metrics_by_regime(predictions: Iterable[object], *, dataset: DirectionDataset, probability: float) -> dict[str, dict[str, object]]`
- Function L803: `_prediction_items_by_regime(predictions: Iterable[object], *, dataset: DirectionDataset) -> dict[object, tuple[object, ...]]`
- Function L814: `_probability_metrics(*, labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray) -> dict[str, object]`
- Function L860: `_write_dataset(*, path: Path, dataset: object) -> None`
- Function L870: `_write_predictions(*, path: Path, development: Sequence[object], holdout: Sequence[object], weights: np.ndarray) -> None`
- Function L897: `_market_slugs(*, start: datetime, end: datetime) -> Iterable[str]`
- Function L904: `_batches(values: Sequence[str], size: int) -> Iterable[tuple[str, ...]]`
- Function L909: `_dates(start: date, end: date) -> Iterable[date]`
- Function L916: `_data_hash(*, archives: Sequence[Path], catalog_path: Path) -> str`
- Function L926: `_git_provenance() -> dict[str, object]`
- Function L960: `_date(value: str) -> date`
- Function L967: `_ns(value: datetime) -> int`
- Function L971: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_opening_price_edge_proxy.py`
- Imports: `__future__, argparse, asyncio, btc_short_horizon, collections, dataclasses, httpx, json, numpy, pandas, pathlib`
- Function L38: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L53: `run(args: argparse.Namespace) -> dict[str, object]`
- Function L219: `_maximum_prediction_offset_seconds(*, predictions: pd.DataFrame, markets_by_slug: Mapping[str, MarketWindow]) -> int`
- Function L235: `_load_or_fetch_prices(*, cache_path: Path, markets: Sequence[MarketWindow], max_concurrency: int, maximum_prediction_offset_seconds: int) -> pd.DataFrame`
- Function L298: `_price_cache_coverage(path: Path) -> set[str]`
- Function L308: `async _fetch_market_price_batches(*, markets: Sequence[MarketWindow], cached_tokens: set[str], max_concurrency: int, maximum_prediction_offset_seconds: int) -> tuple[TokenPricePoint, ...]`
- Function L343: `_build_candidates(*, predictions: pd.DataFrame, prices: pd.DataFrame, markets: Sequence[MarketWindow], entry_price_buffer: float, max_price_age_seconds: int) -> tuple[pd.DataFrame, dict[str, object]]`
- Function L436: `_select_one_entry_per_market(candidates: pd.DataFrame, *, threshold: float, minimum_consecutive_signals: int = 1, maximum_signal_gap_seconds: int = 5) -> pd.DataFrame`
- Function L476: `_select_one_entry_per_market_by_regime(candidates: pd.DataFrame, *, thresholds_by_regime: Mapping[str, float], minimum_consecutive_signals: int = 1, maximum_signal_gap_seconds: int = 5) -> pd.DataFrame`
- Function L501: `_select_positive_confidence_threshold(sweep: Sequence[dict[str, object]], *, min_development_entries: int) -> dict[str, object] | None`
- Function L526: `_select_regime_thresholds(candidates: pd.DataFrame, *, requested_markets: int, min_development_entries: int, bootstrap_resamples: int, minimum_consecutive_signals: int, maximum_signal_gap_seconds: int) -> dict[str, dict[str, object]]`
- Function L585: `_sensitivity_entries(candidates: pd.DataFrame, *, threshold: float | None, thresholds_by_regime: Mapping[str, float] | None = None, additional_entry_cost: float, max_price_age_seconds: int, minimum_consecutive_signals: int = 1, maximum_signal_gap_seconds: int = 5) -> pd.DataFrame`
- Function L619: `_entry_diagnostics(entries: pd.DataFrame) -> dict[str, object]`
- Function L642: `_entry_metrics_by_regime(entries: pd.DataFrame, *, requested_markets: int, bootstrap_resamples: int) -> dict[str, dict[str, object]]`
- Function L662: `_entry_metrics(entries: pd.DataFrame, *, requested_markets: int, bootstrap_resamples: int, seed: int) -> dict[str, object]`
- Function L704: `_validate_args(args: argparse.Namespace) -> None`
- Function L721: `_validate_predictions(predictions: pd.DataFrame) -> None`
- Function L731: `_validate_market_labels(*, predictions: pd.DataFrame, markets: Sequence[MarketWindow]) -> None`
- Function L741: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_opening_proxy_shadow.py`
- Imports: `__future__, argparse, asyncio, btc_short_horizon, collections, dataclasses, datetime, json, pathlib, pyarrow`
- Function L61: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L83: `build_shadow_predictions(*, model: FittedDirectionModel, metadata: ModelArtifactMetadata, market: MarketWindow, klines: BinanceKlineHistory, observations: Sequence[OpeningMarketObservation], availability_delay: timedelta = timedelta(seconds=1)) -> tuple[tuple[OpeningMispricingPrediction, ...], tuple[BtcOpeningMispricingSignal, ...]]`
- Function L113: `shadow_bootstrap_window(*, market_start: datetime, last_decision_time: datetime, max_lookback_seconds: int, availability_delay: timedelta) -> tuple[datetime, datetime]`
- Function L132: `async run_async(args: argparse.Namespace) -> dict[str, object]`
- Function L293: `_load_summary(load: ForwardBookEventLoad) -> dict[str, int]`
- Function L303: `_shadow_regime_coverage(*, market: MarketWindow, decisions: Sequence[int], observations: Sequence[OpeningMarketObservation], predictions: Sequence[OpeningMispricingPrediction], stale_after_seconds: float) -> dict[str, dict[str, int]]`
- Function L349: `_datetime(value: str) -> datetime`
- Function L359: `_ns(value: datetime) -> int`
- Function L363: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_opening_shadow_scheduler.py`
- Imports: `__future__, argparse, asyncio, btc_short_horizon, collections, datetime, math, pathlib, scripts, signal`
- Function L39: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L58: `async run_async(args: argparse.Namespace) -> None`
- Function L285: `_write_status(*, store: RuntimeStatusStore, started_at: datetime, state: str, healthy: bool, details: dict[str, object]) -> None`
- Function L306: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_pmxt_coverage_audit.py`
- Imports: `__future__, argparse, asyncio, btc_short_horizon, collections, datetime, json, pandas, pathlib, prediction_market_extensions`
- Function L34: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L52: `run(args: argparse.Namespace) -> dict[str, object]`
- Function L84: `async _audit(*, market_slug: str, expected_up_token_id: str, expected_down_token_id: str, start_time: datetime, end_time: datetime, up_token_index: int, down_token_index: int, sources: Sequence[str]) -> dict[str, object]`
- Function L155: `_token_summary(load: PmxtBookEventLoad, *, trade_tick_count: int, gap_hours: Sequence[object]) -> dict[str, object]`
- Function L170: `_validate_token_mapping(*, expected_up_token_id: str, expected_down_token_id: str, actual_up_token_id: str | None, actual_down_token_id: str | None) -> None`
- Function L184: `_parse_utc_datetime(value: str) -> datetime`
- Function L194: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_runtime_control.py`
- Imports: `__future__, argparse, btc_short_horizon, collections, datetime, pathlib, scripts`
- Function L21: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L36: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_runtime_dashboard.py`
- Imports: `__future__, argparse, btc_short_horizon, collections, pathlib, scripts`
- Function L20: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L35: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/btc_runtime_healthcheck.py`
- Imports: `__future__, argparse, btc_short_horizon, collections, datetime, json, pathlib, scripts`
- Function L25: `parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace`
- Function L38: `main(argv: Sequence[str] | None = None) -> int`

### `scripts/generate_codebase_uml.py`
- Imports: `__future__, ast, dataclasses, datetime, pathlib`
- Function L51: `_is_included_python_file(path: Path) -> bool`
- Function L60: `_unparse(node: ast.AST | None) -> str`
- Function L69: `_format_arg(arg: ast.arg, default: ast.AST | None = None) -> str`
- Function L77: `_callable_info(node: ast.FunctionDef | ast.AsyncFunctionDef) -> CallableInfo`
- Function L103: `_imports(tree: ast.Module) -> list[str]`
- Function L113: `_module_info(path: Path) -> ModuleInfo`
- Function L138: `_mermaid_overview() -> str`
- Function L154: `_render_module(module: ModuleInfo) -> list[str]`
- Function L175: `build_document() -> str`
- Function L206: `main() -> int`
- Class L27: `CallableInfo`
- Class L36: `ClassInfo`
- Class L44: `ModuleInfo`

### `scripts/pmxt_download_raws.py`
- Imports: `__future__, argparse, json, pathlib, scripts`
- Function L17: `_parse_archive_source(value: str) -> tuple[str, str]`
- Function L30: `main() -> int`

### `scripts/run_all_backtests.py`
- Imports: `__future__, argparse, dataclasses, pathlib, subprocess, sys, time, tqdm`
- Function L33: `discover_runner_paths() -> list[Path]`
- Function L41: `_resolve_selected_runners(raw_values: list[str] | None) -> list[Path]`
- Function L72: `_run_runner(relative_path: Path, *, python_executable: str) -> RunnerResult`
- Function L83: `main() -> int`
- Class L23: `RunnerResult`
  - Method L29: `ok(self) -> bool`

### `scripts/telonex_download_data.py`
- Imports: `__future__, argparse, dotenv, json, pathlib, scripts`
- Function L23: `main() -> int`

### `strategies/__init__.py`
- Imports: `strategies`

### `strategies/_validation.py`
- Imports: `__future__, decimal, math`
- Function L7: `require_positive_decimal(name: str, value: Decimal) -> None`
- Function L12: `require_positive_int(name: str, value: int) -> None`
- Function L17: `require_nonnegative_int(name: str, value: int) -> None`
- Function L22: `require_finite_nonnegative_float(name: str, value: float) -> None`
- Function L29: `require_probability(name: str, value: float) -> None`
- Function L36: `require_percentage(name: str, value: float) -> None`
- Function L40: `require_rsi(name: str, value: float) -> None`
- Function L47: `require_less(name: str, left: float | int, other_name: str, right: float | int) -> None`

### `strategies/account_trade_replay.py`
- Imports: `__future__, collections, dataclasses, decimal, msgspec, nautilus_trader, typing`
- Function L179: `_coerce_int(value: object, *, name: str) -> int`
- Function L189: `_coerce_decimal(value: object, *, name: str) -> Decimal`
- Class L19: `_ScheduledTrade`
- Class L28: `AccountReplayTrade(msgspec.Struct)`
- Class L36: `BookAccountTradeReplayConfig(StrategyConfig)`
  - Method L42: `__post_init__(self) -> None`
- Class L49: `BookAccountTradeReplayStrategy(Strategy)`
  - Method L58: `__init__(self, config: BookAccountTradeReplayConfig) -> None`
  - Method L64: `on_start(self) -> None`
  - Method L77: `on_order_book_deltas(self, deltas) -> None`
  - Method L80: `on_trade_tick(self, trade) -> None`
  - Method L83: `on_stop(self) -> None`
  - Method L86: `on_reset(self) -> None`
  - Method L90: `_process_due(self, *, ts_ns: int) -> None`
  - Method L98: `_submit_scheduled_trade(self, scheduled: _ScheduledTrade) -> None`
  - Method L133: `_normalize_trades(trades: tuple[AccountReplayTrade, ...]) -> tuple[_ScheduledTrade, ...]`

### `strategies/binary_pair_arbitrage.py`
- Imports: `__future__, decimal, nautilus_trader, prediction_market_extensions, strategies`
- Function L41: `_as_float(value: object | None) -> float | None`
- Function L55: `_decimal_or_none(value: object | None) -> Decimal | None`
- Function L64: `_clamp_probability(value: Decimal) -> Decimal`
- Function L68: `_fee_per_share(*, price: Decimal, taker_fee: Decimal) -> Decimal`
- Class L74: `BookBinaryPairArbitrageConfig(StrategyConfig)`
  - Method L101: `__post_init__(self) -> None`
- Class L121: `BookBinaryPairArbitrageStrategy(Strategy)`
  - Method L135: `__init__(self, config: BookBinaryPairArbitrageConfig) -> None`
  - Method L145: `on_start(self) -> None`
  - Method L169: `on_order_book_deltas(self, deltas) -> None`
  - Method L181: `_best_ask_state(self, instrument_id: InstrumentId) -> tuple[float, float, float] | None`
  - Method L195: `_instrument_fee_rate(self, instrument_id: InstrumentId) -> Decimal`
  - Method L203: `_free_quote_balance(self, instrument_id: InstrumentId) -> Decimal | None`
  - Method L215: `_avg_entry_price(self, instrument_id: InstrumentId, size: Decimal) -> float | None`
  - Method L231: `_rounded_quantity(self, instrument_id: InstrumentId, size: Decimal) -> Any`
  - Method L247: `_pair_has_position(self, pair: tuple[InstrumentId, InstrumentId]) -> bool`
  - Method L250: `_evaluate_pair(self, pair: tuple[InstrumentId, InstrumentId]) -> None`
  - Method L328: `_submit_pair_entry(self, *, pair: tuple[InstrumentId, InstrumentId], quantities: list[object], visible_size: float, net_unit_cost: float, edge: float) -> None`
  - Method L367: `_event_order_is_closed(self, event) -> bool`
  - Method L382: `_mark_order_event(self, event) -> None`
  - Method L390: `on_order_filled(self, event) -> None`
  - Method L393: `on_order_rejected(self, event) -> None`
  - Method L396: `on_order_denied(self, event) -> None`
  - Method L399: `on_order_canceled(self, event) -> None`
  - Method L402: `on_order_expired(self, event) -> None`
  - Method L405: `on_stop(self) -> None`
  - Method L414: `on_reset(self) -> None`

### `strategies/breakout.py`
- Imports: `__future__, collections, decimal, math, nautilus_trader, strategies, typing`
- Class L40: `_BreakoutConfig(Protocol)`
- Class L55: `BarBreakoutConfig(StrategyConfig)`
  - Method L69: `__post_init__(self) -> None`
- Class L84: `BookBreakoutConfig(StrategyConfig)`
  - Method L97: `__post_init__(self) -> None`
- Class L112: `_BreakoutBase(LongOnlyPredictionMarketStrategy)`
  - Method L117: `__init__(self, config: _BreakoutConfig) -> None`
  - Method L124: `_append_price(self, price: float) -> None`
  - Method L128: `_breakout_buffer(self) -> float`
  - Method L131: `_mean_reversion_buffer(self) -> float`
  - Method L134: `_min_holding_periods(self) -> int`
  - Method L137: `_reentry_cooldown(self) -> int`
  - Method L140: `_requires_fresh_breakout_cross(self) -> bool`
  - Method L148: `_on_price(self, price: float, *, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L204: `on_order_filled(self, event) -> None`
  - Method L213: `on_reset(self) -> None`
- Class L221: `BarBreakoutStrategy(_BreakoutBase)`
  - Method L222: `_subscribe(self) -> None`
  - Method L225: `on_bar(self, bar: Bar) -> None`
- Class L230: `BookBreakoutStrategy(_BreakoutBase)`
  - Method L231: `_subscribe(self) -> None`
  - Method L237: `on_order_book(self, order_book) -> None`

### `strategies/core.py`
- Imports: `__future__, decimal, nautilus_trader, prediction_market_extensions, typing`
- Function L42: `_decimal_or_none(value: object) -> Decimal | None`
- Function L51: `_estimate_entry_unit_cost(*, reference_price: Decimal, taker_fee: Decimal) -> Decimal`
- Function L56: `_cap_entry_size_to_free_balance(*, desired_size: Decimal, reference_price: Decimal | None, taker_fee: Decimal, free_balance: Decimal | None) -> Decimal`
- Function L80: `_cap_entry_size_to_visible_liquidity(*, desired_size: Decimal, visible_size: Decimal | None) -> Decimal`
- Function L92: `_effective_entry_reference_price(*, reference_price: Decimal | None, visible_size: Decimal | None) -> Decimal`
- Class L37: `LongOnlyConfig(Protocol)`
- Class L105: `LongOnlyPredictionMarketStrategy(Strategy)`
  - Method L110: `__init__(self, config: LongOnlyConfig) -> None`
  - Method L125: `_warn_entry_unfillable(self, *, desired_size: Decimal, reference_price: float | None, reason: str) -> None`
  - Method L141: `_subscribe(self) -> None`
  - Method L144: `on_start(self) -> None`
  - Method L152: `on_order_book_deltas(self, deltas) -> None`
  - Method L160: `_in_position(self) -> bool`
  - Method L163: `_free_quote_balance(self) -> Decimal | None`
  - Method L173: `_remember_market_context(self, *, entry_reference_price: float | None, entry_visible_size: float | None, exit_visible_size: float | None = None) -> None`
  - Method L186: `_order_tags(self, *, intent: str, visible_size: float | None) -> list[str]`
  - Method L193: `_entry_quantity(self, *, reference_price: float | None = None, visible_size: float | None = None) -> Any`
  - Method L257: `_submit_entry(self, *, reference_price: float | None = None, visible_size: float | None = None) -> None`
  - Method L286: `_submit_exit(self) -> None`
  - Method L348: `_entry_price_with_fees(self) -> float | None`
  - Method L361: `_exit_price_after_fees(self, price: float) -> float`
  - Method L369: `_risk_exit(self, *, price: float, take_profit: float, stop_loss: float) -> bool`
  - Method L389: `_event_order_is_closed(self, event) -> bool`
  - Method L404: `on_order_filled(self, event) -> None`
  - Method L428: `on_order_rejected(self, event) -> None`
  - Method L431: `on_order_denied(self, event) -> None`
  - Method L434: `on_order_canceled(self, event) -> None`
  - Method L437: `on_order_expired(self, event) -> None`
  - Method L440: `on_stop(self) -> None`
  - Method L444: `on_reset(self) -> None`

### `strategies/deep_value.py`
- Imports: `__future__, decimal, nautilus_trader, strategies`
- Class L30: `BookDeepValueHoldConfig(StrategyConfig)`
  - Method L36: `__post_init__(self) -> None`
- Class L41: `_DeepValueHoldBase(LongOnlyPredictionMarketStrategy)`
  - Method L46: `__init__(self, config: BookDeepValueHoldConfig) -> None`
  - Method L50: `_on_price(self, price: float, *, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L78: `on_order_filled(self, event) -> None`
  - Method L83: `on_reset(self) -> None`
- Class L88: `BookDeepValueHoldStrategy(_DeepValueHoldBase)`
  - Method L89: `_subscribe(self) -> None`
  - Method L95: `on_order_book(self, order_book) -> None`

### `strategies/ema_crossover.py`
- Imports: `__future__, decimal, nautilus_trader, strategies, typing`
- Class L37: `_EMACrossoverConfig(Protocol)`
- Class L47: `BarEMACrossoverConfig(StrategyConfig)`
  - Method L57: `__post_init__(self) -> None`
- Class L67: `BookEMACrossoverConfig(StrategyConfig)`
  - Method L76: `__post_init__(self) -> None`
- Class L86: `_EMACrossoverBase(LongOnlyPredictionMarketStrategy)`
  - Method L91: `__init__(self, config: _EMACrossoverConfig) -> None`
  - Method L101: `_on_price(self, price: float, *, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L159: `on_reset(self) -> None`
- Class L167: `BarEMACrossoverStrategy(_EMACrossoverBase)`
  - Method L168: `_subscribe(self) -> None`
  - Method L171: `on_bar(self, bar: Bar) -> None`
- Class L176: `BookEMACrossoverStrategy(_EMACrossoverBase)`
  - Method L177: `_subscribe(self) -> None`
  - Method L183: `on_order_book(self, order_book) -> None`

### `strategies/final_period_momentum.py`
- Imports: `__future__, decimal, nautilus_trader, strategies, typing`
- Class L25: `_FinalPeriodMomentumConfig(Protocol)`
- Class L35: `BarFinalPeriodMomentumConfig(StrategyConfig)`
  - Method L45: `__post_init__(self) -> None`
- Class L54: `BookFinalPeriodMomentumConfig(StrategyConfig)`
  - Method L63: `__post_init__(self) -> None`
- Class L72: `_FinalPeriodMomentumBase(LongOnlyPredictionMarketStrategy)`
  - Method L77: `__init__(self, config: _FinalPeriodMomentumConfig) -> None`
  - Method L82: `_final_period_start_ns(self) -> int`
  - Method L90: `_is_in_final_period(self, ts_event_ns: int) -> bool`
  - Method L96: `_crossed_above_entry(self, previous_price: float | None, price: float) -> bool`
  - Method L101: `_on_price(self, *, price: float, ts_event_ns: int, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L142: `on_reset(self) -> None`
  - Method L147: `on_order_filled(self, event) -> None`
- Class L153: `BarFinalPeriodMomentumStrategy(_FinalPeriodMomentumBase)`
  - Method L154: `_subscribe(self) -> None`
  - Method L157: `on_bar(self, bar: Bar) -> None`
- Class L166: `BookFinalPeriodMomentumStrategy(_FinalPeriodMomentumBase)`
  - Method L167: `_subscribe(self) -> None`
  - Method L173: `on_order_book(self, order_book) -> None`

### `strategies/late_favorite_limit_hold.py`
- Imports: `__future__, decimal, nautilus_trader, strategies`
- Function L22: `_validate_late_favorite_config(*, trade_size: Decimal, entry_price: float, activation_start_time_ns: int, market_close_time_ns: int) -> None`
- Class L47: `BookLateFavoriteLimitHoldConfig(StrategyConfig)`
  - Method L54: `__post_init__(self) -> None`
- Class L63: `_LateFavoriteLimitHoldBase(LongOnlyPredictionMarketStrategy)`
  - Method L71: `__init__(self, config: BookLateFavoriteLimitHoldConfig) -> None`
  - Method L75: `_on_price(self, *, signal_price: float, order_price: float, ts_event_ns: int, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L118: `on_order_filled(self, event) -> None`
  - Method L123: `on_order_expired(self, event) -> None`
  - Method L126: `on_order_accepted(self, event) -> None`
  - Method L130: `on_stop(self) -> None`
  - Method L134: `on_reset(self) -> None`
- Class L139: `BookLateFavoriteLimitHoldStrategy(_LateFavoriteLimitHoldBase)`
  - Method L140: `_subscribe(self) -> None`
  - Method L146: `on_order_book(self, order_book) -> None`
- Class L161: `BookLateFavoriteTakerHoldConfig(StrategyConfig)`
  - Method L176: `__post_init__(self) -> None`
- Class L202: `BookLateFavoriteTakerHoldStrategy(LongOnlyPredictionMarketStrategy)`
  - Method L214: `__init__(self, config: BookLateFavoriteTakerHoldConfig) -> None`
  - Method L218: `_subscribe(self) -> None`
  - Method L224: `on_order_book(self, order_book) -> None`
  - Method L242: `_entry_window_is_open(self, ts_event_ns: int) -> bool`
  - Method L253: `_on_book_signal(self, *, bid: float, ask: float, midpoint: float, spread: float, ask_size: float | None, ts_event_ns: int) -> None`
  - Method L293: `on_order_filled(self, event) -> None`
  - Method L298: `on_stop(self) -> None`
  - Method L301: `on_reset(self) -> None`

### `strategies/mean_reversion.py`
- Imports: `__future__, collections, decimal, nautilus_trader, strategies, typing`
- Class L32: `_MeanReversionConfig(Protocol)`
- Class L42: `BarMeanReversionConfig(StrategyConfig)`
  - Method L52: `__post_init__(self) -> None`
- Class L67: `BookMeanReversionConfig(StrategyConfig)`
  - Method L76: `__post_init__(self) -> None`
- Class L91: `_MeanReversionBase(LongOnlyPredictionMarketStrategy)`
  - Method L96: `__init__(self, config: _MeanReversionConfig) -> None`
  - Method L100: `_on_price(self, price: float, *, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L141: `on_reset(self) -> None`
- Class L146: `BarMeanReversionStrategy(_MeanReversionBase)`
  - Method L147: `_subscribe(self) -> None`
  - Method L150: `on_bar(self, bar: Bar) -> None`
- Class L155: `BookMeanReversionStrategy(_MeanReversionBase)`
  - Method L156: `_subscribe(self) -> None`
  - Method L162: `on_order_book(self, order_book) -> None`

### `strategies/microprice_imbalance.py`
- Imports: `__future__, decimal, nautilus_trader, strategies, typing`
- Function L99: `_as_float(value: object | None) -> float | None`
- Function L110: `_as_int(value: object | None) -> int | None`
- Class L38: `_MicropriceImbalanceConfig(Protocol)`
- Class L59: `BookMicropriceImbalanceConfig(StrategyConfig)`
  - Method L76: `__post_init__(self) -> None`
- Class L122: `BookMicropriceImbalanceStrategy(LongOnlyPredictionMarketStrategy)`
  - Method L131: `__init__(self, config: _MicropriceImbalanceConfig) -> None`
  - Method L139: `_subscribe(self) -> None`
  - Method L145: `_expected_entry_price(self, order_book: OrderBook) -> float | None`
  - Method L155: `_depth_sum(self, levels: list[object]) -> float`
  - Method L163: `on_order_book(self, order_book: OrderBook) -> None`
  - Method L203: `_on_book_signal(self, *, bid: float, ask: float, spread: float, imbalance: float, microprice_edge: float, expected_entry_price: float | None, entry_visible_size: float | None, exit_visible_size: float | None, current_ts_ns: int | None = None) -> None`
  - Method L269: `_seconds_elapsed(self, *, start_ts_ns: int | None, current_ts_ns: int | None, seconds: float) -> bool`
  - Method L280: `_min_holding_elapsed(self, current_ts_ns: int | None) -> bool`
  - Method L287: `_reentry_cooldown_elapsed(self, current_ts_ns: int | None) -> bool`
  - Method L296: `_fill_ts_ns(self, event: object) -> int | None`
  - Method L299: `on_order_filled(self, event) -> None`
  - Method L315: `on_reset(self) -> None`

### `strategies/panic_fade.py`
- Imports: `__future__, collections, decimal, nautilus_trader, strategies, typing`
- Class L38: `_PanicFadeConfig(Protocol)`
- Class L50: `BarPanicFadeConfig(StrategyConfig)`
  - Method L62: `__post_init__(self) -> None`
- Class L73: `BookPanicFadeConfig(StrategyConfig)`
  - Method L84: `__post_init__(self) -> None`
- Class L95: `_PanicFadeBase(LongOnlyPredictionMarketStrategy)`
  - Method L100: `__init__(self, config: _PanicFadeConfig) -> None`
  - Method L105: `_on_price(self, price: float, *, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L150: `on_order_filled(self, event) -> None`
  - Method L155: `on_reset(self) -> None`
- Class L161: `BarPanicFadeStrategy(_PanicFadeBase)`
  - Method L162: `_subscribe(self) -> None`
  - Method L165: `on_bar(self, bar: Bar) -> None`
- Class L170: `BookPanicFadeStrategy(_PanicFadeBase)`
  - Method L171: `_subscribe(self) -> None`
  - Method L177: `on_order_book(self, order_book) -> None`

### `strategies/rsi_reversion.py`
- Imports: `__future__, decimal, nautilus_trader, strategies, typing`
- Class L38: `_RSIReversionConfig(Protocol)`
- Class L48: `BarRSIReversionConfig(StrategyConfig)`
  - Method L58: `__post_init__(self) -> None`
- Class L68: `BookRSIReversionConfig(StrategyConfig)`
  - Method L77: `__post_init__(self) -> None`
- Class L87: `_RSIReversionBase(LongOnlyPredictionMarketStrategy)`
  - Method L92: `__init__(self, config: _RSIReversionConfig) -> None`
  - Method L100: `_update_rsi(self, price: float) -> float | None`
  - Method L128: `_on_price(self, price: float, *, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L166: `on_reset(self) -> None`
- Class L175: `BarRSIReversionStrategy(_RSIReversionBase)`
  - Method L176: `_subscribe(self) -> None`
  - Method L179: `on_bar(self, bar: Bar) -> None`
- Class L184: `BookRSIReversionStrategy(_RSIReversionBase)`
  - Method L185: `_subscribe(self) -> None`
  - Method L191: `on_order_book(self, order_book) -> None`

### `strategies/threshold_momentum.py`
- Imports: `__future__, decimal, nautilus_trader, strategies, typing`
- Class L24: `_ThresholdMomentumConfig(Protocol)`
- Class L34: `BarThresholdMomentumConfig(StrategyConfig)`
  - Method L44: `__post_init__(self) -> None`
- Class L53: `BookThresholdMomentumConfig(StrategyConfig)`
  - Method L62: `__post_init__(self) -> None`
- Class L71: `_ThresholdMomentumBase(LongOnlyPredictionMarketStrategy)`
  - Method L76: `__init__(self, config: _ThresholdMomentumConfig) -> None`
  - Method L81: `_crossed_above_entry(self, previous_price: float | None, price: float) -> bool`
  - Method L86: `_entry_window_is_open(self, ts_event_ns: int) -> bool`
  - Method L97: `_on_price(self, *, price: float, ts_event_ns: int, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L138: `on_reset(self) -> None`
  - Method L143: `on_order_filled(self, event) -> None`
- Class L149: `BarThresholdMomentumStrategy(_ThresholdMomentumBase)`
  - Method L150: `_subscribe(self) -> None`
  - Method L153: `on_bar(self, bar: Bar) -> None`
- Class L162: `BookThresholdMomentumStrategy(_ThresholdMomentumBase)`
  - Method L163: `_subscribe(self) -> None`
  - Method L169: `on_order_book(self, order_book) -> None`

### `strategies/vwap_reversion.py`
- Imports: `__future__, collections, decimal, nautilus_trader, strategies`
- Class L30: `BookVWAPReversionConfig(StrategyConfig)`
  - Method L40: `__post_init__(self) -> None`
- Class L57: `_VWAPReversionBase(LongOnlyPredictionMarketStrategy)`
  - Method L63: `__init__(self, config: BookVWAPReversionConfig) -> None`
  - Method L69: `_append_point(self, *, price: float, size: float) -> None`
  - Method L83: `_recompute_sums(self) -> None`
  - Method L87: `_on_price_size(self, *, price: float, size: float, entry_price: float | None = None, visible_size: float | None = None, exit_visible_size: float | None = None) -> None`
  - Method L132: `on_reset(self) -> None`
- Class L139: `BookVWAPReversionStrategy(_VWAPReversionBase)`
  - Method L144: `_subscribe(self) -> None`
  - Method L150: `on_order_book(self, order_book) -> None`
