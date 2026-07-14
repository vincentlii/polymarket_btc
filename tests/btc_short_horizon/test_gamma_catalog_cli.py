from __future__ import annotations

import pytest

from btc_short_horizon.config import load_btc_project_config
from scripts.btc_gamma_catalog import closed_filter, parse_args, select_family


def test_gamma_catalog_cli_uses_configured_15m_family() -> None:
    args = parse_args(
        [
            "--family",
            "15m",
            "--closed",
            "open",
            "--rule-epoch",
            "chainlink-btc-usd-v1",
            "--output",
            "data/metadata/btc-15m.json",
        ]
    )

    family = select_family(load_btc_project_config(args.config), args.family)

    assert family.name == "btc_updown_15m"
    assert closed_filter(args.closed) is False


def test_gamma_catalog_cli_defaults_to_open_markets() -> None:
    args = parse_args(
        [
            "--rule-epoch",
            "chainlink-btc-usd-v1",
            "--output",
            "data/metadata/btc-15m.json",
        ]
    )

    assert args.closed == "open"


def test_gamma_catalog_cli_accepts_exact_market_slug_filters() -> None:
    args = parse_args(
        [
            "--rule-epoch",
            "chainlink-btc-usd-v1",
            "--market-slug",
            "btc-updown-15m-1776038400",
            "--market-slug",
            "btc-updown-15m-1776039300",
            "--output",
            "data/metadata/btc-15m.json",
        ]
    )

    assert args.market_slugs == ["btc-updown-15m-1776038400", "btc-updown-15m-1776039300"]


@pytest.mark.parametrize(
    ("value", "expected"),
    (("all", None), ("open", False), ("closed", True)),
)
def test_gamma_catalog_cli_maps_closed_filter(value: str, expected: bool | None) -> None:
    assert closed_filter(value) is expected
