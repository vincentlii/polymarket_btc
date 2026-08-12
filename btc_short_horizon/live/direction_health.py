"""Durable, market-level direction evidence for Research Paper monitoring."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
import sqlite3

from btc_short_horizon.data import MarketOutcome, MarketWindow
from btc_short_horizon.live.dashboard_state import (
    DirectionHealthSnapshot,
    DirectionStageSummary,
)
from btc_short_horizon.models.opening_mispricing import OpeningMispricingPrediction
from btc_short_horizon.strategy.stage_policy import OpeningStage


_MINIMUM_BIAS_SAMPLE = 100


class DirectionEvidenceStore:
    """Persist all activated markets and shared predictions once per execution epoch."""

    def __init__(self, runtime_root: Path, execution_epoch: str) -> None:
        if not execution_epoch or any(character in execution_epoch for character in "/\\"):
            raise ValueError("execution_epoch must be a simple identifier")
        self.path = runtime_root / "paper" / "epochs" / execution_epoch / "direction.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS markets (
                market_slug TEXT PRIMARY KEY,
                t0_ns INTEGER NOT NULL,
                t1_ns INTEGER NOT NULL,
                outcome TEXT,
                label_available_ts_ns INTEGER
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS predictions (
                market_slug TEXT NOT NULL,
                decision_ts_ns INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                stage TEXT NOT NULL,
                p_up REAL NOT NULL,
                PRIMARY KEY (market_slug, decision_ts_ns, model_version),
                FOREIGN KEY (market_slug) REFERENCES markets(market_slug)
            ) WITHOUT ROWID;
            """
        )
        self._connection.commit()

    def register_market(self, market: MarketWindow) -> None:
        values = (
            market.slug,
            int(market.t0.timestamp() * 1_000_000_000),
            int(market.t1.timestamp() * 1_000_000_000),
        )
        with self._connection:
            cursor = self._connection.execute(
                "INSERT OR IGNORE INTO markets (market_slug, t0_ns, t1_ns) VALUES (?, ?, ?)",
                values,
            )
            if cursor.rowcount:
                return
            existing = self._connection.execute(
                "SELECT market_slug, t0_ns, t1_ns FROM markets WHERE market_slug = ?",
                (market.slug,),
            ).fetchone()
            if existing != values:
                raise ValueError("immutable direction market conflict")

    def append_prediction(
        self,
        prediction: OpeningMispricingPrediction,
        *,
        stage: OpeningStage | str,
    ) -> None:
        stage_value = OpeningStage(stage).value
        values = (
            prediction.market_slug,
            prediction.trigger_ts_ns,
            prediction.model_version,
            stage_value,
            prediction.p_up,
        )
        try:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO predictions
                        (market_slug, decision_ts_ns, model_version, stage, p_up)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    values,
                )
                if cursor.rowcount:
                    return
                existing = self._connection.execute(
                    """
                    SELECT market_slug, decision_ts_ns, model_version, stage, p_up
                    FROM predictions
                    WHERE market_slug = ? AND decision_ts_ns = ? AND model_version = ?
                    """,
                    values[:3],
                ).fetchone()
                if existing != values:
                    raise ValueError("immutable direction prediction conflict")
        except sqlite3.IntegrityError as exc:
            raise ValueError("direction prediction requires a registered market") from exc

    def settle(
        self,
        market_slug: str,
        outcome: MarketOutcome,
        *,
        label_available_ts_ns: int,
    ) -> None:
        if not isinstance(outcome, MarketOutcome):
            outcome = MarketOutcome(outcome)
        if isinstance(label_available_ts_ns, bool) or label_available_ts_ns < 0:
            raise ValueError("label_available_ts_ns must be a non-negative integer")
        existing = self._connection.execute(
            "SELECT outcome, label_available_ts_ns FROM markets WHERE market_slug = ?",
            (market_slug,),
        ).fetchone()
        if existing is None:
            raise ValueError("direction outcome requires a registered market")
        expected = (outcome.value, label_available_ts_ns)
        if existing[0] is not None:
            if existing != expected:
                raise ValueError("immutable direction outcome conflict")
            return
        with self._connection:
            self._connection.execute(
                """
                UPDATE markets SET outcome = ?, label_available_ts_ns = ?
                WHERE market_slug = ? AND outcome IS NULL
                """,
                (*expected, market_slug),
            )

    def unresolved_market_slugs(self) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT market_slug FROM markets WHERE outcome IS NULL ORDER BY t0_ns"
        )
        return tuple(str(row[0]) for row in rows)

    def snapshot(self) -> DirectionHealthSnapshot | None:
        market_rows = tuple(
            self._connection.execute(
                "SELECT market_slug, t0_ns, outcome FROM markets ORDER BY t0_ns"
            )
        )
        if not market_rows:
            return None
        prediction_rows = tuple(
            self._connection.execute(
                "SELECT market_slug, stage, p_up FROM predictions ORDER BY decision_ts_ns"
            )
        )
        outcomes = {str(slug): outcome for slug, _t0_ns, outcome in market_rows}
        stage_values: dict[tuple[str, str], list[float]] = defaultdict(list)
        for market_slug, stage, p_up in prediction_rows:
            stage_values[(str(market_slug), str(stage))].append(float(p_up))
        stage_means = {key: sum(values) / len(values) for key, values in stage_values.items()}
        market_stage_means: dict[str, list[float]] = defaultdict(list)
        for (market_slug, _stage), probability in stage_means.items():
            market_stage_means[market_slug].append(probability)
        market_means = {
            market_slug: sum(values) / len(values)
            for market_slug, values in market_stage_means.items()
        }
        paired = {
            market_slug: (probability, str(outcomes[market_slug]))
            for market_slug, probability in market_means.items()
            if outcomes.get(market_slug) in {MarketOutcome.UP.value, MarketOutcome.DOWN.value}
        }
        overall = _summarize_probabilities(paired)
        stages = tuple(
            _stage_summary(
                stage,
                {
                    market_slug: (probability, str(outcomes[market_slug]))
                    for (market_slug, candidate_stage), probability in stage_means.items()
                    if candidate_stage == stage
                    and outcomes.get(market_slug)
                    in {MarketOutcome.UP.value, MarketOutcome.DOWN.value}
                },
            )
            for stage in sorted({stage for _market_slug, stage in stage_means})
        )
        return DirectionHealthSnapshot(
            scope="paired_resolved_markets",
            coverage_started_at=datetime.fromtimestamp(
                min(int(row[1]) for row in market_rows) / 1_000_000_000,
                tz=UTC,
            ),
            activated_market_count=len(market_rows),
            resolved_market_count=sum(outcome is not None for outcome in outcomes.values()),
            paired_market_count=overall[0],
            prediction_count=len(prediction_rows),
            actual_up_count=overall[1],
            actual_down_count=overall[2],
            predicted_up_count=overall[3],
            predicted_down_count=overall[4],
            mean_p_up=overall[5],
            calibration_z=overall[6],
            bias_state=overall[7],
            stage_summaries=stages,
        )

    def checkpoint(self) -> None:
        self._connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()

    def close(self) -> None:
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self._connection.close()


