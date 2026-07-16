"""Lightweight helpers shared by long-running runtime-control scripts."""

from __future__ import annotations

from pathlib import Path


def resolve_runtime_root(*, config_path: Path, runtime_root: Path | None) -> Path:
    """Avoid importing heavy replay dependencies when the production path is explicit."""

    if runtime_root is not None:
        return runtime_root
    from btc_short_horizon.config import load_btc_project_config

    return load_btc_project_config(config_path).paths.artifact_root / "runtime"
