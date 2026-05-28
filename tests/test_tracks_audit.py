"""tests/test_tracks_audit.py — NDJSON audit emission from tracks DAL.

Verifies that every write operation in scripts/lib/tracks.py appends a
corresponding NDJSON event to track_events.ndjson (ADR-005 compliance).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration
import tracks


def _create_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT,
            priority TEXT DEFAULT 'P2',
            pr_ref TEXT, gate TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT, entity_type TEXT, entity_id TEXT,
            from_state TEXT, to_state TEXT, actor TEXT, reason TEXT,
            metadata_json TEXT, occurred_at TEXT, project_id TEXT
        )
    """)
    conn.commit()

    sql = (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 22, sql)
    conn.commit()
    conn.close()
    return db_path.parent


def _read_events(state_dir: Path) -> list[dict]:
    path = state_dir.parent / "events" / "track_events.ndjson"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


@pytest.fixture()
def state_dir(tmp_path):
    return _create_db(tmp_path)


class TestTrackCreatedAudit:
    def test_create_track_emits_event(self, state_dir):
        tracks.create_track(state_dir, "track-01", "Title", "Goal")
        events = _read_events(state_dir)
        assert any(e["event_type"] == "track_created" for e in events)

    def test_create_track_event_has_track_id(self, state_dir):
        tracks.create_track(state_dir, "track-01", "Title", "Goal")
        events = _read_events(state_dir)
        created = [e for e in events if e["event_type"] == "track_created"]
        assert len(created) >= 1
        assert created[0]["track_id"] == "track-01"

    def test_create_track_event_has_actor(self, state_dir):
        tracks.create_track(state_dir, "track-01", "Title", "Goal")
        events = _read_events(state_dir)
        created = [e for e in events if e["event_type"] == "track_created"]
        assert created[0]["actor"] == "system"

    def test_create_track_event_has_timestamp(self, state_dir):
        tracks.create_track(state_dir, "track-01", "Title", "Goal")
        events = _read_events(state_dir)
        created = [e for e in events if e["event_type"] == "track_created"]
        assert "timestamp" in created[0]


class TestTransitionPhaseAudit:
    def test_transition_emits_event(self, state_dir):
        tracks.create_track(state_dir, "track-01", "T", "G", phase="queued")
        tracks.transition_phase(state_dir, "track-01", "active", actor="operator")
        events = _read_events(state_dir)
        assert any(e["event_type"] == "track_phase_transition" for e in events)

    def test_transition_event_details(self, state_dir):
        tracks.create_track(state_dir, "track-01", "T", "G", phase="queued")
        tracks.transition_phase(state_dir, "track-01", "active", actor="T0")
        events = _read_events(state_dir)
        transitions = [e for e in events if e["event_type"] == "track_phase_transition"]
        assert len(transitions) >= 1
        evt = transitions[0]
        assert evt["track_id"] == "track-01"
        assert evt["actor"] == "T0"
        assert evt["details"]["from"] == "queued"
        assert evt["details"]["to"] == "active"


class TestSetNextUpAudit:
    def test_set_next_up_emits_event(self, state_dir):
        tracks.create_track(state_dir, "track-01", "T", "G")
        tracks.set_next_up(state_dir, "track-01")
        events = _read_events(state_dir)
        assert any(e["event_type"] == "track_next_up_set" for e in events)

    def test_set_next_up_event_track_id(self, state_dir):
        tracks.create_track(state_dir, "track-01", "T", "G")
        tracks.set_next_up(state_dir, "track-01")
        events = _read_events(state_dir)
        next_up_events = [e for e in events if e["event_type"] == "track_next_up_set"]
        assert next_up_events[0]["track_id"] == "track-01"


class TestLinkOpenItemAudit:
    def test_link_open_item_emits_event(self, state_dir):
        tracks.create_track(state_dir, "track-01", "T", "G")
        tracks.link_open_item(state_dir, "track-01", "OI-999", "blocks", "manual")
        events = _read_events(state_dir)
        assert any(e["event_type"] == "track_oi_linked" for e in events)

    def test_link_open_item_event_details(self, state_dir):
        tracks.create_track(state_dir, "track-01", "T", "G")
        tracks.link_open_item(state_dir, "track-01", "OI-999", "warns", "file_path")
        events = _read_events(state_dir)
        oi_events = [e for e in events if e["event_type"] == "track_oi_linked"]
        assert oi_events[0]["details"]["oi_id"] == "OI-999"
        assert oi_events[0]["details"]["link_type"] == "warns"


