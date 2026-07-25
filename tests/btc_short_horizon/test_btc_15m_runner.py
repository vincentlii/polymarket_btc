from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from backtests.polymarket_btc_15m_opening_mispricing_maker import (
    _validate_formal_results,
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
            feature_schema_hash="c" * 64,
            market_window_start_ts_ns=int(T0.timestamp() * 1_000_000_000),
            trigger_ts_ns=int((T0 + timedelta(seconds=3)).timestamp() * 1_000_000_000),
            p_up=0.64,
            p_boundary_up=0.62,
            p_market_mid_up=0.60,
            data_age_seconds=0.1,
        )
    )


def _args(
    tmp_path: Path,
    *,
    metadata_path: Path | None = None,
    catalog_path: Path | None = None,
    scenario: str = "p95",
):
    signal_path = tmp_path / "opening-signals.parquet"
    write_opening_mispricing_signals(signal_path, (_signal(),))
    signal_manifest_path = tmp_path / "signal-manifest.json"
    model_directory = tmp_path / "model"
    model_directory.mkdir(exist_ok=True)
    model_bytes = b"test model artifact"
    model_hash = sha256(model_bytes).hexdigest()
    (model_directory / "model.joblib").write_bytes(model_bytes)
    (model_directory / "metadata.json").write_text(
        json.dumps(
            {
                "model_id": "opening-model-v1",
                "feature_schema_hash": "c" * 64,
                "training_start_ns": 1,
                "training_end_ns": 2,
                "calibration_start_ns": 3,
                "calibration_end_ns": 4,
                "data_hash": "f" * 64,
                "code_revision": "d" * 40,
                "config": {},
                "model_sha256": model_hash,
            }
        ),
        encoding="utf-8",
    )
    signal_manifest_path.write_text(
        json.dumps(
            {
                "study_type": "opening_proxy_one_shot_shadow",
                "mode": "shadow",
                "market_slug": SLUG,
                "ingest_version": "btc-short-horizon-v11",
                "model": {
                    "model_id": "opening-model-v1",
                    "model_sha256": model_hash,
                    "feature_schema_hash": "c" * 64,
                },
                "coverage": {"predictions": 1},
                "orders_submitted": 0,
            }
        ),
        encoding="utf-8",
    )
    pmxt_coverage_path = tmp_path / "pmxt-coverage.json"
    pmxt_coverage_path.write_text(
        json.dumps(
            {
                "market_slug": SLUG,
                "window": {
                    "start": T0.isoformat(),
                    "end": (T0 + timedelta(minutes=15)).isoformat(),
                },
                "tokens": {
                    side: {
                        "token_id": token_id,
                        "source_book_event_count": 10,
                        "reconstructed_book_state_event_count": 10,
                        "trade_tick_count": 2,
                        "records_sha256": ("1" if side == "up" else "2") * 64,
                        "gap_hours": [],
                    }
                    for side, token_id in (("up", "up-token"), ("down", "down-token"))
                },
                "coverage": {
                    "books_covered": True,
                    "trade_ticks_covered": True,
                    "no_archive_gaps": True,
                    "eligible_for_joint_replay": True,
                },
            }
        ),
        encoding="utf-8",
    )
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
        scenario,
        "--artifact-directory",
        str(tmp_path / "artifacts"),
        "--code-revision",
        "d" * 40,
        "--upstream-revision",
        "e" * 40,
        "--signal-manifest",
        str(signal_manifest_path),
        "--model-directory",
        str(model_directory),
        "--pmxt-coverage",
        str(pmxt_coverage_path),
        "--seed",
        "7",
    ]
    if metadata_path is not None:
        arguments.extend(("--metadata-json", str(metadata_path)))
    else:
        assert catalog_path is not None
        arguments.extend(("--market-catalog", str(catalog_path), "--market-slug", SLUG))
    return parse_args(arguments)


