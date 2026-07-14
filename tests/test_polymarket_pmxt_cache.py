from __future__ import annotations

import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from nautilus_trader.adapters.polymarket.common.parsing import parse_polymarket_instrument

from prediction_market_extensions.adapters.polymarket import pmxt as pmxt_module
from prediction_market_extensions.adapters.polymarket.pmxt import PolymarketPMXTDataLoader
from prediction_market_extensions._runtime_log import capture_loader_events


def _make_loader(
    cache_dir: Path | None, *, local_archive_dir: Path | None = None
) -> PolymarketPMXTDataLoader:
    loader = object.__new__(PolymarketPMXTDataLoader)
    loader._pmxt_cache_dir = cache_dir
    loader._pmxt_local_archive_dir = local_archive_dir
    loader._condition_id = "condition-123"
    loader._token_id = "token-yes-123"
    loader._pmxt_prefetch_workers = 2
    loader._pmxt_download_progress_callback = None
    loader._pmxt_scan_progress_callback = None
    loader._pmxt_progress_size_cache = {}
    loader._pmxt_temp_download_root = (
        cache_dir if cache_dir is not None else Path.cwd()
    ) / ".pmxt-temp-downloads"
    loader._pmxt_last_load_gap_hours = ()
    return loader


def _make_instrument():
    return parse_polymarket_instrument(
        market_info={
            "condition_id": "0x" + "1" * 64,
            "question": "Synthetic PMXT market",
            "minimum_tick_size": "0.01",
            "minimum_order_size": "1",
            "end_date_iso": "2026-12-31T00:00:00Z",
            "maker_base_fee": "0",
            "taker_base_fee": "0",
        },
        token_id="2" * 64,
        outcome="Yes",
        ts_init=0,
    )


def test_delta_columns_preserve_instrument_rounding(tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = _make_instrument()

    records = loader._deltas_records_from_columns(
        {
            "event_index": [0],
            "action": [1],
            "side": [1],
            "price": [0.105],
            "size": [1009.1234564],
            "flags": [0],
            "sequence": [0],
            "ts_event": [100],
            "ts_init": [100],
        }
    )
    delta = records[0].deltas[0]

    assert delta.order.price.raw == loader.instrument.make_price(0.105).raw
    assert delta.order.size.raw == loader.instrument.make_qty(1009.1234564).raw


def test_materialized_deltas_cache_round_trips(tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = _make_instrument()
    start = pd.Timestamp("2026-03-16T12:00:00Z")
    end = pd.Timestamp("2026-03-16T13:00:00Z")
    records = loader._deltas_records_from_columns(
        {
            "event_index": [0, 1],
            "action": [4, 1],
            "side": [0, 1],
            "price": [0.0, 0.105],
            "size": [0.0, 1009.1234564],
            "flags": [0, 0],
            "sequence": [0, 1],
            "ts_event": [100, 200],
            "ts_init": [100, 200],
        }
    )

    loader._write_deltas_cache_for_range(records, start, end)
    cached = loader._load_deltas_cache_for_range(start, end)

    assert cached is not None
    assert len(cached) == len(records)
    assert [int(record.ts_event) for record in cached] == [100, 200]
    assert cached[1].deltas[0].order.price.raw == loader.instrument.make_price(0.105).raw


def test_materialized_deltas_cache_serializes_same_target_replacements(tmp_path, monkeypatch):
    loader = _make_loader(tmp_path)
    loader._instrument = _make_instrument()
    start = pd.Timestamp("2026-03-16T12:00:00Z")
    end = pd.Timestamp("2026-03-16T13:00:00Z")
    records = loader._deltas_records_from_columns(
        {
            "event_index": [0],
            "action": [1],
            "side": [1],
            "price": [0.105],
            "size": [10.0],
            "flags": [0],
            "sequence": [0],
            "ts_event": [100],
            "ts_init": [100],
        }
    )
    write_barrier = threading.Barrier(2)
    replacement_lock = threading.Lock()
    active_replacements = 0
    max_active_replacements = 0
    original_write_table = pmxt_module.pq.write_table
    original_replace = pmxt_module.os.replace

    def write_table_with_barrier(table, where, *args, **kwargs):  # type: ignore[no-untyped-def]
        write_barrier.wait(timeout=5)
        return original_write_table(table, where, *args, **kwargs)

    def tracked_replace(source, target):  # type: ignore[no-untyped-def]
        nonlocal active_replacements, max_active_replacements
        with replacement_lock:
            active_replacements += 1
            max_active_replacements = max(max_active_replacements, active_replacements)
        try:
            time.sleep(0.05)
            return original_replace(source, target)
        finally:
            with replacement_lock:
                active_replacements -= 1

    monkeypatch.setattr(pmxt_module.pq, "write_table", write_table_with_barrier)
    monkeypatch.setattr(pmxt_module.os, "replace", tracked_replace)
    threads = [
        threading.Thread(target=loader._write_deltas_cache_for_range, args=(records, start, end))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert max_active_replacements == 1
    assert loader._load_deltas_cache_for_range(start, end) is not None


def test_pmxt_v2_cache_paths_do_not_reuse_prior_schema(tmp_path):
    loader = _make_loader(tmp_path)
    hour = pd.Timestamp("2026-03-16T12:00:00Z")
    legacy_path = (
        tmp_path / "condition-123" / "token-yes-123" / "polymarket_orderbook_2026-03-16T12.parquet"
    )
    legacy_path.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "event_type": ["book"],
                "timestamp_ns": [1_774_094_400_001_000_000],
                "asset_id": ["token-yes-123"],
                "bids": ['[["0.48","11"]]'],
                "asks": ['[["0.52","9"]]'],
                "price": [None],
                "size": [None],
                "side": [None],
            }
        ),
        legacy_path,
    )

    cache_path = loader._cache_path_for_hour(hour)

    assert cache_path is not None
    assert cache_path != legacy_path
    assert cache_path.parent.parts[-3:] == ("filtered-v2", "condition-123", "token-yes-123")
    assert loader._load_cached_market_table(hour) is None
    next_hour = pd.Timestamp(int(hour.value) + 3_600_000_000_000, unit="ns", tz="UTC")
    assert loader._window_cache_path_for_range(hour, next_hour).parts[-4] == "window-v2"
    assert loader._deltas_cache_path_for_range(hour, next_hour).parts[-4] == "book-deltas-v3"


