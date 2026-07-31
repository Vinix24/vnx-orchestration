"""tests/test_oi880_lock_error_classification.py — OI-880: lock-busy errors from the write path.

The lock-retry helper (coordination_retry.rearm_busy_timeout) only raises
CoordinationLockError when the deadline is already exhausted at call time.  The
write path can instead hit SQLite's OWN ``sqlite3.OperationalError: database is
locked`` when a statement exhausts its busy_timeout on a held lock.  That error
was swallowed by the call sites' generic ``except Exception`` / ``except
sqlite3.Error`` at WARNING level — a silently lost audit event.

These tests drive the actual write path under contention (a second connection
holding the write lock) and assert the lock-busy error surfaces as
CoordinationLockError instead of vanishing.  They fail against the pre-fix code
(which swallows) and pass once the classification is in place.

Run against a temporary DB only — never the live central store.
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

import coordination_retry  # noqa: E402  (sys.path bootstrap above)
from coordination_retry import CoordinationLockError  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def short_lock_timeout(monkeypatch):
    """Shrink the 5s default lock deadline to 1s so contention tests stay fast.

    The call sites import DEFAULT_LOCK_TIMEOUT_SECONDS from coordination_retry
    at call time, so patching the module attribute is sufficient.
    """
    monkeypatch.setattr(coordination_retry, "DEFAULT_LOCK_TIMEOUT_SECONDS", 1.0)
    yield


def _init_coordination_events(db_path: Path) -> None:
    """Minimal coordination_events schema (no project_id → legacy insert path)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
            occurred_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def _init_intelligence_injections(db_path: Path) -> None:
    """Minimal intelligence_injections schema (no project_id / ab_arm columns)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS intelligence_injections (
            injection_id     TEXT PRIMARY KEY,
            dispatch_id      TEXT NOT NULL,
            injection_point  TEXT NOT NULL,
            task_class       TEXT,
            items_injected   INTEGER NOT NULL DEFAULT 0,
            items_suppressed INTEGER NOT NULL DEFAULT 0,
            payload_chars    INTEGER NOT NULL DEFAULT 0,
            items_json       TEXT NOT NULL DEFAULT '{}',
            suppressed_json  TEXT NOT NULL DEFAULT '{}'
        );
    """)
    conn.commit()
    conn.close()


def _hold_write_lock(db_path: str, hold_secs: float, ready: threading.Event) -> None:
    """Acquire a write lock on *db_path* and hold it for *hold_secs*."""
    conn = sqlite3.connect(db_path, timeout=1)
    conn.execute("PRAGMA busy_timeout = 1000")
    conn.execute("BEGIN IMMEDIATE")
    ready.set()  # Signal that the lock is held.
    time.sleep(hold_secs)
    conn.rollback()
    conn.close()


def _make_injection_result(dispatch_id: str, n_items: int = 0):
    """Build a minimal InjectionResult; empty items → suppression event."""
    from intelligence_sources._common import IntelligenceItem, SuppressionRecord
    from intelligence_sources._models import InjectionResult

    items = [
        IntelligenceItem(
            item_id=f"intel_test_{i}",
            item_class="proven_pattern",
            title="test pattern",
            content="test content",
            confidence=0.9,
            evidence_count=2,
            last_seen="2026-07-31T00:00:00Z",
            scope_tags=[],
        )
        for i in range(n_items)
    ]
    return InjectionResult(
        injection_point="dispatch_create",
        injected_at="2026-07-31T00:00:00Z",
        items=items,
        suppressed=[SuppressionRecord(item_class="proven_pattern", reason="test")],
        task_class="coding_interactive",
        dispatch_id=dispatch_id,
    )


# ---------------------------------------------------------------------------
# Write path under real contention — the gap the existing helper-only tests miss
# ---------------------------------------------------------------------------

def test_emit_event_lock_busy_error_not_swallowed(tmp_path, short_lock_timeout):
    """emit_event must NOT swallow SQLite's own ``database is locked`` error.

    Pre-fix: the OperationalError falls into the generic ``except Exception``,
    logs at WARNING and returns None — the audit event is silently lost.
    """
    from intelligence_selector import IntelligenceSelector

    state_dir = tmp_path / "coord"
    db_path = state_dir / "runtime_coordination.db"
    _init_coordination_events(db_path)

    selector = IntelligenceSelector(coord_db_state_dir=str(state_dir))
    result = _make_injection_result("dispatch-oi880-test-1")

    ready = threading.Event()
    holder = threading.Thread(
        target=_hold_write_lock, args=(str(db_path), 3.0, ready), daemon=True
    )
    holder.start()
    try:
        assert ready.wait(timeout=5), "lock holder did not acquire the write lock"
        with pytest.raises(CoordinationLockError):
            selector.emit_event(result)
    finally:
        holder.join(timeout=10)
        selector.close()


