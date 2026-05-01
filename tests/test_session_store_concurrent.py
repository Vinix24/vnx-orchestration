#!/usr/bin/env python3
"""Concurrent-write regression tests for SessionStore (OI-1121 / W4A).

Verifies that fcntl.flock around read-modify-write in save() and clear()
prevents lost updates under concurrent access.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

TEST_DIR = Path(__file__).resolve().parent
SCRIPTS_LIB_DIR = TEST_DIR.parent / "scripts" / "lib"
if str(SCRIPTS_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB_DIR))

from session_store import SessionStore  # noqa: E402


# ---------------------------------------------------------------------------
# Single-threaded baseline (regression guard)
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(state_dir=tmp_path)
    store.save("T1", "session-abc", dispatch_id="d-001")
    assert store.load("T1") == "session-abc"


def test_save_overwrites_prior_entry(tmp_path: Path) -> None:
    store = SessionStore(state_dir=tmp_path)
    store.save("T1", "first-session")
    store.save("T1", "second-session")
    assert store.load("T1") == "second-session"


def test_clear_removes_entry(tmp_path: Path) -> None:
    store = SessionStore(state_dir=tmp_path)
    store.save("T1", "session-xyz")
    store.clear("T1")
    assert store.load("T1") is None


def test_clear_nonexistent_is_noop(tmp_path: Path) -> None:
    store = SessionStore(state_dir=tmp_path)
    store.clear("T99")  # must not raise


def test_load_missing_file_returns_none(tmp_path: Path) -> None:
    store = SessionStore(state_dir=tmp_path)
    assert store.load("T1") is None


def test_save_empty_session_id_is_noop(tmp_path: Path) -> None:
    store = SessionStore(state_dir=tmp_path)
    store.save("T1", "")
    assert store.load("T1") is None


def test_all_sessions_returns_all(tmp_path: Path) -> None:
    store = SessionStore(state_dir=tmp_path)
    store.save("T1", "s1")
    store.save("T2", "s2")
    sessions = store.all_sessions()
    assert sessions == {"T1": "s1", "T2": "s2"}


def test_save_creates_file_on_first_write(tmp_path: Path) -> None:
    store = SessionStore(state_dir=tmp_path)
    from session_store import SESSIONS_FILENAME
    sessions_file = tmp_path / SESSIONS_FILENAME
    assert not sessions_file.exists()
    store.save("T1", "session-new")
    assert sessions_file.exists()


# ---------------------------------------------------------------------------
# Concurrent-write tests (OI-1121)
# ---------------------------------------------------------------------------


def test_concurrent_saves_no_lost_updates(tmp_path: Path) -> None:
    """20 threads each save to a unique terminal_id; all entries must survive.

    Without the flock, one thread's atomic rename clobbers another's update
    because both read the same initial state and the last rename wins for all
    keys, not just the writer's key.
    """
    n_threads = 20
    results: list[tuple[str, str]] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(terminal_id: str, session_id: str) -> None:
        try:
            store = SessionStore(state_dir=tmp_path)
            store.save(terminal_id, session_id, dispatch_id="concurrent-test")
            with lock:
                results.append((terminal_id, session_id))
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"T{i}", f"session-{i}"))
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker exceptions: {errors}"
    assert len(results) == n_threads

    store = SessionStore(state_dir=tmp_path)
    for terminal_id, session_id in results:
        loaded = store.load(terminal_id)
        assert loaded == session_id, (
            f"lost update: {terminal_id} expected={session_id!r} got={loaded!r}"
        )


def test_concurrent_saves_same_key_no_corruption(tmp_path: Path) -> None:
    """Multiple threads writing to the same terminal_id must not corrupt the file.

    Last writer wins for the specific key, but all other terminals' data must
    remain intact, and the file must remain valid JSON throughout.
    """
    n_writers = 10
    n_other = 5
    errors: list[Exception] = []
    lock = threading.Lock()

    # Pre-populate some sibling entries that must survive.
    store = SessionStore(state_dir=tmp_path)
    for i in range(n_other):
        store.save(f"Stable{i}", f"stable-session-{i}")

    def writer(session_id: str) -> None:
        try:
            SessionStore(state_dir=tmp_path).save("T1", session_id, dispatch_id="race-test")
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(f"race-session-{i}",))
        for i in range(n_writers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker exceptions: {errors}"

    store = SessionStore(state_dir=tmp_path)

    # The file must be valid — T1 has some session (whichever writer won).
    t1_session = store.load("T1")
    assert t1_session is not None
    assert t1_session.startswith("race-session-")

    # Sibling entries must be intact — no clobbering.
    for i in range(n_other):
        loaded = store.load(f"Stable{i}")
        assert loaded == f"stable-session-{i}", (
            f"sibling lost: Stable{i} got {loaded!r}"
        )


def test_concurrent_saves_and_clears_no_corruption(tmp_path: Path) -> None:
    """Mixed concurrent save() and clear() calls must not corrupt the JSON store."""
    errors: list[Exception] = []
    lock = threading.Lock()

    def saver(terminal_id: str, session_id: str) -> None:
        try:
            SessionStore(state_dir=tmp_path).save(terminal_id, session_id)
        except Exception as exc:
            with lock:
                errors.append(exc)

    def clearer(terminal_id: str) -> None:
        try:
            SessionStore(state_dir=tmp_path).clear(terminal_id)
        except Exception as exc:
            with lock:
                errors.append(exc)

    # Pre-seed so clear() has something to remove.
    for i in range(5):
        SessionStore(state_dir=tmp_path).save(f"T{i}", f"pre-session-{i}")

    threads = (
        [threading.Thread(target=saver, args=(f"T{i}", f"new-session-{i}")) for i in range(10)]
        + [threading.Thread(target=clearer, args=(f"T{i % 5}",)) for i in range(10)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker exceptions: {errors}"

    # File must still be readable and return a valid dict.
    store = SessionStore(state_dir=tmp_path)
    sessions = store.all_sessions()
    assert isinstance(sessions, dict)
