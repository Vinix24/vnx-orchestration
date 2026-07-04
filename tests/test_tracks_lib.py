"""tests/test_tracks_lib.py — unit tests for scripts/lib/tracks.py.

Tests:
- create/get/list/transition/set_next_up happy paths
- Validation errors (invalid phase, invalid actor, invalid transition)
- link_open_item and add_dependency
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration
import tracks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_db(tmp_path: Path) -> Path:
    """Create a minimal runtime_coordination.db with migration 0022 applied."""
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
            terminal_id     TEXT,
            track           TEXT,
            priority        TEXT    DEFAULT 'P2',
            pr_ref          TEXT,
            gate            TEXT,
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            bundle_path     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_after   TEXT,
            metadata_json   TEXT    DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT,
            event_type  TEXT,
            entity_type TEXT,
            entity_id   TEXT,
            from_state  TEXT,
            to_state    TEXT,
            actor       TEXT,
            reason      TEXT,
            metadata_json TEXT,
            occurred_at TEXT,
            project_id  TEXT
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


# ---------------------------------------------------------------------------
# create_track
# ---------------------------------------------------------------------------

class TestCreateTrack:
    def test_create_returns_dict(self, state_dir):
        t = tracks.create_track(state_dir, "track-01", "vnx-dev", "Title One", "Goal One")
        assert t["track_id"] == "track-01"
        assert t["title"] == "Title One"
        assert t["goal_state"] == "Goal One"
        assert t["phase"] == "queued"

    def test_create_with_priority(self, state_dir):
        t = tracks.create_track(state_dir, "track-02", "vnx-dev", "T2", "G2", priority="high")
        assert t["priority"] == "high"

    def test_create_invalid_phase(self, state_dir):
        with pytest.raises(tracks.InvalidPhaseError):
            tracks.create_track(state_dir, "track-03", "vnx-dev", "T3", "G3", phase="invalid")

    def test_create_duplicate_raises(self, state_dir):
        tracks.create_track(state_dir, "track-04", "vnx-dev", "T4", "G4")
        with pytest.raises(Exception):  # UNIQUE constraint violation
            tracks.create_track(state_dir, "track-04", "vnx-dev", "T4", "G4")


# ---------------------------------------------------------------------------
# get_track
# ---------------------------------------------------------------------------

class TestGetTrack:
    def test_get_existing(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        t = tracks.get_track(state_dir, "track-01", "vnx-dev")
        assert t is not None
        assert t["track_id"] == "track-01"

    def test_get_nonexistent_returns_none(self, state_dir):
        assert tracks.get_track(state_dir, "track-nonexistent", "vnx-dev") is None


# ---------------------------------------------------------------------------
# list_tracks
# ---------------------------------------------------------------------------

class TestListTracks:
    def test_list_all(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        tracks.create_track(state_dir, "track-02", "vnx-dev", "T2", "G2", phase="active")
        result = tracks.list_tracks(state_dir, "vnx-dev")
        assert len(result) == 2

    def test_list_by_phase(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        tracks.create_track(state_dir, "track-02", "vnx-dev", "T2", "G2", phase="active")
        queued = tracks.list_tracks(state_dir, "vnx-dev", phase="queued")
        assert len(queued) == 1
        assert queued[0]["track_id"] == "track-01"

    def test_list_invalid_phase_raises(self, state_dir):
        with pytest.raises(tracks.InvalidPhaseError):
            tracks.list_tracks(state_dir, "vnx-dev", phase="invalid")

    def test_list_empty(self, state_dir):
        assert tracks.list_tracks(state_dir, "vnx-dev") == []

    def test_list_project_isolation(self, state_dir):
        tracks.create_track(state_dir, "track-01", "project-a", "T1", "G1")
        tracks.create_track(state_dir, "track-02", "project-b", "T2", "G2")
        result = tracks.list_tracks(state_dir, "project-a")
        assert len(result) == 1
        assert result[0]["track_id"] == "track-01"


# ---------------------------------------------------------------------------
# transition_phase
# ---------------------------------------------------------------------------

class TestTransitionPhase:
    def test_queued_to_active(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        t = tracks.transition_phase(state_dir, "track-01", "vnx-dev", "active", actor="operator")
        assert t["phase"] == "active"

    def test_active_to_done(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="active")
        t = tracks.transition_phase(state_dir, "track-01", "vnx-dev", "done", actor="T0")
        assert t["phase"] == "done"
        assert t.get("completed_at") is not None

    def test_active_to_parked(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="active")
        t = tracks.transition_phase(state_dir, "track-01", "vnx-dev", "parked", actor="operator", reason="blocked")
        assert t["phase"] == "parked"

    def test_parked_to_queued_unpark(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="parked")
        t = tracks.transition_phase(state_dir, "track-01", "vnx-dev", "queued", actor="operator")
        assert t["phase"] == "queued"

    def test_done_to_queued_still_illegal(self, state_dir):
        """done→queued is NOT an allowed transition (only done→active is)."""
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="active")
        tracks.transition_phase(state_dir, "track-01", "vnx-dev", "done", actor="T0")
        with pytest.raises(tracks.InvalidTransitionError):
            tracks.transition_phase(state_dir, "track-01", "vnx-dev", "queued", actor="operator")

    def test_done_to_active_allowed(self, state_dir):
        """done→active is the reopen valve — allowed and writes a history row."""
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="active")
        tracks.transition_phase(state_dir, "track-01", "vnx-dev", "done", actor="T0")
        t = tracks.transition_phase(
            state_dir, "track-01", "vnx-dev", "active",
            actor="operator",
            reason="reopen pr_ref=#42 | follow-up work needed",
            approval_id="reopen-approval-001",
        )
        assert t["phase"] == "active"

        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        row = conn.execute(
            "SELECT from_phase, to_phase, actor, reason, approval_id "
            "FROM track_phase_history "
            "WHERE track_id='track-01' AND from_phase='done' AND to_phase='active'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "done"
        assert row[1] == "active"
        assert row[2] == "operator"
        assert row[3] == "reopen pr_ref=#42 | follow-up work needed"
        assert row[4] == "reopen-approval-001"

    def test_invalid_actor_raises(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        with pytest.raises(ValueError, match="Invalid actor"):
            tracks.transition_phase(state_dir, "track-01", "vnx-dev", "active", actor="robot")

    def test_invalid_to_phase_raises(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        with pytest.raises(tracks.InvalidPhaseError):
            tracks.transition_phase(state_dir, "track-01", "vnx-dev", "flying", actor="operator")

    def test_nonexistent_track_raises(self, state_dir):
        with pytest.raises(tracks.TrackNotFoundError):
            tracks.transition_phase(state_dir, "track-nope", "vnx-dev", "active", actor="operator")

    def test_phase_history_recorded(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        tracks.transition_phase(state_dir, "track-01", "vnx-dev", "active", actor="operator", reason="go")

        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        row = conn.execute(
            "SELECT from_phase, to_phase, actor, reason FROM track_phase_history "
            "WHERE track_id = 'track-01'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "queued"
        assert row[1] == "active"
        assert row[2] == "operator"
        assert row[3] == "go"

    def test_noop_transition_returns_track(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        t = tracks.transition_phase(state_dir, "track-01", "vnx-dev", "queued", actor="operator")
        assert t["phase"] == "queued"


# ---------------------------------------------------------------------------
# set_next_up
# ---------------------------------------------------------------------------

class TestSetNextUp:
    def test_set_next_up(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        tracks.set_next_up(state_dir, "track-01", "vnx-dev")
        t = tracks.get_track(state_dir, "track-01", "vnx-dev")
        assert t["next_up"] == 1

    def test_set_next_up_clears_previous(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="queued")
        tracks.create_track(state_dir, "track-02", "vnx-dev", "T2", "G2", phase="queued")
        tracks.set_next_up(state_dir, "track-01", "vnx-dev")
        tracks.set_next_up(state_dir, "track-02", "vnx-dev")

        t1 = tracks.get_track(state_dir, "track-01", "vnx-dev")
        t2 = tracks.get_track(state_dir, "track-02", "vnx-dev")
        assert t1["next_up"] == 0
        assert t2["next_up"] == 1

    def test_set_next_up_nonexistent_raises(self, state_dir):
        with pytest.raises(tracks.TrackNotFoundError):
            tracks.set_next_up(state_dir, "track-nope", "vnx-dev")


# ---------------------------------------------------------------------------
# get_recent_receipts
# ---------------------------------------------------------------------------

class TestGetRecentReceipts:
    def test_get_recent_receipts_raises_on_corrupt_db(self, monkeypatch, tmp_path):
        class BrokenConnection:
            def __init__(self):
                self.closed = False

            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("database disk image is malformed")

            def close(self):
                self.closed = True

        conn = BrokenConnection()
        monkeypatch.setattr(tracks, "_get_conn", lambda state_dir: conn)

        with pytest.raises(sqlite3.OperationalError, match="malformed"):
            tracks.get_recent_receipts(tmp_path, "track-01", "vnx-dev")

        assert conn.closed is True


# ---------------------------------------------------------------------------
# link_open_item
# ---------------------------------------------------------------------------

class TestLinkOpenItem:
    def test_link_open_item(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        tracks.link_open_item(state_dir, "track-01", "vnx-dev", "OI-1234", "blocks", "manual")

        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        row = conn.execute(
            "SELECT oi_id, link_type, link_source FROM track_open_items WHERE track_id = 'track-01'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "OI-1234"
        assert row[1] == "blocks"

    def test_invalid_link_type_raises(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        with pytest.raises(ValueError, match="link_type"):
            tracks.link_open_item(state_dir, "track-01", "vnx-dev", "OI-1", "invalid", "manual")

    def test_invalid_link_source_raises(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        with pytest.raises(ValueError, match="link_source"):
            tracks.link_open_item(state_dir, "track-01", "vnx-dev", "OI-1", "warns", "bad_source")

    def test_nonexistent_track_raises(self, state_dir):
        with pytest.raises(tracks.TrackNotFoundError):
            tracks.link_open_item(state_dir, "track-nope", "vnx-dev", "OI-1", "warns", "manual")

    def test_link_open_item_idempotent_no_duplicates(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        tracks.link_open_item(state_dir, "track-01", "vnx-dev", "OI-1234", "blocks", "manual")
        tracks.link_open_item(state_dir, "track-01", "vnx-dev", "OI-1234", "blocks", "manual")
        tracks.link_open_item(state_dir, "track-01", "vnx-dev", "OI-1234", "blocks", "manual")

        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        count = conn.execute(
            "SELECT COUNT(*) FROM track_open_items "
            "WHERE track_id='track-01' AND oi_id='OI-1234' AND link_type='blocks'"
        ).fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# add_dependency
# ---------------------------------------------------------------------------

class TestAddDependency:
    def test_add_dependency(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1", phase="active")
        tracks.create_track(state_dir, "track-02", "vnx-dev", "T2", "G2", phase="queued")
        tracks.add_dependency(state_dir, "track-02", "vnx-dev", "track-01", "vnx-dev", "hard", "manual")

        conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
        row = conn.execute(
            "SELECT kind, derivation_source FROM track_dependencies "
            "WHERE from_track_id = 'track-02' AND to_track_id = 'track-01'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "hard"
        assert row[1] == "manual"

    def test_invalid_kind_raises(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        tracks.create_track(state_dir, "track-02", "vnx-dev", "T2", "G2")
        with pytest.raises(ValueError, match="kind"):
            tracks.add_dependency(state_dir, "track-02", "vnx-dev", "track-01", "vnx-dev", "strict", "manual")

    def test_invalid_derivation_source_raises(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        tracks.create_track(state_dir, "track-02", "vnx-dev", "T2", "G2")
        with pytest.raises(ValueError, match="derivation_source"):
            tracks.add_dependency(state_dir, "track-02", "vnx-dev", "track-01", "vnx-dev", "hard", "unknown_source")

    def test_nonexistent_track_raises(self, state_dir):
        tracks.create_track(state_dir, "track-01", "vnx-dev", "T1", "G1")
        with pytest.raises(tracks.TrackNotFoundError):
            tracks.add_dependency(state_dir, "track-nope", "vnx-dev", "track-01", "vnx-dev", "hard", "manual")
