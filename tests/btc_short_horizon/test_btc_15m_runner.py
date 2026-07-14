from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from backtests.polymarket_btc_15m_opening_mispricing_maker import (
    build_backtest_from_args,
    build_run_artifacts,
    load_runner_inputs,
    parse_args,
)
from btc_short_horizon.backtest import to_opening_mispricing_signal
from btc_short_horizon.backtest.signal_io import write_opening_mispricing_signals
from btc_short_horizon.data import (
    BTC_15M_MARKET_FAMILY,
    MarketCatalog,
    MarketWindow,
    write_market_catalog,
)
from btc_short_horizon.models import OpeningMispricingPrediction


T0 = datetime(2026, 4, 13, tzinfo=UTC)
SLUG = "btc-updown-15m-1776038400"


def _signal() -> object:
    return to_opening_mispricing_signal(
        OpeningMispricingPrediction(
            market_slug=SLUG,
            model_version="opening-model-v1",
            feature_schema_hash="schema-v1",
            market_window_start_ts_ns=int(T0.timestamp() * 1_000_000_000),
            trigger_ts_ns=int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000),
            p_up=0.64,
            p_boundary_up=0.62,
            p_market_mid_up=0.60,
            data_age_seconds=0.1,
        )
    )


def _args(tmp_path: Path, *, metadata_path: Path | None = None, catalog_path: Path | None = None):
    signal_path = tmp_path / "opening-signals.parquet"
    write_opening_mispricing_signals(signal_path, (_signal(),))
    arguments = [
        "--config",
        "configs/btc_short_horizon/baseline.toml",
        "--signals",
        str(signal_path),
        "--rule-epoch",
        "chainlink-btc-usd-v1",
        "--start-time",
        T0.isoformat(),
        "--end-time",
        (T0 + timedelta(seconds=60)).isoformat(),
        "--scenario",
        "p99_pessimistic",
        "--artifact-directory",
        str(tmp_path / "artifacts"),
        "--code-revision",
        "code-revision",
        "--upstream-revision",
        "upstream-revision",
        "--model-hash",
        "a" * 64,
        "--raw-data-hash",
        "b" * 64,
        "--seed",
        "7",
    ]
    if metadata_path is not None:
        arguments.extend(("--metadata-json", str(metadata_path)))
    else:
        assert catalog_path is not None
        arguments.extend(("--market-catalog", str(catalog_path), "--market-slug", SLUG))
    return parse_args(arguments)


def test_runner_builds_one_pessimistic_dual_token_replay_from_explicit_artifacts(
    tmp_path: Path,
) -> None:
    metadata_path = tmp_path / "market.json"
    metadata_path.write_text(
        json.dumps(
            {
                "slug": SLUG,
                "conditionId": "condition",
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-token", "down-token"],
                "closed": False,
                "question": "Bitcoin Up or Down?",
            }
        ),
        encoding="utf-8",
    )

    backtest = build_backtest_from_args(_args(tmp_path, metadata_path=metadata_path))

    assert backtest.name.endswith(f"{SLUG}-p99_pessimistic")
    assert [replay.token_index for replay in backtest.replays] == [0, 1]
    assert backtest.execution.queue_position
    assert backtest.execution.latency_model.cancel_latency_ms == 100.0
    assert backtest.data.sources[0].startswith("local:")


def test_runner_artifacts_record_opening_probability_and_replay_inputs(tmp_path: Path) -> None:
    metadata_path = tmp_path / "market.json"
    metadata_path.write_text(
        json.dumps(
            {
                "slug": SLUG,
                "conditionId": "condition",
                "outcomes": ["Up", "Down"],
                "clobTokenIds": ["up-token", "down-token"],
                "closed": False,
            }
        ),
        encoding="utf-8",
    )
    args = _args(tmp_path, metadata_path=metadata_path)
    artifacts = build_run_artifacts(
        args=args,
        inputs=load_runner_inputs(args),
        order_events=(
            {
                "event_type": "submit",
                "ts_ns": 1_776_038_400_000_000_000,
                "client_order_id": "client-1",
                "price": 0.58,
                "size": 1.0,
                "p_fair": 0.64,
                "p_market": 0.60,
            },
        ),
        results=(
            {
                "instrument_id": "UP.POLYMARKET",
                "token_index": 0,
                "fills": 1,
                "pnl": 0.02,
                "outcome": "up",
                "realized_outcome": 1.0,
                "fill_events": [{"price": 0.58, "size": 1.0}],
                "price_series": [["2026-04-13T00:00:01Z", 0.58]],
                "portfolio_stats": {"total_orders": 1},
            },
        ),
    )

    assert artifacts.manifest.data_hashes["raw_data"] == "b" * 64
    assert artifacts.manifest.model_hashes == {"opening-model-v1": "a" * 64}
    assert artifacts.predictions[0]["p_up"] == 0.64
    assert artifacts.fills[0]["instrument_id"] == "UP.POLYMARKET"
    assert artifacts.orders[0]["p_fair"] == 0.64
    assert artifacts.opening_paths[0]["price"] == 0.58


def test_runner_loads_verified_market_from_catalog_and_binds_its_hash(tmp_path: Path) -> None:
    market = MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=SLUG,
        condition_id="condition",
        up_token_id="up-token",
        down_token_id="down-token",
        t0=T0,
        t1=T0 + timedelta(minutes=15),
        rule_epoch="chainlink-btc-usd-v1",
        rule_hash="a" * 64,
    )
    catalog_path = tmp_path / "catalog.json"
    write_market_catalog(
        path=catalog_path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
        collected_at=T0,
    )
    args = _args(tmp_path, catalog_path=catalog_path)

    inputs = load_runner_inputs(args)
    artifacts = build_run_artifacts(args=args, inputs=inputs, results=())

    assert inputs.market == market
    assert set(artifacts.manifest.data_hashes) == {
        "market_catalog",
        "opening_mispricing_signals",
        "raw_data",
    }
