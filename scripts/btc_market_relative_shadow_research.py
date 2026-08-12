"""Run the bounded point-rule market-relative Logistic development study."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data.catalog_io import read_market_catalog  # noqa: E402
from btc_short_horizon.research.market_relative_shadow import (  # noqa: E402
    load_market_relative_shadow_dataset,
    run_market_relative_logistic_development,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shadow-root", type=Path, required=True)
    parser.add_argument("--market-catalog", type=Path, action="append", required=True)
    parser.add_argument("--end-before", required=True, help="Exclusive ISO-8601 rule boundary.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-iterations", type=int, default=5_000)
    parser.add_argument("--maximum-pair-age-seconds", type=float, default=1.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    end_before = datetime.fromisoformat(args.end_before.replace("Z", "+00:00"))
    if end_before.tzinfo is None or end_before.utcoffset() is None:
        raise ValueError("--end-before must include a timezone")
    labels: dict[str, int] = {}
    for path in args.market_catalog:
        for market in read_market_catalog(path).windows():
            if market.resolution is None or market.resolution.value == "void":
                continue
            label = int(market.resolution.value == "up")
            previous = labels.setdefault(market.slug, label)
            if previous != label:
                raise ValueError(f"catalogs disagree on outcome for {market.slug}")
    build = load_market_relative_shadow_dataset(
        shadow_root=args.shadow_root,
        labels_by_market=labels,
        end_before_epoch_seconds=int(end_before.timestamp()),
        maximum_pair_age_seconds=args.maximum_pair_age_seconds,
    )
    development = run_market_relative_logistic_development(
        dataset=build.dataset,
        bootstrap_iterations=args.bootstrap_iterations,
    )
    source_hash = _source_hash(
        [*args.market_catalog, *sorted(args.shadow_root.glob("*/predictions.parquet"))]
    )
    payload = {
        "study_type": "market_relative_shadow_logistic_development_v1",
        "rule_epoch": "chainlink-btc-usd-point-v1",
        "end_before": end_before.isoformat(),
        "source_hash": source_hash,
        "coverage": {
            "discovered_market_files": build.discovered_market_files,
            "eligible_market_count": build.dataset.market_count,
            "eligible_snapshot_count": len(build.dataset.labels),
            "excluded_missing_label_markets": build.excluded_missing_label_markets,
            "excluded_invalid_artifact_markets": build.excluded_invalid_artifact_markets,
            "excluded_missing_stage_markets": build.excluded_missing_stage_markets,
            "excluded_at_or_after_rule_boundary_markets": (
                build.excluded_at_or_after_rule_boundary_markets
            ),
        },
        "protocol": {
            "logistic_c": [0.01, 0.03, 0.1, 0.3, 1.0],
            "train_days": 4,
            "calibration_days": 1,
            "test_days": 1,
            "pair_maximum_age_seconds": args.maximum_pair_age_seconds,
            "calibration": "identity",
            "weighting": "one-market-one-weight-equal-three-stages-v1",
            "bootstrap_iterations": args.bootstrap_iterations,
            "sealed_holdout_evaluated": False,
        },
        "development": asdict(development),
        "artifact_published": False,
        "deployment_allowed": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        f"Market-relative development accepted={development.accepted} "
        f"eligible={build.dataset.market_count} oof={development.oof_market_count} "
        f"report={args.output}"
    )
    return 0


def _source_hash(paths: Sequence[Path]) -> str:
    digest = sha256()
    for path in sorted(paths, key=lambda item: str(item)):
        digest.update(str(path.name).encode())
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
