from btc_short_horizon.execution_timing import paper_execution_lifecycle_tail_seconds


def test_maker_then_fak_tail_adds_both_serial_latency_phases() -> None:
    assert (
        paper_execution_lifecycle_tail_seconds(
            mode="maker_then_fak",
            maker_work_seconds=5.0,
            cancel_latency_ms=250.0,
            taker_latency_ms=200.0,
            taker_server_delay_ms=250.0,
        )
        == 5.7
    )


def test_maker_and_immediate_fak_use_only_their_own_route_latency() -> None:
    assert (
        paper_execution_lifecycle_tail_seconds(
            mode="maker",
            maker_work_seconds=15.0,
            cancel_latency_ms=250.0,
            taker_latency_ms=200.0,
            taker_server_delay_ms=250.0,
        )
        == 15.25
    )
    assert (
        paper_execution_lifecycle_tail_seconds(
            mode="immediate_fak",
            maker_work_seconds=0.0,
            cancel_latency_ms=250.0,
            taker_latency_ms=200.0,
            taker_server_delay_ms=250.0,
        )
        == 0.45
    )
