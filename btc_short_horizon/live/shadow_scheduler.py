"""Find bounded, complete forward windows eligible for post-window causal Shadow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path

from btc_short_horizon.data import BtcMarketFamily, MarketWindow, read_market_catalog


@dataclass(frozen=True, slots=True)
class ShadowWindow:
    catalog_path: Path
    market: MarketWindow
    output_directory: Path


@dataclass(frozen=True, slots=True)
class ShadowWindowScan:
    ready: tuple[ShadowWindow, ...]
    incomplete_outputs: tuple[Path, ...]
    catalog_errors: tuple[str, ...]


def scan_shadow_windows(
    *,
    catalog_directory: Path,
    output_root: Path,
    family: BtcMarketFamily,
    model_sha256: str,
    now: datetime,
    handoff_delay_seconds: float,
    flush_interval_seconds: float,
    lookback: timedelta,
) -> ShadowWindowScan:
    """Select only recently completed windows whose raw-writer handoff has elapsed."""

    if len(model_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in model_sha256
    ):
        raise ValueError("model_sha256 must be a 64-character lower-case hexadecimal digest")
    if (
        not isfinite(handoff_delay_seconds)
        or not isfinite(flush_interval_seconds)
        or handoff_delay_seconds <= 0.0
        or flush_interval_seconds <= 0.0
    ):
        raise ValueError("handoff and flush intervals must be finite and > 0")
    if lookback <= timedelta(0):
        raise ValueError("lookback must be > 0")
    current_time = _as_utc(now)
    minimum_end = current_time - lookback
    ready: list[ShadowWindow] = []
    incomplete: list[Path] = []
    errors: list[str] = []
    suffix = model_sha256[:12]
    for path in sorted(catalog_directory.glob("*.json")):
        try:
            catalog = read_market_catalog(path)
        except (OSError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        for market in catalog.windows():
            if market.family != family or market.t1 < minimum_end:
                continue
            eligible_at = market.t1 + timedelta(
                seconds=handoff_delay_seconds + flush_interval_seconds
            )
            if current_time < eligible_at:
                continue
            output_directory = output_root / f"{market.slug}-{suffix}"
            if (output_directory / "metrics.json").exists() or (
                output_directory / "skip.json"
            ).exists():
                continue
            if output_directory.exists():
                incomplete.append(output_directory)
                continue
            ready.append(
                ShadowWindow(
                    catalog_path=path,
                    market=market,
                    output_directory=output_directory,
                )
            )
    return ShadowWindowScan(
        ready=tuple(sorted(ready, key=lambda item: (item.market.t0, item.catalog_path))),
        incomplete_outputs=tuple(sorted(incomplete)),
        catalog_errors=tuple(errors),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)
