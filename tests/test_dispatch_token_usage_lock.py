"""Tests for thread-safe _dispatch_token_usage accessors in delivery.py.

Covers: single-thread roundtrip, concurrent writers (no dropped writes),
concurrent reader+writer (no KeyError / data corruption), and cleanup.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from subprocess_dispatch_internals.delivery import (
    _dispatch_token_usage,
    clear_token_usage,
    get_token_usage,
    set_token_usage,
)


def _reset():
    """Clear the global dict between tests without bypassing the lock."""
    keys = list(_dispatch_token_usage.keys())
    for k in keys:
        clear_token_usage(k)


def test_single_thread_roundtrip():
    _reset()
    set_token_usage("d-001", {"input_tokens": 100, "output_tokens": 50})
    result = get_token_usage("d-001")
    assert result == {"input_tokens": 100, "output_tokens": 50}
    clear_token_usage("d-001")
    assert get_token_usage("d-001") is None


def test_missing_key_returns_none():
    _reset()
    assert get_token_usage("nonexistent") is None


def test_clear_missing_key_returns_none():
    _reset()
    assert clear_token_usage("nonexistent") is None


def test_clear_returns_value():
    _reset()
    set_token_usage("d-clear", {"input_tokens": 10})
    val = clear_token_usage("d-clear")
    assert val == {"input_tokens": 10}
    assert get_token_usage("d-clear") is None


def test_concurrent_writers_no_dropped_writes():
    """20 threads each write a different dispatch_id — all writes must survive."""
    _reset()
    n = 20
    barrier = threading.Barrier(n)
    errors: list[str] = []

    def writer(i: int):
        barrier.wait()
        dispatch_id = f"d-concurrent-{i}"
        set_token_usage(dispatch_id, {"thread": i})
        val = get_token_usage(dispatch_id)
        if val != {"thread": i}:
            errors.append(f"thread {i}: expected {{'thread': {i}}}, got {val}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Dropped writes detected:\n" + "\n".join(errors)

    written = [get_token_usage(f"d-concurrent-{i}") for i in range(n)]
    assert all(w is not None for w in written), "Some writes were lost"
    _reset()


def test_concurrent_reader_writer_no_error():
    """Concurrent reads and writes on the same dispatch_id — no exception raised."""
    _reset()
    dispatch_id = "d-rw"
    set_token_usage(dispatch_id, {"v": 0})
    stop = threading.Event()
    errors: list[str] = []

    def writer():
        for i in range(200):
            if stop.is_set():
                break
            set_token_usage(dispatch_id, {"v": i})

    def reader():
        for _ in range(200):
            if stop.is_set():
                break
            try:
                get_token_usage(dispatch_id)
            except Exception as exc:
                errors.append(str(exc))

    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    wt.start()
    rt.start()
    wt.join()
    rt.join()
    stop.set()

    assert not errors, f"Exceptions during concurrent read/write:\n" + "\n".join(errors)
    _reset()