def test_runner_builds_one_stressed_dual_token_replay_from_explicit_artifacts(
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

    assert backtest.name.endswith(f"{SLUG}-p95")
    assert [replay.token_index for replay in backtest.replays] == [0, 1]
    assert backtest.execution.queue_position
    assert backtest.execution.latency_model.cancel_latency_ms == 40.0
    assert not backtest.execution.maker_rebates_enabled
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
                "event_type": "plan",
                "ts_ns": 1_776_038_399_500_000_000,
                "entry_book_age_seconds_max": 0.5,
            },
            {
                "event_type": "submit",
                "ts_ns": 1_776_038_400_000_000_000,
                "client_order_id": "client-1",
                "price": 0.58,
                "size": 1.0,
                "p_fair": 0.64,
                "p_market": 0.60,
            },
            {
                "event_type": "fill",
                "ts_ns": 1_776_038_401_000_000_000,
                "client_order_id": "client-1",
                "instrument_id": "UP.POLYMARKET",
                "side": "up",
                "price": 0.58,
                "size": 1.0,
                "p_boundary": 0.62,
                "p_fair": 0.64,
                "p_market": 0.60,
                "net_edge": 0.05,
                "commission": "0.0 pUSD",
                "liquidity_side": "MAKER",
            },
            {
                "event_type": "fill_markout",
                "ts_ns": 1_776_038_402_000_000_000,
                "client_order_id": "client-1",
                "trade_id": "",
                "horizon_seconds": 1,
                "markout": -0.01,
                "midpoint": 0.57,
                "book_ts_ns": 1_776_038_401_500_000_000,
                "book_age_seconds": 0.5,
                "available": True,
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
                "fill_events": [
                    {
                        "order_id": "client-1",
                        "price": 0.58,
                        "quantity": 1.0,
                        "commission": 0.0,
                    }
                ],
                "price_series": [["2026-04-13T00:00:01Z", 0.58]],
                "portfolio_stats": {"total_orders": 1},
            },
        ),
    )

    assert len(artifacts.manifest.data_hashes["pmxt_coverage"]) == 64
    assert artifacts.manifest.model_hashes == {
        "opening-model-v1": sha256(b"test model artifact").hexdigest()
    }
    assert artifacts.manifest.code_revision == "d" * 40
    assert artifacts.manifest.upstream_revision == "e" * 40
    assert artifacts.predictions[0]["p_up"] == 0.64
    assert artifacts.fills[0]["instrument_id"] == "UP.POLYMARKET"
    submitted_order = next(
        event for event in artifacts.orders if event.get("event_type") == "submit"
    )
    assert submitted_order["p_fair"] == 0.64
    assert artifacts.opening_paths[0]["price"] == 0.58
    assert artifacts.metrics["total_filled_shares"] == 1.0
    assert artifacts.metrics["average_entry_price"] == 0.58
    assert artifacts.metrics["gross_expected_edge_per_share"] == pytest.approx(0.06)
    assert artifacts.metrics["buffered_net_expected_edge_per_share"] == pytest.approx(0.05)
    assert artifacts.metrics["submitted_order_count"] == 1
    assert artifacts.metrics["placement_plan_count"] == 1
    assert artifacts.metrics["entry_book_age_seconds_max"] == pytest.approx(0.5)
    assert artifacts.metrics["fill_event_count"] == 1
    assert artifacts.metrics["fill_markout_event_count"] == 1
    assert artifacts.data_quality["execution_anomaly_free"] is True
    assert artifacts.data_quality["pmxt_coverage"]["eligible_for_joint_replay"] is True
    assert artifacts.data_quality["pmxt_tokens"]["up"]["trade_tick_count"] == 2
    assert artifacts.fills[0]["markout_1s"] == pytest.approx(-0.01)
    assert artifacts.fills[0]["markout_1s_book_age_seconds"] == pytest.approx(0.5)
    assert artifacts.fills[0]["markout_1s_available"] is True
    assert artifacts.metrics["markout_1s_per_share"] == pytest.approx(-0.01)
    assert artifacts.metrics["markout_1s_book_age_seconds_max"] == pytest.approx(0.5)


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
        "config",
        "market_catalog",
        "opening_mispricing_signals",
        "signal_manifest",
        "model_metadata",
        "pmxt_coverage",
    }


