"""Discover validated BTC Gamma markets and write a reusable market catalog."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

if __package__ in {None, ""}:
    from _script_helpers import ensure_repo_root
else:
    from ._script_helpers import ensure_repo_root

ensure_repo_root(__file__)

from btc_short_horizon.config import BtcProjectConfig, load_btc_project_config  # noqa: E402
from btc_short_horizon.data.catalog_io import write_market_catalog  # noqa: E402
from btc_short_horizon.data.gamma import GammaMarketClient  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/btc_short_horizon/baseline.toml"),
        help="BTC project TOML configuration.",
    )
    parser.add_argument(
        "--family",
        choices=("15m", "5m"),
        default="15m",
        help="BTC market family to discover; 5m remains collection-only.",
    )
    parser.add_argument("--rule-epoch", required=True, help="Current verified market-rule epoch.")
    parser.add_argument(
        "--closed",
        choices=("all", "open", "closed"),
        default="open",
        help="Gamma market state to include.",
    )
    parser.add_argument(
        "--market-slug",
        action="append",
        dest="market_slugs",
        help="Exact BTC market slug to fetch. Repeat to build a bounded catalog.",
    )
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True, help="Destination catalog JSON file.")
    return parser.parse_args(argv)


def select_family(config: BtcProjectConfig, name: str):  # type: ignore[no-untyped-def]
    if name == "15m":
        return config.primary_family
    if name == "5m":
        return config.collection_only_family
    raise ValueError(f"unsupported BTC family {name!r}")


def closed_filter(value: str) -> bool | None:
    return {"all": None, "open": False, "closed": True}[value]


async def discover(args: argparse.Namespace) -> int:
    config = load_btc_project_config(args.config)
    family = select_family(config, args.family)
    catalog = await GammaMarketClient().discover_catalog(
        family=family,
        rule_epoch=args.rule_epoch,
        closed=closed_filter(args.closed),
        page_size=args.page_size,
        slugs=args.market_slugs,
    )
    write_market_catalog(path=args.output, catalog=catalog)
    print(f"Wrote {len(catalog)} {family.name} Gamma markets to {args.output}.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(discover(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
