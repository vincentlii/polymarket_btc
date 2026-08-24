from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketOutcome, MarketWindow
from btc_short_horizon.research.pmxt_btc_history import reconstruct_pmxt_decision_books
from scripts.btc_market_relative_core_history import _fresh_decision_books, _in_enabled_range


def _market() -> MarketWindow:
    t0 = datetime(2026, 4, 28, 0, 15, tzinfo=UTC)
    return MarketWindow(
        slug=BTC_15M_MARKET_FAMILY.slug_for(t0),
        condition_id="0x" + "12" * 32,
        up_token_id="up",
        down_token_id="down",
        t0=t0,
        t1=t0 + timedelta(minutes=15),
        family=BTC_15M_MARKET_FAMILY,
        rule_epoch="point",
        rule_hash="a" * 64,
        resolution=MarketOutcome.UP,
        label_available_ts=t0 + timedelta(minutes=15),
    )


def _write_hour(root: Path, market: MarketWindow) -> None:
    hour = market.t0
    path = root / hour.strftime("%Y/%m/%d") / f"polymarket_orderbook_{hour:%Y-%m-%dT%H}.parquet"
    path.parent.mkdir(parents=True)
    received = [
        market.t0 - timedelta(seconds=1),
        market.t0 - timedelta(seconds=1),
        market.t0 + timedelta(seconds=4),
        market.t0 + timedelta(seconds=6),
    ]
    pq.write_table(
        pa.table(
            {
                "timestamp_received": pa.array(received, type=pa.timestamp("ns", tz="UTC")),
                "timestamp": pa.array(received, type=pa.timestamp("ns", tz="UTC")),
                "market": [market.condition_id.encode()] * 4,
                "event_type": ["book", "book", "price_change", "price_change"],
                "asset_id": ["up", "down", "up", "down"],
                "bids": ['[["0.48","10"]]', '[["0.49","12"]]', None, None],
                "asks": ['[["0.52","11"]]', '[["0.51","13"]]', None, None],
                "price": [None, None, Decimal("0.49"), Decimal("0.50")],
                "size": [None, None, Decimal("9"), Decimal("8")],
                "side": [None, None, "BUY", "SELL"],
                "best_bid": [None, None, Decimal("0.49"), Decimal("0.49")],
                "best_ask": [None, None, Decimal("0.52"), Decimal("0.50")],
            }
        ),
        path,
    )


def test_reconstruct_pmxt_decision_books_is_causal_and_dual_token(tmp_path) -> None:
    market = _market()
    _write_hour(tmp_path, market)
    decisions = tuple(
        int((market.t0 + timedelta(seconds=seconds)).timestamp() * 1e9) for seconds in (3, 5, 10)
    )

    books = reconstruct_pmxt_decision_books(
        raw_root=tmp_path, market=market, decision_ts_ns=decisions
    )

    assert [item.decision_ts_ns for item in books] == list(decisions)
    assert books[0].up.bid == 0.48
    assert books[1].up.bid == 0.49
    assert books[1].down.ask == 0.51
    assert books[2].down.ask == 0.50
    assert books[0].market_p_up == 0.5


def test_reconstruct_pmxt_decision_books_fails_closed_when_hour_missing(tmp_path) -> None:
    market = _market()
    decisions = (int(market.t0.timestamp() * 1e9),)

    assert not reconstruct_pmxt_decision_books(
        raw_root=tmp_path, market=market, decision_ts_ns=decisions
    )


def test_enabled_stage_requires_only_one_causal_early_decision() -> None:
    ranges = ((3.0, 30.0),)

    assert _in_enabled_range(5.0, ranges)
    assert _in_enabled_range(30.0, ranges)
    assert not _in_enabled_range(35.0, ranges)


def test_historical_core_rejects_books_older_than_live_stale_gate(tmp_path) -> None:
    market = _market()
    _write_hour(tmp_path, market)
    decisions = (int((market.t0 + timedelta(seconds=3)).timestamp() * 1e9),)
    (book,) = reconstruct_pmxt_decision_books(
        raw_root=tmp_path,
        market=market,
        decision_ts_ns=decisions,
    )

    assert _fresh_decision_books(
        (replace(book, data_age_seconds=1.0),),
        maximum_age_seconds=1.0,
    )
    assert not _fresh_decision_books(
        (replace(book, data_age_seconds=1.000_001),),
        maximum_age_seconds=1.0,
    )
