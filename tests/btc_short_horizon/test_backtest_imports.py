from __future__ import annotations

import importlib
import sys


def test_backtest_package_keeps_joint_replay_dependency_lazy() -> None:
    sys.modules.pop("btc_short_horizon.backtest.joint", None)

    importlib.import_module("btc_short_horizon.backtest")

    assert "btc_short_horizon.backtest.joint" not in sys.modules
