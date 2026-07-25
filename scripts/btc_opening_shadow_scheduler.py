"""Run bounded post-window causal Shadow passes for fresh BTC 15m forward data."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from math import isfinite
import signal
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.config import load_btc_project_config  # noqa: E402
from btc_short_horizon.live.runtime import (  # noqa: E402
    RuntimeControl,
    RuntimeStatus,
    RuntimeStatusStore,
    build_runtime_identity,
    filesystem_usage,
)
from btc_short_horizon.live.shadow_scheduler import scan_shadow_windows  # noqa: E402
from btc_short_horizon.models import ModelArtifactStore  # noqa: E402
from btc_short_horizon.research.opening_proxy import (  # noqa: E402
    opening_proxy_feature_schema,
    opening_proxy_protocol,
    validate_opening_proxy_protocol,
)
from scripts.btc_opening_proxy_shadow import run_async as run_shadow_pass  # noqa: E402
from scripts._runtime_helpers import resolve_runtime_root  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
    )
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--catalog-directory", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--raw-data-root", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--lookback-hours", type=float, default=2.0)
    parser.add_argument("--book-lookback-seconds", type=int, default=300)
    parser.add_argument("--availability-delay-seconds", type=float, default=1.0)
    return parser.parse_args(argv)


async def run_async(args: argparse.Namespace) -> None:
    if (
        not isfinite(args.poll_seconds)
        or not isfinite(args.lookback_hours)
        or not isfinite(args.availability_delay_seconds)
        or args.poll_seconds <= 0.0
        or args.lookback_hours <= 0.0
        or args.availability_delay_seconds < 0.0
        or args.book_lookback_seconds < 0
    ):
        raise ValueError("scheduler timing values are invalid")
    project = load_btc_project_config(args.config)
    runtime_root = resolve_runtime_root(config_path=args.config, runtime_root=args.runtime_root)
    store = RuntimeStatusStore(runtime_root)
    control = RuntimeControl(runtime_root)
    started_at = datetime.now(UTC)
    runtime_context: dict[str, object] = {
        "identity": build_runtime_identity(
            config_path=args.config,
            ingest_version=project.collection.ingest_version,
        ),
        "storage": {"runtime": filesystem_usage(runtime_root)},
    }
    stop_request = control.stop_request()
    if stop_request is not None:
        _write_status(
            store=store,
            started_at=started_at,
            state="stopped",
            healthy=True,
            details={
                **runtime_context,
                "model_validation": "not_started",
                "stop_reason": stop_request.reason,
            },
        )
        return
    try:
        schema = opening_proxy_feature_schema(1)
        _model, metadata = ModelArtifactStore.load(
            directory=args.model_directory,
            expected_schema_hash=schema.hash,
        )
        protocol = opening_proxy_protocol(
            entry_start_seconds=project.research_timing.entry_start_seconds,
            entry_end_seconds=project.research_timing.entry_end_seconds,
            snapshot_seconds=project.research_timing.training_snapshot_seconds,
        )
        validate_opening_proxy_protocol(metadata.config, expected=protocol)
    except Exception as exc:
        _write_status(
            store=store,
            started_at=started_at,
            state="failed",
            healthy=False,
            details={**runtime_context, "error": f"{type(exc).__name__}: {exc}"},
        )
        raise
    runtime_context["identity"] = build_runtime_identity(
        config_path=args.config,
        ingest_version=project.collection.ingest_version,
        model_sha256=metadata.model_sha256,
    )
    catalog_directory = (
        args.catalog_directory
        or project.paths.raw_data_root.parent / "metadata" / "forward_catalogs"
    )
    output_root = args.output_root or project.paths.artifact_root / "shadow"
    raw_data_root = args.raw_data_root or project.paths.raw_data_root
    runtime_context["storage"] = {
        "runtime": filesystem_usage(runtime_root),
        "raw_data": filesystem_usage(raw_data_root),
        "shadow_output": filesystem_usage(output_root),
    }
    _write_status(
        store=store,
        started_at=started_at,
        state="running",
        healthy=True,
        details={
            **runtime_context,
            "model_id": metadata.model_id,
            "model_sha256": metadata.model_sha256,
            "completed_windows": 0,
            "last_shadow": None,
            "phase": "initialized",
            "study_type": "post_window_causal_shadow",
        },
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    for item in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(item, stop_event.set)
        except NotImplementedError:  # Windows event loops do not expose signal handlers.
            break
        else:
            registered_signals.append(item)
    completed = 0
    last_shadow: dict[str, object] | None = None
    last_error: str | None = None
    try:
        while not stop_event.is_set():
            stop_request = control.stop_request()
            if stop_request is not None:
                _write_status(
                    store=store,
                    started_at=started_at,
                    state="stopped",
                    healthy=True,
                    details={
                        **runtime_context,
                        "model_id": metadata.model_id,
                        "model_sha256": metadata.model_sha256,
                        "completed_windows": completed,
                        "last_shadow": last_shadow,
                        "stop_reason": stop_request.reason,
                    },
                )
                return
            scan = scan_shadow_windows(
                catalog_directory=catalog_directory,
                output_root=output_root,
                family=project.primary_family,
                model_sha256=metadata.model_sha256,
                now=datetime.now(UTC),
                handoff_delay_seconds=project.collection.opening_handoff_delay_seconds,
                flush_interval_seconds=project.collection.flush_interval_seconds,
                lookback=timedelta(hours=args.lookback_hours),
            )
            healthy = not scan.incomplete_outputs and not scan.catalog_errors
            for window in scan.ready:
                _write_status(
                    store=store,
                    started_at=started_at,
                    state="running",
                    healthy=healthy,
                    details={
                        **runtime_context,
                        "model_id": metadata.model_id,
                        "model_sha256": metadata.model_sha256,
                        "completed_windows": completed,
                        "ready_windows": len(scan.ready),
                        "last_shadow": last_shadow,
                        "last_error": last_error,
                        "phase": "processing",
                        "active_market": window.market.slug,
                        "study_type": "post_window_causal_shadow",
                    },
                )
                shadow_args = argparse.Namespace(
                    config=args.config,
                    market_catalog=window.catalog_path,
                    market_slug=window.market.slug,
                    model_directory=args.model_directory,
                    output_directory=window.output_directory,
                    raw_data_root=raw_data_root,
                    book_lookback_seconds=args.book_lookback_seconds,
                    availability_delay_seconds=args.availability_delay_seconds,
                    as_of=(
                        window.market.t0
                        + timedelta(
                            seconds=(
                                project.research_timing.entry_end_seconds
                                + args.availability_delay_seconds
                            )
                        )
                    ),
                )
                try:
                    result = await run_shadow_pass(shadow_args)
                except Exception as exc:
                    healthy = False
                    last_error = f"{window.market.slug}: {type(exc).__name__}: {exc}"
                    break
                completed += 1
                last_shadow = {
                    "market_slug": result["market_slug"],
                    "output_directory": str(window.output_directory),
                    "evidence_type": "post_window_causal_shadow",
                    "as_of": result["as_of"],
                    "orders_submitted": result["orders_submitted"],
                    "coverage": result["coverage"],
                    "diagnostics": result["diagnostics"],
                }
                last_error = None
            _write_status(
                store=store,
                started_at=started_at,
                state="running",
                healthy=healthy,
                details={
                    **runtime_context,
                    "model_id": metadata.model_id,
                    "model_sha256": metadata.model_sha256,
                    "completed_windows": completed,
                    "ready_windows": len(scan.ready),
                    "incomplete_outputs": [str(path) for path in scan.incomplete_outputs],
                    "catalog_errors": list(scan.catalog_errors),
                    "last_shadow": last_shadow,
                    "last_error": last_error,
                    "phase": "idle",
                    "active_market": None,
                    "study_type": "post_window_causal_shadow",
                },
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=args.poll_seconds)
            except TimeoutError:
                continue
        _write_status(
            store=store,
            started_at=started_at,
            state="stopped",
            healthy=True,
            details={
                **runtime_context,
                "model_id": metadata.model_id,
                "model_sha256": metadata.model_sha256,
                "completed_windows": completed,
                "last_shadow": last_shadow,
                "stop_reason": "signal",
            },
        )
    except asyncio.CancelledError:
        _write_status(
            store=store,
            started_at=started_at,
            state="stopped",
            healthy=True,
            details={
                **runtime_context,
                "model_id": metadata.model_id,
                "model_sha256": metadata.model_sha256,
                "completed_windows": completed,
                "last_shadow": last_shadow,
                "stop_reason": "task_cancelled",
            },
        )
        raise
    except Exception as exc:
        _write_status(
            store=store,
            started_at=started_at,
            state="failed",
            healthy=False,
            details={
                **runtime_context,
                "model_id": metadata.model_id,
                "model_sha256": metadata.model_sha256,
                "completed_windows": completed,
                "last_shadow": last_shadow,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise
    finally:
        for item in registered_signals:
            loop.remove_signal_handler(item)


def _write_status(
    *,
    store: RuntimeStatusStore,
    started_at: datetime,
    state: str,
    healthy: bool,
    details: dict[str, object],
) -> None:
    store.write(
        RuntimeStatus(
            service="opening_shadow",
            mode="post_window_shadow",
            state=state,
            healthy=healthy,
            started_at=started_at,
            updated_at=datetime.now(UTC),
            details=details,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        asyncio.run(run_async(parse_args(argv)))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
