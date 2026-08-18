from __future__ import annotations

import json
import math

import pyarrow.parquet as pq
import pytest

from btc_short_horizon.reporting import (
    BtcRunArtifacts,
    MakerFillEvaluation,
    ProbabilityEvaluation,
    RunArtifactWriter,
    RunManifest,
    evaluate_maker_fills,
    evaluate_probabilities,
)


def test_probability_and_maker_metrics_preserve_edge_attribution() -> None:
    probability_metrics = evaluate_probabilities(
        (
            ProbabilityEvaluation("one", 0.6, 1),
            ProbabilityEvaluation("two", 0.8, 0),
        )
    )
    fill = MakerFillEvaluation(
        market_slug="one",
        side="up",
        p_boundary=0.58,
        p_fair=0.64,
        p_market=0.61,
        entry_price=0.60,
        shares=10.0,
        fee=0.1,
        rebate=0.0,
        outcome=1,
    )
    fill_summary = evaluate_maker_fills((fill,))

    assert probability_metrics.brier_score == pytest.approx(0.4)
    assert fill.attribution.total == pytest.approx(fill.expected_edge_per_share)
    assert fill.net_expected_value == pytest.approx(0.3)
    assert fill_summary.realized_pnl == pytest.approx(3.9)


def test_artifact_writer_emits_complete_required_bundle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    artifacts = BtcRunArtifacts(
        manifest=RunManifest(
            code_revision="code",
            upstream_revision="upstream",
            data_hashes={"raw": "hash"},
            model_hashes={"direction": "model"},
            scenario={"queue": "pessimistic"},
            seed=17,
        ),
        data_quality={"reconstruction_rate": 1.0},
        predictions=({"market_slug": "one", "p_up": 0.6},),
        fills=({"market_slug": "one", "price": 0.6, "shares": 1.0},),
        metrics={"net_ev": 0.01},
    )

    output = tmp_path / "run"
    RunArtifactWriter.write(directory=output, artifacts=artifacts, title="BTC run")

    required = {
        "run_manifest.json",
        "data_quality.json",
        "predictions.parquet",
        "opening_paths.parquet",
        "opportunities.parquet",
        "orders.parquet",
        "fills.parquet",
        "closed_trades.parquet",
        "metrics.json",
        "report.html",
    }
    assert required <= {path.name for path in output.iterdir()}
    assert json.loads((output / "run_manifest.json").read_text())["code_revision"] == "code"
    assert pq.read_table(output / "fills.parquet").num_rows == 1


def test_artifact_writer_preserves_union_of_heterogeneous_event_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    artifacts = BtcRunArtifacts(
        manifest=RunManifest(
            code_revision="code",
            upstream_revision="upstream",
            data_hashes={},
            model_hashes={},
            scenario={},
            seed=0,
        ),
        data_quality={},
        orders=(
            {"event_type": "plan", "p_fair": 0.64},
            {"event_type": "cancel_request", "client_order_id": "order-1"},
        ),
    )

    output = tmp_path / "run"
    RunArtifactWriter.write(directory=output, artifacts=artifacts, title="BTC run")

    rows = pq.read_table(output / "orders.parquet").to_pylist()
    assert rows == [
        {
            "client_order_id": None,
            "event_type": "plan",
            "p_fair": 0.64,
        },
        {
            "client_order_id": "order-1",
            "event_type": "cancel_request",
            "p_fair": None,
        },
    ]

    with pytest.raises(FileExistsError, match="already exists"):
        RunArtifactWriter.write(directory=output, artifacts=artifacts, title="BTC rerun")


def test_artifact_writer_serializes_undefined_statistics_as_null(tmp_path) -> None:  # type: ignore[no-untyped-def]
    artifacts = BtcRunArtifacts(
        manifest=RunManifest("code", "upstream", {}, {}, {}, 0),
        data_quality={},
        metrics={"undefined_statistic": math.nan},
    )
    output = tmp_path / "run"

    RunArtifactWriter.write(directory=output, artifacts=artifacts, title="BTC run")

    assert json.loads((output / "metrics.json").read_text(encoding="utf-8")) == {
        "undefined_statistic": None
    }
