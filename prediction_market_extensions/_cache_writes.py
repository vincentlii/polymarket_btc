from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _CacheReplaceLockState:
    lock: threading.Lock
    users: int = 0


_CACHE_REPLACE_LOCKS_LOCK = threading.Lock()
_CACHE_REPLACE_LOCKS: dict[str, _CacheReplaceLockState] = {}


@contextmanager
def cache_replace_slot(path: Path) -> Iterator[None]:
    """Serialize in-process replacements of one cache path.

    Windows can reject simultaneous ``os.replace`` calls for the same target.
    The reference count keeps a lock alive while another writer is waiting,
    without retaining completed cache paths indefinitely.
    """
    key = os.path.normcase(os.path.abspath(path))
    with _CACHE_REPLACE_LOCKS_LOCK:
        state = _CACHE_REPLACE_LOCKS.get(key)
        if state is None:
            state = _CacheReplaceLockState(lock=threading.Lock())
            _CACHE_REPLACE_LOCKS[key] = state
        state.users += 1

    state.lock.acquire()
    try:
        yield
    finally:
        state.lock.release()
        with _CACHE_REPLACE_LOCKS_LOCK:
            state.users -= 1
            if state.users == 0 and _CACHE_REPLACE_LOCKS.get(key) is state:
                del _CACHE_REPLACE_LOCKS[key]
