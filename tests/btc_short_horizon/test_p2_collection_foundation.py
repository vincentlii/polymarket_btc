from __future__ import annotations

from pathlib import Path

import pytest

from btc_short_horizon.config import load_btc_project_config
from btc_short_horizon.data.disk_pressure import DiskPressureState, DiskProtectionPolicy
from btc_short_horizon.data.rule_contract import (
    CHAINLINK_BTC_USD_POINT_V1,
    CHAINLINK_BTC_USD_TWAP_60S_V1,
    rule_contract_sha256,
)


def test_rule_contract_sha256_is_stable_for_each_resolution_contract() -> None:
    assert rule_contract_sha256(CHAINLINK_BTC_USD_POINT_V1) == (
        "59a458c5bb6666f0376ae2c76517d67f4745dfce447fc2d6bdc7d48ce11e0c18"
    )
    assert rule_contract_sha256(CHAINLINK_BTC_USD_TWAP_60S_V1) == (
        "56571bed3c09246f9c3d6214266a795f319383dc66c60ce944264e0bd5595885"
    )


def test_rule_contract_sha256_rejects_an_unknown_epoch() -> None:
    with pytest.raises(ValueError, match="unsupported BTC 15m rule epoch"):
        rule_contract_sha256("unknown-rule")


def test_baseline_collects_full_lifecycle_and_explicit_lightweight_feeds() -> None:
    config = load_btc_project_config(Path("configs/btc_short_horizon/baseline.toml"))

    assert config.collection.polymarket_capture_lead_seconds == 90.0
    assert config.collection.opening_handoff_delay_seconds == 900.0
    assert config.collection.binance_spot_streams == (
        "btcusdt@kline_1s",
        "btcusdt@aggTrade",
        "btcusdt@depth20@100ms",
    )
    assert config.collection.binance_futures_market_streams == ("btcusdt@aggTrade",)
    assert config.collection.binance_futures_public_streams == ("btcusdt@depth20@100ms",)
    assert tuple(
        (item.channel, item.instrument) for item in config.collection.okx_subscriptions
    ) == (
        ("books5", "BTC-USDT"),
        ("trades", "BTC-USDT"),
        ("books5", "BTC-USDT-SWAP"),
        ("trades", "BTC-USDT-SWAP"),
    )
    assert config.collection.ingest_version == "btc-short-horizon-v16"


def test_disk_protection_policy_has_ordered_fail_safe_thresholds() -> None:
    policy = DiskProtectionPolicy(
        warning_free_gib=20.0,
        optional_feeds_free_gib=15.0,
        extended_capture_free_gib=10.0,
    )

    assert policy.evaluate_free_gib(25.0) is DiskPressureState.NORMAL
    assert policy.evaluate_free_gib(20.0) is DiskPressureState.WARNING
    assert policy.evaluate_free_gib(15.0) is DiskPressureState.SHED_OPTIONAL_FEEDS
    assert policy.evaluate_free_gib(10.0) is DiskPressureState.SUSPEND_EXTENDED_CAPTURE


def test_disk_protection_policy_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError, match="warning > optional feeds > extended capture"):
        DiskProtectionPolicy(
            warning_free_gib=15.0,
            optional_feeds_free_gib=20.0,
            extended_capture_free_gib=10.0,
        )
