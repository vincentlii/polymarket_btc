"""Build a market-relative Core dataset from Legacy OOF predictions and filtered PMXT."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
from pathlib import Path

import numpy as np
import pandas as pd

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data import read_market_catalog  # noqa: E402
from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.data.storage import sha256_file, write_atomic_json  # noqa: E402
from btc_short_horizon.features.market_relative import market_relative_feature_values_v2  # noqa: E402
from btc_short_horizon.research.exit_replay import fee_rule_sha256  # noqa: E402
from btc_short_horizon.research.market_relative_dataset_io import (  # noqa: E402
    save_anchored_direction_dataset,
)
from btc_short_horizon.research.market_relative_stage_oof import (  # noqa: E402
    AnchoredDirectionDataset,
)
from btc_short_horizon.research.market_relative_v2 import (  # noqa: E402
    market_relative_v2_profile_families,
    market_relative_v2_research_schema,
)
from btc_short_horizon.research.pipeline import DirectionDataset  # noqa: E402
from btc_short_horizon.research.pmxt_btc_history import (  # noqa: E402
    PmxtDecisionBook,
    reconstruct_pmxt_decision_books,
)
from btc_short_horizon.research.walk_forward import ResearchSample  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/btc_short_horizon/baseline.toml")
    )
    parser.add_argument("--legacy-artifact-directory", type=Path, required=True)
    parser.add_argument("--pmxt-root", type=Path, required=True)
    parser.add_argument("--output-dataset", type=Path, required=True)
    parser.add_argument("--output-receipt", type=Path, required=True)
    parser.add_argument("--start", type=_datetime, required=True)
    parser.add_argument("--end", type=_datetime, required=True)
    parser.add_argument("--fee-rate", type=float, required=True)
    parser.add_argument("--fee-exponent", type=int, default=1)
    parser.add_argument("--fee-schedule-source", required=True)
    parser.add_argument("--minimum-eligible-markets", type=int, default=300)
    parser.add_argument("--workers", type=int, default=2)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.start >= args.end:
        raise ValueError("start must precede end")
    if not 0.0 <= args.fee_rate < 1.0 or args.fee_exponent != 1:
        raise ValueError("only a finite fee rate in [0,1) and exponent 1 are supported")
    if args.minimum_eligible_markets < 1:
        raise ValueError("minimum-eligible-markets must be positive")
    if args.workers < 1 or args.workers > 4:
        raise ValueError("workers must be in [1, 4]")
    if not args.fee_schedule_source.strip():
        raise ValueError("fee-schedule-source must not be empty")
    project = load_btc_project_config(args.config)
    if project.paper_research.market_relative_feature_profile != "core":
        raise ValueError("historical Core builder requires feature profile 'core'")
    if args.end > project.paper_research.sealed_forward_start:
        raise ValueError("historical development data cannot cross sealed_forward_start")
    artifact = args.legacy_artifact_directory
    dataset_path = artifact / "dataset.parquet"
    predictions_path = artifact / "predictions.parquet"
    catalog_path = artifact / "market_catalog.json"
    legacy = pd.read_parquet(dataset_path)
    predictions = pd.read_parquet(predictions_path)
    required_legacy = {
        "sample_id",
        "feature_ts",
        "label_available_ts",
        "label",
        "sample_weight",
        "p_boundary_up",
        "data_age_seconds",
    }
    required_predictions = {"sample_id", "feature_ts_ns", "p_up", "label"}
    if missing := required_legacy - set(legacy.columns):
        raise ValueError(f"Legacy dataset missing columns: {sorted(missing)}")
    if missing := required_predictions - set(predictions.columns):
        raise ValueError(f"Legacy predictions missing columns: {sorted(missing)}")
    joined = legacy.merge(
        predictions[["sample_id", "feature_ts_ns", "p_up", "label"]],
        on="sample_id",
        suffixes=("", "_prediction"),
        validate="one_to_one",
    )
    if (joined["label"] != joined["label_prediction"]).any():
        raise ValueError("Legacy dataset and predictions disagree on labels")
    joined["market_slug"] = joined["sample_id"].str.rsplit("@", n=1).str[0]
    catalog = read_market_catalog(catalog_path, allow_unproven_legacy=True)
    markets = {
        market.slug: market for market in catalog.windows() if args.start <= market.t0 < args.end
    }
    joined = joined[joined["market_slug"].isin(markets)].copy()
    if joined.empty:
        raise ValueError("no Legacy samples fall in the requested interval")
    families = market_relative_v2_profile_families("core")
    schema = market_relative_v2_research_schema(families)
    samples: list[ResearchSample] = []
    vectors: list[tuple[float, ...]] = []
    anchors: list[float] = []
    up_asks: list[float] = []
    down_asks: list[float] = []
    up_sizes: list[float] = []
    down_sizes: list[float] = []
    weights: list[float] = []
    excluded: dict[str, int] = {}
    eligible_markets = 0
    missing_bbo_decisions = 0
    stale_bbo_decisions = 0
    retained_decisions = 0
    enabled_ranges = tuple(
        (rule.start_seconds, rule.end_seconds)
        for rule in project.stage_policy.rules
        if rule.enabled
    )
    if not enabled_ranges:
        raise ValueError("historical Core builder requires at least one enabled stage")
    jobs = tuple(
        (
            str(slug),
            group.sort_values("feature_ts_ns"),
            markets[str(slug)],
        )
        for slug, group in joined.groupby("market_slug", sort=True)
    )
    with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="pmxt-replay") as executor:
        replayed = executor.map(
            lambda job: reconstruct_pmxt_decision_books(
                raw_root=args.pmxt_root,
                market=job[2],
                decision_ts_ns=tuple(int(value) for value in job[1]["feature_ts_ns"]),
            ),
            jobs,
        )
        market_replays = zip(jobs, replayed, strict=True)
        for (slug, group, market), books in market_replays:
            all_by_decision = {item.decision_ts_ns: item for item in books}
            by_decision = _fresh_decision_books(
                books,
                maximum_age_seconds=project.maker.stale_after_seconds,
            )
            retained = group[group["feature_ts_ns"].isin(by_decision)].copy()
            missing_bbo_decisions += len(group) - len(all_by_decision)
            stale_bbo_decisions += len(all_by_decision) - len(by_decision)
            t0_ns = int(market.t0.timestamp() * 1e9)
            enabled = retained[
                retained["feature_ts_ns"].map(
                    lambda value: _in_enabled_range((int(value) - t0_ns) / 1e9, enabled_ranges)
                )
            ]
            if enabled.empty:
                reason = "no_causal_bbo_in_enabled_stage"
                excluded[reason] = excluded.get(reason, 0) + 1
                continue
            eligible_markets += 1
            retained_decisions += len(retained)
            market_weight = 1.0 / len(retained)
            for row in retained.itertuples(index=False):
                book = by_decision[int(row.feature_ts_ns)]
                elapsed = (int(row.feature_ts_ns) - t0_ns) / 1e9
                values = market_relative_feature_values_v2(
                    direction_p_up=float(row.p_up),
                    boundary_p_up=float(row.p_boundary_up),
                    market_p_up=book.market_p_up,
                    elapsed_seconds=elapsed,
                    btc_data_age_seconds=float(row.data_age_seconds),
                    up=book.up,
                    down=book.down,
                )
                vectors.append(tuple(float(values[name]) for name in schema.names))
                samples.append(
                    ResearchSample(
                        sample_id=str(row.sample_id),
                        group_id=str(slug),
                        feature_ts=_as_datetime(row.feature_ts),
                        label_available_ts=_as_datetime(row.label_available_ts),
                        label=int(row.label),
                    )
                )
                anchors.append(book.market_p_up)
                up_asks.append(book.up.ask)
                down_asks.append(book.down.ask)
                up_sizes.append(book.up.ask_size)
                down_sizes.append(book.down.ask_size)
                weights.append(market_weight)
    if eligible_markets < args.minimum_eligible_markets:
        raise ValueError(
            f"only {eligible_markets} eligible PMXT markets; need {args.minimum_eligible_markets}"
        )
    direction = DirectionDataset(
        samples=tuple(samples),
        vectors=np.asarray(vectors, dtype=float),
        schema=schema,
        sample_weights=np.asarray(weights, dtype=float),
    )
    fee_hash = fee_rule_sha256(args.fee_rate, args.fee_exponent)
    anchored = AnchoredDirectionDataset(
        dataset=direction,
        market_up_probabilities=np.asarray(anchors),
        up_best_asks=np.asarray(up_asks),
        down_best_asks=np.asarray(down_asks),
        up_best_ask_sizes=np.asarray(up_sizes),
        down_best_ask_sizes=np.asarray(down_sizes),
        fee_rates=np.full(len(samples), args.fee_rate),
        fee_rule_hash=fee_hash,
    )
    rule_epochs = {markets[str(slug)].rule_epoch for slug in joined["market_slug"].unique()}
    if len(rule_epochs) != 1:
        raise ValueError("historical Core dataset cannot mix rule epochs")
    catalog_rule_epoch = next(iter(rule_epochs))
    if catalog_rule_epoch not in {project.model_rule_epoch, "chainlink-btc-usd-v1"}:
        raise ValueError("legacy catalog rule epoch is not the configured point-model epoch")
    lineage = {
        "builder": "btc-market-relative-core-history-v2",
        "profile": "core",
        "families": [value.value for value in families],
        "rule_epoch": project.model_rule_epoch,
        "catalog_rule_epoch": catalog_rule_epoch,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "legacy_dataset_sha256": sha256_file(dataset_path),
        "legacy_predictions_sha256": sha256_file(predictions_path),
        "market_catalog_sha256": sha256_file(catalog_path),
        "pmxt_inventory_sha256": sha256_file(args.pmxt_root / "btc_pmxt_inventory.json"),
        "project_config_sha256": sha256_file(args.config),
        "sealed_forward_start": project.paper_research.sealed_forward_start.isoformat(),
        "fee_rule_hash": fee_hash,
        "fee_rate": args.fee_rate,
        "fee_exponent": args.fee_exponent,
        "fee_schedule_source": args.fee_schedule_source,
        "replay_workers": args.workers,
    }
    dataset_sha = save_anchored_direction_dataset(
        path=args.output_dataset,
        dataset=anchored,
        lineage=lineage,
    )
    receipt = {
        "schema_version": "btc-market-relative-core-history-receipt-v2",
        "status": "ready_for_development_oof",
        "dataset": str(args.output_dataset),
        "dataset_sha256": dataset_sha,
        "input_market_count": int(joined["market_slug"].nunique()),
        "eligible_market_count": eligible_markets,
        "sample_count": len(samples),
        "retained_decision_count": retained_decisions,
        "missing_bbo_decision_count": missing_bbo_decisions,
        "stale_bbo_decision_count": stale_bbo_decisions,
        "maximum_bbo_age_seconds": project.maker.stale_after_seconds,
        "excluded_markets": excluded,
        "feature_schema_hash": schema.hash,
        "lineage": lineage,
        "sealed_holdout_evaluated": False,
        "runtime_promotion_eligible": False,
    }
    write_atomic_json(args.output_receipt, receipt)
    return receipt


def _in_enabled_range(
    elapsed_seconds: float,
    enabled_ranges: Sequence[tuple[float, float]],
) -> bool:
    return any(start <= elapsed_seconds <= end for start, end in enabled_ranges)


def _fresh_decision_books(
    books: Sequence[PmxtDecisionBook],
    *,
    maximum_age_seconds: float,
) -> dict[int, PmxtDecisionBook]:
    return {
        book.decision_ts_ns: book for book in books if book.data_age_seconds <= maximum_age_seconds
    }


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _as_datetime(value: object) -> datetime:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize(UTC)
    return parsed.tz_convert(UTC).to_pydatetime()


def main(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
