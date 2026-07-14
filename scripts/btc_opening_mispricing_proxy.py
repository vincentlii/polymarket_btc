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
import subprocess

import httpx
import numpy as np
import pandas as pd

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketCatalog  # noqa: E402
from btc_short_horizon.data.catalog_io import read_market_catalog, write_market_catalog  # noqa: E402
from btc_short_horizon.data.gamma import GammaMarketClient  # noqa: E402
from btc_short_horizon.models import (  # noqa: E402
    DirectionModelConfig,
    ModelArtifactMetadata,
    ModelArtifactStore,
)
from btc_short_horizon.research import (  # noqa: E402
    WalkForwardConfig,
    run_sealed_holdout_model,
    run_walk_forward_model,
)
from btc_short_horizon.research.binance_history import (  # noqa: E402
    binance_spot_kline_url,
    load_binance_kline_archives,
)
from btc_short_horizon.research.opening_proxy import build_opening_proxy_dataset  # noqa: E402


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
    parser.add_argument("--binance-directory", type=Path, required=True)
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
    archives = (
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
    catalog, expected_market_count = _catalog_for_study(
        catalog_path=args.market_catalog,
        start=start,
        end=end,
        rule_epoch=args.rule_epoch,
    )
    build = build_opening_proxy_dataset(
        markets=catalog.windows(),
        klines=load_binance_kline_archives(archives, interval=args.interval),
        snapshot_seconds=args.snapshot_seconds,
        entry_start_seconds=args.entry_start_seconds,
        entry_end_seconds=args.entry_end_seconds,
    )
    split = _split_config(args.profile)
    development: list[tuple[str, DirectionModelConfig, object, dict[str, object]]] = []
    for name, config in _candidate_configs():
        model_run = run_walk_forward_model(
            dataset=build.dataset,
            split_config=split,
            model_config=config,
        )
        development.append(
            (
                name,
                config,
                model_run,
                _prediction_metrics(
                    model_run.predictions,
                    weights=build.dataset.sample_weights,
                ),
            )
        )
    selected_name, selected_config, selected_development, selected_metrics = min(
        development,
        key=lambda item: (float(item[3]["log_loss"]), float(item[3]["brier"])),
    )
    holdout = run_sealed_holdout_model(
        dataset=build.dataset,
        split_config=split,
        model_config=selected_config,
    )
    output = args.output_directory
    output.mkdir(parents=True)
    write_market_catalog(path=output / "market_catalog.json", catalog=catalog)
    data_hash = _data_hash(archives=archives, catalog_path=output / "market_catalog.json")
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
            model_id=f"btc-15m-opening-proxy-{selected_name}",
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
            code_revision=_git_revision(),
            config=holdout.model.config_dict,
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
    result = {
        "study_type": "opening_mispricing_fair_probability_proxy",
        "profile": args.profile,
        "date_range": {
            "start_inclusive": args.start_date.isoformat(),
            "end_exclusive": args.end_date.isoformat(),
        },
        "entry_protocol": {
            "start_seconds": args.entry_start_seconds,
            "end_seconds": args.entry_end_seconds,
            "snapshot_seconds": args.snapshot_seconds,
            "per_market_total_sample_weight": 1.0,
        },
        "sources": {
            "gamma": "https://gamma-api.polymarket.com/markets/keyset",
            "binance": (
                f"https://data.binance.vision/data/spot/daily/klines/BTCUSDT/{args.interval}/"
            ),
            "archive_count": len(archives),
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
            "snapshots_per_market": build.snapshots_per_market,
            "feature_schema_hash": build.dataset.schema.hash,
            "historical_availability_delay_seconds": 1,
        },
        "development_candidates": [
            {"name": name, "config": asdict(config), "metrics": metrics}
            for name, config, _, metrics in development
        ],
        "selected_on_development": {"name": selected_name, "metrics": selected_metrics},
        "sealed_holdout": {
            "metrics": _prediction_metrics(
                holdout.predictions,
                weights=build.dataset.sample_weights,
            ),
            "rolling_prior_probability": prior_probability,
            "rolling_prior_metrics": _probability_metrics(
                labels=np.asarray([item.label for item in holdout.predictions], dtype=int),
                probabilities=np.full(len(holdout.predictions), prior_probability),
                weights=holdout_weights,
            ),
            "sample_count": len(holdout.predictions),
        },
        "model_sha256": artifact.model_sha256,
        "limitations": [
            "This proxy evaluates fair-probability feasibility, not Polymarket mispricing or maker PnL.",
            "The opening reference is the last causally available Binance kline, not the final Chainlink reference.",
            "No historical Polymarket CLOB probability, L2 queue, fill, fee, rebate, or latency evidence is used.",
            "Binance archive timestamps are event-time-only and do not measure network latency.",
        ],
    }
    (output / "metrics.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


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


def _probability_metrics(
    *, labels: np.ndarray, probabilities: np.ndarray, weights: np.ndarray
) -> dict[str, object]:
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
        digest.update(path.name.encode())
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _git_revision() -> str:
    try:
        return subprocess.check_output(
            ("git", "rev-parse", "HEAD"), text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "working-tree"


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def main(argv: Sequence[str] | None = None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
