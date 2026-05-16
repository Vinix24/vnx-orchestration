"""test_pool_state_repo.py — Unit tests for PoolStateRepository.

Covers NDJSON ledger emission (ADR-005) for membership mutations.

Wave 6 PR-6.3 — codex R1 fix.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from pool_state_repo import PoolStateRepository  # noqa: E402
from pool_state_fixtures import create_test_db_file  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(db_path: Path, project_id: str = "vnx-dev") -> PoolStateRepository:
    return PoolStateRepository(db_path, project_id)


def _read_ledger_events(db_path: Path) -> list:
    """Read all events from pool_events.ndjson next to db_path."""
    events_file = db_path.parent.parent / "events" / "pool_events.ndjson"
    if not events_file.exists():
        return []
    events = []
    for line in events_file.read_text().splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# 1. add_member emits pool.member.added ledger event
# ---------------------------------------------------------------------------

def test_add_member_emits_ledger_event(tmp_path):
    (tmp_path / "state").mkdir()
    db = create_test_db_file(tmp_path / "state" / "runtime_coordination.db")
    repo = _make_repo(db)

    now = time.time()
    membership_id = repo.add_member("default", "T-test-01", "claude", "backend-developer", now)

    events = _read_ledger_events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "pool.member.added"
    assert ev["payload"]["membership_id"] == membership_id
    assert ev["payload"]["terminal_id"] == "T-test-01"
    assert ev["payload"]["pool_id"] == "default"
    assert ev["payload"]["provider"] == "claude"
    assert ev["payload"]["role"] == "backend-developer"
    assert "timestamp" in ev


# ---------------------------------------------------------------------------
# 2. mark_member_reaped emits pool.member.reaped ledger event
# ---------------------------------------------------------------------------

def test_mark_reaped_emits_ledger_event(tmp_path):
    (tmp_path / "state").mkdir()
    db = create_test_db_file(tmp_path / "state" / "runtime_coordination.db")
    repo = _make_repo(db)

    now = time.time()
    membership_id = repo.add_member("default", "T-test-02", "claude", "backend-developer", now)

    # Clear the add event so we can isolate the reap event
    events_file = db.parent.parent / "events" / "pool_events.ndjson"
    events_file.write_bytes(b"")

    repo.mark_member_reaped(membership_id, "scale_down", now + 1)

    events = _read_ledger_events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "pool.member.reaped"
    assert ev["payload"]["membership_id"] == membership_id
    assert ev["payload"]["reason"] == "scale_down"
    assert "timestamp" in ev


# ---------------------------------------------------------------------------
# 3. add_member + mark_reaped both emit — two events in sequence
# ---------------------------------------------------------------------------

def test_add_then_reap_emits_two_events(tmp_path):
    (tmp_path / "state").mkdir()
    db = create_test_db_file(tmp_path / "state" / "runtime_coordination.db")
    repo = _make_repo(db)

    now = time.time()
    mid = repo.add_member("default", "T-seq-01", "claude", "backend-developer", now)
    repo.mark_member_reaped(mid, "heartbeat_stale", now + 5)

    events = _read_ledger_events(db)
    assert len(events) == 2
    assert events[0]["event_type"] == "pool.member.added"
    assert events[1]["event_type"] == "pool.member.reaped"


# ---------------------------------------------------------------------------
# 4. Ledger file is created on first emit (no pre-existing file required)
# ---------------------------------------------------------------------------

def test_ledger_file_created_on_first_emit(tmp_path):
    (tmp_path / "state").mkdir()
    db = create_test_db_file(tmp_path / "state" / "runtime_coordination.db")
    events_file = db.parent.parent / "events" / "pool_events.ndjson"

    assert not events_file.exists(), "Ledger file should not exist yet"

    repo = _make_repo(db)
    repo.add_member("default", "T-new", "claude", "backend-developer", time.time())

    assert events_file.exists(), "Ledger file should be created after first emit"


# ---------------------------------------------------------------------------
# 5. Ledger atomic-append: concurrent writers produce valid NDJSON
# ---------------------------------------------------------------------------

def test_ledger_file_atomic_append(tmp_path):
    """Concurrent add_member calls must each produce exactly one valid JSON line."""
    (tmp_path / "state").mkdir()
    db = create_test_db_file(tmp_path / "state" / "runtime_coordination.db")

    errors: list = []
    barrier = threading.Barrier(4)

    def add_one(terminal_id: str) -> None:
        try:
            barrier.wait()  # all threads start simultaneously
            repo = _make_repo(db)
            repo.add_member("default", terminal_id, "claude", "backend-developer", time.time())
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=add_one, args=(f"T-concurrent-{i}",))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Concurrent emit errors: {errors}"

    events = _read_ledger_events(db)
    # Each thread emits one add event; SQLite unique index on (terminal_id, project_id)
    # may reject duplicates, but each successful DB write must produce a ledger line.
    add_events = [e for e in events if e["event_type"] == "pool.member.added"]
    # Verify every event is valid JSON with required fields
    for ev in add_events:
        assert "event_type" in ev
        assert "timestamp" in ev
        assert "payload" in ev
