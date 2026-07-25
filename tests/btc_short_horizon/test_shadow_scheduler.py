from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from btc_short_horizon.data import (
    BTC_15M_MARKET_FAMILY,
    MarketCatalog,
    MarketWindow,
    write_market_catalog,
)
from btc_short_horizon.live.shadow_scheduler import scan_shadow_windows
from btc_short_horizon.live.runtime import RuntimeControl, RuntimeStatusStore
import scripts.btc_opening_shadow_scheduler as shadow_scheduler_script
from scripts.btc_opening_shadow_scheduler import run_async
from scripts.btc_opening_proxy_shadow import ShadowEvidenceUnavailableError


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

    (completed / "metrics.json").unlink()
    (completed / "skip.json").write_text("{}", encoding="utf-8")
    skipped = scan_shadow_windows(
        catalog_directory=catalog_directory,
        output_root=output_root,
        family=BTC_15M_MARKET_FAMILY,
        model_sha256=model_sha256,
        now=market.t1 + timedelta(seconds=241),
        handoff_delay_seconds=180,
        flush_interval_seconds=60,
        lookback=timedelta(hours=2),
    )

    assert skipped.ready == ()
    assert skipped.incomplete_outputs == ()


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


def test_shadow_scheduler_publishes_current_running_status_before_first_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    observed = []
    monkeypatch.setenv("BTC_CODE_REVISION", "current-revision")
    monkeypatch.setattr(
        shadow_scheduler_script.ModelArtifactStore,
        "load",
        staticmethod(
            lambda **_kwargs: (
                object(),
                SimpleNamespace(
                    config={},
                    model_id="test-model",
                    model_sha256="a" * 64,
                ),
            )
        ),
    )
    monkeypatch.setattr(
        shadow_scheduler_script,
        "validate_opening_proxy_protocol",
        lambda *_args, **_kwargs: None,
    )

    def inspect_first_scan(**_kwargs):
        observed.append(RuntimeStatusStore(runtime_root).read("opening_shadow"))
        RuntimeControl(runtime_root).request_stop(
            reason="test complete",
            requested_at=datetime.now(UTC),
        )
        return SimpleNamespace(ready=(), incomplete_outputs=(), catalog_errors=())

    monkeypatch.setattr(
        shadow_scheduler_script,
        "scan_shadow_windows",
        inspect_first_scan,
    )
    args = argparse.Namespace(
        config=Path("configs/btc_short_horizon/baseline.toml"),
        model_directory=tmp_path / "model",
        runtime_root=runtime_root,
        catalog_directory=tmp_path / "catalogs",
        output_root=tmp_path / "shadow",
        raw_data_root=tmp_path / "raw",
        poll_seconds=0.01,
        lookback_hours=2.0,
        book_lookback_seconds=300,
        availability_delay_seconds=1.0,
    )

    asyncio.run(run_async(args))

    assert len(observed) == 1
    status = observed[0]
    assert status is not None
    assert status.state == "running"
    assert status.healthy
    assert status.details["phase"] == "initialized"
    assert status.details["identity"]["code_revision"] == "current-revision"


def test_shadow_scheduler_terminally_skips_window_without_current_epoch_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    output_root = tmp_path / "shadow"
    market = _market(datetime(2026, 7, 16, 12, tzinfo=UTC))
    window = SimpleNamespace(
        market=market,
        catalog_path=tmp_path / "catalog.json",
        output_directory=output_root / f"{market.slug}-{'a' * 12}",
    )
    scans = 0
    shadow_calls = 0
    monkeypatch.setenv("BTC_CODE_REVISION", "current-revision")
    monkeypatch.setattr(
        shadow_scheduler_script.ModelArtifactStore,
        "load",
        staticmethod(
            lambda **_kwargs: (
                object(),
                SimpleNamespace(config={}, model_id="test-model", model_sha256="a" * 64),
            )
        ),
    )
    monkeypatch.setattr(
        shadow_scheduler_script,
        "validate_opening_proxy_protocol",
        lambda *_args, **_kwargs: None,
    )

    def scan(**_kwargs):
        nonlocal scans
        scans += 1
        if scans == 1:
            return SimpleNamespace(ready=(window,), incomplete_outputs=(), catalog_errors=())
        assert (window.output_directory / "skip.json").is_file()
        RuntimeControl(runtime_root).request_stop(
            reason="test complete",
            requested_at=datetime.now(UTC),
        )
        return SimpleNamespace(ready=(), incomplete_outputs=(), catalog_errors=())

    async def no_evidence(_args):
        nonlocal shadow_calls
        shadow_calls += 1
        raise ShadowEvidenceUnavailableError(
            "forward CLOB data produced no causal shadow observations"
        )

    monkeypatch.setattr(shadow_scheduler_script, "scan_shadow_windows", scan)
    monkeypatch.setattr(shadow_scheduler_script, "run_shadow_pass", no_evidence)
    args = argparse.Namespace(
        config=Path("configs/btc_short_horizon/baseline.toml"),
        model_directory=tmp_path / "model",
        runtime_root=runtime_root,
        catalog_directory=tmp_path / "catalogs",
        output_root=output_root,
        raw_data_root=tmp_path / "raw",
        poll_seconds=0.01,
        lookback_hours=2.0,
        book_lookback_seconds=300,
        availability_delay_seconds=1.0,
    )

    asyncio.run(run_async(args))

    assert shadow_calls == 1
    status = RuntimeStatusStore(runtime_root).read("opening_shadow")
    assert status is not None
    assert status.healthy
    assert status.details["skipped_windows"] == 1
