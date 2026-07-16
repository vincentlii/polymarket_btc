from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from btc_short_horizon.data import (
    BTC_15M_MARKET_FAMILY,
    MarketCatalog,
    MarketWindow,
    write_market_catalog,
)
from btc_short_horizon.live.shadow_scheduler import scan_shadow_windows
from btc_short_horizon.live.runtime import RuntimeStatusStore
from scripts.btc_opening_shadow_scheduler import run_async


def _market(start: datetime) -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(start),
        condition_id="condition",
        up_token_id="up",
        down_token_id="down",
        t0=start,
        t1=start + timedelta(minutes=15),
        rule_epoch="chainlink-btc-usd-v1",
        rule_hash="a" * 64,
    )


def test_shadow_scan_waits_for_handoff_and_skips_completed_output(tmp_path) -> None:
    market = _market(datetime(2026, 7, 16, 12, tzinfo=UTC))
    catalog_directory = tmp_path / "catalogs"
    catalog_path = catalog_directory / "market.json"
    write_market_catalog(
        path=catalog_path,
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
    )
    output_root = tmp_path / "shadow"
    model_sha256 = "b" * 64
    before_handoff = scan_shadow_windows(
        catalog_directory=catalog_directory,
        output_root=output_root,
        family=BTC_15M_MARKET_FAMILY,
        model_sha256=model_sha256,
        now=market.t1 + timedelta(seconds=60),
        handoff_delay_seconds=180,
        flush_interval_seconds=60,
        lookback=timedelta(hours=2),
    )

    assert before_handoff.ready == ()

    after_handoff = scan_shadow_windows(
        catalog_directory=catalog_directory,
        output_root=output_root,
        family=BTC_15M_MARKET_FAMILY,
        model_sha256=model_sha256,
        now=market.t1 + timedelta(seconds=241),
        handoff_delay_seconds=180,
        flush_interval_seconds=60,
        lookback=timedelta(hours=2),
    )

    assert len(after_handoff.ready) == 1
    assert after_handoff.ready[0].catalog_path == catalog_path
    assert after_handoff.ready[0].output_directory.name.endswith("-" + model_sha256[:12])

    completed = after_handoff.ready[0].output_directory
    completed.mkdir(parents=True)
    (completed / "metrics.json").write_text("{}", encoding="utf-8")
    rescanned = scan_shadow_windows(
        catalog_directory=catalog_directory,
        output_root=output_root,
        family=BTC_15M_MARKET_FAMILY,
        model_sha256=model_sha256,
        now=market.t1 + timedelta(seconds=241),
        handoff_delay_seconds=180,
        flush_interval_seconds=60,
        lookback=timedelta(hours=2),
    )

    assert rescanned.ready == ()


def test_shadow_scan_marks_partial_output_for_operator_review(tmp_path) -> None:
    market = _market(datetime(2026, 7, 16, 12, tzinfo=UTC))
    catalog_directory = tmp_path / "catalogs"
    write_market_catalog(
        path=catalog_directory / "market.json",
        catalog=MarketCatalog(families=(BTC_15M_MARKET_FAMILY,), windows=(market,)),
    )
    output = tmp_path / "shadow" / f"{market.slug}-{'a' * 12}"
    output.mkdir(parents=True)

    scan = scan_shadow_windows(
        catalog_directory=catalog_directory,
        output_root=tmp_path / "shadow",
        family=BTC_15M_MARKET_FAMILY,
        model_sha256="a" * 64,
        now=market.t1 + timedelta(minutes=5),
        handoff_delay_seconds=180,
        flush_interval_seconds=60,
        lookback=timedelta(hours=2),
    )

    assert scan.ready == ()
    assert scan.incomplete_outputs == (output,)


def test_shadow_scheduler_marks_invalid_model_failed(tmp_path) -> None:
    args = argparse.Namespace(
        config=Path("configs/btc_short_horizon/baseline.toml"),
        model_directory=tmp_path / "missing-model",
        runtime_root=tmp_path / "runtime",
        catalog_directory=None,
        output_root=None,
        raw_data_root=None,
        poll_seconds=60.0,
        lookback_hours=2.0,
        book_lookback_seconds=300,
        availability_delay_seconds=1.0,
    )

    try:
        asyncio.run(run_async(args))
    except FileNotFoundError:
        pass
    else:  # pragma: no cover - documents the required model artifact boundary.
        raise AssertionError("missing model artifact should fail")

    status = RuntimeStatusStore(args.runtime_root).read("opening_shadow")
    assert status is not None
    assert status.state == "failed"
    assert not status.healthy
