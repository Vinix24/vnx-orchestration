"""Regression tests for project_id scoping fixes (2026-05-20 audit).

Covers:
- LEAK-WRITE fix A: coordination_db._append_event stamps project_id
- LEAK-WRITE fix B + LEAK-READ fix C: _recording._stamp_source_dispatch_id uses project_id filter
- LEAK-READ fix D: coordination_db.get_lease scoped by project_id
- LEAK-READ fix E: coordination_db.get_events scoped by project_id
- LEAK-READ fix F: coordination_db.project_terminal_state scoped by project_id
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = REPO_ROOT / "scripts" / "lib"
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_coord_db(project_id_col: bool = True) -> sqlite3.Connection:
    """In-memory runtime_coordination.db with minimal schema + optional project_id."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE coordination_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT,
            entity_type TEXT,
            entity_id TEXT,
            from_state TEXT,
            to_state TEXT,
            actor TEXT,
            reason TEXT,
            metadata_json TEXT,
            occurred_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE terminal_leases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            terminal_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'idle',
            dispatch_id TEXT,
            generation INTEGER NOT NULL DEFAULT 1,
            leased_at TEXT,
            expires_at TEXT,
            last_heartbeat_at TEXT,
            released_at TEXT,
            metadata_json TEXT DEFAULT '{}'
        )"""
    )
    if project_id_col:
        conn.execute("ALTER TABLE coordination_events ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'")
        conn.execute("ALTER TABLE terminal_leases ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'")
    conn.commit()
    return conn


def _make_quality_db(project_id_col: bool = True) -> sqlite3.Connection:
    """In-memory quality_intelligence.db with minimal schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            confidence_score REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT,
            first_seen TEXT,
            last_used TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            severity TEXT,
            occurrence_count INTEGER DEFAULT 1,
            source_dispatch_ids TEXT,
            first_seen TEXT,
            last_seen TEXT
        )"""
    )
    if project_id_col:
        conn.execute("ALTER TABLE success_patterns ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'")
        conn.execute("ALTER TABLE antipatterns ADD COLUMN project_id TEXT NOT NULL DEFAULT 'vnx-dev'")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Fix A: _append_event stamps correct project_id (LEAK-WRITE)
# ---------------------------------------------------------------------------


def test_append_event_stamps_project_id_not_default():
    """Event from project-b must NOT be stamped project_id='vnx-dev'."""
    from coordination_db import _append_event

    conn = _make_coord_db(project_id_col=True)
    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-b", "VNX_PROJECT_FILTER": "1"}):
        _append_event(conn, event_type="dispatch_queued", entity_type="dispatch", entity_id="d-1")
        conn.commit()

    row = conn.execute("SELECT project_id FROM coordination_events WHERE entity_id='d-1'").fetchone()
    assert row is not None, "event row must exist"
    assert row[0] == "project-b", f"expected project-b, got {row[0]!r}"


def test_append_event_project_a_invisible_to_project_b():
    """Event written for project-a must not appear in a project-b query."""
    from coordination_db import _append_event, get_events

    conn = _make_coord_db(project_id_col=True)
    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-a", "VNX_PROJECT_FILTER": "1"}):
        _append_event(conn, event_type="dispatch_queued", entity_type="dispatch", entity_id="d-a")
        conn.commit()

    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-b", "VNX_PROJECT_FILTER": "1"}):
        events = get_events(conn)

    assert all(e["entity_id"] != "d-a" for e in events), "project-a event must not leak into project-b query"


def test_append_event_no_project_id_col_fallback():
    """Legacy DB without project_id column: _append_event must still succeed."""
    from coordination_db import _append_event

    conn = _make_coord_db(project_id_col=False)
    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-x", "VNX_PROJECT_FILTER": "1"}):
        event_id = _append_event(conn, event_type="test_event", entity_type="dispatch", entity_id="d-legacy")
        conn.commit()

    row = conn.execute("SELECT event_id FROM coordination_events WHERE entity_id='d-legacy'").fetchone()
    assert row is not None, "event must be written to legacy DB"
    assert row[0] == event_id


# ---------------------------------------------------------------------------
# Fix B+C: _stamp_source_dispatch_id (LEAK-WRITE + LEAK-READ)
# ---------------------------------------------------------------------------


def test_stamp_does_not_update_other_project_row():
    """Stamp for project-b must not modify a success_pattern row owned by project-a."""
    from intelligence_sources._recording import _stamp_source_dispatch_id
    from intelligence_sources._models import InjectionResult
    from intelligence_sources._common import IntelligenceItem

    db = _make_quality_db(project_id_col=True)
    # Insert a pattern row belonging to project-a
    db.execute(
        "INSERT INTO success_patterns (id, title, description, confidence_score, usage_count, project_id) "
        "VALUES (1, 'pattern-a', 'desc', 0.9, 5, 'project-a')"
    )
    db.commit()

    item = IntelligenceItem(
        item_id="intel_sp_1",
        item_class="proven_pattern",
        title="pattern-a",
        content="desc",
        confidence=0.9,
        evidence_count=5,
        last_seen="2026-01-01T00:00:00Z",
        scope_tags=[],
    )

    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-b", "VNX_PROJECT_FILTER": "1"}):
        result = _stamp_source_dispatch_id(db, item, "dispatch-from-b")
        db.commit()

    # The stamp should return False (row not found for project-b) and leave project-a row unchanged
    row = db.execute("SELECT source_dispatch_ids FROM success_patterns WHERE id=1").fetchone()
    existing = row[0]
    assert existing is None or "dispatch-from-b" not in (existing or ""), (
        "dispatch-from-b must NOT appear in project-a's source_dispatch_ids"
    )


def test_stamp_updates_correct_project_row():
    """Stamp for project-a must update only the project-a row, not the project-b row with same id."""
    from intelligence_sources._recording import _stamp_source_dispatch_id
    from intelligence_sources._common import IntelligenceItem

    db = _make_quality_db(project_id_col=True)
    # id=5 exists for BOTH project-a and project-b (central DB scenario)
    db.execute(
        "INSERT INTO success_patterns (id, title, description, confidence_score, usage_count, project_id) "
        "VALUES (5, 'shared-id-a', 'desc-a', 0.9, 3, 'project-a')"
    )
    db.execute(
        "INSERT INTO success_patterns (id, title, description, confidence_score, usage_count, project_id) "
        "VALUES (NULL, 'shared-id-b', 'desc-b', 0.8, 2, 'project-b')"
    )
    db.commit()
    # Get the actual id for project-b row
    pid_b_id = db.execute("SELECT id FROM success_patterns WHERE project_id='project-b'").fetchone()[0]

    item = IntelligenceItem(
        item_id="intel_sp_5",
        item_class="proven_pattern",
        title="shared-id-a",
        content="desc-a",
        confidence=0.9,
        evidence_count=3,
        last_seen="2026-01-01T00:00:00Z",
        scope_tags=[],
    )

    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-a", "VNX_PROJECT_FILTER": "1"}):
        _stamp_source_dispatch_id(db, item, "dispatch-from-a")
        db.commit()

    row_a = db.execute("SELECT source_dispatch_ids FROM success_patterns WHERE id=5").fetchone()
    row_b = db.execute(f"SELECT source_dispatch_ids FROM success_patterns WHERE id={pid_b_id}").fetchone()

    assert row_a[0] and "dispatch-from-a" in row_a[0], "project-a row must be stamped"
    assert not row_b[0] or "dispatch-from-a" not in (row_b[0] or ""), "project-b row must NOT be stamped"


# ---------------------------------------------------------------------------
# Fix D: get_lease scoped by project_id (LEAK-READ)
# ---------------------------------------------------------------------------


def test_get_lease_scoped_by_project_id():
    """get_lease must return None when the lease belongs to a different project."""
    from coordination_db import get_lease

    conn = _make_coord_db(project_id_col=True)
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, state, project_id) VALUES ('T1', 'idle', 'project-a')"
    )
    conn.commit()

    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-b", "VNX_PROJECT_FILTER": "1"}):
        result = get_lease(conn, "T1")

    assert result is None, "project-b must not see project-a's T1 lease"


def test_get_lease_returns_own_project_lease():
    """get_lease must return the lease that belongs to the current project."""
    from coordination_db import get_lease

    conn = _make_coord_db(project_id_col=True)
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, state, project_id) VALUES ('T1', 'idle', 'project-a')"
    )
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, state, project_id) VALUES ('T1', 'leased', 'project-b')"
    )
    conn.commit()

    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-a", "VNX_PROJECT_FILTER": "1"}):
        result = get_lease(conn, "T1")

    assert result is not None, "project-a must find its own T1 lease"
    assert result["state"] == "idle"


# ---------------------------------------------------------------------------
# Fix E: get_events scoped by project_id (LEAK-READ)
# ---------------------------------------------------------------------------


def test_get_events_scoped_by_project_id():
    """get_events for project-b must not return events from project-a."""
    from coordination_db import _append_event, get_events

    conn = _make_coord_db(project_id_col=True)
    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-a", "VNX_PROJECT_FILTER": "1"}):
        _append_event(conn, event_type="ev_a", entity_type="dispatch", entity_id="e-a")
    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-b", "VNX_PROJECT_FILTER": "1"}):
        _append_event(conn, event_type="ev_b", entity_type="dispatch", entity_id="e-b")
    conn.commit()

    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-b", "VNX_PROJECT_FILTER": "1"}):
        events = get_events(conn)

    entity_ids = [e["entity_id"] for e in events]
    assert "e-b" in entity_ids, "project-b event must be visible"
    assert "e-a" not in entity_ids, "project-a event must NOT leak into project-b query"


# ---------------------------------------------------------------------------
# Fix F: project_terminal_state scoped by project_id (LEAK-READ)
# ---------------------------------------------------------------------------


def test_project_terminal_state_scoped_by_project_id():
    """project_terminal_state must return only terminals belonging to the current project."""
    from coordination_db import project_terminal_state

    conn = _make_coord_db(project_id_col=True)
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, state, project_id) VALUES ('T1', 'idle', 'project-a')"
    )
    conn.execute(
        "INSERT INTO terminal_leases (terminal_id, state, project_id) VALUES ('T1', 'leased', 'project-b')"
    )
    conn.commit()

    with patch.dict(os.environ, {"VNX_PROJECT_ID": "project-a", "VNX_PROJECT_FILTER": "1"}):
        state = project_terminal_state(conn)

    terminals = state.get("terminals", {})
    assert "T1" in terminals, "project-a T1 must appear"
    assert terminals["T1"]["status"] == "idle", "project-a T1 must be idle"
    # project-b T1 is 'leased' — if it leaked, status would be 'working'
    # Only one T1 should appear (the project-a one)
    assert terminals["T1"]["status"] != "working", "project-b T1 (leased) must not override project-a T1"