def test_record_injection_audit_lock_busy_error_not_swallowed(tmp_path, short_lock_timeout):
    """record_injection_audit must NOT swallow SQLite's ``database is locked``.

    Pre-fix: the OperationalError is caught by ``except sqlite3.Error`` and
    swallowed at WARNING — the audit row is silently lost.
    """
    from intelligence_sources._recording import record_injection_audit

    state_dir = tmp_path / "coord"
    db_path = state_dir / "runtime_coordination.db"
    _init_intelligence_injections(db_path)

    result = _make_injection_result("dispatch-oi880-test-2")

    ready = threading.Event()
    holder = threading.Thread(
        target=_hold_write_lock, args=(str(db_path), 3.0, ready), daemon=True
    )
    holder.start()
    try:
        assert ready.wait(timeout=5), "lock holder did not acquire the write lock"
        with pytest.raises(CoordinationLockError):
            record_injection_audit(result, state_dir, None)
    finally:
        holder.join(timeout=10)


def test_record_injection_lock_busy_propagates(tmp_path):
    """record_injection surfaces a lock-busy get_connection error loudly.

    Pre-fix: the OperationalError is swallowed by ``except sqlite3.Error`` and
    the method returns normally — the audit row is silently lost.
    """
    from unittest.mock import patch

    from intelligence_selector import IntelligenceSelector

    state_dir = tmp_path / "coord"
    selector = IntelligenceSelector(coord_db_state_dir=str(state_dir))
    result = _make_injection_result("dispatch-oi880-test-4")

    import runtime_coordination as _rc

    with patch.object(
        _rc, "get_connection", side_effect=sqlite3.OperationalError("database is locked")
    ):
        with pytest.raises(CoordinationLockError):
            selector.record_injection(result)
    selector.close()


def test_record_pattern_usage_lock_busy_error_not_swallowed(tmp_path, short_lock_timeout, monkeypatch):
    """record_pattern_usage applies the same classification (OI-880).

    Uses a monkeypatched upsert to raise SQLite's lock-busy error deterministically
    — the quality DB is caller-owned, so real thread contention is not the write path.
    """
    from intelligence_sources._recording import record_pattern_usage

    db_path = tmp_path / "quality_intelligence.db"
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row

    def _fake_resolve(_db, table, preferred, fallback):
        return ("pattern_id",)

    def _boom(_db, item, now, project_id, has_project, conflict_target):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "intelligence_sources._recording._resolve_conflict_target", _fake_resolve
    )
    monkeypatch.setattr(
        "intelligence_sources._recording._upsert_pattern_usage", _boom
    )
    monkeypatch.setattr(
        "intelligence_sources._recording._upsert_dispatch_pattern_offered",
        lambda *a, **k: None,
    )

    result = _make_injection_result("dispatch-oi880-test-3", n_items=1)
    try:
        with pytest.raises(CoordinationLockError):
            record_pattern_usage(result, db, lambda _t, _c: True)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Content-based classification — a non-lock OperationalError is NOT contention
# ---------------------------------------------------------------------------

def test_is_lock_timeout_error_is_content_based():
    """OperationalError covers real bugs too; only lock-busy is contention.

    Fails on the pre-fix code with an ImportError (the helper did not exist).
    """
    from coordination_retry import is_lock_timeout_error

    # Message fallback — a hand-constructed OperationalError carries no
    # sqlite_errorcode, so the classifier must still recognise lock-busy.
    assert is_lock_timeout_error(sqlite3.OperationalError("database is locked"))
    assert is_lock_timeout_error(sqlite3.OperationalError("database table is locked"))
    assert is_lock_timeout_error(CoordinationLockError("deadline exhausted"))

    # Real bugs are NOT lock contention and must keep the existing swallow path.
    assert not is_lock_timeout_error(
        sqlite3.OperationalError("no such table: coordination_events")
    )
    assert not is_lock_timeout_error(
        sqlite3.OperationalError("no such column: project_id")
    )
    assert not is_lock_timeout_error(
        sqlite3.IntegrityError("UNIQUE constraint failed: pattern_usage.pattern_id")
    )
    assert not is_lock_timeout_error(ValueError("not a sqlite error"))

    # Structured code path (Python 3.11+): BUSY / LOCKED, incl. extended codes.
    busy = sqlite3.OperationalError("db locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    locked = sqlite3.OperationalError("db locked")
    locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
    extended = sqlite3.OperationalError("db locked")
    extended.sqlite_errorcode = sqlite3.SQLITE_BUSY_SNAPSHOT
    real_bug = sqlite3.OperationalError("db locked")
    real_bug.sqlite_errorcode = sqlite3.SQLITE_ERROR
    assert is_lock_timeout_error(busy)
    assert is_lock_timeout_error(locked)
    assert is_lock_timeout_error(extended)
    assert not is_lock_timeout_error(real_bug)