def _stage_summary(
    stage: str,
    paired: dict[str, tuple[float, str]],
) -> DirectionStageSummary:
    summary = _summarize_probabilities(paired)
    return DirectionStageSummary(
        stage=stage,
        paired_market_count=summary[0],
        actual_up_count=summary[1],
        actual_down_count=summary[2],
        predicted_up_count=summary[3],
        predicted_down_count=summary[4],
        mean_p_up=summary[5],
        calibration_z=summary[6],
        bias_state=summary[7],
    )


def _summarize_probabilities(
    paired: dict[str, tuple[float, str]],
) -> tuple[int, int, int, int, int, float | None, float | None, str]:
    values = tuple(paired.values())
    count = len(values)
    actual_up = sum(outcome == MarketOutcome.UP.value for _probability, outcome in values)
    predicted_up = sum(probability >= 0.5 for probability, _outcome in values)
    if not values:
        return 0, 0, 0, 0, 0, None, None, "insufficient_data"
    mean_p_up = sum(probability for probability, _outcome in values) / count
    residual = sum(
        (1.0 if outcome == MarketOutcome.UP.value else 0.0) - probability
        for probability, outcome in values
    )
    variance = sum(probability * (1.0 - probability) for probability, _outcome in values)
    calibration_z = None if variance <= 0.0 else residual / sqrt(variance)
    if count < _MINIMUM_BIAS_SAMPLE:
        state = "insufficient_data"
    elif calibration_z is not None and abs(calibration_z) > 1.96:
        state = "investigate"
    else:
        state = "consistent"
    return (
        count,
        actual_up,
        count - actual_up,
        predicted_up,
        count - predicted_up,
        mean_p_up,
        calibration_z,
        state,
    )


__all__ = ["DirectionEvidenceStore"]
