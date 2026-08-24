from __future__ import annotations

import sqlite3

import pytest

from btc_short_horizon.live.research_paper import (
    PaperEvaluationObservation,
    PaperEvaluationStore,
)


def _evaluation(*, selected: bool = False, edge: float = 0.0) -> PaperEvaluationObservation:
    return PaperEvaluationObservation(
        variant_id="independent_fak_2x5s",
        evaluation_id="market:100",
        market_slug="market",
        decision_ts_ns=100,
        entry_regime="early_3s_to_30s",
        price_bucket="core",
        reason="selected" if selected else "edge_below_threshold",
        selected_side="up" if selected else None,
        fair_probability=0.65 if selected else 0.55,
        executable_vwap=0.52,
        net_edge=edge,
        model_version="model-v1",
    )


def test_evaluation_store_is_incremental_idempotent_and_aggregated(tmp_path) -> None:
    store = PaperEvaluationStore(tmp_path, "paper-v5-stage-integrity")

    assert store.append((_evaluation(),)) == 1
    assert store.append((_evaluation(),)) == 0
    assert (
        store.append(
            (
                PaperEvaluationObservation(
                    **(
                        _evaluation(selected=True, edge=0.08).to_json()
                        | {"evaluation_id": "market:105"}
                    )
                ),
            )
        )
        == 1
    )

    counts = store.counts_by_variant()["independent_fak_2x5s"]
    assert counts.evaluation_count == 2
    assert counts.qualified_signal_count == 1
    assert store.path.name == "evaluations.sqlite3"


def test_evaluation_store_separates_qualified_edge_from_all_candidates(tmp_path) -> None:
    store = PaperEvaluationStore(tmp_path, "paper-v5-stage-integrity")
    selected = PaperEvaluationObservation(
        **(_evaluation(selected=True, edge=0.04).to_json() | {"gross_edge": 0.09})
    )
    rejected = PaperEvaluationObservation(
        **(
            _evaluation(selected=False, edge=-0.05).to_json()
            | {"evaluation_id": "market:105", "gross_edge": -0.02}
        )
    )
    store.append((selected, rejected))

    counts = store.counts_by_variant()[selected.variant_id]

    assert counts.mean_gross_edge == pytest.approx(0.09)
    assert counts.mean_net_edge == pytest.approx(0.04)
    assert counts.candidate_mean_gross_edge == pytest.approx(0.035)
    assert counts.candidate_mean_net_edge == pytest.approx(-0.005)


def test_evaluation_store_fails_closed_for_identity_conflict(tmp_path) -> None:
    store = PaperEvaluationStore(tmp_path, "paper-v5-stage-integrity")
    store.append((_evaluation(),))

    with pytest.raises(ValueError, match="immutable Research Paper evaluation conflict"):
        store.append((_evaluation(selected=True, edge=0.08),))


def test_evaluation_store_rejects_unknown_schema(tmp_path) -> None:
    path = tmp_path / "paper" / "epochs" / "paper-v5-stage-integrity" / "evaluations.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=99")
    connection.close()

    with pytest.raises(ValueError, match="unsupported Research Paper evaluation schema"):
        PaperEvaluationStore(tmp_path, "paper-v5-stage-integrity")


def test_evaluation_store_migrates_v1_and_preserves_cost_attribution(tmp_path) -> None:
    path = tmp_path / "paper" / "epochs" / "paper-v5-stage-integrity" / "evaluations.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE evaluations (
            variant_id TEXT NOT NULL, evaluation_id TEXT NOT NULL, market_slug TEXT NOT NULL,
            decision_ts_ns INTEGER NOT NULL, entry_regime TEXT NOT NULL,
            price_bucket TEXT NOT NULL, reason TEXT NOT NULL, selected_side TEXT,
            fair_probability REAL, executable_vwap REAL, net_edge REAL,
            model_version TEXT NOT NULL, PRIMARY KEY (variant_id, evaluation_id)
        ) WITHOUT ROWID
        """
    )
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    store = PaperEvaluationStore(tmp_path, "paper-v5-stage-integrity")
    attributed = PaperEvaluationObservation(
        **(
            _evaluation(selected=True, edge=0.04).to_json()
            | {
                "point_fair_probability": 0.61,
                "probability_lower": 0.58,
                "probability_upper": 0.64,
                "market_anchor": 0.53,
                "gross_edge": 0.09,
                "fee_per_share": 0.01,
                "slippage_stress": 0.02,
                "latency_stress": 0.02,
            }
        )
    )
    store.append((attributed,))

    counts = store.counts_by_variant()[attributed.variant_id]
    assert counts.mean_gross_edge == pytest.approx(0.09)
    assert counts.mean_fee_per_share == pytest.approx(0.01)
    assert counts.mean_net_edge == pytest.approx(0.04)
    with sqlite3.connect(path) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 2
