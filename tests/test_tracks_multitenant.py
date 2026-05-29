"""tests/test_tracks_multitenant.py — multi-project coexistence tests.

Verifies:
- (track-01, vnx-dev) and (track-01, seocrawler-v2) coexist in tracks table
- next_up=1 per-project UNIQUE: (track-01, vnx-dev) next_up=1 allowed simultaneously
  with (track-01, seocrawler-v2) next_up=1
- set_next_up scoped: setting track-01 next_up in vnx-dev doesn't clear seocrawler-v2
- add_dependency cross-project: vnx-dev track can depend on seocrawler-v2 track
- list_tracks --all-projects returns both
- UNIQUE constraint: setting second next_up within same project clears the first
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _db_path(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "runtime_coordination.db"

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL, project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2',
            pr_ref TEXT, gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
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
    return state_dir


def _noop_emit(*args, **kwargs):
    pass


# ---------------------------------------------------------------------------
# Tests: coexistence
# ---------------------------------------------------------------------------

def test_same_track_id_different_projects_coexist(tmp_path):
    state_dir = _db_path(tmp_path)
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        t1 = tracks.create_track(state_dir, "track-01", "vnx-dev", "VNX T1", "G")
        t2 = tracks.create_track(state_dir, "track-01", "seocrawler-v2", "SEO T1", "G")

    assert t1["project_id"] == "vnx-dev"
    assert t2["project_id"] == "seocrawler-v2"

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    count = conn.execute("SELECT COUNT(*) FROM tracks WHERE track_id = 'track-01'").fetchone()[0]
    conn.close()
    assert count == 2


def test_next_up_per_project_independent(tmp_path):
    """next_up=1 on (track-01, vnx-dev) and (track-01, seocrawler-v2) must both be allowed."""
    state_dir = _db_path(tmp_path)
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "V", "G")
        tracks.create_track(state_dir, "track-01", "seocrawler-v2", "S", "G")
        tracks.set_next_up(state_dir, "track-01", "vnx-dev")
        tracks.set_next_up(state_dir, "track-01", "seocrawler-v2")

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    rows = conn.execute(
        "SELECT project_id, next_up FROM tracks WHERE track_id = 'track-01'"
    ).fetchall()
    conn.close()

    by_project = {r[0]: r[1] for r in rows}
    assert by_project["vnx-dev"] == 1
    assert by_project["seocrawler-v2"] == 1


def test_set_next_up_scoped_clears_only_same_project(tmp_path):
    """set_next_up on vnx-dev must not affect seocrawler-v2's next_up."""
    state_dir = _db_path(tmp_path)
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "V1", "G")
        tracks.create_track(state_dir, "track-02", "vnx-dev", "V2", "G")
        tracks.create_track(state_dir, "track-01", "seocrawler-v2", "S1", "G")
        # Set track-01 as next_up in vnx-dev
        tracks.set_next_up(state_dir, "track-01", "vnx-dev")
        # Set track-01 as next_up in seocrawler-v2
        tracks.set_next_up(state_dir, "track-01", "seocrawler-v2")
        # Now change next_up in vnx-dev to track-02
        tracks.set_next_up(state_dir, "track-02", "vnx-dev")

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    vnx_rows = conn.execute(
        "SELECT track_id, next_up FROM tracks WHERE project_id = 'vnx-dev' ORDER BY track_id"
    ).fetchall()
    seo_rows = conn.execute(
        "SELECT track_id, next_up FROM tracks WHERE project_id = 'seocrawler-v2'"
    ).fetchall()
    conn.close()

    vnx_map = {r[0]: r[1] for r in vnx_rows}
    assert vnx_map["track-01"] == 0, "track-01 vnx-dev must be cleared when track-02 becomes next_up"
    assert vnx_map["track-02"] == 1

    seo_map = {r[0]: r[1] for r in seo_rows}
    assert seo_map["track-01"] == 1, "seocrawler-v2 track-01 must still be next_up"


def test_cross_project_dependency_allowed(tmp_path):
    """add_dependency allows cross-project: vnx-dev track → seocrawler-v2 track."""
    state_dir = _db_path(tmp_path)
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "V", "G")
        tracks.create_track(state_dir, "track-02", "seocrawler-v2", "S", "G")
        tracks.add_dependency(
            state_dir,
            "track-01", "vnx-dev",
            "track-02", "seocrawler-v2",
            "soft", "manual",
        )

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    deps = conn.execute("SELECT * FROM track_dependencies").fetchall()
    conn.close()

    assert len(deps) == 1
    # Verify cross-project: from vnx-dev → seocrawler-v2
    col_names = [d[1] for d in sqlite3.connect(":memory:").execute("PRAGMA table_info('track_dependencies')").fetchall()]
    # Use positional access: from_track_id=0, from_project_id=1, to_track_id=2, to_project_id=3
    # Actually just verify by querying the DB directly
    conn2 = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn2.row_factory = sqlite3.Row
    dep = conn2.execute("SELECT * FROM track_dependencies").fetchone()
    assert dep["from_project_id"] == "vnx-dev"
    assert dep["to_project_id"] == "seocrawler-v2"
    conn2.close()


def test_list_all_projects_sees_all_tracks(tmp_path):
    state_dir = _db_path(tmp_path)
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "V", "G")
        tracks.create_track(state_dir, "track-01", "seocrawler-v2", "S", "G")
        tracks.create_track(state_dir, "track-02", "mc", "M", "G")

    all_rows = tracks.list_tracks(state_dir, "", all_projects=True)
    assert len(all_rows) == 3
    project_ids = {r["project_id"] for r in all_rows}
    assert project_ids == {"vnx-dev", "seocrawler-v2", "mc"}


def test_unique_next_up_within_project_enforced(tmp_path):
    """UNIQUE index ux_tracks_next_up_per_project allows only one next_up=1 per project."""
    state_dir = _db_path(tmp_path)
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "V1", "G")
        tracks.create_track(state_dir, "track-02", "vnx-dev", "V2", "G")
        tracks.set_next_up(state_dir, "track-01", "vnx-dev")

    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    with pytest.raises(sqlite3.IntegrityError):
        # Direct raw INSERT of a second next_up=1 within same project violates UNIQUE
        conn.execute(
            "UPDATE tracks SET next_up = 1 WHERE track_id = 'track-02' AND project_id = 'vnx-dev'"
        )
        conn.commit()
    conn.close()
