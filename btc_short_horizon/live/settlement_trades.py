"""Public, immutable trade evidence used for deferred Paper maker fills."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
from uuid import uuid4

import httpx

from btc_short_horizon.data import MarketWindow


_DATA_API_HOST = "https://data-api.polymarket.com"
_PAGE_LIMIT = 10_000


@dataclass(frozen=True, slots=True)
class SettlementTrade:
    token_id: str
    price: float
    size: float
    timestamp_seconds: int
    evidence_id: str


@dataclass(frozen=True, slots=True)
class SettlementTradeEvidence:
    market_slug: str
    condition_id: str
    start_seconds: int
    end_seconds: int
    fetched_at: datetime
    trades: tuple[SettlementTrade, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": "polymarket-public-settlement-trades-v1",
            "market_slug": self.market_slug,
            "condition_id": self.condition_id,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "fetched_at": self.fetched_at.astimezone(UTC).isoformat(),
            "trade_count": len(self.trades),
            "trades": [
                {
                    "token_id": item.token_id,
                    "price": item.price,
                    "size": item.size,
                    "timestamp_seconds": item.timestamp_seconds,
                    "evidence_id": item.evidence_id,
                }
                for item in self.trades
            ],
        }


class PublicSettlementTradesClient:
    """Fetch seller-initiated public trades once a market has resolved."""

    def __init__(
        self,
        *,
        base_url: str = _DATA_API_HOST,
        timeout_seconds: float = 15.0,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("public trade base_url must use https")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise ValueError("timeout_seconds must be finite and > 0")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def fetch(
        self,
        market: MarketWindow,
        *,
        start_seconds: int,
        end_seconds: int,
        client: httpx.AsyncClient | None = None,
    ) -> SettlementTradeEvidence:
        if start_seconds >= end_seconds:
            raise ValueError("settlement trade interval must be non-empty")
        owns_client = client is None
        active = client or httpx.AsyncClient(timeout=self.timeout_seconds)
        rows: list[object] = []
        try:
            offset = 0
            while True:
                response = await active.get(
                    f"{self.base_url}/trades",
                    params={
                        "market": market.condition_id,
                        "side": "SELL",
                        "start": start_seconds,
                        "end": end_seconds - 1,
                        "takerOnly": "true",
                        "limit": _PAGE_LIMIT,
                        "offset": offset,
                    },
                )
                response.raise_for_status()
                page = response.json()
                if not isinstance(page, list):
                    raise ValueError("public trade response must be a JSON array")
                rows.extend(page)
                if len(page) < _PAGE_LIMIT:
                    break
                offset += _PAGE_LIMIT
                if offset > _PAGE_LIMIT:
                    raise ValueError("public trade result exceeds the auditable API window")
        finally:
            if owns_client:
                await active.aclose()
        trades = _parse_trades(
            rows,
            market=market,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        return SettlementTradeEvidence(
            market_slug=market.slug,
            condition_id=market.condition_id,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            fetched_at=datetime.now(UTC),
            trades=trades,
        )


class SettlementTradeEvidenceStore:
    def __init__(self, runtime_root: Path, execution_epoch: str) -> None:
        self.root = runtime_root / "paper" / "epochs" / execution_epoch / "settlement-trades"

    def write(self, evidence: SettlementTradeEvidence) -> Path:
        path = self.root / f"{evidence.market_slug}.json"
        payload = evidence.to_json()
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path


def _parse_trades(
    rows: list[object],
    *,
    market: MarketWindow,
    start_seconds: int,
    end_seconds: int,
) -> tuple[SettlementTrade, ...]:
    expected_tokens = {market.up_token_id, market.down_token_id}
    unique: dict[str, SettlementTrade] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("public trade row must be a JSON object")
        condition_id = str(raw.get("conditionId", ""))
        token_id = str(raw.get("asset", ""))
        side = str(raw.get("side", ""))
        if condition_id.casefold() != market.condition_id.casefold():
            raise ValueError("public trade condition does not match the market")
        if token_id not in expected_tokens:
            raise ValueError("public trade token does not match the market")
        if side != "SELL":
            raise ValueError("public settlement evidence must contain only SELL trades")
        price = _positive_number(raw.get("price"), "price")
        size = _positive_number(raw.get("size"), "size")
        timestamp = raw.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            raise ValueError("public trade timestamp must be an integer")
        if not start_seconds <= timestamp < end_seconds:
            raise ValueError("public trade timestamp is outside the requested interval")
        identity = json.dumps(
            {
                "asset": token_id,
                "conditionId": condition_id.casefold(),
                "price": price,
                "proxyWallet": str(raw.get("proxyWallet", "")),
                "side": side,
                "size": size,
                "timestamp": timestamp,
                "transactionHash": str(raw.get("transactionHash", "")),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        evidence_id = sha256(identity).hexdigest()
        unique[evidence_id] = SettlementTrade(
            token_id=token_id,
            price=price,
            size=size,
            timestamp_seconds=timestamp,
            evidence_id=evidence_id,
        )
    return tuple(
        sorted(unique.values(), key=lambda item: (item.timestamp_seconds, item.evidence_id))
    )


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"public trade {name} must be numeric")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"public trade {name} must be numeric") from exc
    if not isfinite(result) or result <= 0.0:
        raise ValueError(f"public trade {name} must be finite and > 0")
    return result


__all__ = [
    "PublicSettlementTradesClient",
    "SettlementTrade",
    "SettlementTradeEvidence",
    "SettlementTradeEvidenceStore",
]
