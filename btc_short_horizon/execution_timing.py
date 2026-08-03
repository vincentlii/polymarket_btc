"""Shared route-aware timing bounds for BTC Research Paper execution."""

from __future__ import annotations

from math import isfinite


CLOB_DELAYED_TAKER_SERVER_MS = 250.0


def paper_execution_lifecycle_tail_seconds(
    *,
    mode: str,
    maker_work_seconds: float,
    cancel_latency_ms: float,
    taker_latency_ms: float,
    taker_server_delay_ms: float,
) -> float:
    """Return the post-decision tail for one execution route."""

    if mode not in {"maker", "immediate_fak", "maker_then_fak"}:
        raise ValueError("unsupported paper execution mode")
    for name, value in (
        ("maker_work_seconds", maker_work_seconds),
        ("cancel_latency_ms", cancel_latency_ms),
        ("taker_latency_ms", taker_latency_ms),
        ("taker_server_delay_ms", taker_server_delay_ms),
    ):
        if isinstance(value, bool) or not isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be finite and >= 0")
    cancel_seconds = cancel_latency_ms / 1_000.0
    taker_seconds = (taker_latency_ms + taker_server_delay_ms) / 1_000.0
    if mode == "maker":
        return maker_work_seconds + cancel_seconds
    if mode == "immediate_fak":
        return taker_seconds
    return maker_work_seconds + cancel_seconds + taker_seconds


__all__ = ["CLOB_DELAYED_TAKER_SERVER_MS", "paper_execution_lifecycle_tail_seconds"]
