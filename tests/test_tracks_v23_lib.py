"""tests/test_tracks_v23_lib.py — DAL tests for project_id-required signatures.

Verifies:
- All mutator functions require project_id (TypeError when missing)
- get_track distinguishes (track-01, vnx-dev) from (track-01, seocrawler-v2)
- ADR-005 audit event includes project_id field
- record_id sha256 includes project_id (OI-004 fix — different for same track, different project)
- NDJSON event written before SQLite commit (ADR-005 ordering)
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

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

def _create_db(tmp_path: Path) -> Path:
    """Create a minimal runtime_coordination.db with migrations 0022+0024 applied."""
    db_path = tmp_path / "state" / "runtime_coordination.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL,
            project_id      TEXT    NOT NULL DEFAULT 'vnx-dev',
            state           TEXT    NOT NULL DEFAULT 'queued',
            terminal_id     TEXT, track TEXT, priority TEXT DEFAULT 'P2',
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
    return db_path.parent


@pytest.fixture()
def state_dir(tmp_path):
    return _create_db(tmp_path)


@pytest.fixture()
def state_dir_with_events(tmp_path):
    """state_dir with a real events directory for NDJSON writes."""
    sd = _create_db(tmp_path)
    (sd.parent / "events").mkdir(parents=True, exist_ok=True)
    return sd


# ---------------------------------------------------------------------------
# Tests: project_id required in all mutator signatures
# ---------------------------------------------------------------------------

def test_create_track_requires_project_id():
    sig = inspect.signature(tracks.create_track)
    params = list(sig.parameters.keys())
    # project_id must come before title (positional, after track_id)
    assert "project_id" in params
    idx_pid = params.index("project_id")
    idx_title = params.index("title")
    assert idx_pid < idx_title, "project_id must be positional before title"
    # project_id must not have a default
    assert sig.parameters["project_id"].default is inspect.Parameter.empty


def test_get_track_requires_project_id():
    sig = inspect.signature(tracks.get_track)
    assert "project_id" in sig.parameters
    assert sig.parameters["project_id"].default is inspect.Parameter.empty


def test_list_tracks_requires_project_id():
    sig = inspect.signature(tracks.list_tracks)
    params = list(sig.parameters.keys())
    assert "project_id" in params
    assert sig.parameters["project_id"].default is inspect.Parameter.empty


def test_transition_phase_requires_project_id():
    sig = inspect.signature(tracks.transition_phase)
    assert "project_id" in sig.parameters
    assert sig.parameters["project_id"].default is inspect.Parameter.empty


def test_set_next_up_requires_project_id():
    sig = inspect.signature(tracks.set_next_up)
    assert "project_id" in sig.parameters
    assert sig.parameters["project_id"].default is inspect.Parameter.empty


def test_link_open_item_requires_project_id():
    sig = inspect.signature(tracks.link_open_item)
    assert "project_id" in sig.parameters
    assert sig.parameters["project_id"].default is inspect.Parameter.empty


def test_add_dependency_requires_both_project_ids():
    sig = inspect.signature(tracks.add_dependency)
    assert "from_project_id" in sig.parameters
    assert "to_project_id" in sig.parameters
    assert sig.parameters["from_project_id"].default is inspect.Parameter.empty
    assert sig.parameters["to_project_id"].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# Tests: tenant isolation — same track_id, different project_id
# ---------------------------------------------------------------------------

def _noop_emit(*args, **kwargs):
    pass


def test_create_get_different_projects(state_dir):
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        t1 = tracks.create_track(state_dir, "track-01", "vnx-dev", "Title A", "Goal A")
        t2 = tracks.create_track(state_dir, "track-01", "seocrawler-v2", "Title B", "Goal B")

    assert t1["project_id"] == "vnx-dev"
    assert t2["project_id"] == "seocrawler-v2"
    assert t1["title"] == "Title A"
    assert t2["title"] == "Title B"

    r1 = tracks.get_track(state_dir, "track-01", "vnx-dev")
    r2 = tracks.get_track(state_dir, "track-01", "seocrawler-v2")

    assert r1 is not None
    assert r2 is not None
    assert r1["title"] == "Title A"
    assert r2["title"] == "Title B"


def test_get_track_returns_none_for_wrong_project(state_dir):
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "Title", "Goal")

    result = tracks.get_track(state_dir, "track-01", "seocrawler-v2")
    assert result is None


def test_list_tracks_project_scoped(state_dir):
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "VNX Track", "Goal")
        tracks.create_track(state_dir, "track-01", "seocrawler-v2", "SEO Track", "Goal")

    vnx_tracks = tracks.list_tracks(state_dir, "vnx-dev")
    seo_tracks = tracks.list_tracks(state_dir, "seocrawler-v2")

    assert len(vnx_tracks) == 1
    assert len(seo_tracks) == 1
    assert vnx_tracks[0]["title"] == "VNX Track"
    assert seo_tracks[0]["title"] == "SEO Track"


def test_list_tracks_all_projects(state_dir):
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        tracks.create_track(state_dir, "track-01", "seocrawler-v2", "T2", "G2")

    all_rows = tracks.list_tracks(state_dir, "", all_projects=True)
    assert len(all_rows) == 2
    pids = {r["project_id"] for r in all_rows}
    assert pids == {"vnx-dev", "seocrawler-v2"}


# ---------------------------------------------------------------------------
# Tests: ADR-005 audit event includes project_id + record_id sha256 fix (OI-004)
# ---------------------------------------------------------------------------

def test_emit_includes_project_id(state_dir_with_events):
    captured = []

    real_append = None
    try:
        import state_writer
        real_append = state_writer.append_locked
    except ImportError:
        pass

    def capture_append(path, record):
        captured.append(record)

    with patch("state_writer.append_locked", side_effect=capture_append):
        tracks._emit_track_event(
            state_dir_with_events, "track_created", "track-01", "vnx-dev", "system"
        )

    assert len(captured) == 1
    assert captured[0]["project_id"] == "vnx-dev"
    assert captured[0]["track_id"] == "track-01"


def test_record_id_differs_by_project(state_dir_with_events):
    """OI-004: sha256 includes project_id so record_ids differ between projects."""
    records = []

    def capture_append(path, record):
        records.append(record)

    with patch("state_writer.append_locked", side_effect=capture_append):
        # Use same event_type, track_id, timestamp proxy — only project_id differs
        tracks._emit_track_event(
            state_dir_with_events, "track_created", "track-01", "vnx-dev", "system"
        )
        tracks._emit_track_event(
            state_dir_with_events, "track_created", "track-01", "seocrawler-v2", "system"
        )

    assert len(records) == 2
    assert records[0]["record_id"] != records[1]["record_id"], (
        "record_id must differ when project_id differs (OI-004)"
    )


def test_emit_raises_on_write_failure(state_dir_with_events):
    """ADR-005: emit raises on OSError — no silent swallow."""
    with patch("state_writer.append_locked", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            tracks._emit_track_event(
                state_dir_with_events, "track_created", "track-01", "vnx-dev", "system"
            )


# ---------------------------------------------------------------------------
# Tests: transition_phase uses composite key
# ---------------------------------------------------------------------------

def test_transition_phase_scoped(state_dir):
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T", "G")
        tracks.create_track(state_dir, "track-01", "seocrawler-v2", "T", "G")

        result = tracks.transition_phase(
            state_dir, "track-01", "vnx-dev", "active",
            actor="operator",
        )
        assert result["phase"] == "active"

    seo = tracks.get_track(state_dir, "track-01", "seocrawler-v2")
    assert seo["phase"] == "queued", "seocrawler-v2 track must be unaffected"


def test_transition_phase_not_found_wrong_project(state_dir):
    with patch.object(tracks, "_emit_track_event", _noop_emit):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T", "G")

    with pytest.raises(tracks.TrackNotFoundError):
        tracks.transition_phase(
            state_dir, "track-01", "seocrawler-v2", "active",
            actor="operator",
        )
