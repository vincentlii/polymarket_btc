"""Replay Hold/Sell-FAK/Pair-lock research over recorded executable L2 ladders."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import os
import subprocess
from typing import Sequence
from uuid import uuid4

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.research.exit_replay import (  # noqa: E402
    BookLevel,
    ExitReplayInput,
    replay_exit_policies,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.output_json.exists():
        raise FileExistsError(f"output already exists: {args.output_json}")
    encoded = args.input_json.read_bytes()
    raw = json.loads(encoded)
    if not isinstance(raw, list) or not raw:
        raise ValueError("input JSON must be a non-empty array")
    reports = [replay_exit_policies(_input(value)) for value in raw]
    payload = {
        "schema_version": "btc-exit-policy-replay-v1",
        "promotion_eligible": False,
        "evidence_level": "operator_supplied_ladders_not_raw_derived",
        "input_sha256": sha256(encoded).hexdigest(),
        "code_revision": _code_revision(),
        "config_sha256": sha256(
            json.dumps(
                [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"side_bids", "opposite_asks"}
                    }
                    for item in raw
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "rule_hashes": sorted({str(item.get("rule_hash")) for item in raw}),
        "fee_rule_hashes": sorted({report.fee_rule_hash for report in reports}),
        "market_count": len({report.market_slug for report in reports}),
        "decision_count": len(reports),
        "realized_pnl": sum(report.realized_pnl for report in reports),
        "reports": [asdict(report) for report in reports],
        "policy_summary": {
            action: {
                "count": sum(report.action.value == action for report in reports),
                "realized_pnl": sum(
                    report.realized_pnl for report in reports if report.action.value == action
                ),
            }
            for action in ("hold", "sell_fak", "buy_opposite_fak")
        },
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    staging = args.output_json.with_name(f".{args.output_json.name}.staging-{uuid4().hex}")
    try:
        with staging.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        staging.replace(args.output_json)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    print(
        json.dumps(
            {key: payload[key] for key in ("market_count", "decision_count", "realized_pnl")}
        )
    )
    return 0


def _input(raw: object) -> ExitReplayInput:
    if not isinstance(raw, dict):
        raise ValueError("each replay input must be an object")
    values = dict(raw)
    values.pop("rule_hash", None)
    values["side_bids"] = tuple(BookLevel(**value) for value in values.get("side_bids", ()))
    values["opposite_asks"] = tuple(BookLevel(**value) for value in values.get("opposite_asks", ()))
    return ExitReplayInput(**values)


def _code_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
