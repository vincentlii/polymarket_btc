"""Replay one BTC 15m Research Paper market from immutable forward raw data."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.execution_timing import (  # noqa: E402
    paper_execution_lifecycle_tail_seconds,
)
from btc_short_horizon.data import read_market_catalog  # noqa: E402
from btc_short_horizon.live.paper_replay import (  # noqa: E402
    PaperReplayRawStream,
    build_replay_binance_history,
    load_research_paper_events,
    replay_research_paper,
)
from btc_short_horizon.live.paper_runtime import (  # noqa: E402
    ModelPaperPredictor,
    build_research_paper_portfolio,
)
from btc_short_horizon.live.research_paper import PaperRuleSnapshotStore  # noqa: E402


def _final_decision_ts_ns(project, market_start_ns: int) -> int:  # type: ignore[no-untyped-def]
    return market_start_ns + round(
        (
            project.research_timing.entry_end_seconds
            + max(
                variant.maker_work_seconds
                for variant in project.paper_execution_variants
                if variant.enabled
            )
        )
        * 1_000_000_000
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
    )
    parser.add_argument("--market-catalog", type=Path, required=True)
    parser.add_argument("--market-slug", required=True)
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--raw-data-root", type=Path, required=True)
    parser.add_argument(
        "--rules-runtime-root",
        type=Path,
        required=True,
        help="Runtime root containing the frozen Paper rule snapshot.",
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--starting-balance", type=float, default=1_000.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = load_btc_project_config(args.config)
    enabled_variants = tuple(
        variant for variant in project.paper_execution_variants if variant.enabled
    )
    market = read_market_catalog(args.market_catalog).require(args.market_slug)
    rules = PaperRuleSnapshotStore(
        args.rules_runtime_root,
        project.paper_execution_epoch,
    ).read(market)
    epoch_root = args.output_root / "paper" / "epochs" / project.paper_execution_epoch
    if epoch_root.exists():
        raise FileExistsError(
            "replay output epoch already exists; choose a new output root to preserve evidence"
        )

    predictor = ModelPaperPredictor(
        project=project,
        model_directory=args.model_directory,
    )
    portfolio = build_research_paper_portfolio(
        project=project,
        predictor=predictor,
        runtime_root=args.output_root,
        starting_balance=args.starting_balance,
    )
    scenario = project.require_scenario("p99_half_volume_book_first").execution
    latency = scenario.latency_model
    maximum_taker_server_delay_ms = max(rule.taker_server_delay_ms for rule in rules.values())
    lifecycle_tail_seconds = max(
        paper_execution_lifecycle_tail_seconds(
            mode=variant.mode,
            maker_work_seconds=variant.maker_work_seconds,
            cancel_latency_ms=latency.base_latency_ms + latency.cancel_latency_ms,
            taker_latency_ms=latency.base_latency_ms + latency.insert_latency_ms,
            taker_server_delay_ms=maximum_taker_server_delay_ms,
        )
        for variant in enabled_variants
    )
    replay_end = market.t0 + timedelta(
        seconds=(project.research_timing.entry_end_seconds + lifecycle_tail_seconds + 1.0)
    )
    raw_start = market.t0 - timedelta(
        seconds=project.research_timing.max_feature_lookback_seconds + 5
    )
    events = load_research_paper_events(
        raw_data_root=args.raw_data_root,
        streams=(
            PaperReplayRawStream("binance_spot", "BTCUSDT"),
            PaperReplayRawStream("polymarket_clob", market.up_token_id),
            PaperReplayRawStream("polymarket_clob", market.down_token_id),
        ),
        start_time=raw_start,
        end_time=replay_end,
    )
    market_start_ns = int(market.t0.timestamp() * 1_000_000_000)
    portfolio.set_kline_history(
        build_replay_binance_history(
            events,
            cutoff_ts_ns=market_start_ns,
            minimum_bars=project.research_timing.max_feature_lookback_seconds,
        )
    )
    cadence_ns = round(project.maker.signal_cadence_seconds * 1_000_000_000)
    first_decision_ns = market_start_ns + round(
        max(
            project.maker.entry_start_seconds,
            project.maker.signal_cadence_seconds,
        )
        * 1_000_000_000
    )
    final_decision_ns = _final_decision_ts_ns(project, market_start_ns)
    decisions = tuple(range(first_decision_ns, final_decision_ns + 1, cadence_ns))
    replay_end_ns = int(replay_end.timestamp() * 1_000_000_000)
    settlement = (
        {}
        if market.resolution is None or market.label_available_ts is None
        else {
            "outcome": market.resolution,
            "label_available_ts_ns": int(market.label_available_ts.timestamp() * 1_000_000_000),
        }
    )
    result = replay_research_paper(
        portfolio=portfolio,
        market=market,
        rules=rules,
        events=events,
        decision_ts_ns=decisions,
        replay_end_ts_ns=replay_end_ns,
        **settlement,
    )
    snapshot = portfolio.dashboard_snapshot(now=replay_end)
    assert snapshot.performance is not None
    summary = {
        "market_slug": market.slug,
        "model_id": portfolio.model_id,
        "paper_execution_epoch": project.paper_execution_epoch,
        "processed_event_count": result.processed_event_count,
        "decision_count": len(result.decisions),
        "last_decisions": portfolio.last_decisions,
        "variants": [item.to_json() for item in snapshot.performance.variant_summaries],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