def test_fixed_schema_preserves_receive_time(tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = _make_instrument()
    hour = pd.Timestamp("2026-03-16T12:00:00Z")
    source_snapshot = int(hour.value) + 1_000_000
    source_change = int(hour.value) + 2_000_000
    received_snapshot = int(hour.value) + 5_000_000
    received_change = int(hour.value) + 6_000_000
    batch = pa.record_batch(
        [
            pa.array(["book", "price_change"]),
            pa.array([source_snapshot, source_change], type=pa.int64()),
            pa.array([received_snapshot, received_change], type=pa.int64()),
            pa.array(["token-yes-123", "token-yes-123"]),
            pa.array(['[["0.48","11"]]', None]),
            pa.array(['[["0.52","9"]]', None]),
            pa.array([None, "0.49"]),
            pa.array([None, "13.5"]),
            pa.array([None, "BUY"]),
            pa.array([None, None]),
        ],
        names=PolymarketPMXTDataLoader._PMXT_FIXED_COLUMNS,
    )
    start = pd.Timestamp(received_snapshot, unit="ns", tz="UTC")
    end = pd.Timestamp(received_change, unit="ns", tz="UTC")

    records = loader.load_order_book_deltas_from_hour_batches(
        start,
        end,
        ((hour, [batch]),),
    )

    assert int(records[0].ts_event) == source_snapshot
    assert int(records[0].ts_init) == received_snapshot
    assert int(records[1].ts_event) == source_change
    assert int(records[1].ts_init) == received_change


def test_pmxt_last_trade_price_loads_as_millisecond_trade_tick(tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = _make_instrument()
    hour = pd.Timestamp("2026-03-16T12:00:00Z")
    source_trade = int(hour.value) + 3_000_000
    received_trade = int(hour.value) + 7_000_000
    batch = pa.record_batch(
        [
            pa.array(["last_trade_price"]),
            pa.array([source_trade], type=pa.int64()),
            pa.array([received_trade], type=pa.int64()),
            pa.array(["token-yes-123"]),
            pa.array([None]),
            pa.array([None]),
            pa.array(["0.51"]),
            pa.array(["7"]),
            pa.array(["SELL"]),
            pa.array(["0xtrade"]),
        ],
        names=PolymarketPMXTDataLoader._PMXT_FIXED_COLUMNS,
    )
    start = pd.Timestamp(received_trade - 1, unit="ns", tz="UTC")
    end = pd.Timestamp(received_trade, unit="ns", tz="UTC")

    loader._archive_hours = lambda _start, _end: [hour]  # type: ignore[method-assign]
    loader._iter_market_batches = lambda _hours, *, batch_size: iter(  # type: ignore[method-assign]
        ((hour, [batch]),)
    )
    trades = loader.load_pmxt_trade_ticks(start, end)

    assert len(trades) == 1
    assert float(trades[0].price) == 0.51
    assert float(trades[0].size) == 7.0
    assert int(trades[0].aggressor_side) == 2
    assert len(str(trades[0].trade_id)) == 36
    assert int(trades[0].ts_event) == source_trade
    assert int(trades[0].ts_init) == received_trade


def test_load_order_book_deltas_prefers_materialized_cache(monkeypatch, tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = _make_instrument()
    start = pd.Timestamp("2026-03-16T12:00:00Z")
    end = pd.Timestamp("2026-03-16T13:00:00Z")
    records = loader._deltas_records_from_columns(
        {
            "event_index": [0],
            "action": [4],
            "side": [0],
            "price": [0.0],
            "size": [0.0],
            "flags": [0],
            "sequence": [0],
            "ts_event": [100],
            "ts_init": [100],
        }
    )
    loader._write_deltas_cache_for_range(records, start, end)

    def fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("PMXT materialized cache should bypass row caches and raw dumps")

    monkeypatch.setattr(loader, "_load_window_cache_batches", fail)
    monkeypatch.setattr(loader, "_iter_market_batches", fail)

    cached = loader.load_order_book_deltas(start, end)

    assert len(cached) == 1
    assert int(cached[0].ts_event) == 100


def test_resolve_cache_dir_defaults_to_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.delenv(PolymarketPMXTDataLoader._PMXT_CACHE_DIR_ENV, raising=False)
    monkeypatch.delenv(PolymarketPMXTDataLoader._PMXT_DISABLE_CACHE_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))

    assert PolymarketPMXTDataLoader._resolve_cache_dir() == (
        tmp_path / "xdg-cache" / "nautilus_trader" / "pmxt"
    )


def test_resolve_prefetch_workers_parses_env(monkeypatch):
    monkeypatch.delenv(PolymarketPMXTDataLoader._PMXT_PREFETCH_WORKERS_ENV, raising=False)
    assert PolymarketPMXTDataLoader._resolve_prefetch_workers() == 16

    monkeypatch.setenv(PolymarketPMXTDataLoader._PMXT_PREFETCH_WORKERS_ENV, "8")
    assert PolymarketPMXTDataLoader._resolve_prefetch_workers() == 8

    monkeypatch.setenv(PolymarketPMXTDataLoader._PMXT_PREFETCH_WORKERS_ENV, "invalid")
    assert PolymarketPMXTDataLoader._resolve_prefetch_workers() == 16


def test_write_window_cache_is_opt_in(monkeypatch):
    monkeypatch.delenv(PolymarketPMXTDataLoader._PMXT_WRITE_WINDOW_CACHE_ENV, raising=False)
    assert not PolymarketPMXTDataLoader._write_window_cache_enabled()

    monkeypatch.setenv(PolymarketPMXTDataLoader._PMXT_WRITE_WINDOW_CACHE_ENV, "1")
    assert PolymarketPMXTDataLoader._write_window_cache_enabled()

    monkeypatch.setenv(PolymarketPMXTDataLoader._PMXT_WRITE_WINDOW_CACHE_ENV, "0")
    assert not PolymarketPMXTDataLoader._write_window_cache_enabled()


def test_resolve_scan_batch_size_parses_env(monkeypatch):
    monkeypatch.delenv(PolymarketPMXTDataLoader._PMXT_SCAN_BATCH_SIZE_ENV, raising=False)
    assert (
        PolymarketPMXTDataLoader._resolve_scan_batch_size()
        == PolymarketPMXTDataLoader._PMXT_DEFAULT_SCAN_BATCH_SIZE
    )

    monkeypatch.setenv(PolymarketPMXTDataLoader._PMXT_SCAN_BATCH_SIZE_ENV, "250000")
    assert PolymarketPMXTDataLoader._resolve_scan_batch_size() == 250_000

    monkeypatch.setenv(PolymarketPMXTDataLoader._PMXT_SCAN_BATCH_SIZE_ENV, "invalid")
    assert (
        PolymarketPMXTDataLoader._resolve_scan_batch_size()
        == PolymarketPMXTDataLoader._PMXT_DEFAULT_SCAN_BATCH_SIZE
    )


def test_load_order_book_deltas_uses_large_default_scan_batch(tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = _make_instrument()
    hour = pd.Timestamp("2026-03-16T12:00:00Z")
    captured: dict[str, int] = {}

    loader._archive_hours = lambda _start, _end: [hour]  # type: ignore[method-assign]

    def _iter_market_batches(hours, *, batch_size):  # type: ignore[no-untyped-def]
        captured["batch_size"] = batch_size
        return iter((hour, []) for hour in hours)

    loader._iter_market_batches = _iter_market_batches  # type: ignore[method-assign]

    data = loader.load_order_book_deltas(hour, hour + pd.Timedelta(hours=1))

    assert data == []
    assert captured["batch_size"] == PolymarketPMXTDataLoader._PMXT_DEFAULT_SCAN_BATCH_SIZE


def test_resolve_local_archive_dir_parses_env(monkeypatch, tmp_path):
    monkeypatch.delenv(PolymarketPMXTDataLoader._PMXT_LOCAL_ARCHIVE_DIR_ENV, raising=False)
    assert PolymarketPMXTDataLoader._resolve_local_archive_dir() is None

    monkeypatch.setenv(
        PolymarketPMXTDataLoader._PMXT_LOCAL_ARCHIVE_DIR_ENV, str(tmp_path / "pmxt-archive")
    )
    assert PolymarketPMXTDataLoader._resolve_local_archive_dir() == (tmp_path / "pmxt-archive")

    monkeypatch.setenv(PolymarketPMXTDataLoader._PMXT_LOCAL_ARCHIVE_DIR_ENV, "0")
    assert PolymarketPMXTDataLoader._resolve_local_archive_dir() is None


def test_load_market_table_writes_token_filtered_cache(tmp_path):
    loader = _make_loader(tmp_path)
    hour = pd.Timestamp("2026-03-16T12:00:00Z")
    remote_table = pa.table(
        {
            "update_type": ["book_snapshot", "price_change", "price_change"],
            "data": [
                '{"token_id":"token-yes-123","payload":"keep-1"}',
                '{"token_id":"token-no-456","payload":"drop"}',
                '{"token_id":"token-yes-123","payload":"keep-2"}',
            ],
        }
    )

    loader._load_remote_market_table = lambda _hour, *, batch_size: remote_table  # type: ignore[method-assign]

    loaded = loader._load_market_table(hour, batch_size=1_000)

    assert loaded is not None
    assert loaded.to_pylist() == [
        {"update_type": "book_snapshot", "data": '{"token_id":"token-yes-123","payload":"keep-1"}'},
        {"update_type": "price_change", "data": '{"token_id":"token-yes-123","payload":"keep-2"}'},
    ]
    assert loader._cache_path_for_hour(hour) == (
        tmp_path
        / "filtered-v2"
        / "condition-123"
        / "token-yes-123"
        / "polymarket_orderbook_2026-03-16T12.parquet"
    )

    cached = loader._load_cached_market_table(hour)
    assert cached is not None
    assert cached.to_pylist() == loaded.to_pylist()


def test_load_market_table_prefers_cached_table(tmp_path):
    loader = _make_loader(tmp_path)
    hour = pd.Timestamp("2026-03-16T13:00:00Z")
    cached_table = pa.table(
        {
            "update_type": ["book_snapshot"],
            "data": ['{"token_id":"token-yes-123","payload":"cached"}'],
        }
    )
    loader._write_market_cache(hour, cached_table)

    def _fail_remote(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("remote load should not run when cache exists")

    loader._load_remote_market_table = _fail_remote  # type: ignore[method-assign]

    loaded = loader._load_market_table(hour, batch_size=1_000)

    assert loaded is not None
    assert loaded.to_pylist() == cached_table.to_pylist()


def test_scan_raw_market_batches_emits_scan_progress(tmp_path):
    loader = _make_loader(tmp_path / "cache")
    raw_path = tmp_path / "polymarket_orderbook_2026-03-16T13.parquet"
    pq.write_table(
        pa.table(
            {
                "market_id": ["condition-123", "condition-123"],
                "update_type": ["book_snapshot", "price_change"],
                "data": [
                    '{"token_id":"token-yes-123","payload":"keep"}',
                    '{"token_id":"token-no-456","payload":"drop"}',
                ],
            }
        ),
        raw_path,
    )

    events: list[tuple[int, int, int, int | None, bool]] = []
    loader._pmxt_scan_progress_callback = (
        lambda _source, scanned_batches, scanned_rows, matched_rows, total_bytes, finished: (
            events.append(  # type: ignore[assignment]
                (scanned_batches, scanned_rows, matched_rows, total_bytes, finished)
            )
        )
    )

    dataset = ds.dataset(str(raw_path), format="parquet")
    batches = loader._scan_raw_market_batches(
        dataset, batch_size=1_000, source=str(raw_path), total_bytes=raw_path.stat().st_size
    )

    assert batches
    assert events
    assert events[0] == (0, 0, 0, raw_path.stat().st_size, False)
    assert events[-1] == (1, 2, 1, raw_path.stat().st_size, True)


def test_cleanup_stale_temp_downloads_reaps_dead_process_roots(tmp_path, monkeypatch):
    loader = _make_loader(tmp_path)
    dead_root = loader._pmxt_temp_download_root / "pid-999999"
    dead_hour = dead_root / "hour-dead"
    dead_hour.mkdir(parents=True, exist_ok=True)
    (dead_hour / "payload.parquet").write_bytes(b"stale")

    monkeypatch.setattr(loader, "_pid_is_active", lambda pid: False)

    loader._cleanup_stale_temp_downloads()

    assert not dead_root.exists()


def test_load_remote_market_batches_downloads_to_temp_file_and_emits_progress(
    tmp_path, monkeypatch
):
    loader = _make_loader(tmp_path)
    hour = pd.Timestamp("2026-03-16T13:00:00Z")

    remote_buffer = BytesIO()
    pq.write_table(
        pa.table(
            {
                "market_id": ["condition-123", "condition-123", "other-condition"],
                "update_type": ["book_snapshot", "price_change", "book_snapshot"],
                "data": [
                    '{"token_id":"token-yes-123","payload":"snapshot"}',
                    '{"token_id":"token-yes-123","payload":"delta"}',
                    '{"token_id":"token-yes-123","payload":"drop-market"}',
                ],
            }
        ),
        remote_buffer,
    )

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self._offset = 0

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

        @property
        def headers(self) -> dict[str, str]:
            return {"Content-Length": str(len(self._payload))}

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                size = len(self._payload) - self._offset
            chunk = self._payload[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

    download_events: list[tuple[int, int | None, bool]] = []
    scan_events: list[tuple[int, int, int, int | None, bool]] = []
    loader._pmxt_download_progress_callback = (
        lambda _source, downloaded_bytes, total_bytes, finished: download_events.append(
            (downloaded_bytes, total_bytes, finished)
        )
    )
    loader._pmxt_scan_progress_callback = (
        lambda _source, scanned_batches, scanned_rows, matched_rows, total_bytes, finished: (
            scan_events.append((scanned_batches, scanned_rows, matched_rows, total_bytes, finished))
        )
    )

    requests: list[tuple[object, float | None]] = []

    def fake_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        requests.append((request, timeout))
        return _Response(remote_buffer.getvalue())

    monkeypatch.setattr(pmxt_module, "urlopen", fake_urlopen)

    batches = loader._load_remote_market_batches(hour, batch_size=1_000)

    assert batches is not None
    assert sum(batch.num_rows for batch in batches) == 2
    assert download_events
    assert download_events[0][0] == 0
    assert download_events[-1][0] == len(remote_buffer.getvalue())
    assert download_events[-1][2] is True
    assert scan_events
    assert scan_events[-1][:3] == (1, 2, 2)
    assert scan_events[-1][3] == len(remote_buffer.getvalue())
    assert scan_events[-1][4] is True
    assert loader._pmxt_temp_download_root.exists()
    assert not any(loader._pmxt_temp_download_root.iterdir())
    request, timeout = requests[0]
    assert dict(request.header_items())["User-agent"] == "prediction-market-backtesting/1.0"
    assert timeout == 30


def test_remote_archive_failure_emits_error_instead_of_silent_gap(tmp_path, monkeypatch) -> None:
    loader = _make_loader(tmp_path)
    hour = pd.Timestamp("2026-03-16T13:00:00Z")

    def fail_download(_url: str, _destination: Path) -> int:
        raise OSError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(loader, "_download_to_file_with_progress", fail_download)

    with capture_loader_events() as capture:
        assert loader._load_remote_market_batches(hour, batch_size=1_000) is None

    event = next(event for event in capture.events if event.stage == "raw_read")
    assert event.level == "ERROR"
    assert event.status == "error"
    assert event.vendor == "pmxt"
    assert event.source_kind == "remote"
    assert event.attrs["error"] == "HTTP Error 403: Forbidden"


def test_load_market_batches_prefers_local_archive_before_remote(tmp_path):
    raw_root = tmp_path / "raw-hours"
    loader = _make_loader(tmp_path / "cache", local_archive_dir=raw_root)
    hour = pd.Timestamp("2026-03-16T13:00:00Z")
    raw_path = raw_root / "polymarket_orderbook_2026-03-16T13.parquet"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "market_id": ["condition-123", "condition-123", "other-condition"],
                "update_type": ["book_snapshot", "price_change", "book_snapshot"],
                "data": [
                    '{"token_id":"token-yes-123","payload":"local-book"}',
                    '{"token_id":"token-yes-123","payload":"local-price"}',
                    '{"token_id":"token-yes-123","payload":"drop-market"}',
                ],
            }
        ),
        raw_path,
    )

    def _fail_remote(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("remote load should not run when local archive exists")

    loader._load_remote_market_batches = _fail_remote  # type: ignore[method-assign]

    batches = loader._load_market_batches(hour, batch_size=1_000)

    assert batches is not None
    assert [batch.to_pylist() for batch in batches] == [
        [
            {
                "update_type": "book_snapshot",
                "data": '{"token_id":"token-yes-123","payload":"local-book"}',
            },
            {
                "update_type": "price_change",
                "data": '{"token_id":"token-yes-123","payload":"local-price"}',
            },
        ]
    ]


def test_load_market_batches_reads_nested_local_archive_layout(tmp_path):
    raw_root = tmp_path / "raw-hours"
    loader = _make_loader(tmp_path / "cache", local_archive_dir=raw_root)
    hour = pd.Timestamp("2026-03-17T05:00:00Z")
    raw_path = raw_root / "2026/03/17/polymarket_orderbook_2026-03-17T05.parquet"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "market_id": ["condition-123"],
                "update_type": ["book_snapshot"],
                "data": ['{"token_id":"token-yes-123","payload":"nested-local"}'],
            }
        ),
        raw_path,
    )

    batches = loader._load_market_batches(hour, batch_size=1_000)

    assert batches is not None
    assert batches[0].column("data")[0].as_py() == (
        '{"token_id":"token-yes-123","payload":"nested-local"}'
    )


