"""Evaluate sparse Polymarket price edge using existing OOF/holdout BTC probabilities."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import asdict
import json
from math import ceil
from numbers import Integral, Real
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np
import pandas as pd

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.data import MarketOutcome, MarketWindow, read_market_catalog  # noqa: E402
from btc_short_horizon.data.storage import sha256_file, write_atomic_json  # noqa: E402
from btc_short_horizon.research.polymarket_price_history import (  # noqa: E402
    TokenPricePoint,
    fetch_polymarket_price_history,
)
from btc_short_horizon.research.opening_proxy import (  # noqa: E402
    OPENING_REGIMES,
    opening_regime_for_elapsed_seconds,
)
from btc_short_horizon.research.provenance import git_provenance  # noqa: E402


_THRESHOLDS = (0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.075, 0.10)
_REQUIRED_PRICE_COLUMNS = {"token_id", "ts_seconds", "price"}
_SELECTION_FAMILY_ALPHA = 0.05 / (len(_THRESHOLDS) * len(OPENING_REGIMES))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-artifact-directory", type=Path, required=True)
    parser.add_argument("--price-cache", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--entry-price-buffer", type=float, default=0.01)
    parser.add_argument("--max-price-age-seconds", type=int, default=75)
    parser.add_argument("--min-development-entries", type=int, default=100)
    parser.add_argument("--bootstrap-resamples", type=int, default=10_000)
    parser.add_argument("--max-fetch-concurrency", type=int, default=4)
    parser.add_argument("--minimum-consecutive-signals", type=int, default=2)
    parser.add_argument("--maximum-signal-gap-seconds", type=int, default=5)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, object]:
    _validate_args(args)
    if args.output_directory.exists():
        raise FileExistsError(f"output directory already exists: {args.output_directory}")
    code_provenance = git_provenance()
    predictions_path = args.proxy_artifact_directory / "predictions.parquet"
    catalog_path = args.proxy_artifact_directory / "market_catalog.json"
    predictions = pd.read_parquet(predictions_path)
    _validate_predictions(predictions)
    predictions = predictions.copy()
    predictions["market_slug"] = predictions["sample_id"].str.rsplit("@", n=1).str[0]
    catalog = read_market_catalog(catalog_path)
    markets_by_slug = {market.slug: market for market in catalog.windows()}
    selected_slugs = tuple(sorted(predictions["market_slug"].unique().tolist()))
    missing_markets = set(selected_slugs) - set(markets_by_slug)
    if missing_markets:
        raise ValueError(f"prediction markets missing from catalog: {len(missing_markets)}")
    markets = tuple(markets_by_slug[slug] for slug in selected_slugs)
    _validate_market_labels(predictions=predictions, markets=markets)
    maximum_prediction_offset_seconds = _maximum_prediction_offset_seconds(
        predictions=predictions,
        markets_by_slug=markets_by_slug,
    )
    prices = _load_or_fetch_prices(
        cache_path=args.price_cache,
        markets=markets,
        max_concurrency=args.max_fetch_concurrency,
        maximum_prediction_offset_seconds=maximum_prediction_offset_seconds,
    )
    coverage_path = args.price_cache.with_suffix(f"{args.price_cache.suffix}.coverage.json")
    source_hashes = {
        "predictions_sha256": sha256_file(predictions_path),
        "market_catalog_sha256": sha256_file(catalog_path),
        "price_cache_sha256": sha256_file(args.price_cache),
        "price_cache_coverage_sha256": sha256_file(coverage_path),
    }
    candidates, coverage = _build_candidates(
        predictions=predictions,
        prices=prices,
        markets=markets,
        entry_price_buffer=args.entry_price_buffer,
        max_price_age_seconds=args.max_price_age_seconds,
    )
    if candidates.empty:
        raise ValueError("no prediction had causally aligned dual-token price history")

    development = candidates[candidates["split"] == "development_oof"]
    holdout = candidates[candidates["split"] == "sealed_holdout"]
    development_market_count = predictions.loc[
        predictions["split"] == "development_oof", "market_slug"
    ].nunique()
    holdout_market_count = predictions.loc[
        predictions["split"] == "sealed_holdout", "market_slug"
    ].nunique()
    regime_selection = _select_regime_thresholds(
        development,
        requested_markets=development_market_count,
        min_development_entries=args.min_development_entries,
        bootstrap_resamples=args.bootstrap_resamples,
        minimum_consecutive_signals=args.minimum_consecutive_signals,
        maximum_signal_gap_seconds=args.maximum_signal_gap_seconds,
    )
    thresholds_by_regime = {
        regime: float(selection["threshold"])
        for regime, selection in regime_selection.items()
        if selection["threshold"] is not None
    }
    if not thresholds_by_regime:
        raise ValueError("no opening regime has the required development entry count")
    development_entries = _select_one_entry_per_market_by_regime(
        development,
        thresholds_by_regime=thresholds_by_regime,
        minimum_consecutive_signals=args.minimum_consecutive_signals,
        maximum_signal_gap_seconds=args.maximum_signal_gap_seconds,
    )
    holdout_entries = _select_one_entry_per_market_by_regime(
        holdout,
        thresholds_by_regime=thresholds_by_regime,
        minimum_consecutive_signals=args.minimum_consecutive_signals,
        maximum_signal_gap_seconds=args.maximum_signal_gap_seconds,
    )
    holdout_metrics = _entry_metrics(
        holdout_entries,
        requested_markets=holdout_market_count,
        bootstrap_resamples=args.bootstrap_resamples,
        seed=29,
    )
    holdout_sensitivities = [
        {
            "name": name,
            "additional_entry_cost": additional_cost,
            "max_price_age_seconds": max_age,
            **_entry_metrics(
                _sensitivity_entries(
                    holdout,
                    threshold=None,
                    thresholds_by_regime=thresholds_by_regime,
                    additional_entry_cost=additional_cost,
                    max_price_age_seconds=max_age,
                    minimum_consecutive_signals=args.minimum_consecutive_signals,
                    maximum_signal_gap_seconds=args.maximum_signal_gap_seconds,
                ),
                requested_markets=holdout_market_count,
                bootstrap_resamples=args.bootstrap_resamples,
                seed=seed,
            ),
        }
        for name, additional_cost, max_age, seed in (
            ("frozen_regime_thresholds_extra_1c", 0.01, args.max_price_age_seconds, 31),
            ("frozen_regime_thresholds_extra_1c_age15s", 0.01, 15, 37),
        )
    ]

    output = args.output_directory
    output.mkdir(parents=True)
    candidates.to_parquet(output / "opportunities.parquet", index=False)
    pd.concat(
        (
            development_entries.assign(split="development_oof"),
            holdout_entries.assign(split="sealed_holdout"),
        ),
        ignore_index=True,
    ).to_parquet(output / "selected_entries.parquet", index=False)
    result = {
        "study_type": "opening_market_price_edge_proxy",
        "probability_artifact": str(args.proxy_artifact_directory),
        "price_cache": str(args.price_cache),
        "source_hashes": source_hashes,
        "code_provenance": code_provenance,
        "entry_protocol": {
            "one_entry_per_market": True,
            "earliest_qualifying_candidate": True,
            "entry_price_buffer": args.entry_price_buffer,
            "max_price_age_seconds": args.max_price_age_seconds,
            "price_fidelity_minutes": 1,
            "minimum_consecutive_signals": args.minimum_consecutive_signals,
            "maximum_signal_gap_seconds": args.maximum_signal_gap_seconds,
            "bootstrap_resamples": args.bootstrap_resamples,
            "selection_family_size": len(_THRESHOLDS) * len(OPENING_REGIMES),
            "selection_family_alpha": _SELECTION_FAMILY_ALPHA,
            "regimes": [regime.value for regime in OPENING_REGIMES],
        },
        "coverage": coverage,
        "selected_on_development": {
            "thresholds_by_regime": thresholds_by_regime,
            "regimes": regime_selection,
            "combined": _entry_metrics(
                development_entries,
                requested_markets=development_market_count,
                bootstrap_resamples=args.bootstrap_resamples,
                seed=17,
            ),
            "diagnostics": _entry_diagnostics(development_entries),
        },
        "sealed_holdout": {
            "thresholds_by_regime": thresholds_by_regime,
            **holdout_metrics,
            "diagnostics": _entry_diagnostics(holdout_entries),
            "by_regime": _entry_metrics_by_regime(
                holdout_entries,
                requested_markets=holdout_market_count,
                bootstrap_resamples=args.bootstrap_resamples,
            ),
            "fixed_threshold_sensitivities": holdout_sensitivities,
        },
        "limitations": [
            "Polymarket prices-history is sparse one-minute token price evidence, not L2 BBO.",
            "The entry buffer is a conservative proxy, not queue position, latency, or a fill.",
            "This study can validate market-relative model edge but cannot validate maker PnL.",
            "Threshold selection uses development OOF only; sealed holdout is evaluated once.",
        ],
    }
    write_atomic_json(output / "metrics.json", result)
    return result


def _maximum_prediction_offset_seconds(
    *, predictions: pd.DataFrame, markets_by_slug: Mapping[str, MarketWindow]
) -> int:
    offsets: list[int] = []
    for prediction in predictions.itertuples(index=False):
        market = markets_by_slug[str(prediction.market_slug)]
        elapsed_seconds = int(prediction.feature_ts_ns // 1_000_000_000) - int(
            market.t0.timestamp()
        )
        opening_regime_for_elapsed_seconds(elapsed_seconds)
        offsets.append(elapsed_seconds)
    if not offsets:
        raise ValueError("prediction artifact contains no opening decisions")
    return max(offsets)


def _load_or_fetch_prices(
    *,
    cache_path: Path,
    markets: Sequence[MarketWindow],
    max_concurrency: int,
    maximum_prediction_offset_seconds: int,
) -> pd.DataFrame:
    expected_tokens = {
        token_id for market in markets for token_id in (market.up_token_id, market.down_token_id)
    }
    coverage_path = cache_path.with_suffix(f"{cache_path.suffix}.coverage.json")
    cache_exists = cache_path.is_file()
    if cache_exists:
        cached = pd.read_parquet(cache_path)
        _validate_price_rows(cached)
    else:
        cached = pd.DataFrame(columns=sorted(_REQUIRED_PRICE_COLUMNS))
    cached_tokens = set(cached["token_id"].astype(str).unique().tolist())
    manifest_tokens = _price_cache_coverage(coverage_path)
    if manifest_tokens - cached_tokens:
        raise ValueError("price cache coverage claims tokens which have no cached evidence")
    complete_tokens = manifest_tokens | cached_tokens
    missing_markets = tuple(
        market
        for market in sorted(markets, key=lambda item: item.t0)
        if market.up_token_id not in complete_tokens or market.down_token_id not in complete_tokens
    )
    if missing_markets:
        fetched = asyncio.run(
            _fetch_market_price_batches(
                markets=missing_markets,
                cached_tokens=complete_tokens,
                max_concurrency=max_concurrency,
                maximum_prediction_offset_seconds=maximum_prediction_offset_seconds,
            )
        )
        new_rows = pd.DataFrame(
            (asdict(point) for point in fetched),
            columns=sorted(_REQUIRED_PRICE_COLUMNS),
        )
        if not new_rows.empty:
            _validate_price_rows(new_rows)
            cached = new_rows if cached.empty else pd.concat((cached, new_rows), ignore_index=True)
            _validate_price_rows(cached)
            cached = cached.drop_duplicates(
                subset=["token_id", "ts_seconds", "price"],
            ).sort_values(["token_id", "ts_seconds", "price"])
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(f".{cache_path.name}.{uuid4().hex}.tmp")
            try:
                cached.to_parquet(temporary, index=False)
                temporary.replace(cache_path)
            finally:
                temporary.unlink(missing_ok=True)
        complete_tokens = set(cached["token_id"].astype(str).unique().tolist())
    write_atomic_json(
        coverage_path,
        {"version": 2, "complete_token_ids": sorted(complete_tokens)},
    )
    return cached[cached["token_id"].astype(str).isin(expected_tokens)].copy()


def _price_cache_coverage(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "attempted_token_ids" in payload:
        return set()
    values = payload.get("complete_token_ids") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("version") != 2:
        values = None
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value or value.strip() != value for value in values
    ):
        raise ValueError("price cache coverage manifest is invalid or obsolete")
    if len(values) != len(set(values)):
        raise ValueError("price cache coverage token IDs must be unique")
    return set(values)


def _validate_price_rows(frame: pd.DataFrame) -> None:
    if not _REQUIRED_PRICE_COLUMNS.issubset(frame.columns):
        raise ValueError("price cache has an unsupported schema")
    if frame.empty:
        return
    token_ids = frame["token_id"]
    if token_ids.isna().any() or any(
        not isinstance(value, str) or not value or value.strip() != value
        for value in token_ids.tolist()
    ):
        raise ValueError("price cache token IDs must be non-empty and trimmed")
    raw_timestamps = frame["ts_seconds"].to_numpy()
    raw_prices = frame["price"].to_numpy()
    if np.issubdtype(raw_timestamps.dtype, np.bool_) or not np.issubdtype(
        raw_timestamps.dtype, np.number
    ):
        raise ValueError("price cache timestamps must use a numeric dtype")
    if np.issubdtype(raw_prices.dtype, np.bool_) or not np.issubdtype(raw_prices.dtype, np.number):
        raise ValueError("price cache probabilities must use a numeric dtype")
    timestamps = np.asarray(raw_timestamps, dtype=float)
    prices = np.asarray(raw_prices, dtype=float)
    if not np.isfinite(timestamps).all() or np.any(timestamps != np.floor(timestamps)):
        raise ValueError("price cache timestamps must be finite integer seconds")
    if np.any(timestamps < 0.0):
        raise ValueError("price cache timestamps must be non-negative")
    if not np.isfinite(prices).all() or np.any((prices <= 0.0) | (prices >= 1.0)):
        raise ValueError("price cache probabilities must be finite and lie in (0, 1)")
    conflicts = (
        pd.DataFrame(
            {
                "token_id": token_ids.to_numpy(),
                "ts_seconds": timestamps.astype(np.int64),
                "_numeric_price": prices,
            }
        )
        .groupby(["token_id", "ts_seconds"], dropna=False)["_numeric_price"]
        .nunique()
    )
    if (conflicts > 1).any():
        raise ValueError("price cache contains conflicting prices for one token timestamp")


async def _fetch_market_price_batches(
    *,
    markets: Sequence[MarketWindow],
    cached_tokens: set[str],
    max_concurrency: int,
    maximum_prediction_offset_seconds: int,
) -> tuple[TokenPricePoint, ...]:
    groups = tuple(markets[offset : offset + 10] for offset in range(0, len(markets), 10))
    semaphore = asyncio.Semaphore(max_concurrency)
    async with httpx.AsyncClient(timeout=30.0) as client:

        async def fetch(group: Sequence[MarketWindow]) -> tuple[TokenPricePoint, ...]:
            tokens = tuple(
                token_id
                for market in group
                for token_id in (market.up_token_id, market.down_token_id)
                if token_id not in cached_tokens
            )
            async with semaphore:
                return await fetch_polymarket_price_history(
                    token_ids=tokens,
                    start_ts=min(int(market.t0.timestamp()) for market in group),
                    end_ts=max(
                        int(market.t0.timestamp()) + maximum_prediction_offset_seconds + 1
                        for market in group
                    ),
                    fidelity_minutes=1,
                    max_concurrency=1,
                    client=client,
                )

        results = await asyncio.gather(*(fetch(group) for group in groups))
    return tuple(point for result in results for point in result)


def _build_candidates(
    *,
    predictions: pd.DataFrame,
    prices: pd.DataFrame,
    markets: Sequence[MarketWindow],
    entry_price_buffer: float,
    max_price_age_seconds: int,
) -> tuple[pd.DataFrame, dict[str, object]]:
    rows: list[dict[str, object]] = []
    price_points_by_token = {
        str(token_id): tuple(
            (int(row.ts_seconds), float(row.price))
            for row in group.sort_values("ts_seconds").itertuples(index=False)
        )
        for token_id, group in prices.groupby("token_id", sort=False)
    }
    predictions_by_market = {
        str(slug): group.sort_values("feature_ts_ns")
        for slug, group in predictions.groupby("market_slug", sort=False)
    }
    dual_token_markets = 0
    for market in markets:
        up_all = price_points_by_token.get(market.up_token_id, ())
        down_all = price_points_by_token.get(market.down_token_id, ())
        if not up_all or not down_all:
            continue
        market_predictions = predictions_by_market.get(market.slug)
        if market_predictions is None or market_predictions.empty:
            continue
        start_ts = int(market.t0.timestamp())
        end_ts = int(market_predictions["feature_ts_ns"].max() // 1_000_000_000)
        up = tuple(point for point in up_all if start_ts <= point[0] <= end_ts)
        down = tuple(point for point in down_all if start_ts <= point[0] <= end_ts)
        if not up or not down:
            continue
        dual_token_markets += 1
        up_index = -1
        down_index = -1
        for prediction in market_predictions.itertuples(index=False):
            decision_ts = int(prediction.feature_ts_ns // 1_000_000_000)
            while up_index + 1 < len(up) and up[up_index + 1][0] <= decision_ts:
                up_index += 1
            while down_index + 1 < len(down) and down[down_index + 1][0] <= decision_ts:
                down_index += 1
            if up_index < 0 or down_index < 0:
                continue
            up_ts, up_price = up[up_index]
            down_ts, down_price = down[down_index]
            if max(decision_ts - up_ts, decision_ts - down_ts) > max_price_age_seconds:
                continue
            up_entry = up_price + entry_price_buffer
            down_entry = down_price + entry_price_buffer
            if up_entry >= 1.0 and down_entry >= 1.0:
                continue
            up_edge = float(prediction.p_up) - up_entry
            down_edge = 1.0 - float(prediction.p_up) - down_entry
            side = (
                "up" if down_entry >= 1.0 or (up_entry < 1.0 and up_edge >= down_edge) else "down"
            )
            entry_price = up_entry if side == "up" else down_entry
            predicted_edge = up_edge if side == "up" else down_edge
            outcome = int(prediction.label) if side == "up" else 1 - int(prediction.label)
            elapsed_seconds = decision_ts - start_ts
            rows.append(
                {
                    "split": prediction.split,
                    "market_slug": market.slug,
                    "decision_ts_ns": int(prediction.feature_ts_ns),
                    "elapsed_seconds": elapsed_seconds,
                    "regime": opening_regime_for_elapsed_seconds(elapsed_seconds).value,
                    "side": side,
                    "p_side": (
                        float(prediction.p_up) if side == "up" else 1.0 - float(prediction.p_up)
                    ),
                    "observed_token_price": up_price if side == "up" else down_price,
                    "entry_price": entry_price,
                    "predicted_edge": predicted_edge,
                    "outcome": outcome,
                    "pnl_per_share": outcome - entry_price,
                    "price_age_seconds": max(decision_ts - up_ts, decision_ts - down_ts),
                }
            )
    candidates = pd.DataFrame(rows)
    return candidates, {
        "prediction_markets": len(markets),
        "dual_token_price_markets": dual_token_markets,
        "candidate_markets": len({row["market_slug"] for row in rows}),
        "candidate_rows": len(rows),
        "candidate_rows_by_regime": {
            regime.value: int((candidates["regime"] == regime.value).sum())
            if not candidates.empty
            else 0
            for regime in OPENING_REGIMES
        },
    }


def _select_one_entry_per_market(
    candidates: pd.DataFrame,
    *,
    threshold: float,
    minimum_consecutive_signals: int = 1,
    maximum_signal_gap_seconds: int = 5,
    thresholds_by_regime: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    if any(
        isinstance(value, bool) or not isinstance(value, Integral) or value < 1
        for value in (minimum_consecutive_signals, maximum_signal_gap_seconds)
    ):
        raise ValueError("signal persistence controls must be >= 1")
    if isinstance(threshold, bool) or not isinstance(threshold, Real) or np.isnan(float(threshold)):
        raise ValueError("threshold must be numeric and not NaN")
    if thresholds_by_regime is not None and any(
        not isinstance(regime, str)
        or not regime
        or regime.strip() != regime
        or isinstance(regime_threshold, bool)
        or not isinstance(regime_threshold, Real)
        or not np.isfinite(float(regime_threshold))
        for regime, regime_threshold in thresholds_by_regime.items()
    ):
        raise ValueError("regime thresholds must map valid names to finite values")
    selected_indexes: list[int] = []
    maximum_gap_ns = maximum_signal_gap_seconds * 1_000_000_000
    ordered = candidates.sort_values(["market_slug", "decision_ts_ns"])
    for _, group in ordered.groupby("market_slug", sort=False):
        run_length = 0
        previous_side: str | None = None
        previous_ts_ns: int | None = None
        for row in group.itertuples():
            decision_ts_ns = int(row.decision_ts_ns)
            row_threshold = (
                thresholds_by_regime.get(str(row.regime))
                if thresholds_by_regime is not None
                else threshold
            )
            if row_threshold is None or float(row.predicted_edge) < row_threshold:
                run_length = 0
                previous_side = None
                previous_ts_ns = None
                continue
            gap_ns = decision_ts_ns - previous_ts_ns if previous_ts_ns is not None else None
            if (
                previous_side == str(row.side)
                and gap_ns is not None
                and 0 < gap_ns <= maximum_gap_ns
            ):
                run_length += 1
            else:
                run_length = 1
            previous_side = str(row.side)
            previous_ts_ns = decision_ts_ns
            if run_length >= minimum_consecutive_signals:
                selected_indexes.append(int(row.Index))
                break
    return candidates.loc[selected_indexes].sort_values("decision_ts_ns").copy()


def _select_one_entry_per_market_by_regime(
    candidates: pd.DataFrame,
    *,
    thresholds_by_regime: Mapping[str, float],
    minimum_consecutive_signals: int = 1,
    maximum_signal_gap_seconds: int = 5,
) -> pd.DataFrame:
    """Apply frozen regime thresholds while preserving one earliest entry per market."""

    if candidates.empty:
        return candidates.copy()
    if "regime" not in candidates.columns:
        raise ValueError("regime thresholds require a regime column")
    return _select_one_entry_per_market(
        candidates,
        threshold=float("-inf"),
        minimum_consecutive_signals=minimum_consecutive_signals,
        maximum_signal_gap_seconds=maximum_signal_gap_seconds,
        thresholds_by_regime=thresholds_by_regime,
    )


def _select_positive_confidence_threshold(
    sweep: Sequence[dict[str, object]],
    *,
    min_development_entries: int,
) -> dict[str, object] | None:
    """Choose a development-only threshold with a strictly positive lower bound."""

    selectable = [
        item
        for item in sweep
        if int(item["entry_count"]) >= min_development_entries
        and int(item["utc_day_count"]) >= 2
        and float(item["selection_adjusted_ev_ci_lower"]) > 0.0
    ]
    if not selectable:
        return None
    return max(
        selectable,
        key=lambda item: (
            float(item["selection_adjusted_ev_ci_lower"]),
            float(item["realized_ev_per_share"]),
            -float(item["threshold"]),
        ),
    )


def _select_regime_thresholds(
    candidates: pd.DataFrame,
    *,
    requested_markets: int,
    min_development_entries: int,
    bootstrap_resamples: int,
    minimum_consecutive_signals: int,
    maximum_signal_gap_seconds: int,
) -> dict[str, dict[str, object]]:
    selections: dict[str, dict[str, object]] = {}
    for regime_index, regime in enumerate(OPENING_REGIMES):
        regime_candidates = candidates[candidates["regime"] == regime.value]
        sweep: list[dict[str, object]] = []
        for threshold in _THRESHOLDS:
            entries = _select_one_entry_per_market(
                regime_candidates,
                threshold=threshold,
                minimum_consecutive_signals=minimum_consecutive_signals,
                maximum_signal_gap_seconds=maximum_signal_gap_seconds,
            )
            sweep.append(
                {
                    "threshold": threshold,
                    **_entry_metrics(
                        entries,
                        requested_markets=requested_markets,
                        bootstrap_resamples=bootstrap_resamples,
                        seed=17 + regime_index,
                        selection_alpha=_SELECTION_FAMILY_ALPHA,
                    ),
                }
            )
        selected = _select_positive_confidence_threshold(
            sweep,
            min_development_entries=min_development_entries,
        )
        if selected is None:
            has_required_entries = any(
                int(item["entry_count"]) >= min_development_entries for item in sweep
            )
            selections[regime.value] = {
                "threshold": None,
                "candidate_rows": len(regime_candidates),
                "threshold_sweep": sweep,
                "reason": (
                    "no_positive_development_confidence_bound"
                    if has_required_entries
                    else "insufficient_development_entries"
                ),
            }
            continue
        selections[regime.value] = {
            "threshold": float(selected["threshold"]),
            "candidate_rows": len(regime_candidates),
            "threshold_sweep": sweep,
            "selected": selected,
        }
    return selections


def _sensitivity_entries(
    candidates: pd.DataFrame,
    *,
    threshold: float | None,
    thresholds_by_regime: Mapping[str, float] | None = None,
    additional_entry_cost: float,
    max_price_age_seconds: int,
    minimum_consecutive_signals: int = 1,
    maximum_signal_gap_seconds: int = 5,
) -> pd.DataFrame:
    if (
        isinstance(additional_entry_cost, bool)
        or not isinstance(additional_entry_cost, Real)
        or not np.isfinite(float(additional_entry_cost))
        or not 0.0 <= additional_entry_cost < 1.0
    ):
        raise ValueError("additional_entry_cost must be finite and lie in [0, 1)")
    if (
        isinstance(max_price_age_seconds, bool)
        or not isinstance(max_price_age_seconds, Integral)
        or max_price_age_seconds < 0
    ):
        raise ValueError("max_price_age_seconds must be a non-negative integer")
    adjusted = candidates[candidates["price_age_seconds"] <= max_price_age_seconds].copy()
    adjusted["entry_price"] += additional_entry_cost
    adjusted = adjusted[adjusted["entry_price"] < 1.0].copy()
    adjusted["predicted_edge"] -= additional_entry_cost
    adjusted["pnl_per_share"] = adjusted["outcome"] - adjusted["entry_price"]
    if thresholds_by_regime is not None:
        return _select_one_entry_per_market_by_regime(
            adjusted,
            thresholds_by_regime=thresholds_by_regime,
            minimum_consecutive_signals=minimum_consecutive_signals,
            maximum_signal_gap_seconds=maximum_signal_gap_seconds,
        )
    if threshold is None:
        raise ValueError("a scalar or regime threshold is required")
    return _select_one_entry_per_market(
        adjusted,
        threshold=threshold,
        minimum_consecutive_signals=minimum_consecutive_signals,
        maximum_signal_gap_seconds=maximum_signal_gap_seconds,
    )


def _entry_diagnostics(entries: pd.DataFrame) -> dict[str, object]:
    if entries.empty:
        return {
            "side_counts": {},
            "elapsed_seconds_counts": {},
            "price_age_seconds": {"median": None, "p95": None, "max": None},
        }
    return {
        "side_counts": {
            str(side): int(count) for side, count in entries["side"].value_counts().items()
        },
        "elapsed_seconds_counts": {
            str(int(seconds)): int(count)
            for seconds, count in entries["elapsed_seconds"].value_counts().sort_index().items()
        },
        "price_age_seconds": {
            "median": float(entries["price_age_seconds"].median()),
            "p95": float(entries["price_age_seconds"].quantile(0.95)),
            "max": int(entries["price_age_seconds"].max()),
        },
    }


def _entry_metrics_by_regime(
    entries: pd.DataFrame,
    *,
    requested_markets: int,
    bootstrap_resamples: int,
) -> dict[str, dict[str, object]]:
    return {
        regime.value: {
            **_entry_metrics(
                entries[entries["regime"] == regime.value],
                requested_markets=requested_markets,
                bootstrap_resamples=bootstrap_resamples,
                seed=41 + index,
            ),
            "diagnostics": _entry_diagnostics(entries[entries["regime"] == regime.value]),
        }
        for index, regime in enumerate(OPENING_REGIMES)
    }


def _entry_metrics(
    entries: pd.DataFrame,
    *,
    requested_markets: int,
    bootstrap_resamples: int,
    seed: int,
    selection_alpha: float | None = None,
) -> dict[str, object]:
    if (
        isinstance(requested_markets, bool)
        or not isinstance(requested_markets, Integral)
        or requested_markets < 1
    ):
        raise ValueError("requested_markets must be a positive integer")
    if (
        isinstance(bootstrap_resamples, bool)
        or not isinstance(bootstrap_resamples, Integral)
        or bootstrap_resamples < 100
    ):
        raise ValueError("bootstrap_resamples must be an integer >= 100")
    if isinstance(seed, bool) or not isinstance(seed, Integral):
        raise ValueError("bootstrap seed must be an integer")
    if selection_alpha is not None and not 0.0 < selection_alpha < 0.05:
        raise ValueError("selection_alpha must lie in (0, 0.05)")
    if entries.empty:
        return {
            "entry_count": 0,
            "market_coverage": 0.0,
            "realized_ev_per_share": 0.0,
            "realized_ev_ci95_lower": 0.0,
            "realized_ev_ci95_upper": 0.0,
            "utc_day_count": 0,
            "bootstrap_block_days": 0,
            "selection_adjusted_ev_ci_lower": 0.0,
            "selection_confidence_level": (
                1.0 - selection_alpha if selection_alpha is not None else None
            ),
        }
    pnl = entries["pnl_per_share"].to_numpy(dtype=float)
    if not np.isfinite(pnl).all():
        raise ValueError("entry PnL values must be finite")
    dates = pd.to_datetime(entries["decision_ts_ns"], unit="ns", utc=True).dt.date
    daily = pd.DataFrame({"date": dates, "pnl": pnl}).groupby("date")["pnl"].agg(["sum", "count"])
    if len(daily) == 1:
        lower = upper = float(np.mean(pnl))
        selection_lower = lower
        effective_block_days = 1
    else:
        rng = np.random.default_rng(seed)
        first_day = daily.index.min()
        last_day = daily.index.max()
        calendar_day_count = (last_day - first_day).days + 1
        sums_by_day = np.zeros(calendar_day_count, dtype=float)
        counts_by_day = np.zeros(calendar_day_count, dtype=float)
        for day, row in daily.iterrows():
            position = (day - first_day).days
            sums_by_day[position] = float(row["sum"])
            counts_by_day[position] = float(row["count"])
        effective_block_days = min(7, max(1, calendar_day_count // 2))
        block_count = ceil(calendar_day_count / effective_block_days)
        block_offsets = np.arange(effective_block_days, dtype=int)
        bootstrap_chunks: list[np.ndarray] = []
        remaining = int(bootstrap_resamples)
        for _ in range(100):
            draw_count = min(max(remaining * 2, 256), 2_048)
            starts = rng.integers(
                0,
                calendar_day_count,
                size=(draw_count, block_count),
            )
            choices = ((starts[:, :, None] + block_offsets) % calendar_day_count).reshape(
                draw_count, -1
            )[:, :calendar_day_count]
            sums = sums_by_day[choices].sum(axis=1)
            counts = counts_by_day[choices].sum(axis=1)
            valid = counts > 0.0
            if np.any(valid):
                values = (sums[valid] / counts[valid])[:remaining]
                bootstrap_chunks.append(values)
                remaining -= len(values)
            if remaining == 0:
                break
        if remaining:
            raise ValueError("bootstrap could not sample entries from sparse research dates")
        bootstrap_ev = np.concatenate(bootstrap_chunks)
        lower, upper = np.quantile(bootstrap_ev, (0.025, 0.975)).tolist()
        selection_lower = (
            float(np.quantile(bootstrap_ev, selection_alpha / 2.0))
            if selection_alpha is not None
            else float(lower)
        )
    positive = float(pnl[pnl > 0.0].sum())
    negative = float(-pnl[pnl < 0.0].sum())
    return {
        "entry_count": len(entries),
        "utc_day_count": len(daily),
        "bootstrap_block_days": effective_block_days,
        "market_coverage": len(entries) / requested_markets,
        "win_rate": float(entries["outcome"].mean()),
        "average_entry_price": float(entries["entry_price"].mean()),
        "average_predicted_edge": float(entries["predicted_edge"].mean()),
        "realized_ev_per_share": float(np.mean(pnl)),
        "realized_ev_ci95_lower": float(lower),
        "realized_ev_ci95_upper": float(upper),
        "selection_adjusted_ev_ci_lower": float(selection_lower),
        "selection_confidence_level": (
            1.0 - selection_alpha if selection_alpha is not None else None
        ),
        "profit_factor": positive / negative if negative > 0.0 else None,
    }


def _validate_args(args: argparse.Namespace) -> None:
    if isinstance(args.entry_price_buffer, bool) or not 0.0 <= args.entry_price_buffer < 0.25:
        raise ValueError("entry_price_buffer must be in [0, 0.25)")
    controls = (
        args.max_price_age_seconds,
        args.min_development_entries,
        args.bootstrap_resamples,
        args.max_fetch_concurrency,
        args.minimum_consecutive_signals,
        args.maximum_signal_gap_seconds,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, Integral) or value < 1
        for value in controls
    ):
        raise ValueError("integer research controls must be >= 1")
    if args.bootstrap_resamples < 10_000:
        raise ValueError("formal threshold selection requires bootstrap_resamples >= 10000")


def _validate_predictions(predictions: pd.DataFrame) -> None:
    required = {"split", "sample_id", "feature_ts_ns", "p_up", "label"}
    if not required.issubset(predictions.columns):
        raise ValueError("prediction artifact has an unsupported schema")
    if set(predictions["split"].unique()) != {"development_oof", "sealed_holdout"}:
        raise ValueError("prediction artifact must contain development OOF and sealed holdout")
    if predictions["sample_id"].duplicated().any():
        raise ValueError("prediction sample IDs must be unique")
    if predictions.empty:
        raise ValueError("prediction artifact must not be empty")
    sample_ids = predictions["sample_id"].tolist()
    if any(
        not isinstance(value, str) or not value or value.strip() != value for value in sample_ids
    ):
        raise ValueError("prediction sample IDs must be non-empty and trimmed")
    raw_timestamps = predictions["feature_ts_ns"].to_numpy()
    if np.issubdtype(raw_timestamps.dtype, np.bool_) or not np.issubdtype(
        raw_timestamps.dtype, np.integer
    ):
        raise ValueError("prediction timestamps must use an integer nanosecond type")
    timestamps = raw_timestamps.astype(np.int64, copy=False)
    probabilities = pd.to_numeric(predictions["p_up"], errors="coerce").to_numpy(dtype=float)
    raw_labels = predictions["label"].to_numpy()
    if np.issubdtype(raw_labels.dtype, np.bool_) or not (
        np.issubdtype(raw_labels.dtype, np.integer) or np.issubdtype(raw_labels.dtype, np.floating)
    ):
        raise ValueError("prediction labels must be numeric binary values")
    labels = np.asarray(raw_labels, dtype=float)
    if np.any(timestamps < 0.0):
        raise ValueError("prediction timestamps must be non-negative")
    if not np.isfinite(probabilities).all() or np.any(
        (probabilities <= 0.0) | (probabilities >= 1.0)
    ):
        raise ValueError("prediction probabilities must be finite and lie in (0, 1)")
    if not np.isfinite(labels).all() or np.any((labels != 0.0) & (labels != 1.0)):
        raise ValueError("prediction labels must be binary")
    for sample_id, timestamp in zip(sample_ids, timestamps, strict=True):
        prefix, separator, suffix = sample_id.rpartition("@")
        if not prefix or not separator or not suffix.isdigit() or int(suffix) != int(timestamp):
            raise ValueError("prediction sample_id timestamps must match feature_ts_ns")


def _validate_market_labels(*, predictions: pd.DataFrame, markets: Sequence[MarketWindow]) -> None:
    for market in markets:
        if market.resolution not in {MarketOutcome.UP, MarketOutcome.DOWN}:
            raise ValueError("price-edge proxy requires final binary market labels")
        expected = 1 if market.resolution is MarketOutcome.UP else 0
        labels = set(predictions.loc[predictions["market_slug"] == market.slug, "label"].tolist())
        if labels != {expected}:
            raise ValueError(f"prediction label disagrees with catalog for {market.slug!r}")
        splits = set(predictions.loc[predictions["market_slug"] == market.slug, "split"].tolist())
        if len(splits) != 1:
            raise ValueError(f"prediction market crosses research splits: {market.slug!r}")


def main(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(run(parse_args(argv)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