class TestAddDependencyAudit:
    def test_add_dependency_emits_event(self, state_dir):
        tracks.create_track(state_dir, "track-01", "T1", "G1", phase="active")
        tracks.create_track(state_dir, "track-02", "T2", "G2")
        tracks.add_dependency(state_dir, "track-02", "track-01", "hard", "manual")
        events = _read_events(state_dir)
        assert any(e["event_type"] == "track_dep_added" for e in events)

    def test_add_dependency_event_details(self, state_dir):
        tracks.create_track(state_dir, "track-01", "T1", "G1", phase="active")
        tracks.create_track(state_dir, "track-02", "T2", "G2")
        tracks.add_dependency(state_dir, "track-02", "track-01", "soft", "manual")
        events = _read_events(state_dir)
        dep_events = [e for e in events if e["event_type"] == "track_dep_added"]
        assert dep_events[0]["track_id"] == "track-02"
        assert dep_events[0]["details"]["to"] == "track-01"
        assert dep_events[0]["details"]["kind"] == "soft"


# ---------------------------------------------------------------------------
# ADR-005: mutation functions raise on audit write failure; no DB row committed
# ---------------------------------------------------------------------------

import state_writer as _sw


class TestAuditWriteFailureRaises:
    """All 5 mutation functions must raise and leave no committed DB row
    when the NDJSON write fails (ADR-005 ledger-first guarantee)."""

    def _count(self, state_dir: Path, table: str, where: str, val: str) -> int:
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where} = ?", (val,)
        ).fetchone()
        conn.close()
        return row[0]

    def test_create_track_raises_on_audit_failure(self, state_dir):
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            with pytest.raises(OSError):
                tracks.create_track(state_dir, "track-fail-01", "T", "G")

    def test_create_track_no_row_after_audit_failure(self, state_dir):
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            try:
                tracks.create_track(state_dir, "track-fail-02", "T", "G")
            except OSError:
                pass
        assert self._count(state_dir, "tracks", "track_id", "track-fail-02") == 0

    def test_transition_phase_raises_on_audit_failure(self, state_dir):
        tracks.create_track(state_dir, "track-tp-01", "T", "G", phase="queued")
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            with pytest.raises(OSError):
                tracks.transition_phase(state_dir, "track-tp-01", "active", actor="operator")

    def test_transition_phase_no_phase_change_after_audit_failure(self, state_dir):
        tracks.create_track(state_dir, "track-tp-02", "T", "G", phase="queued")
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            try:
                tracks.transition_phase(state_dir, "track-tp-02", "active", actor="operator")
            except OSError:
                pass
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT phase FROM tracks WHERE track_id = 'track-tp-02'").fetchone()
        conn.close()
        assert row[0] == "queued"

    def test_set_next_up_raises_on_audit_failure(self, state_dir):
        tracks.create_track(state_dir, "track-nu-01", "T", "G")
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            with pytest.raises(OSError):
                tracks.set_next_up(state_dir, "track-nu-01")

    def test_set_next_up_not_committed_after_audit_failure(self, state_dir):
        tracks.create_track(state_dir, "track-nu-02", "T", "G")
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            try:
                tracks.set_next_up(state_dir, "track-nu-02")
            except OSError:
                pass
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT next_up FROM tracks WHERE track_id = 'track-nu-02'").fetchone()
        conn.close()
        assert row[0] == 0

    def test_link_open_item_raises_on_audit_failure(self, state_dir):
        tracks.create_track(state_dir, "track-oi-01", "T", "G")
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            with pytest.raises(OSError):
                tracks.link_open_item(state_dir, "track-oi-01", "OI-x", "blocks", "manual")

    def test_link_open_item_no_row_after_audit_failure(self, state_dir):
        tracks.create_track(state_dir, "track-oi-02", "T", "G")
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            try:
                tracks.link_open_item(state_dir, "track-oi-02", "OI-y", "blocks", "manual")
            except OSError:
                pass
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM track_open_items WHERE track_id = 'track-oi-02'"
        ).fetchone()
        conn.close()
        assert row[0] == 0

    def test_add_dependency_raises_on_audit_failure(self, state_dir):
        tracks.create_track(state_dir, "track-dep-a", "T", "G", phase="active")
        tracks.create_track(state_dir, "track-dep-b", "T", "G")
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            with pytest.raises(OSError):
                tracks.add_dependency(state_dir, "track-dep-b", "track-dep-a", "hard", "manual")

    def test_add_dependency_no_row_after_audit_failure(self, state_dir):
        tracks.create_track(state_dir, "track-dep-c", "T", "G", phase="active")
        tracks.create_track(state_dir, "track-dep-d", "T", "G")
        with patch.object(_sw, "append_locked", side_effect=OSError("full")):
            try:
                tracks.add_dependency(state_dir, "track-dep-d", "track-dep-c", "hard", "manual")
            except OSError:
                pass
        db_path = state_dir / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT COUNT(*) FROM track_dependencies "
            "WHERE from_track_id = 'track-dep-d'"
        ).fetchone()
        conn.close()
        assert row[0] == 0