def test_runner_rejects_unbound_signal_or_ineligible_pmxt_evidence(tmp_path: Path) -> None:
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
    signal_manifest = json.loads(args.signal_manifest.read_text(encoding="utf-8"))
    signal_manifest["model"]["model_id"] = "other-model"
    args.signal_manifest.write_text(json.dumps(signal_manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="model_id"):
        load_runner_inputs(args)

    args = _args(tmp_path, metadata_path=metadata_path)
    pmxt_coverage = json.loads(args.pmxt_coverage.read_text(encoding="utf-8"))
    pmxt_coverage["coverage"]["eligible_for_joint_replay"] = False
    args.pmxt_coverage.write_text(json.dumps(pmxt_coverage), encoding="utf-8")

    with pytest.raises(ValueError, match="eligible_for_joint_replay"):
        load_runner_inputs(args)

    args = _args(tmp_path, metadata_path=metadata_path)
    (args.model_directory / "model.joblib").write_bytes(b"tampered model artifact")

    with pytest.raises(ValueError, match="model artifact SHA-256"):
        load_runner_inputs(args)


def test_runner_rejects_truthy_shadow_order_flag_and_symbolic_revision(tmp_path: Path) -> None:
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
    signal_manifest = json.loads(args.signal_manifest.read_text(encoding="utf-8"))
    signal_manifest["orders_submitted"] = False
    args.signal_manifest.write_text(json.dumps(signal_manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="zero-order Shadow"):
        load_runner_inputs(args)

    args = _args(tmp_path, metadata_path=metadata_path)
    args.code_revision = "HEAD"
    with pytest.raises(ValueError, match="full hexadecimal Git revision"):
        build_run_artifacts(args=args, inputs=load_runner_inputs(args), results=())


def test_formal_artifacts_require_settlement_and_maker_only_fill_audit() -> None:
    results = (
        {
            "instrument_id": "UP.POLYMARKET",
            "fills": 1,
            "fill_events": [
                {
                    "order_id": "client-1",
                    "quantity": 3.0,
                    "price": 0.5,
                    "commission": 0.0,
                }
            ],
            "token_index": 0,
            "pnl": 1.5,
            "realized_outcome": 1.0,
            "settlement_pnl_applied": True,
            "terminated_early": False,
        },
        {
            "instrument_id": "DOWN.POLYMARKET",
            "fills": 0,
            "fill_events": [],
            "token_index": 1,
            "pnl": 0.0,
            "realized_outcome": 0.0,
            "settlement_pnl_applied": True,
            "terminated_early": False,
        },
    )
    fills = (
        {
            "event_type": "fill",
            "client_order_id": "client-1",
            "instrument_id": "UP.POLYMARKET",
            "ts_ns": 1_000_000_000,
            "size": 2.0,
            "price": 0.5,
            "commission": "0.0 pUSD",
            "liquidity_side": "MAKER",
            "trade_id": "trade-1",
        },
        {
            "event_type": "fill",
            "client_order_id": "client-1",
            "instrument_id": "UP.POLYMARKET",
            "ts_ns": 1_100_000_000,
            "size": 1.0,
            "price": 0.5,
            "commission": "0.0 pUSD",
            "liquidity_side": "MAKER",
            "trade_id": "trade-2",
        },
    )
    markouts = tuple(
        {
            "event_type": "fill_markout",
            "client_order_id": fill["client_order_id"],
            "trade_id": fill["trade_id"],
            "ts_ns": fill["ts_ns"] + horizon * 1_000_000_000,
            "horizon_seconds": horizon,
            "fill_price": 0.5,
            "midpoint": 0.51,
            "markout": 0.01,
            "book_ts_ns": fill["ts_ns"] + horizon * 1_000_000_000 - 500_000_000,
            "book_age_seconds": 0.5,
            "available": True,
        }
        for fill in fills
        for horizon in (1, 3, 10, 30, 60)
    )
    events = (*fills, *markouts)
    _validate_formal_results(
        results=results,
        order_events=events,
    )

    with pytest.raises(ValueError, match="maker-side"):
        _validate_formal_results(
            results=results,
            order_events=(dict(fills[0], liquidity_side="TAKER"), fills[1], *markouts),
        )

    with pytest.raises(ValueError, match="observable settlement"):
        _validate_formal_results(
            results=(dict(results[0], settlement_pnl_applied=False), results[1]),
            order_events=events,
        )

    with pytest.raises(ValueError, match="cancel rejection"):
        _validate_formal_results(
            results=results,
            order_events=(
                *events,
                {
                    "event_type": "cancel_rejected",
                    "client_order_id": "client-1",
                },
            ),
        )

    with pytest.raises(ValueError, match="complementary"):
        _validate_formal_results(
            results=(
                dict(results[0], realized_outcome=1.0),
                dict(results[1], realized_outcome=1.0),
            ),
            order_events=events,
        )

    with pytest.raises(ValueError, match="trade_id"):
        _validate_formal_results(
            results=results,
            order_events=(dict(fills[0], trade_id=""), fills[1], *markouts),
        )

    with pytest.raises(ValueError, match="complete .* markout grid"):
        _validate_formal_results(
            results=results,
            order_events=events[:-1],
        )

    with pytest.raises(ValueError, match="early-terminated"):
        _validate_formal_results(
            results=(dict(results[0], terminated_early=None), results[1]),
            order_events=events,
        )

    with pytest.raises(ValueError, match="numeric ledger value is required"):
        _validate_formal_results(
            results=(dict(results[0], realized_outcome=None), results[1]),
            order_events=events,
        )

    with pytest.raises(ValueError, match="non-negative integer"):
        _validate_formal_results(
            results=(dict(results[0], fills=1.5), results[1]),
            order_events=events,
        )
