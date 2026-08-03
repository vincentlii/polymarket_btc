"""Run the development-only frozen BTC opening-factor challenge."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pandas as pd

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.research.binance_history import (  # noqa: E402
    binance_spot_kline_url,
    binance_um_futures_kline_url,
    load_binance_kline_archives,
)
from btc_short_horizon.research.materialized_dataset import read_materialized_direction_dataset  # noqa: E402
from btc_short_horizon.research.opening_factor_challenge import build_opening_factor_dataset  # noqa: E402
from btc_short_horizon.research.opening_factor_research import (  # noqa: E402
    FACTOR_CANDIDATES,
    FactorCandidateResult,
    development_report,
    run_opening_factor_development,
)
from btc_short_horizon.research.opening_proxy import opening_proxy_feature_schema  # noqa: E402
from btc_short_horizon.research.provenance import git_provenance  # noqa: E402
from btc_short_horizon.research.walk_forward import WalkForwardConfig  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--materialized-dataset", type=Path, required=True)
    parser.add_argument("--spot-directory", type=Path, required=True)
    parser.add_argument("--perp-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 4, 27))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 7, 12))
    parser.add_argument("--download-missing", action="store_true")
    parser.add_argument("--bootstrap-iterations", type=int, default=10_000)
    parser.add_argument("--minimum-eligible-markets", type=int, default=1)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.output_directory.exists():
        raise FileExistsError(f"output directory already exists: {args.output_directory}")
    if args.end_date < args.start_date:
        raise ValueError("end-date must not precede start-date")
    dates = tuple(_dates(args.start_date, args.end_date))
    spot = _archives(
        args.spot_directory, dates, url=binance_spot_kline_url, download=args.download_missing
    )
    perp = _archives(
        args.perp_directory, dates, url=binance_um_futures_kline_url, download=args.download_missing
    )
    control = read_materialized_direction_dataset(
        args.materialized_dataset, expected_schema=opening_proxy_feature_schema(1)
    )
    build = build_opening_factor_dataset(
        control=control,
        spot_klines=load_binance_kline_archives(spot["paths"], interval="1m"),
        perp_klines=load_binance_kline_archives(perp["paths"], interval="1m"),
    )
    progress_path = args.output_directory.with_name(f".{args.output_directory.name}.progress.json")
    if progress_path.exists():
        raise FileExistsError(f"progress file already exists: {progress_path}")
    progress: dict[str, object] = {
        "development_only": True,
        "sealed_holdout_evaluated": False,
        "status": "running",
        "total_candidates": len(FACTOR_CANDIDATES),
        "completed": [],
    }
    _write_progress_atomic(progress_path, progress)

    def record_candidate(item: FactorCandidateResult) -> None:
        completed = list(progress["completed"])
        completed.append(
            {
                "name": item.candidate.name,
                "log_loss": item.log_loss,
                "brier": item.brier,
                "calibration_error": item.calibration_error,
                "oof_prediction_count": len(item.predictions),
            }
        )
        progress["completed"] = completed
        _write_progress_atomic(progress_path, progress)
        print(
            f"factor challenge candidate {len(completed)}/{len(FACTOR_CANDIDATES)}: "
            f"{item.candidate.name}",
            flush=True,
        )

    try:
        development = run_opening_factor_development(
            build=build,
            split_config=WalkForwardConfig(
                train_duration=timedelta(days=35),
                calibration_duration=timedelta(days=10),
                test_duration=timedelta(days=7),
                step_duration=timedelta(days=7),
                embargo_duration=timedelta(hours=4, minutes=15),
                sealed_holdout_duration=timedelta(days=14),
            ),
            bootstrap_iterations=args.bootstrap_iterations,
            minimum_eligible_markets=args.minimum_eligible_markets,
            progress_callback=record_candidate,
        )
    except Exception as exc:
        progress["status"] = "failed"
        progress["error_type"] = type(exc).__name__
        progress["error"] = str(exc)
        _write_progress_atomic(progress_path, progress)
        raise
    report = development_report(development)
    lineage = {
        "development_only": True,
        "sealed_holdout_evaluated": False,
        "materialized_dataset": {
            "path": str(args.materialized_dataset),
            "sha256": _sha256(args.materialized_dataset),
        },
        "spot_archives": _archive_manifest(spot),
        "perp_archives": _archive_manifest(perp),
        "control_schema_hash": build.paired_control_dataset.schema.hash,
        "factor_schema_hash": build.factor_dataset.schema.hash,
        "git_provenance": git_provenance(),
    }
    report["dataset_lineage_sha256"] = _canonical_hash(lineage)
    report["dataset_lineage"] = lineage
    rows = [
        {
            "candidate": item.candidate.name,
            "fold_index": p.fold_index,
            "sample_id": p.sample_id,
            "feature_ts_ns": p.feature_ts_ns,
            "p_up": p.p_up,
            "label": p.label,
            "sample_weight": float(build.paired_control_dataset.sample_weights[p.sample_index]),
        }
        for item in development.candidates
        for p in item.predictions
    ]
    progress["status"] = "complete"
    _write_progress_atomic(progress_path, progress)
    _write_atomic_output(
        args.output_directory,
        report=report,
        lineage=lineage,
        rows=rows,
        progress=progress,
    )
    progress_path.unlink()
    return report


def _archives(
    directory: Path, dates: tuple[date, ...], *, url, download: bool
) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    downloaded: list[Path] = []
    for current in dates:
        path = directory / f"BTCUSDT-1m-{current.isoformat()}.zip"
        if not path.is_file():
            if not download:
                raise FileNotFoundError(f"missing 1m archive: {path}")
            temporary = path.with_suffix(".download")
            try:
                with httpx.stream("GET", url(day=current.isoformat()), timeout=60.0) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            handle.write(chunk)
                temporary.replace(path)
                downloaded.append(path)
            finally:
                temporary.unlink(missing_ok=True)
        paths.append(path)
    return {"paths": tuple(paths), "downloaded": tuple(downloaded)}


def _archive_manifest(payload: dict[str, object]) -> dict[str, object]:
    paths = tuple(payload["paths"])
    downloaded = {Path(path) for path in tuple(payload["downloaded"])}
    return {
        "planned_days": len(paths),
        "actual_bytes": sum(path.stat().st_size for path in paths),
        "downloaded_bytes": sum(path.stat().st_size for path in downloaded),
        "days": [
            {
                "name": path.name,
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "downloaded": path in downloaded,
            }
            for path in paths
        ],
    }


def _write_atomic_output(
    output: Path,
    *,
    report: dict[str, object],
    lineage: dict[str, object],
    rows: list[dict[str, object]],
    progress: dict[str, object],
) -> None:
    staging = output.with_name(f".{output.name}.staging-{uuid4().hex}")
    try:
        staging.mkdir(parents=True)
        (staging / "development_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (staging / "dataset_lineage.json").write_text(
            json.dumps(lineage, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (staging / "candidate_progress.json").write_text(
            json.dumps(progress, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        pd.DataFrame(rows).to_parquet(staging / "predictions.parquet", index=False)
        staging.replace(output)
    except Exception:
        for child in staging.glob("*") if staging.exists() else ():
            child.unlink()
        staging.rmdir() if staging.exists() else None
        raise


def _write_progress_atomic(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _dates(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
