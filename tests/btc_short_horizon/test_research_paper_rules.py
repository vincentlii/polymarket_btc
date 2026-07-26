from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from btc_short_horizon.data import BTC_15M_MARKET_FAMILY, MarketWindow
from btc_short_horizon.live.paper_runtime import PublicPaperRulesClient, ResearchPaperRuntime


T0 = datetime(2026, 7, 27, tzinfo=UTC)


def _market() -> MarketWindow:
    return MarketWindow(
        family=BTC_15M_MARKET_FAMILY,
        slug=BTC_15M_MARKET_FAMILY.slug_for(T0),
        condition_id="0x" + "ab" * 32,
        up_token_id="1",
        down_token_id="2",
        t0=T0,
        t1=T0 + timedelta(minutes=15),
        rule_epoch="btc-chainlink-v1",
        rule_hash="a" * 64,
    )


@pytest.mark.asyncio
async def test_public_paper_rules_use_one_clob_snapshot_and_require_taker_only_fees() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/clob-markets/{_market().condition_id}"
        return httpx.Response(
            200,
            json={
                "t": [{"t": "1", "o": "Up"}, {"t": "2", "o": "Down"}],
                "mos": 5,
                "mts": 0.01,
                "mbf": 1000,
                "nr": None,
                "fd": {"r": 0.07, "e": 1, "to": True},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rules = await PublicPaperRulesClient().fetch(_market(), client=client)

    assert set(rules) == {"1", "2"}
    assert rules["1"].tick_size == "0.01"
    assert rules["1"].minimum_order_size == 5.0
    assert rules["1"].maker_fee_rate_bps == 0


@pytest.mark.asyncio
async def test_public_paper_rules_fail_closed_if_fee_schedule_is_not_taker_only() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "t": [{"t": "1", "o": "Up"}, {"t": "2", "o": "Down"}],
                "mos": 5,
                "mts": 0.01,
                "mbf": 1,
                "nr": False,
                "fd": {"r": 0.03, "e": 2, "to": False},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="taker-only"):
            await PublicPaperRulesClient().fetch(_market(), client=client)


def test_paper_runtime_keeps_deciding_through_order_work_horizon() -> None:
    class Engine:
        def __init__(self) -> None:
            self.market = _market()
            self.decisions: list[int] = []
            self.advances: list[int] = []

        def decide(self, *, now_ts_ns: int) -> str:
            self.decisions.append(now_ts_ns)
            return "continue"

        def advance(self, *, now_ts_ns: int) -> None:
            self.advances.append(now_ts_ns)

    runtime = object.__new__(ResearchPaperRuntime)
    runtime.engine = Engine()  # type: ignore[assignment]
    runtime.project = SimpleNamespace(
        maker=SimpleNamespace(
            entry_end_seconds=180.0,
            max_work_seconds=15.0,
            signal_cadence_seconds=5.0,
        )
    )
    runtime._next_decision_ns = int(T0.timestamp() * 1_000_000_000) + 185_000_000_000
    runtime._last_decision_result = "not_started"
    runtime._prediction_errors = 0

    for second in (185, 190, 195, 200):
        runtime._run_due_decision(T0 + timedelta(seconds=second))

    assert runtime.engine.decisions == [
        int(T0.timestamp() * 1_000_000_000) + second * 1_000_000_000 for second in (185, 190, 195)
    ]
    assert runtime.engine.advances == [int(T0.timestamp() * 1_000_000_000) + 200_000_000_000]
    assert runtime._next_decision_ns is None


@pytest.mark.asyncio
async def test_paper_runtime_fails_before_processing_after_event_buffer_overflow() -> None:
    runtime = object.__new__(ResearchPaperRuntime)
    published: list[tuple[str, bool]] = []

    async def bootstrap() -> None:
        return None

    runtime._bootstrap_history = bootstrap  # type: ignore[method-assign]
    runtime._drain_events = lambda: pytest.fail("overflowed events must not be processed")  # type: ignore[method-assign]
    runtime._publish = lambda *, now, state, healthy: published.append((state, healthy))  # type: ignore[method-assign]
    runtime.event_buffer = SimpleNamespace(overflowed=True)
    runtime._last_error = None
    runtime._ready = False

    await runtime.run(stop_event=asyncio.Event())

    assert runtime._last_error == "admitted_event_buffer_overflow"
    assert published == [("failed", False)]


@pytest.mark.asyncio
async def test_paper_rules_retry_is_rate_limited_after_failure() -> None:
    runtime = object.__new__(ResearchPaperRuntime)
    attempts = 0

    async def fail_rules(_market) -> None:  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1

    runtime._rules = {}
    runtime._rule_tasks = {}
    runtime._next_rule_retry = {}
    runtime._fetch_rules = fail_rules  # type: ignore[method-assign]
    market = _market()

    runtime._schedule_rule_fetch(market, now=T0)
    await runtime._rule_tasks[market.slug]
    runtime._schedule_rule_fetch(market, now=T0 + timedelta(seconds=1))
    runtime._schedule_rule_fetch(market, now=T0 + timedelta(seconds=5))
    await runtime._rule_tasks[market.slug]

    assert attempts == 2


@pytest.mark.asyncio
async def test_resolution_poll_failure_is_recoverable_and_clears_after_success() -> None:
    runtime = object.__new__(ResearchPaperRuntime)
    runtime._recoverable_errors = {}
    runtime._resolution_errors = 0

    async def fail() -> None:
        raise httpx.ReadTimeout("temporary")

    runtime._settle_resolved_markets = fail  # type: ignore[method-assign]
    await runtime._refresh_resolutions()

    assert runtime._resolution_errors == 1
    assert "resolution" in runtime._recoverable_errors

    async def succeed() -> None:
        return None

    runtime._settle_resolved_markets = succeed  # type: ignore[method-assign]
    await runtime._refresh_resolutions()

    assert runtime._recoverable_errors == {}


def test_running_projection_is_published_at_five_second_cadence() -> None:
    runtime = object.__new__(ResearchPaperRuntime)
    published: list[datetime] = []
    runtime._next_publish_at = datetime.min.replace(tzinfo=UTC)
    runtime._recoverable_errors = {}
    runtime._publish = lambda *, now, state, healthy: published.append(now)  # type: ignore[method-assign]

    runtime._publish_running_if_due(T0)
    runtime._publish_running_if_due(T0 + timedelta(seconds=1))
    runtime._publish_running_if_due(T0 + timedelta(seconds=5))

    assert published == [T0, T0 + timedelta(seconds=5)]
