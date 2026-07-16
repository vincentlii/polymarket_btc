"""Lightweight public Polymarket token-price history adapter for proxy research."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite

import httpx


_BATCH_PRICE_HISTORY_URL = "https://clob.polymarket.com/batch-prices-history"
_MAX_BATCH_SIZE = 20


@dataclass(frozen=True, slots=True)
class TokenPricePoint:
    token_id: str
    ts_seconds: int
    price: float

    def __post_init__(self) -> None:
        if not self.token_id or self.ts_seconds < 0:
            raise ValueError("token_id and non-negative ts_seconds are required")
        if not isfinite(self.price) or not 0.0 < self.price < 1.0:
            raise ValueError("token price must be finite and in (0, 1)")


async def fetch_polymarket_price_history(
    *,
    token_ids: Sequence[str],
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int = 1,
    max_concurrency: int = 4,
    client: httpx.AsyncClient | None = None,
) -> tuple[TokenPricePoint, ...]:
    """Fetch deduplicated price points in official batches of at most 20 tokens."""

    normalized = tuple(dict.fromkeys(token_id.strip() for token_id in token_ids))
    if not normalized or any(not token_id for token_id in normalized):
        raise ValueError("token_ids must contain non-empty values")
    if start_ts < 0 or end_ts <= start_ts:
        raise ValueError("price-history time range is invalid")
    if fidelity_minutes < 1 or max_concurrency < 1:
        raise ValueError("fidelity_minutes and max_concurrency must be >= 1")

    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=30.0)
    semaphore = asyncio.Semaphore(max_concurrency)
    try:
        batches = tuple(
            normalized[offset : offset + _MAX_BATCH_SIZE]
            for offset in range(0, len(normalized), _MAX_BATCH_SIZE)
        )

        async def fetch(batch: tuple[str, ...]) -> tuple[TokenPricePoint, ...]:
            async with semaphore:
                return await _fetch_batch(
                    client=active_client,
                    token_ids=batch,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    fidelity_minutes=fidelity_minutes,
                )

        results = await asyncio.gather(*(fetch(batch) for batch in batches))
    finally:
        if owns_client:
            await active_client.aclose()
    unique = {
        (point.token_id, point.ts_seconds, point.price): point
        for batch in results
        for point in batch
    }
    return tuple(
        sorted(unique.values(), key=lambda point: (point.token_id, point.ts_seconds, point.price))
    )


async def _fetch_batch(
    *,
    client: httpx.AsyncClient,
    token_ids: tuple[str, ...],
    start_ts: int,
    end_ts: int,
    fidelity_minutes: int,
) -> tuple[TokenPricePoint, ...]:
    payload = {
        "markets": list(token_ids),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "fidelity": fidelity_minutes,
    }
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = await client.post(_BATCH_PRICE_HISTORY_URL, json=payload)
            response.raise_for_status()
            return _parse_batch_response(response.json(), requested_tokens=set(token_ids))
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(0.25 * (2**attempt))
    assert last_error is not None
    raise RuntimeError(f"Polymarket price-history batch failed: {last_error}") from last_error


def _parse_batch_response(
    payload: object, *, requested_tokens: set[str]
) -> tuple[TokenPricePoint, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("history"), dict):
        raise ValueError("batch price-history response must contain a history object")
    history = payload["history"]
    points: list[TokenPricePoint] = []
    for token_id, values in history.items():
        if token_id not in requested_tokens:
            raise ValueError("batch price-history response contains an unrequested token")
        if not isinstance(values, list):
            raise ValueError("token price history must be a list")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("token price point must be an object")
            points.append(
                TokenPricePoint(
                    token_id=token_id,
                    ts_seconds=int(value["t"]),
                    price=float(value["p"]),
                )
            )
    return tuple(points)


__all__ = ["TokenPricePoint", "fetch_polymarket_price_history"]
