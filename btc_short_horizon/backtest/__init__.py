"""Nautilus-backed BTC joint-market backtest components.

The signal bridge stays lightweight so model-only shadow jobs do not import the
PMXT/DuckDB replay stack. Replay and strategy objects are loaded on demand.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from btc_short_horizon.backtest.signals import (
    BtcOpeningMispricingSignal,
    to_opening_mispricing_signal,
)


_LAZY_EXPORTS = {
    "BtcOpeningMispricingConfig": (
        "btc_short_horizon.backtest.strategy",
        "BtcOpeningMispricingConfig",
    ),
    "BtcOpeningMispricingStrategy": (
        "btc_short_horizon.backtest.strategy",
        "BtcOpeningMispricingStrategy",
    ),
    "BtcJointReplayConfig": (
        "btc_short_horizon.backtest.joint",
        "BtcJointReplayConfig",
    ),
    "build_btc_joint_backtest": (
        "btc_short_horizon.backtest.joint",
        "build_btc_joint_backtest",
    ),
    "collect_btc_order_events": (
        "btc_short_horizon.backtest.joint",
        "collect_btc_order_events",
    ),
}

__all__ = [
    "BtcOpeningMispricingConfig",
    "BtcOpeningMispricingStrategy",
    "BtcJointReplayConfig",
    "BtcOpeningMispricingSignal",
    "build_btc_joint_backtest",
    "collect_btc_order_events",
    "to_opening_mispricing_signal",
]


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
