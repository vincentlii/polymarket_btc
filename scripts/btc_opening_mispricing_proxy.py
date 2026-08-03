"""Fit the bounded post-open BTC fair-probability proxy from existing public archives."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketCatalog  # noqa: E402
from btc_short_horizon.data.catalog_io import read_market_catalog, write_market_catalog  # noqa: E402
from btc_short_horizon.data.gamma import GammaMarketClient  # noqa: E402
from btc_short_horizon.features.schema import FeatureSchema  # noqa: E402
from btc_short_horizon.models import (  # noqa: E402
    DirectionModelConfig,
    ModelArtifactMetadata,
    ModelArtifactStore,
)
from btc_short_horizon.research import (  # noqa: E402
    DirectionDataset,
    HoldoutPrediction,
    WalkForwardConfig,
    run_sealed_holdout_model,
    run_walk_forward_model,
)
from btc_short_horizon.research.binance_history import (  # noqa: E402
    binance_spot_kline_url,
    load_binance_kline_archives,
)
from btc_short_horizon.research.opening_proxy import (  # noqa: E402
    OPENING_REGIMES,
    OpeningProxyDatasetBuild,
    build_opening_proxy_dataset,
    opening_proxy_feature_schema,
    opening_proxy_protocol,
    opening_regime_for_elapsed_seconds,
)
from btc_short_horizon.research.opening_model_gate import (  # noqa: E402
    CandidateEvaluation,
    build_direction_gate_artifact,
    calibration_slope,
    candidate_evaluation_dict,
    paired_daily_block_bootstrap,
    select_direction_candidate,
    target_confidence_bands,
    weighted_calibration_error,
)
from btc_short_horizon.research.materialized_dataset import (  # noqa: E402
    read_materialized_direction_dataset,
)
from btc_short_horizon.research.provenance import git_provenance  # noqa: E402
from btc_short_horizon.research.walk_forward import ResearchSample  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", required=True, type=_date, help="Inclusive UTC date.")
    parser.add_argument("--end-date", required=True, type=_date, help="Exclusive UTC date.")
    parser.add_argument(
        "--rule-epoch", required=True, help="Verified Gamma-rule epoch for this sample."
    )
    parser.add_argument(
        "--market-catalog",
        type=Path,
        help="Immutable Gamma catalog to reuse instead of querying Gamma.",
    )
    parser.add_argument(
        "--profile",
        choices=("quick", "full"),
        default="quick",
        help="quick is a proxy feasibility test; full uses the documented 90/21/14/28 schedule.",
    )
    parser.add_argument("--binance-directory", type=Path)
    parser.add_argument(
        "--materialized-dataset",
        type=Path,
        help="Reuse a prior proxy dataset instead of loading the raw Kline archives again.",
    )
    parser.add_argument(
        "--materialized-market-stride",
        type=int,
        default=1,
        help="Use every Nth chronological market from a materialized dataset (default: 1).",
    )
    parser.add_argument(
        "--interval",
        choices=("1s", "1m"),
        default="1s",
        help="Use existing archives only unless --download-binance is explicitly supplied.",
    )
    parser.add_argument("--download-binance", action="store_true")
    parser.add_argument("--snapshot-seconds", type=int, default=5)
    parser.add_argument("--entry-start-seconds", type=int, default=3)
    parser.add_argument("--entry-end-seconds", type=int, default=180)
    parser.add_argument("--output-directory", type=Path, required=True)
    return parser.parse_args(argv)


async def discover_closed_markets(
    *, start: datetime, end: datetime, rule_epoch: str
) -> MarketCatalog:
    client = GammaMarketClient()
    catalog = MarketCatalog(families=(BTC_15M_MARKET_FAMILY,))
    for batch in _batches(tuple(_market_slugs(start=start, end=end)), 100):
        discovered = await client.discover_catalog(
            family=BTC_15M_MARKET_FAMILY,
            rule_epoch=rule_epoch,
            closed=True,
            slugs=batch,
        )
        for window in discovered.windows():
            catalog.register(window)
    return catalog


def download_binance_archives(
    *, directory: Path, start: date, end: date, interval: str
) -> tuple[Path, ...]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for current in _dates(start - timedelta(days=1), end):
        path = directory / f"BTCUSDT-{interval}-{current.isoformat()}.zip"
        if not path.is_file():
            temporary = path.with_suffix(".download")
            try:
                with httpx.stream(
                    "GET",
                    binance_spot_kline_url(day=current.isoformat(), interval=interval),
                    timeout=60.0,
                ) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        paths.append(path)
    return tuple(paths)


def existing_binance_archives(
    *, directory: Path, start: date, end: date, interval: str
) -> tuple[Path, ...]:
    paths = tuple(
        directory / f"BTCUSDT-{interval}-{current.isoformat()}.zip"
        for current in _dates(start - timedelta(days=1), end)
    )
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "missing "
            f"{len(missing)} Binance archive(s); rerun with --download-binance, e.g. {missing[0]}"
        )
    return paths


def run(args: argparse.Namespace) -> dict[str, object]:
    start = datetime.combine(args.start_date, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(args.end_date, datetime.min.time(), tzinfo=UTC)
    if end - start < timedelta(days=7):
        raise ValueError("date range must span at least seven days")
    if args.output_directory.exists():
        raise FileExistsError(f"output directory already exists: {args.output_directory}")
    if args.materialized_dataset is not None and args.download_binance:
        raise ValueError("--materialized-dataset cannot be combined with --download-binance")
    if args.materialized_dataset is None and args.binance_directory is None:
        raise ValueError("--binance-directory is required without --materialized-dataset")
    if args.materialized_market_stride < 1:
        raise ValueError("--materialized-market-stride must be >= 1")
    if args.materialized_dataset is None and args.materialized_market_stride != 1:
        raise ValueError("--materialized-market-stride requires --materialized-dataset")
    code_provenance = git_provenance()
    protocol = opening_proxy_protocol(
        entry_start_seconds=args.entry_start_seconds,
        entry_end_seconds=args.entry_end_seconds,
        snapshot_seconds=args.snapshot_seconds,
    )
    archives = (
        ()
        if args.materialized_dataset is not None
        else (
            download_binance_archives(
                directory=args.binance_directory,
                start=args.start_date,
                end=args.end_date,
                interval=args.interval,
            )
            if args.download_binance
            else existing_binance_archives(
                directory=args.binance_directory,
                start=args.start_date,
                end=args.end_date,
                interval=args.interval,
            )
        )
    )
    catalog, expected_market_count = _catalog_for_study(
        catalog_path=args.market_catalog,
        start=start,
        end=end,
        rule_epoch=args.rule_epoch,
    )
    if args.materialized_dataset is None:
        build = build_opening_proxy_dataset(
            markets=catalog.windows(),
            klines=load_binance_kline_archives(archives, interval=args.interval),
            snapshot_seconds=args.snapshot_seconds,
            entry_start_seconds=args.entry_start_seconds,
            entry_end_seconds=args.entry_end_seconds,
        )
    else:
        expected_group_ids = tuple(
            market.slug
            for market in sorted(catalog.windows(), key=lambda item: (item.t0, item.slug))[
                :: args.materialized_market_stride
            ]
        )
        dataset = load_materialized_opening_proxy_dataset(
            path=args.materialized_dataset,
            interval_seconds=int(args.interval.removesuffix("s").removesuffix("m"))
            * (60 if args.interval.endswith("m") else 1),
            snapshot_seconds=args.snapshot_seconds,
            entry_start_seconds=args.entry_start_seconds,
            entry_end_seconds=args.entry_end_seconds,
            market_stride=args.materialized_market_stride,
            expected_market_group_ids=expected_group_ids,
        )
        included_markets = len({sample.group_id for sample in dataset.samples})
        requested_markets = len(catalog.windows())
        expected_included_markets = (
            requested_markets + args.materialized_market_stride - 1
        ) // args.materialized_market_stride
        if included_markets != expected_included_markets:
            raise ValueError(
                "materialized dataset does not contain the expected complete market sample"
            )
        build = OpeningProxyDatasetBuild(
            dataset=dataset,
            requested_markets=requested_markets,
            excluded_void_markets=0,
            excluded_insufficient_history=(
                requested_markets - included_markets if args.materialized_market_stride == 1 else 0
            ),
            excluded_kline_gaps=0,
            snapshots_per_market=len(dataset.samples) // included_markets,
        )
    split = _split_config(args.profile)
    development: list[tuple[str, DirectionModelConfig, object, dict[str, object]]] = []
    for name, config in _candidate_configs():
        model_run = run_walk_forward_model(
            dataset=build.dataset,
            split_config=split,
            model_config=config,
        )
        metrics = _prediction_metrics(
            model_run.predictions,
            weights=build.dataset.sample_weights,
        )
        metrics["by_regime"] = _prediction_metrics_by_regime(
            model_run.predictions,
            dataset=build.dataset,
        )
        development.append((name, config, model_run, metrics))
    best_logistic = min(
        (item for item in development if item[1].kind == "logistic"),
        key=lambda item: (
            float(item[3]["log_loss"]),
            float(item[3]["brier"]),
            float(item[3]["weighted_calibration_error"]),
        ),
    )
    candidate_evaluations: list[CandidateEvaluation] = []
    lightgbm_comparison_count = sum(config.kind == "lightgbm" for _, config, _, _ in development)
    for name, config, model_run, metrics in development:
        paired = (
            None
            if config.kind == "logistic"
            else paired_daily_block_bootstrap(
                candidate_predictions=model_run.predictions,
                baseline_predictions=best_logistic[2].predictions,
                weights=build.dataset.sample_weights,
                baseline_name=best_logistic[0],
                alpha=0.05 / lightgbm_comparison_count,
            )
        )
        candidate_evaluations.append(
            CandidateEvaluation(
                name=name,
                kind=config.kind,
                log_loss=float(metrics["log_loss"]),
                brier=float(metrics["brier"]),
                calibration_error=float(metrics["weighted_calibration_error"]),
                paired_vs_best_logistic=paired,
            )
        )
    selection = select_direction_candidate(candidate_evaluations)
    selected_name, selected_config, selected_development, selected_metrics = next(
        item for item in development if item[0] == selection.selected_name
    )
    holdout = run_sealed_holdout_model(
        dataset=build.dataset,
        split_config=split,
        model_config=selected_config,
    )
    output = args.output_directory
    output.mkdir(parents=True)
    write_market_catalog(path=output / "market_catalog.json", catalog=catalog)
    source_paths = archives or (args.materialized_dataset,)
    data_hash = _data_hash(
        archives=tuple(path for path in source_paths if path is not None),
        catalog_path=output / "market_catalog.json",
    )
    _write_dataset(path=output / "dataset.parquet", dataset=build.dataset)
    _write_predictions(
        path=output / "predictions.parquet",
        development=selected_development.predictions,
        holdout=holdout.predictions,
        weights=build.dataset.sample_weights,
    )
    artifact = ModelArtifactStore.save(
        directory=output / "model",
        model=holdout.model,
        metadata=ModelArtifactMetadata(
            model_id=(
                "btc-15m-opening-proxy-"
                f"{selected_name}-{args.entry_start_seconds}to{args.entry_end_seconds}s"
            ),
            feature_schema_hash=build.dataset.schema.hash,
            training_start_ns=_ns(build.dataset.samples[holdout.train_indices[0]].feature_ts),
            training_end_ns=_ns(build.dataset.samples[holdout.train_indices[-1]].feature_ts),
            calibration_start_ns=_ns(
                build.dataset.samples[holdout.calibration_indices[0]].feature_ts
            ),
            calibration_end_ns=_ns(
                build.dataset.samples[holdout.calibration_indices[-1]].feature_ts
            ),
            data_hash=data_hash,
            code_revision=str(code_provenance["revision_label"]),
            config={
                **holdout.model.config_dict,
                "opening_proxy_protocol": protocol,
                "materialized_market_stride": args.materialized_market_stride,
                "fit_sample_count": len(holdout.fit_indices),
                "early_stopping_sample_count": len(holdout.early_stopping_indices),
                "calibration_sample_count": len(holdout.calibration_indices),
                "fit_market_count": len(
                    {build.dataset.samples[index].group_id for index in holdout.fit_indices}
                ),
                "early_stopping_market_count": len(
                    {
                        build.dataset.samples[index].group_id
                        for index in holdout.early_stopping_indices
                    }
                ),
                "calibration_market_count": len(
                    {build.dataset.samples[index].group_id for index in holdout.calibration_indices}
                ),
                "code_provenance": code_provenance,
            },
        ),
    )
    holdout_weights = build.dataset.sample_weights[np.asarray(holdout.holdout_indices, dtype=int)]
    prior_weights = build.dataset.sample_weights[np.asarray(holdout.train_indices, dtype=int)]
    prior_probability = float(
        np.average(
            build.dataset.labels[np.asarray(holdout.train_indices, dtype=int)],
            weights=prior_weights,
        )
    )
    holdout_metrics = _prediction_metrics(
        holdout.predictions,
        weights=build.dataset.sample_weights,
    )
    holdout_metrics["by_regime"] = _prediction_metrics_by_regime(
        holdout.predictions,
        dataset=build.dataset,
    )
    training_prior_metrics = _probability_metrics(
        labels=np.asarray([item.label for item in holdout.predictions], dtype=int),
        probabilities=np.full(len(holdout.predictions), prior_probability),
        weights=holdout_weights,
    )
    training_prior_metrics["by_regime"] = _constant_probability_metrics_by_regime(
        holdout.predictions,
        dataset=build.dataset,
        probability=prior_probability,
    )
    prior_predictions = tuple(
        HoldoutPrediction(
            sample_index=item.sample_index,
            sample_id=item.sample_id,
            feature_ts_ns=item.feature_ts_ns,
            p_up=prior_probability,
            label=item.label,
        )
        for item in holdout.predictions
    )
    paired_holdout = paired_daily_block_bootstrap(
        candidate_predictions=holdout.predictions,
        baseline_predictions=prior_predictions,
        weights=build.dataset.sample_weights,
        baseline_name="training_prior",
    )
    holdout_indices = np.asarray([item.sample_index for item in holdout.predictions], dtype=int)
    holdout_labels = np.asarray([item.label for item in holdout.predictions], dtype=int)
    holdout_probabilities = np.asarray([item.p_up for item in holdout.predictions], dtype=float)
    holdout_sample_weights = build.dataset.sample_weights[holdout_indices]
    target_bands = target_confidence_bands(
        predictions=holdout.predictions,
        weights=build.dataset.sample_weights,
    )
    protocol_failures: list[str] = ["missing_causal_polymarket_implied_probability_baseline"]
    if code_provenance["dirty"] is not False:
        protocol_failures.append("dirty_or_unknown_code_provenance")
    if args.profile != "full":
        protocol_failures.append("non_full_walk_forward_profile")
    if args.materialized_market_stride != 1:
        protocol_failures.append("approximate_materialized_market_stride")
    if expected_market_count != build.requested_markets:
        protocol_failures.append("incomplete_gamma_market_coverage")
    direction_gate = build_direction_gate_artifact(
        sealed_holdout_markets=len(
            {_sample_group_id(item.sample_id) for item in holdout.predictions}
        ),
        log_loss_improvement=paired_holdout.log_loss_improvement,
        log_loss_ci_lower=paired_holdout.log_loss_ci_lower,
        log_loss_ci_upper=paired_holdout.log_loss_ci_upper,
        brier_improvement=paired_holdout.brier_improvement,
        brier_ci_lower=paired_holdout.brier_ci_lower,
        brier_ci_upper=paired_holdout.brier_ci_upper,
        calibration_slope=calibration_slope(
            labels=holdout_labels,
            probabilities=holdout_probabilities,
            weights=holdout_sample_weights,
        ),
        target_bands=target_bands,
        protocol_eligible=not protocol_failures,
        protocol_failures=protocol_failures,
        confidence_level=paired_holdout.confidence_level,
    )
    limitations = [
        "This proxy evaluates fair-probability feasibility, not Polymarket mispricing or maker PnL.",
        "The opening reference is the last causally available Binance kline, not the final Chainlink reference.",
        "No historical Polymarket CLOB probability, L2 queue, fill, fee, rebate, or latency evidence is used.",
        "Binance archive timestamps are event-time-only and do not measure network latency.",
    ]
    if args.materialized_market_stride > 1:
        limitations.append(
            "This memory-bounded run uses one of every "
            f"{args.materialized_market_stride} chronological markets; it is an approximate research result."
        )
    result = {
        "study_type": "opening_mispricing_fair_probability_proxy",
        "profile": args.profile,
        "date_range": {
            "start_inclusive": args.start_date.isoformat(),
            "end_exclusive": args.end_date.isoformat(),
        },
        "entry_protocol": {
            **protocol,
            "per_market_total_sample_weight": 1.0,
        },
        "sources": {
            "gamma": "https://gamma-api.polymarket.com/markets/keyset",
            "binance": (
                f"https://data.binance.vision/data/spot/daily/klines/BTCUSDT/{args.interval}/"
            ),
            "archive_count": len(archives),
            "materialized_dataset": (
                str(args.materialized_dataset) if args.materialized_dataset is not None else None
            ),
            "materialized_market_stride": args.materialized_market_stride,
            "interval": args.interval,
            "data_hash": data_hash,
        },
        "dataset": {
            "sample_count": len(build.dataset.samples),
            "feature_count": len(build.dataset.schema.names),
            "requested_markets": build.requested_markets,
            "gamma_expected_markets": expected_market_count,
            "gamma_missing_markets": expected_market_count - build.requested_markets,
            "excluded_void_markets": build.excluded_void_markets,
            "excluded_insufficient_history": build.excluded_insufficient_history,
            "excluded_kline_gaps": build.excluded_kline_gaps,
            "not_selected_by_materialized_market_stride": (
                build.requested_markets - len({sample.group_id for sample in build.dataset.samples})
                if args.materialized_market_stride > 1
                else 0
            ),
            "snapshots_per_market": build.snapshots_per_market,
            "feature_schema_hash": build.dataset.schema.hash,
            "historical_availability_delay_seconds": 1,
        },
        "development_candidates": [
            {
                "name": name,
                "config": asdict(config),
                "metrics": metrics,
                "selection_evidence": candidate_evaluation_dict(candidate),
            }
            for (name, config, _, metrics), candidate in zip(
                development, candidate_evaluations, strict=True
            )
        ],
        "selected_on_development": {
            "name": selected_name,
            "metrics": selected_metrics,
            "best_logistic_name": selection.best_logistic_name,
            "reason": selection.reason,
            "eligible_lightgbm_candidates": list(selection.eligible_lightgbm_candidates),
        },
        "sealed_holdout": {
            "metrics": holdout_metrics,
            "training_prior_probability": prior_probability,
            "training_prior_metrics": training_prior_metrics,
            "sample_count": len(holdout.predictions),
        },
        "direction_gate": direction_gate,
        "code_provenance": code_provenance,
        "model_sha256": artifact.model_sha256,
        "limitations": limitations,
    }
    (output / "metrics.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "direction_gate.json").write_text(
        json.dumps(direction_gate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def load_materialized_opening_proxy_dataset(
    *,
    path: Path,
    interval_seconds: int,
    snapshot_seconds: int,
    entry_start_seconds: int,
    entry_end_seconds: int,
    market_stride: int = 1,
    expected_market_group_ids: Sequence[str] | None = None,
) -> DirectionDataset:
    """Load one narrower protocol without bulk-copying a full feature table."""

    schema = opening_proxy_feature_schema(interval_seconds)
    first_offset = (
        (entry_start_seconds + snapshot_seconds - 1) // snapshot_seconds
    ) * snapshot_seconds
    expected_offsets = tuple(range(first_offset, entry_end_seconds + 1, snapshot_seconds))
    if not expected_offsets:
        raise ValueError("materialized proxy timing produces no snapshots")
    dataset = read_materialized_direction_dataset(
        path,
        expected_schema=schema,
        market_stride=market_stride,
        expected_offsets=expected_offsets,
    )
    observed_group_ids = tuple(dict.fromkeys(sample.group_id for sample in dataset.samples))
    if expected_market_group_ids is not None:
        expected = tuple(expected_market_group_ids)
        if not expected or any(
            not isinstance(value, str) or not value or value.strip() != value for value in expected
        ):
            raise ValueError("expected materialized market group IDs must be non-empty and trimmed")
        if len(expected) != len(set(expected)):
            raise ValueError("expected materialized market group IDs must be unique")
        if observed_group_ids != expected:
            raise ValueError("materialized proxy market sequence does not match the study catalog")
    return dataset


_MATERIALIZED_BATCH_SIZE = 8_192


def _materialized_selected_row_count(
    *,
    parquet: pq.ParquetFile,
    expected_offsets: tuple[int, ...],
    market_stride: int,
) -> int:
    count = 0
    current_group_id: str | None = None
    market_index = -1
    seen_group_ids: set[str] = set()
    for batch in parquet.iter_batches(
        batch_size=_MATERIALIZED_BATCH_SIZE,
        columns=["sample_id", "elapsed_seconds"],
    ):
        sample_ids = batch.column(0).to_pylist()
        elapsed = _strict_integer_array(
            batch.column(1).to_numpy(zero_copy_only=False),
            name="elapsed_seconds",
        )
        for row_index in np.flatnonzero(np.isin(elapsed, expected_offsets)).tolist():
            group_id = _sample_group_id(str(sample_ids[row_index]))
            if group_id != current_group_id:
                if group_id in seen_group_ids:
                    raise ValueError("materialized proxy dataset must keep each market contiguous")
                seen_group_ids.add(group_id)
                current_group_id = group_id
                market_index += 1
            if market_index % market_stride == 0:
                count += 1
    return count


def _load_materialized_proxy_batches(
    *,
    parquet: pq.ParquetFile,
    schema: FeatureSchema,
    expected_offsets: tuple[int, ...],
    market_stride: int,
    vectors: np.ndarray,
) -> tuple[list[ResearchSample], list[float]]:
    columns = (
        "sample_id",
        "feature_ts",
        "label_available_ts",
        "label",
        *schema.names,
    )
    samples: list[ResearchSample] = []
    weights: list[float] = []
    source_group_id: str | None = None
    source_market_index = -1
    active_group_id: str | None = None
    active_offsets: list[int] = []
    active_label: int | None = None
    active_label_available_ts: datetime | None = None
    previous_feature_ts: datetime | None = None
    seen_group_ids: set[str] = set()
    write_index = 0

    def finish_active_group() -> None:
        if active_group_id is None:
            return
        if tuple(active_offsets) != expected_offsets:
            raise ValueError(
                f"materialized proxy market {active_group_id!r} has incomplete snapshots"
            )

    for batch in parquet.iter_batches(batch_size=_MATERIALIZED_BATCH_SIZE, columns=columns):
        values = {name: batch.column(index) for index, name in enumerate(columns)}
        elapsed = _strict_integer_array(
            values["elapsed_seconds"].to_numpy(zero_copy_only=False),
            name="elapsed_seconds",
        )
        selected_indexes = np.flatnonzero(np.isin(elapsed, expected_offsets))
        if not len(selected_indexes):
            continue
        sample_ids = values["sample_id"].to_pylist()
        feature_times = values["feature_ts"].to_pylist()
        label_available_times = values["label_available_ts"].to_pylist()
        labels = _strict_integer_array(
            values["label"].to_numpy(zero_copy_only=False),
            name="label",
        )
        active_indexes: list[int] = []
        for row_index in selected_indexes.tolist():
            sample_id = str(sample_ids[row_index])
            group_id = _sample_group_id(sample_id)
            feature_ts = pd.Timestamp(feature_times[row_index]).to_pydatetime()
            label_available_ts = pd.Timestamp(label_available_times[row_index]).to_pydatetime()
            label = int(labels[row_index])
            if label not in {0, 1}:
                raise ValueError("materialized proxy labels must be binary")
            _, _, timestamp = sample_id.rpartition("@")
            if not timestamp.isdigit() or int(timestamp) != _ns(feature_ts):
                raise ValueError("materialized proxy sample_id timestamp must equal feature_ts")
            if previous_feature_ts is not None and feature_ts < previous_feature_ts:
                raise ValueError("materialized proxy dataset must be chronological")
            previous_feature_ts = feature_ts
            if source_group_id != group_id:
                finish_active_group()
                if group_id in seen_group_ids:
                    raise ValueError("materialized proxy dataset must keep each market contiguous")
                seen_group_ids.add(group_id)
                source_group_id = group_id
                source_market_index += 1
                active_group_id = group_id if source_market_index % market_stride == 0 else None
                active_offsets = []
                active_label = label
                active_label_available_ts = label_available_ts
            if active_group_id is None:
                continue
            if active_label != label or active_label_available_ts != label_available_ts:
                raise ValueError(f"materialized proxy market {group_id!r} has inconsistent labels")
            active_offsets.append(int(elapsed[row_index]))
            active_indexes.append(row_index)
            samples.append(
                ResearchSample(
                    sample_id=sample_id,
                    feature_ts=feature_ts,
                    label_available_ts=label_available_ts,
                    label=label,
                    group_id=group_id,
                )
            )
            weights.append(1.0 / len(expected_offsets))
        end_index = write_index + len(active_indexes)
        for feature_index, name in enumerate(schema.names):
            feature_values = np.asarray(values[name].to_numpy(zero_copy_only=False), dtype=float)
            vectors[write_index:end_index, feature_index] = feature_values[active_indexes]
        write_index = end_index
    finish_active_group()
    if write_index != len(vectors):
        raise ValueError("materialized proxy dataset changed while being read")
    return samples, weights


def _sample_group_id(sample_id: str) -> str:
    group_id, separator, timestamp = sample_id.rpartition("@")
    if not separator or not group_id or not timestamp.isdigit():
        raise ValueError(f"invalid materialized proxy sample_id: {sample_id!r}")
    return group_id


def _strict_integer_array(values: object, *, name: str) -> np.ndarray:
    raw = np.asarray(values)
    if (
        raw.ndim != 1
        or np.issubdtype(raw.dtype, np.bool_)
        or not (np.issubdtype(raw.dtype, np.integer) or np.issubdtype(raw.dtype, np.floating))
    ):
        raise ValueError(f"materialized proxy {name} must contain numeric integers")
    numeric = np.asarray(raw, dtype=float)
    if not np.isfinite(numeric).all() or np.any(numeric != np.floor(numeric)):
        raise ValueError(f"materialized proxy {name} must contain finite integers")
    return numeric.astype(np.int64)


def _candidate_configs() -> tuple[tuple[str, DirectionModelConfig], ...]:
    return (
        ("logistic-c0.1", DirectionModelConfig(kind="logistic", logistic_c=0.1)),
        ("logistic-c1", DirectionModelConfig(kind="logistic", logistic_c=1.0)),
        (
            "lightgbm-small",
            DirectionModelConfig(
                kind="lightgbm",
                lightgbm_num_leaves=7,
                lightgbm_max_depth=3,
                lightgbm_min_child_samples=200,
            ),
        ),
        (
            "lightgbm-base",
            DirectionModelConfig(
                kind="lightgbm",
                lightgbm_num_leaves=15,
                lightgbm_max_depth=4,
                lightgbm_min_child_samples=200,
            ),
        ),
    )


def _catalog_for_study(
    *, catalog_path: Path | None, start: datetime, end: datetime, rule_epoch: str
) -> tuple[MarketCatalog, int]:
    expected_count = sum(1 for _ in _market_slugs(start=start, end=end))
    if catalog_path is None:
        return asyncio.run(
            discover_closed_markets(start=start, end=end, rule_epoch=rule_epoch)
        ), expected_count
    catalog = read_market_catalog(catalog_path)
    windows = tuple(window for window in catalog.windows() if start <= window.t0 < end)
    if not windows:
        raise ValueError("market catalog has no windows in the requested range")
    if any(window.rule_epoch != rule_epoch for window in windows):
        raise ValueError("market catalog rule_epoch does not match --rule-epoch")
    return MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=windows), expected_count


def _split_config(profile: str) -> WalkForwardConfig:
    if profile == "full":
        return WalkForwardConfig()
    return WalkForwardConfig(
        train_duration=timedelta(days=35),
        calibration_duration=timedelta(days=10),
        test_duration=timedelta(days=7),
        step_duration=timedelta(days=7),
        embargo_duration=timedelta(hours=4, minutes=15),
        sealed_holdout_duration=timedelta(days=14),
    )


def _prediction_metrics(predictions: Iterable[object], *, weights: np.ndarray) -> dict[str, object]:
    items = tuple(predictions)
    indices = np.asarray([item.sample_index for item in items], dtype=int)
    return _probability_metrics(
        labels=np.asarray([item.label for item in items], dtype=int),
        probabilities=np.asarray([item.p_up for item in items], dtype=float),
        weights=weights[indices],
    )


def _prediction_metrics_by_regime(
    predictions: Iterable[object], *, dataset: DirectionDataset
) -> dict[str, dict[str, object]]:
    items_by_regime = _prediction_items_by_regime(predictions, dataset=dataset)
    weights = dataset.sample_weights
    assert weights is not None
    return {
        regime.value: _prediction_metrics(items_by_regime[regime], weights=weights)
        for regime in OPENING_REGIMES
    }


def _constant_probability_metrics_by_regime(
    predictions: Iterable[object], *, dataset: DirectionDataset, probability: float
) -> dict[str, dict[str, object]]:
    items_by_regime = _prediction_items_by_regime(predictions, dataset=dataset)
    weights = dataset.sample_weights
    assert weights is not None
    metrics: dict[str, dict[str, object]] = {}
    for regime in OPENING_REGIMES:
        items = items_by_regime[regime]
        indices = np.asarray([item.sample_index for item in items], dtype=int)
        metrics[regime.value] = _probability_metrics(
            labels=np.asarray([item.label for item in items], dtype=int),
            probabilities=np.full(len(items), probability),
            weights=weights[indices],
        )
    return metrics


def _prediction_items_by_regime(
    predictions: Iterable[object], *, dataset: DirectionDataset
) -> dict[object, tuple[object, ...]]:
    elapsed_index = dataset.schema.names.index("elapsed_seconds")
    items: dict[object, list[object]] = {regime: [] for regime in OPENING_REGIMES}
    for prediction in predictions:
        elapsed_seconds = float(dataset.vectors[prediction.sample_index, elapsed_index])
        items[opening_regime_for_elapsed_seconds(elapsed_seconds)].append(prediction)
    return {regime: tuple(items[regime]) for regime in OPENING_REGIMES}


def _probability_metrics(
    *, labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray
) -> dict[str, object]:
    raw_labels = np.asarray(labels)
    probabilities = np.asarray(probabilities, dtype=float)
    weights = np.asarray(weights, dtype=float)
    if not (raw_labels.ndim == probabilities.ndim == weights.ndim == 1):
        raise ValueError("probability metric arrays must be one-dimensional")
    if not len(raw_labels) or not (len(raw_labels) == len(probabilities) == len(weights)):
        raise ValueError("probability metric arrays must be non-empty and aligned")
    if np.issubdtype(raw_labels.dtype, np.bool_) or not (
        np.issubdtype(raw_labels.dtype, np.integer) or np.issubdtype(raw_labels.dtype, np.floating)
    ):
        raise ValueError("probability metric labels must be numeric binary values")
    numeric_labels = np.asarray(raw_labels, dtype=float)
    if not np.isfinite(numeric_labels).all() or np.any(
        (numeric_labels != 0.0) & (numeric_labels != 1.0)
    ):
        raise ValueError("probability metric labels must be binary")
    if not np.isfinite(probabilities).all() or np.any(
        (probabilities < 0.0) | (probabilities > 1.0)
    ):
        raise ValueError("probability metric predictions must be finite and lie in [0, 1]")
    if not np.isfinite(weights).all() or np.any(weights <= 0.0):
        raise ValueError("probability metric weights must be finite and positive")
    labels = numeric_labels
    clipped = np.clip(probabilities, 1e-6, 1.0 - 1e-6)
    bins: list[dict[str, object]] = []
    for lower in np.arange(0.0, 1.0, 0.1):
        upper = lower + 0.1
        mask = (clipped >= lower) & ((clipped < upper) if upper < 1.0 else (clipped <= upper))
        if not np.any(mask):
            continue
        bins.append(
            {
                "lower": round(float(lower), 1),
                "upper": round(float(upper), 1),
                "count": int(np.sum(mask)),
                "weight": float(np.sum(weights[mask])),
                "mean_prediction": float(np.average(clipped[mask], weights=weights[mask])),
                "up_rate": float(np.average(labels[mask], weights=weights[mask])),
            }
        )
    return {
        "sample_count": int(len(labels)),
        "effective_market_weight": float(np.sum(weights)),
        "log_loss": float(
            -np.average(
                labels * np.log(clipped) + (1 - labels) * np.log(1 - clipped), weights=weights
            )
        ),
        "brier": float(np.average((labels - clipped) ** 2, weights=weights)),
        "accuracy_at_0_5": float(np.average((clipped >= 0.5) == labels, weights=weights)),
        "mean_prediction": float(np.average(clipped, weights=weights)),
        "up_rate": float(np.average(labels, weights=weights)),
        "weighted_calibration_error": weighted_calibration_error(
            labels=labels,
            probabilities=clipped,
            weights=weights,
        ),
        "calibration_slope": (
            calibration_slope(labels=labels, probabilities=clipped, weights=weights)
            if len(np.unique(labels)) == 2
            else None
        ),
        "calibration_bins": bins,
    }


def _write_dataset(*, path: Path, dataset: object) -> None:
    frame = pd.DataFrame(dataset.vectors, columns=dataset.schema.names)
    frame.insert(0, "sample_weight", dataset.sample_weights)
    frame.insert(0, "label", dataset.labels)
    frame.insert(0, "label_available_ts", [item.label_available_ts for item in dataset.samples])
    frame.insert(0, "feature_ts", [item.feature_ts for item in dataset.samples])
    frame.insert(0, "sample_id", [item.sample_id for item in dataset.samples])
    frame.to_parquet(path, index=False)


def _write_predictions(
    *, path: Path, development: Sequence[object], holdout: Sequence[object], weights: np.ndarray
) -> None:
    rows = [
        {
            "split": "development_oof",
            "sample_id": item.sample_id,
            "feature_ts_ns": item.feature_ts_ns,
            "p_up": item.p_up,
            "label": item.label,
            "sample_weight": float(weights[item.sample_index]),
        }
        for item in development
    ] + [
        {
            "split": "sealed_holdout",
            "sample_id": item.sample_id,
            "feature_ts_ns": item.feature_ts_ns,
            "p_up": item.p_up,
            "label": item.label,
            "sample_weight": float(weights[item.sample_index]),
        }
        for item in holdout
    ]
    pd.DataFrame(rows).to_parquet(path, index=False)


def _market_slugs(*, start: datetime, end: datetime) -> Iterable[str]:
    current = start
    while current < end:
        yield BTC_15M_MARKET_FAMILY.slug_for(current)
        current += BTC_15M_MARKET_FAMILY.window_seconds_as_timedelta


def _batches(values: Sequence[str], size: int) -> Iterable[tuple[str, ...]]:
    for offset in range(0, len(values), size):
        yield tuple(values[offset : offset + size])


def _dates(start: date, end: date) -> Iterable[date]:
    current = start
    while current < end:
        yield current
        current += timedelta(days=1)


def _data_hash(*, archives: Sequence[Path], catalog_path: Path) -> str:
    digest = sha256()
    for path in (*archives, catalog_path):
        name = path.name.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _ns(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    utc = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


def main(argv: Sequence[str] | None = None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
