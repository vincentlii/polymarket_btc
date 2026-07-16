from __future__ import annotations

import httpx
import pytest

from btc_short_horizon.research.polymarket_price_history import (
    fetch_polymarket_price_history,
)


@pytest.mark.asyncio
async def test_price_history_batches_and_sorts_public_token_points() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read()
        import json

        body = json.loads(payload)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "history": {
                    token: [{"t": 20, "p": 0.6}, {"t": 10, "p": 0.5}] for token in body["markets"]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        points = await fetch_polymarket_price_history(
            token_ids=tuple(f"token-{index}" for index in range(21)),
            start_ts=1,
            end_ts=30,
            max_concurrency=2,
            client=client,
        )

    assert len(requests) == 2
    assert sorted(len(request["markets"]) for request in requests) == [1, 20]
    assert len(points) == 42
    assert points[0].ts_seconds == 10
