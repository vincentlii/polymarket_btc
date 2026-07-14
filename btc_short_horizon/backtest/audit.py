"""Small in-memory lifecycle ledger retained by the BTC replay strategy."""

from __future__ import annotations

from typing import Mapping


class OrderAuditTrail:
    """Append JSON-ready order lifecycle events in strategy observation order."""

    def __init__(self) -> None:
        self._records: list[dict[str, object]] = []

    @property
    def records(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(record) for record in self._records)

    def record(
        self,
        *,
        event_type: str,
        ts_ns: int,
        client_order_id: str | None = None,
        **details: object,
    ) -> None:
        if not event_type or not event_type.strip():
            raise ValueError("event_type is required")
        if ts_ns < 0:
            raise ValueError("ts_ns must be non-negative")
        record: dict[str, object] = {
            "event_type": event_type.strip(),
            "ts_ns": int(ts_ns),
        }
        if client_order_id is not None:
            record["client_order_id"] = client_order_id
        record.update(_json_ready_details(details))
        self._records.append(record)


def _json_ready_details(details: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in details.items():
        if isinstance(value, str | int | float | bool) or value is None:
            normalized[key] = value
        else:
            normalized[key] = str(value)
    return normalized


__all__ = ["OrderAuditTrail"]