def test_timestamp_to_ns_preserves_decimal_precision() -> None:
    assert PolymarketPMXTDataLoader._timestamp_to_ns(1771767624.001295) == 1_771_767_624_001_295_000


def test_iter_market_tables_preserves_hour_order(tmp_path):
    loader = _make_loader(tmp_path)
    hours = [
        pd.Timestamp("2026-03-16T12:00:00Z"),
        pd.Timestamp("2026-03-16T13:00:00Z"),
        pd.Timestamp("2026-03-16T14:00:00Z"),
    ]
    delays = {hours[0]: 0.05, hours[1]: 0.0, hours[2]: 0.01}

    def _load(hour, *, batch_size):  # type: ignore[no-untyped-def]
        time.sleep(delays[hour])
        return pa.table({"update_type": ["book_snapshot"], "data": [hour.isoformat()]})

    loader._load_market_table = _load  # type: ignore[method-assign]

    yielded = list(loader._iter_market_tables(hours, batch_size=1_000))

    assert [hour for hour, _ in yielded] == hours
    assert [table.to_pylist()[0]["data"] for _, table in yielded] == [
        hour.isoformat() for hour in hours
    ]


def test_event_sort_key_orders_book_updates_by_event_then_init(monkeypatch):
    class _FakeOrderBookDeltas:
        def __init__(self, ts_event: int, ts_init: int) -> None:
            self.ts_event = ts_event
            self.ts_init = ts_init

    monkeypatch.setattr(pmxt_module, "OrderBookDeltas", _FakeOrderBookDeltas)

    early = _FakeOrderBookDeltas(ts_event=10, ts_init=11)
    late = _FakeOrderBookDeltas(ts_event=10, ts_init=20)

    ordered = sorted([late, early], key=PolymarketPMXTDataLoader._event_sort_key)

    assert ordered == [early, late]


