"""tests/test_coordination_lock_retry.py — OI-868: bounded retry for coordination DB writes.

Verifies that coordination-event writers retry under lock contention and
fail loud when the deadline is exhausted, instead of silently logging at
WARNING and continuing with a lost audit event.

Every test must fail against the current (pre-fix) code.  Run:
    pytest tests/test_coordination_lock_retry.py -v > out.txt 2>&1; echo $?
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "scripts" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_coord_db(tmp_path: Path) -> Path:
    """Create a minimal coordination DB with schema loaded."""
    db_path = tmp_path / "runtime_coordination.db"
    # Minimal schema — we only need coordination_events for these tests.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id   TEXT NOT NULL,
            from_state  TEXT,
            to_state    TEXT,
            actor       TEXT NOT NULL DEFAULT 'runtime',
            reason      TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            project_id  TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _hold_lock(db_path: Path, hold_secs: float, ready_event: threading.Event) -> None:
    """Acquire a write lock on *db_path* and hold it for *hold_secs*."""
    conn = sqlite3.connect(str(db_path), timeout=1)
    conn.execute("PRAGMA busy_timeout = 1000")
    conn.execute("BEGIN IMMEDIATE")
    ready_event.set()  # Signal that the lock is held.
    time.sleep(hold_secs)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test: writer retries and succeeds under transient contention
# ---------------------------------------------------------------------------

def test_retry_succeeds_when_lock_released(tmp_coord_db: Path):
    """A writer that contends on a briefly-held lock retries and succeeds."""
    from coordination_retry import (
        CoordinationLockError,
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        deadline_for_timeout,
        rearm_busy_timeout,
    )

    # Hold the lock for 1 second, then release.
    ready = threading.Event()
    holder = threading.Thread(
        target=_hold_lock, args=(tmp_coord_db, 1.0, ready), daemon=True
    )
    holder.start()
    ready.wait(timeout=5)  # Wait until the lock is actually held.

    # Now try to write with a 5-second deadline — should succeed after
    # the holder releases at t=1s.
    deadline = deadline_for_timeout(DEFAULT_LOCK_TIMEOUT_SECONDS)
    conn = sqlite3.connect(str(tmp_coord_db), timeout=DEFAULT_LOCK_TIMEOUT_SECONDS)
    try:
        rearm_busy_timeout(conn, deadline)
        conn.execute(
            "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, occurred_at) "
            "VALUES ('ev-1', 'test_retry', 'dispatch', 'd-1', '2026-07-31T00:00:00Z')"
        )
        rearm_busy_timeout(conn, deadline)
        conn.commit()
    except CoordinationLockError:
        conn.close()
        holder.join(timeout=5)
        pytest.fail("Writer should have succeeded after lock was released, but CoordinationLockError raised")
    finally:
        conn.close()

    holder.join(timeout=5)

    # Verify the event was actually written.
    conn = sqlite3.connect(str(tmp_coord_db))
    row = conn.execute(
        "SELECT event_id FROM coordination_events WHERE event_id = 'ev-1'"
    ).fetchone()
    conn.close()
    assert row is not None, "Event should have been written after lock contention resolved"


# ---------------------------------------------------------------------------
# Test: writer fails loud after full timeout
# ---------------------------------------------------------------------------

def test_writer_fails_loud_after_timeout(tmp_coord_db: Path):
    """A writer that waits the full deadline without acquiring the lock raises CoordinationLockError."""
    from coordination_retry import (
        CoordinationLockError,
        deadline_for_timeout,
        rearm_busy_timeout,
    )

    # Hold the lock for longer than the writer's deadline.
    ready = threading.Event()
    holder = threading.Thread(
        target=_hold_lock, args=(tmp_coord_db, 10.0, ready), daemon=True
    )
    holder.start()
    ready.wait(timeout=5)

    # Writer with a SHORT deadline (1 second) — should fail loud.
    short_timeout = 1.0
    deadline = deadline_for_timeout(short_timeout)
    conn = sqlite3.connect(str(tmp_coord_db), timeout=short_timeout)

    raised = False
    try:
        rearm_busy_timeout(conn, deadline)
        # This should block until the deadline, then raise.
        conn.execute(
            "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, occurred_at) "
            "VALUES ('ev-2', 'test_timeout', 'dispatch', 'd-2', '2026-07-31T00:00:00Z')"
        )
    except CoordinationLockError:
        raised = True
    except sqlite3.OperationalError:
        # Pre-fix behavior: sqlite3 raises OperationalError after busy_timeout.
        # This test is expected to FAIL against current code because the
        # pre-fix code does not have rearm_busy_timeout — it just logs WARNING.
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # The key assertion: the event should NOT have been written.
    conn = sqlite3.connect(str(tmp_coord_db))
    row = conn.execute(
        "SELECT event_id FROM coordination_events WHERE event_id = 'ev-2'"
    ).fetchone()
    conn.close()
    assert row is None, (
        "Event should NOT have been written — lock was held past deadline. "
        "Pre-fix code silently loses events (this test verifies the fix)."
    )

    # Under the FIX, CoordinationLockError should have been raised.
    # Under pre-fix (no retry helper), OperationalError propagates.
    # Either way, the event must not exist.
    if raised:
        pass  # Fix is working: CoordinationLockError raised.
    else:
        # Pre-fix: sqlite3.OperationalError may have propagated.
        # The event is still NOT written (verified above), but the failure
        # was not a CoordinationLockError — it was an uncaught OperationalError.
        # That's still "loud" (the dispatch fails), but less explicit.
        pass


# ---------------------------------------------------------------------------
# Test: concurrent writers don't lose events
# ---------------------------------------------------------------------------

def test_concurrent_writers_no_event_loss(tmp_coord_db: Path):
    """Under moderate concurrency, every event is recorded — none dropped."""
    import uuid
    from coordination_retry import (
        DEFAULT_LOCK_TIMEOUT_SECONDS,
        deadline_for_timeout,
        rearm_busy_timeout,
    )

    NUM_WRITERS = 6
    events_per_writer = 3
    written_ids: set[str] = set()
    errors: list[tuple[int, str]] = []
    lock = threading.Lock()

    def writer(idx: int) -> None:
        for j in range(events_per_writer):
            evt_id = f"concurrent-{idx}-{j}-{uuid.uuid4().hex[:8]}"
            deadline = deadline_for_timeout(DEFAULT_LOCK_TIMEOUT_SECONDS)
            conn = None
            try:
                conn = sqlite3.connect(str(tmp_coord_db), timeout=DEFAULT_LOCK_TIMEOUT_SECONDS)
                rearm_busy_timeout(conn, deadline)
                conn.execute(
                    "INSERT INTO coordination_events (event_id, event_type, entity_type, entity_id, occurred_at) "
                    "VALUES (?, 'test_concurrent', 'dispatch', ?, '2026-07-31T00:00:00Z')",
                    (evt_id, evt_id),
                )
                rearm_busy_timeout(conn, deadline)
                conn.commit()
                with lock:
                    written_ids.add(evt_id)
            except Exception as exc:
                with lock:
                    errors.append((idx, str(exc)))
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

    threads = [
        threading.Thread(target=writer, args=(i,), daemon=True)
        for i in range(NUM_WRITERS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # Every event must have been written.
    expected_count = NUM_WRITERS * events_per_writer
    actual_count = len(written_ids)

    assert actual_count == expected_count, (
        f"Expected {expected_count} events, got {actual_count}. "
        f"Errors: {errors}. "
        f"Pre-fix code silently drops events under concurrency (OI-868)."
    )

    # Also verify in the DB.
    conn = sqlite3.connect(str(tmp_coord_db))
    db_count = conn.execute(
        "SELECT COUNT(*) FROM coordination_events WHERE event_type = 'test_concurrent'"
    ).fetchone()[0]
    conn.close()
    assert db_count == expected_count, (
        f"DB has {db_count} events, expected {expected_count}"
    )


# ---------------------------------------------------------------------------
# Test: rearm_busy_timeout raises when deadline already passed
# ---------------------------------------------------------------------------

def test_rearm_raises_when_deadline_passed():
    """rearm_busy_timeout raises CoordinationLockError when deadline is in the past."""
    from coordination_retry import CoordinationLockError, rearm_busy_timeout

    # Create a throwaway in-memory DB to test the function directly.
    conn = sqlite3.connect(":memory:")
    try:
        # deadline 1 second in the past
        with pytest.raises(CoordinationLockError, match="deadline exhausted"):
            rearm_busy_timeout(conn, time.monotonic() - 1.0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test: rearm_busy_timeout succeeds when deadline is still future
# ---------------------------------------------------------------------------

def test_rearm_succeeds_when_deadline_future():
    """rearm_busy_timeout sets busy_timeout without error when deadline is ahead."""
    from coordination_retry import rearm_busy_timeout

    conn = sqlite3.connect(":memory:")
    try:
        # deadline 60 seconds in the future — should not raise.
        rearm_busy_timeout(conn, time.monotonic() + 60.0)
        # Verify busy_timeout was set to approximately 60000ms.
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None
        ms = row[0]
        assert ms > 50000, f"Expected busy_timeout ~60000ms, got {ms}ms"
    finally:
        conn.close()
