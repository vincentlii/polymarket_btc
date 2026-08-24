"""Gate PMXT bulk research from one complete 24-hour BTC 15m coverage pilot."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY  # noqa: E402


def evaluate_pmxt_coverage_pilot(
    *,
    start: datetime,
    end: datetime,
    reports: Mapping[str, Mapping[str, object]],
    minimum_ratio: float = 0.90,
) -> dict[str, object]:
    """Evaluate all 96 expected markets; absent reports count as ineligible."""

    start = _utc(start)
    end = _utc(end)
    if end - start != timedelta(hours=24):
        raise ValueError("PMXT pilot window must be exactly 24 hours")
    if int(start.timestamp()) % BTC_15M_MARKET_FAMILY.window_seconds:
        raise ValueError("PMXT pilot start must align to a BTC 15m market")
    if not 0.0 <= minimum_ratio <= 1.0:
        raise ValueError("minimum_ratio must lie in [0, 1]")

    expected = tuple(
        BTC_15M_MARKET_FAMILY.slug_for(start + timedelta(minutes=15 * index)) for index in range(96)
    )
    eligible: list[str] = []
    missing: list[str] = []
    ineligible: list[str] = []
    for slug in expected:
        report = reports.get(slug)
        if report is None:
            missing.append(slug)
            continue
        coverage = report.get("coverage")
        if not isinstance(coverage, Mapping):
            raise ValueError(f"PMXT report {slug!r} is missing coverage")
        if coverage.get("eligible_for_joint_replay") is True:
            eligible.append(slug)
        else:
            ineligible.append(slug)
    ratio = len(eligible) / len(expected)
    return {
        "schema_version": "btc-pmxt-24h-pilot-v1",
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "expected_market_count": len(expected),
        "reported_market_count": len(expected) - len(missing),
        "eligible_market_count": len(eligible),
        "eligible_market_ratio": ratio,
        "minimum_ratio": minimum_ratio,
        "bulk_acquisition_allowed": ratio >= minimum_ratio,
        "missing_markets": missing,
        "ineligible_markets": ineligible,
        "limitation": (
            "Coverage eligibility proves paired book/trade/gap availability only; "
            "it is not profitability or exact-FIFO evidence."
        ),
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=_parse_time)
    parser.add_argument("--end", required=True, type=_parse_time)
    parser.add_argument("--coverage-directory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--minimum-ratio", type=float, default=0.90)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    reports: dict[str, Mapping[str, object]] = {}
    for path in sorted(args.coverage_directory.glob("**/pmxt_coverage.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError(f"PMXT coverage report must be an object: {path}")
        slug = payload.get("market_slug")
        if not isinstance(slug, str) or not slug:
            raise ValueError(f"PMXT coverage report is missing market_slug: {path}")
        if slug in reports:
            raise ValueError(f"duplicate PMXT coverage report for {slug}")
        reports[slug] = payload
    result = evaluate_pmxt_coverage_pilot(
        start=args.start,
        end=args.end,
        reports=reports,
        minimum_ratio=args.minimum_ratio,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    return result


def _parse_time(value: str) -> datetime:
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 datetime: {value!r}") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC offset")
    return value.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    result = run(parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["bulk_acquisition_allowed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
