from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

from btc_short_horizon.data import (
    BTC_15M_MARKET_FAMILY,
    MarketCatalog,
    MarketOutcome,
    MarketWindow,
    write_market_catalog,
)
from btc_short_horizon.features import BtcBookTop, OpeningFeatureObservation, opening_feature_schema
from btc_short_horizon.research.opening_features import (
    ForwardFeatureSourceSummary,
    ForwardFeatureStateEvent,
    ForwardOpeningFeatureBuild,
)
from scripts import btc_market_relative_v2_research as cli
from btc_short_horizon.strategy import StagePolicyConfig


def test_cli_disk_fixture_runs_raw_materialization_to_promotion_block(
    tmp_path: Path, monkeypatch
) -> None:
    t0 = datetime(2026, 7, 1, tzinfo=UTC)
    market = MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id="condition",
        up_token_id="up",
        down_token_id="down",
        t0=t0,
        t1=t0 + timedelta(minutes=15),
        rule_epoch="chainlink-btc-usd-point-v1",
        rule_hash="a" * 64,
        resolution=MarketOutcome.UP,
        label_available_ts=t0 + timedelta(minutes=16),
    )
    catalog_path = tmp_path / "catalog.json"
    write_market_catalog(
        path=catalog_path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
        collected_at=t0,
    )
    decisions = tuple(
        int(t0.timestamp() * 1e9) + seconds * 1_000_000_000 for seconds in range(5, 181, 5)
    )
    probabilities_path = tmp_path / "legacy.json"
    probabilities_path.write_text(
        json.dumps({market.slug: {str(value): 0.55 for value in decisions}}), encoding="utf-8"
    )
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    calls = []

    def raw_build(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return _raw_build(decisions)

    monkeypatch.setattr(cli, "build_forward_opening_feature_observations", raw_build)
    monkeypatch.setattr(cli, "load_btc_project_config", lambda _path: _config(raw_root))
    monkeypatch.setattr(
        cli,
        "run_market_relative_stage_oof",
        lambda _dataset, **_kwargs: {
            stage: {
                "p_lower": {
                    objective: {unit: {"lower_bound": 0.01} for unit in ("market", "day", "week")}
                    for objective in ("brier", "log_loss", "net_ev")
                }
            }
            for stage in ("early", "price_discovery", "mid_early")
        },
    )
    selection_calls = []

    def select_models(**kwargs):  # type: ignore[no-untyped-def]
        selection_calls.append(kwargs)
        return {
            "schema_version": "market-relative-model-selection-v1",
            "status": "complete",
            "mode": kwargs["mode"],
            "sealed_holdout_evaluated": False,
            "selection_receipt_sha256": "b" * 64,
        }

    monkeypatch.setattr(cli, "select_market_relative_models", select_models)
    output = tmp_path / "receipt.json"
    result = cli.main(
        (
            "--market-catalog",
            str(catalog_path),
            "--raw-data-root",
            str(raw_root),
            "--legacy-probabilities-json",
            str(probabilities_path),
            "--output-json",
            str(output),
            "--family",
            "anchor",
            "--minimum-eligible-markets",
            "1",
            "--bootstrap-resamples",
            "100",
            "--fee-rate",
            "0.0",
            "--fee-exponent",
            "1",
            "--model-search",
            "bounded",
        )
    )

    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert result == 2
    assert calls[0]["raw_data_root"] == raw_root
    assert receipt["coverage"][0]["eligible_market_count"] == 1
    assert receipt["ablations"]["anchor"]["status"] == "complete"
    assert (
        receipt["ablations"]["anchor"]["stage_oof"]["early"]["p_lower"]["brier"]["week"][
            "lower_bound"
        ]
        > 0
    )
    assert receipt["failed_stage"] == "promotion"
    assert receipt["promotion"]["status"] == "blocked"
    assert receipt["rule_fingerprints"][0]["rule_hash"] == "a" * 64
    assert len(receipt["ablations"]["anchor"]["sample_order_lineage_sha256"]) == 64
    assert receipt["model_selection"]["mode"] == "bounded"
    assert selection_calls[0]["mode"] == "bounded"


def _config(raw_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        paths=SimpleNamespace(raw_data_root=raw_root),
        research_timing=SimpleNamespace(
            model_cadence_ms=5_000,
            entry_start_seconds=5,
            entry_end_seconds=180,
            max_feature_lookback_seconds=3_600,
        ),
        collection=SimpleNamespace(
            ingest_version="test-v1",
            polymarket_source_timestamp_regression_tolerance_seconds=1.0,
        ),
        paper_research=SimpleNamespace(
            evidence_target_markets=300,
            tail_entry_price_threshold=0.35,
        ),
        stage_policy=StagePolicyConfig.default(),
    )


def _raw_build(decisions: tuple[int, ...]) -> ForwardOpeningFeatureBuild:
    schema = opening_feature_schema()
    observations = []
    events = []
    for decision in decisions:
        values = {name: 0.0 for name in schema.names}
        values.update(
            elapsed_seconds=(decision - decisions[0]) / 1e9 + 5.0,
            remaining_seconds=900.0 - ((decision - decisions[0]) / 1e9 + 5.0),
            data_age_seconds=0.1,
        )
        observations.append(
            OpeningFeatureObservation(
                market_window_start_ns=decisions[0] - 5_000_000_000,
                ts_event=decision,
                ts_init=decision,
                feature_schema_hash=schema.hash,
                values=schema.vector_from(values),
                p_boundary_up=0.56,
                p_market_mid_up=0.52,
            )
        )
        for token, bid, ask in (("up", 0.51, 0.53), ("down", 0.47, 0.49)):
            book = BtcBookTop(
                source_ts_ns=decision,
                available_ts_ns=decision,
                bid=bid,
                ask=ask,
                bid_size=2.0,
                ask_size=1.0,
                source="polymarket_clob",
                instrument=token,
            )
            events.append(
                ForwardFeatureStateEvent(
                    raw_source="polymarket_clob",
                    state_source="polymarket_clob",
                    state_instrument=token,
                    source_ts_ns=decision,
                    collector_receive_ts_ns=decision,
                    available_ts_ns=decision,
                    sequence_or_hash=f"{token}:{decision}",
                    collector_session_id="fixture",
                    epoch_id=0,
                    value=book,
                )
            )
    return ForwardOpeningFeatureBuild(
        observations=tuple(observations),
        input_events=tuple(events),
        source_summaries=(ForwardFeatureSourceSummary("polymarket_clob", 1, 72, 72, 0),),
    )