def test_load_order_book_deltas_returns_snapshot_event(monkeypatch, tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = SimpleNamespace(id="POLYMARKET.TEST")
    hour = pd.Timestamp("2026-03-16T12:00:00Z")

    class _FakeOrderBookDeltas:
        def __init__(self, ts_event: int, ts_init: int) -> None:
            self.ts_event = ts_event
            self.ts_init = ts_init

    loader._archive_hours = lambda _start, _end: [hour]  # type: ignore[method-assign]
    loader._iter_market_batches = (  # type: ignore[method-assign]
        lambda hours, *, batch_size: iter(
            [
                (
                    hour,
                    [
                        pa.record_batch(
                            [
                                pa.array(["book_snapshot"]),
                                pa.array(
                                    [
                                        (
                                            '{"update_type":"book_snapshot",'
                                            '"market_id":"condition-123",'
                                            '"token_id":"token-yes-123",'
                                            '"side":"YES","best_bid":"0.49",'
                                            '"best_ask":"0.51","timestamp":1.0,'
                                            '"bids":[["0.49","10"]],'
                                            '"asks":[["0.51","10"]]}'
                                        )
                                    ]
                                ),
                            ],
                            names=["update_type", "data"],
                        )
                    ],
                )
            ]
        )
    )

    monkeypatch.setattr(
        pmxt_module,
        "pmxt_payload_delta_rows",
        lambda **_kwargs: (
            True,
            (1_000_000_000, 0),
            {
                "event_index": [0],
                "action": [4],
                "side": [0],
                "price": [0.0],
                "size": [0.0],
                "flags": [0],
                "sequence": [0],
                "ts_event": [10],
                "ts_init": [20],
            },
        ),
    )
    monkeypatch.setattr(
        loader,
        "_deltas_records_from_columns",
        lambda data: [
            _FakeOrderBookDeltas(ts_event=data["ts_event"][0], ts_init=data["ts_init"][0])
        ],
    )

    data = loader.load_order_book_deltas(hour, hour + pd.Timedelta(hours=1))

    assert [type(record).__name__ for record in data] == ["_FakeOrderBookDeltas"]


def test_load_order_book_deltas_delegates_payload_ordering_to_native(monkeypatch, tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = SimpleNamespace(id="POLYMARKET.TEST")
    hour = pd.Timestamp("2026-03-16T12:00:00Z")

    loader._archive_hours = lambda _start, _end: [hour]  # type: ignore[method-assign]
    loader._iter_market_batches = (  # type: ignore[method-assign]
        lambda hours, *, batch_size: iter(
            [
                (
                    hour,
                    [
                        pa.record_batch(
                            [
                                pa.array(["price_change", "book_snapshot"]),
                                pa.array(
                                    [
                                        (
                                            '{"update_type":"price_change",'
                                            '"market_id":"condition-123",'
                                            '"token_id":"token-yes-123",'
                                            '"side":"YES","best_bid":"0.50",'
                                            '"best_ask":"0.52","timestamp":2.0,'
                                            '"change_price":"0.52",'
                                            '"change_size":"5","change_side":"SELL"}'
                                        ),
                                        (
                                            '{"update_type":"book_snapshot",'
                                            '"market_id":"condition-123",'
                                            '"token_id":"token-yes-123",'
                                            '"side":"YES","best_bid":"0.49",'
                                            '"best_ask":"0.51","timestamp":1.0,'
                                            '"bids":[["0.49","10"]],'
                                            '"asks":[["0.51","10"]]}'
                                        ),
                                    ]
                                ),
                            ],
                            names=["update_type", "data"],
                        )
                    ],
                )
            ]
        )
    )
    native_calls: list[dict[str, object]] = []

    def _native_payload_delta_rows(**kwargs):  # type: ignore[no-untyped-def]
        native_calls.append(kwargs)
        return (
            True,
            (2_000_000_000, 1),
            {
                "event_index": [],
                "action": [],
                "side": [],
                "price": [],
                "size": [],
                "flags": [],
                "sequence": [],
                "ts_event": [],
                "ts_init": [],
            },
        )

    monkeypatch.setattr(pmxt_module, "pmxt_payload_delta_rows", _native_payload_delta_rows)

    loader.load_order_book_deltas(hour, hour + pd.Timedelta(hours=1))

    assert native_calls
    assert [str(value) for value in native_calls[0]["update_type_columns"][0]] == [
        "price_change",
        "book_snapshot",
    ]


def test_load_order_book_deltas_uses_native_payload_delta_rows(monkeypatch, tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = SimpleNamespace(id="POLYMARKET.TEST")
    hour = pd.Timestamp("2026-03-16T12:00:00Z")

    class _FakeOrderBookDeltas:
        def __init__(self, ts_event, ts_init):  # type: ignore[no-untyped-def]
            self.ts_event = ts_event
            self.ts_init = ts_init

    loader._archive_hours = lambda _start, _end: [hour]  # type: ignore[method-assign]
    loader._iter_market_batches = (  # type: ignore[method-assign]
        lambda hours, *, batch_size: iter(
            [
                (
                    hour,
                    [
                        pa.record_batch(
                            [
                                pa.array(["book_snapshot"]),
                                pa.array(
                                    [
                                        (
                                            '{"update_type":"book_snapshot",'
                                            '"market_id":"condition-123",'
                                            '"token_id":"token-yes-123",'
                                            '"timestamp":1.0,'
                                            '"bids":[["0.49","10"]],'
                                            '"asks":[["0.51","10"]]}'
                                        )
                                    ]
                                ),
                            ],
                            names=["update_type", "data"],
                        )
                    ],
                )
            ]
        )
    )
    native_calls: list[dict[str, object]] = []

    def _native_payload_delta_rows(**kwargs):  # type: ignore[no-untyped-def]
        native_calls.append(kwargs)
        return (
            True,
            (1_000_000_000, 0),
            {
                "event_index": [0],
                "action": [4],
                "side": [0],
                "price": [0.0],
                "size": [0.0],
                "flags": [0],
                "sequence": [0],
                "ts_event": [1_000_000_000],
                "ts_init": [1_000_000_000],
            },
        )

    monkeypatch.setattr(pmxt_module, "pmxt_payload_delta_rows", _native_payload_delta_rows)
    monkeypatch.setattr(
        loader,
        "_deltas_records_from_columns",
        lambda data: [
            _FakeOrderBookDeltas(ts_event=data["ts_event"][0], ts_init=data["ts_init"][0])
        ],
    )

    data = loader.load_order_book_deltas(hour, hour + pd.Timedelta(hours=1))

    assert native_calls
    assert native_calls[0]["token_id"] == "token-yes-123"
    assert [type(record).__name__ for record in data] == ["_FakeOrderBookDeltas"]


def test_load_order_book_deltas_skips_stale_cross_hour_payloads(monkeypatch, tmp_path):
    loader = _make_loader(tmp_path)
    loader._instrument = SimpleNamespace(id="POLYMARKET.TEST")
    hours = [
        pd.Timestamp("2026-03-16T12:00:00Z"),
        pd.Timestamp("2026-03-16T13:00:00Z"),
    ]
    loader._archive_hours = lambda _start, _end: hours  # type: ignore[method-assign]
    loader._iter_market_batches = (  # type: ignore[method-assign]
        lambda iter_hours, *, batch_size: iter(
            [
                (
                    hours[0],
                    [
                        pa.record_batch(
                            [
                                pa.array(["book_snapshot", "price_change"]),
                                pa.array(
                                    [
                                        (
                                            '{"update_type":"book_snapshot","market_id":"condition-123",'
                                            '"token_id":"token-yes-123","side":"YES","best_bid":"0.49",'
                                            '"best_ask":"0.51","timestamp":1.0,"bids":[["0.49","10"]],'
                                            '"asks":[["0.51","10"]]}'
                                        ),
                                        (
                                            '{"update_type":"price_change","market_id":"condition-123",'
                                            '"token_id":"token-yes-123","side":"YES","best_bid":"0.50",'
                                            '"best_ask":"0.52","timestamp":2.0,"change_price":"0.52",'
                                            '"change_size":"5","change_side":"SELL"}'
                                        ),
                                    ]
                                ),
                            ],
                            names=["update_type", "data"],
                        )
                    ],
                ),
                (
                    hours[1],
                    [
                        pa.record_batch(
                            [
                                pa.array(["book_snapshot"]),
                                pa.array(
                                    [
                                        (
                                            '{"update_type":"book_snapshot","market_id":"condition-123",'
                                            '"token_id":"token-yes-123","side":"YES","best_bid":"0.48",'
                                            '"best_ask":"0.50","timestamp":1.5,"bids":[["0.48","10"]],'
                                            '"asks":[["0.50","10"]]}'
                                        )
                                    ]
                                ),
                            ],
                            names=["update_type", "data"],
                        )
                    ],
                ),
            ]
        )
    )
    native_calls: list[dict[str, object]] = []

    def _native_payload_delta_rows(**kwargs):  # type: ignore[no-untyped-def]
        native_calls.append(kwargs)
        call_index = len(native_calls)
        return (
            True,
            (call_index * 1_000_000_000, call_index - 1),
            {
                "event_index": [],
                "action": [],
                "side": [],
                "price": [],
                "size": [],
                "flags": [],
                "sequence": [],
                "ts_event": [],
                "ts_init": [],
            },
        )

    monkeypatch.setattr(pmxt_module, "pmxt_payload_delta_rows", _native_payload_delta_rows)

    loader.load_order_book_deltas(hours[0], hours[-1] + pd.Timedelta(hours=1))

    assert len(native_calls) == 2
    assert native_calls[0]["last_payload_key"] is None
    assert native_calls[1]["last_payload_key"] == (1_000_000_000, 0)


def test_iter_market_batches_preserves_hour_order(tmp_path):
    loader = _make_loader(tmp_path)
    hours = [
        pd.Timestamp("2026-03-16T12:00:00Z"),
        pd.Timestamp("2026-03-16T13:00:00Z"),
        pd.Timestamp("2026-03-16T14:00:00Z"),
    ]
    delays = {hours[0]: 0.05, hours[1]: 0.0, hours[2]: 0.01}

    def _load(hour, *, batch_size):  # type: ignore[no-untyped-def]
        time.sleep(delays[hour])
        return [
            pa.record_batch(
                [pa.array(["book_snapshot"]), pa.array([hour.isoformat()])],
                names=["update_type", "data"],
            )
        ]

    loader._load_market_batches = _load  # type: ignore[method-assign]

    yielded = list(loader._iter_market_batches(hours, batch_size=1_000))

    assert [hour for hour, _ in yielded] == hours
    assert [batches[0].column("data")[0].as_py() for _, batches in yielded] == [
        hour.isoformat() for hour in hours
    ]
