from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from btc_short_horizon.research.binance_history import (
    fetch_binance_spot_kline_history,
    load_binance_kline_archives,
)


def _rest_kline(open_time_ms: int, index: int) -> list[object]:
    close = 100_000.0 + index
    return [
        open_time_ms,
        str(close - 1.0),
        str(close + 1.0),
        str(close - 2.0),
        str(close),
        "2.0",
        open_time_ms + 999,
        str(close * 2.0),
        3,
        "1.2",
        str(close * 1.2),
        "0",
    ]


@pytest.mark.asyncio
async def test_rest_kline_bootstrap_is_bounded_paginated_and_final_only() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1_000)
    rows = [_rest_kline(start_ms + index * 1_000, index) for index in range(1_003)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        first = int(request.url.params["startTime"])
        last = int(request.url.params["endTime"])
        limit = int(request.url.params["limit"])
        page = [row for row in rows if first <= int(row[0]) <= last][:limit]
        return httpx.Response(200, json=page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        history = await fetch_binance_spot_kline_history(
            start_time=start,
            end_time=start + timedelta(seconds=1_002, milliseconds=500),
            maximum_bars=2_000,
            client=client,
        )

    assert len(requests) == 2
    assert all(request.url.params["limit"] == "1000" for request in requests)
    assert len(history.open_ts_ns) == 1_002
    assert history.open_ts_ns[-1] == (start_ms + 1_001_000) * 1_000_000
    assert history.close[-1] == pytest.approx(101_001.0)


@pytest.mark.asyncio
async def test_rest_kline_bootstrap_rejects_large_or_gapped_history() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        with pytest.raises(ValueError, match="maximum_bars"):
            await fetch_binance_spot_kline_history(
                start_time=start,
                end_time=start + timedelta(seconds=4_001),
                client=client,
            )

    start_ms = int(start.timestamp() * 1_000)
    rows = [_rest_kline(start_ms, 0), _rest_kline(start_ms + 2_000, 2)]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rows))
    ) as client:
        with pytest.raises(ValueError, match="contiguous"):
            await fetch_binance_spot_kline_history(
                start_time=start,
                end_time=start + timedelta(seconds=3),
                client=client,
            )


@pytest.mark.parametrize("include_header", [False, True])
def test_archive_loader_accepts_spot_and_futures_csv_header_conventions(
    tmp_path: Path,
    include_header: bool,
) -> None:
    archive_path = tmp_path / "BTCUSDT-1m-2026-01-01.zip"
    header = (
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
        "taker_buy_volume,taker_buy_quote_volume,ignore\n"
        if include_header
        else ""
    )
    rows = (
        "1767225600000,100,101,99,100.5,2,1767225659999,201,5,1.2,120.6,0\n"
        "1767225660000,100.5,102,100,101.5,3,1767225719999,304.5,6,1.8,182.7,0\n"
    )
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("BTCUSDT-1m-2026-01-01.csv", header + rows)

    history = load_binance_kline_archives((archive_path,), interval="1m")

    assert history.open_ts_ns.tolist() == [1767225600000000000, 1767225660000000000]
    assert history.close.tolist() == pytest.approx([100.5, 101.5])
