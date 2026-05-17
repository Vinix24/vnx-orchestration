"""Tests for thread-safe _dispatch_token_usage accessors in delivery.py.

Covers: single-thread roundtrip, concurrent writers (no dropped writes),
concurrent reader+writer (no KeyError / data corruption), concurrent
set+clear (no exception), and clear() return-value contract.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lib"))

from subprocess_dispatch_internals.delivery import (
    _dispatch_token_usage,
    _dispatch_token_usage_lock,
    clear_token_usage,
    get_token_usage,
    set_token_usage,
)


def _clean(*dispatch_ids: str) -> None:
    """Remove test entries from the shared dict after each test."""
    with _dispatch_token_usage_lock:
        for did in dispatch_ids:
            _dispatch_token_usage.pop(did, None)


# ---------------------------------------------------------------------------
# Single-thread roundtrip
# ---------------------------------------------------------------------------


def test_set_get_roundtrip() -> None:
    did = "test-roundtrip-1"
    usage = {"input_tokens": 100, "output_tokens": 50}
    try:
        set_token_usage(did, usage)
        result = get_token_usage(did)
        assert result == usage
    finally:
        _clean(did)


def test_get_missing_returns_none() -> None:
    assert get_token_usage("test-nonexistent-dispatch-xyz") is None


def test_clear_removes_entry() -> None:
    did = "test-clear-1"
    set_token_usage(did, {"input_tokens": 10})
    clear_token_usage(did)
    assert get_token_usage(did) is None


def test_clear_missing_is_noop() -> None:
    # Must not raise and must return None
    assert clear_token_usage("test-clear-missing-xyz") is None


def test_clear_returns_removed_value() -> None:
    did = "test-clear-return-1"
    set_token_usage(did, {"input_tokens": 42, "output_tokens": 7})
    val = clear_token_usage(did)
    assert val == {"input_tokens": 42, "output_tokens": 7}
    assert get_token_usage(did) is None


# ---------------------------------------------------------------------------
# Multi-thread concurrent writers
# ---------------------------------------------------------------------------


def test_concurrent_writers_no_dropped_writes() -> None:
    """20 threads each write a different dispatch_id; all entries must persist."""
    n = 20
    dispatch_ids = [f"test-concurrent-{i}" for i in range(n)]
    errors: list[Exception] = []

    def writer(did: str) -> None:
        try:
            set_token_usage(did, {"input_tokens": int(did.split("-")[-1])})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(did,)) for did in dispatch_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert not errors, f"Writer threads raised: {errors}"
        for i, did in enumerate(dispatch_ids):
            result = get_token_usage(did)
            assert result is not None, f"Entry missing for {did}"
            assert result["input_tokens"] == i
    finally:
        _clean(*dispatch_ids)


# ---------------------------------------------------------------------------
# Concurrent reader + writer — no race-induced KeyError
# ---------------------------------------------------------------------------


def test_concurrent_reader_writer_no_key_error() -> None:
    """A writer and reader racing on the same dispatch_id must not raise."""
    did = "test-race-rw-1"
    errors: list[Exception] = []
    iterations = 500

    def writer() -> None:
        for _ in range(iterations):
            try:
                set_token_usage(did, {"input_tokens": 1})
            except Exception as exc:
                errors.append(exc)

    def reader() -> None:
        for _ in range(iterations):
            try:
                get_token_usage(did)  # may return None or dict — both are valid
            except Exception as exc:
                errors.append(exc)

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    w.start()
    r.start()
    w.join()
    r.join()

    _clean(did)
    assert not errors, f"Threads raised: {errors}"


# ---------------------------------------------------------------------------
# Clear (cleanup) thread-safety
# ---------------------------------------------------------------------------


def test_concurrent_set_clear_no_error() -> None:
    """set_token_usage and clear_token_usage racing must not raise."""
    did = "test-race-set-clear-1"
    errors: list[Exception] = []
    iterations = 500

    def setter() -> None:
        for _ in range(iterations):
            try:
                set_token_usage(did, {"input_tokens": 1})
            except Exception as exc:
                errors.append(exc)

    def clearer() -> None:
        for _ in range(iterations):
            try:
                clear_token_usage(did)
            except Exception as exc:
                errors.append(exc)

    s = threading.Thread(target=setter)
    c = threading.Thread(target=clearer)
    s.start()
    c.start()
    s.join()
    c.join()

    _clean(did)
    assert not errors, f"Threads raised: {errors}"
