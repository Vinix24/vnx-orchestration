"""tests/test_tracks_v24_audit_ordering.py — ADR-005 emit-first ordering for all mutators.

Verifies that if NDJSON emit (state_writer.append_locked) raises, no SQLite
state change is written. Pattern: emit → execute → commit.

Applies to: create_track, transition_phase, set_next_up, link_open_item, add_dependency.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_MIGRATIONS = Path(__file__).resolve().parent.parent / "schemas" / "migrations"

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
            dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued', terminal_id TEXT, track TEXT,
            priority TEXT DEFAULT 'P2', pr_ref TEXT, gate TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, event_type TEXT, entity_type TEXT,
            entity_id TEXT, from_state TEXT, to_state TEXT,
            actor TEXT, reason TEXT, metadata_json TEXT,
            occurred_at TEXT, project_id TEXT
        )
    """)
    conn.commit()
    for version, filename in [(22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")]:
        sql = (_MIGRATIONS / filename).read_text(encoding="utf-8")
        schema_migration.apply_script_if_below(conn, version, sql)
        conn.commit()
    conn.close()
    return db_path.parent


@pytest.fixture()
def state_dir(tmp_path):
    return _create_db(tmp_path)


def _count_tracks(state_dir: Path, track_id: str, project_id: str) -> int:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    n = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()[0]
    conn.close()
    return n


def _get_phase(state_dir: Path, track_id: str, project_id: str) -> str | None:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _seed_track(state_dir: Path, track_id: str, project_id: str) -> None:
    with patch("state_writer.append_locked"):
        tracks.create_track(state_dir, track_id, project_id, "Title", "Goal")


# ---------------------------------------------------------------------------
# create_track: emit already first in original code; test confirms it
# ---------------------------------------------------------------------------

def test_create_track_emit_failure_leaves_no_row(state_dir):
    with patch("state_writer.append_locked", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            tracks.create_track(state_dir, "track-01", "vnx-dev", "T", "G")
    assert _count_tracks(state_dir, "track-01", "vnx-dev") == 0


# ---------------------------------------------------------------------------
# transition_phase
# ---------------------------------------------------------------------------

def test_transition_phase_emit_failure_leaves_phase_unchanged(state_dir):
    _seed_track(state_dir, "track-01", "vnx-dev")

    with patch("state_writer.append_locked", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            tracks.transition_phase(state_dir, "track-01", "vnx-dev", "active", actor="operator")

    assert _get_phase(state_dir, "track-01", "vnx-dev") == "queued"


def test_transition_phase_emit_failure_leaves_no_history_row(state_dir):
    _seed_track(state_dir, "track-01", "vnx-dev")

    with patch("state_writer.append_locked", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            tracks.transition_phase(state_dir, "track-01", "vnx-dev", "active", actor="operator")

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    count = conn.execute("SELECT COUNT(*) FROM track_phase_history").fetchone()[0]
    conn.close()
    assert count == 0


# ---------------------------------------------------------------------------
# set_next_up
# ---------------------------------------------------------------------------

def test_set_next_up_emit_failure_leaves_next_up_zero(state_dir):
    _seed_track(state_dir, "track-01", "vnx-dev")

    with patch("state_writer.append_locked", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            tracks.set_next_up(state_dir, "track-01", "vnx-dev")

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    next_up = conn.execute(
        "SELECT next_up FROM tracks WHERE track_id = 'track-01' AND project_id = 'vnx-dev'"
    ).fetchone()[0]
    conn.close()
    assert next_up == 0


# ---------------------------------------------------------------------------
# link_open_item
# ---------------------------------------------------------------------------

def test_link_open_item_emit_failure_leaves_no_row(state_dir):
    _seed_track(state_dir, "track-01", "vnx-dev")

    with patch("state_writer.append_locked", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            tracks.link_open_item(state_dir, "track-01", "vnx-dev", "OI-001", "blocks", "manual")

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    count = conn.execute("SELECT COUNT(*) FROM track_open_items").fetchone()[0]
    conn.close()
    assert count == 0


# ---------------------------------------------------------------------------
# add_dependency
# ---------------------------------------------------------------------------

def test_add_dependency_emit_failure_leaves_no_row(state_dir):
    _seed_track(state_dir, "track-01", "vnx-dev")
    _seed_track(state_dir, "track-02", "vnx-dev")

    with patch("state_writer.append_locked", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            tracks.add_dependency(
                state_dir, "track-01", "vnx-dev", "track-02", "vnx-dev", "hard", "manual"
            )

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    count = conn.execute("SELECT COUNT(*) FROM track_dependencies").fetchone()[0]
    conn.close()
    assert count == 0
