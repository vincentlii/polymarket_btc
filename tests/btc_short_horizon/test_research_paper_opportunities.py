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
