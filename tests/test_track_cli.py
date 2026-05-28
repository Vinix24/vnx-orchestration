"""tests/test_track_cli.py — CLI tests for `vnx track` subcommands.

Includes tests for NDJSON audit trail written by `vnx track dispatch`
(ADR-005 compliance, Finding 3 of dispatch 20260528-fut-1-fix1-codex-r1).

Uses subprocess to invoke the installed `vnx` CLI (or falls back to
running the module directly) against a temp project directory with a
pre-initialized coordination DB.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
_SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
_MIGRATIONS = _SCHEMAS / "migrations"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import schema_migration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _init_project(tmp_path: Path) -> Path:
    """Create a minimal VNX project tree with runtime_coordination.db."""
    project_dir = tmp_path / "project"
    state_dir = project_dir / ".vnx-data" / "state"
    state_dir.mkdir(parents=True)

    db_path = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE dispatches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id     TEXT    NOT NULL UNIQUE,
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
            metadata_json   TEXT    DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE coordination_events (
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

    sql = (_MIGRATIONS / "0022_track_layer.sql").read_text(encoding="utf-8")
    schema_migration.apply_script_if_below(conn, 22, sql)
    conn.commit()
    conn.close()
    return project_dir


def _run_vnx(args: list[str], project_dir: Path) -> tuple[int, str, str]:
    """Run `vnx <args>` against project_dir, return (rc, stdout, stderr)."""
    cmd = [sys.executable, "-m", "vnx_cli.main"] + args + ["--project-dir", str(project_dir)]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(_PROJECT_ROOT),
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture()
def project_dir(tmp_path):
    return _init_project(tmp_path)


# ---------------------------------------------------------------------------
# vnx track new
# ---------------------------------------------------------------------------

class TestTrackNew:
    def test_create_track_exit_0(self, project_dir):
        rc, out, err = _run_vnx(
            ["track", "new", "track-01", "--title", "Test Track", "--goal", "Goal state"],
            project_dir,
        )
        assert rc == 0, f"stderr: {err}"

    def test_create_track_output_mentions_id(self, project_dir):
        rc, out, err = _run_vnx(
            ["track", "new", "track-01", "--title", "T1", "--goal", "G1"],
            project_dir,
        )
        assert "track-01" in out

    def test_create_track_with_priority(self, project_dir):
        rc, out, err = _run_vnx(
            ["track", "new", "track-02", "--title", "T2", "--goal", "G2", "--priority", "high"],
            project_dir,
        )
        assert rc == 0, f"stderr: {err}"


# ---------------------------------------------------------------------------
# vnx track list
# ---------------------------------------------------------------------------

class TestTrackList:
    def test_list_empty(self, project_dir):
        rc, out, err = _run_vnx(["track", "list"], project_dir)
        assert rc == 0

    def test_list_shows_created_track(self, project_dir):
        _run_vnx(
            ["track", "new", "track-01", "--title", "Sub Escape", "--goal", "All via tmux"],
            project_dir,
        )
        rc, out, err = _run_vnx(["track", "list"], project_dir)
        assert rc == 0
        assert "track-01" in out

    def test_list_phase_filter(self, project_dir):
        _run_vnx(
            ["track", "new", "track-01", "--title", "T1", "--goal", "G1"],
            project_dir,
        )
        rc, out, err = _run_vnx(["track", "list", "--phase", "queued"], project_dir)
        assert rc == 0
        assert "track-01" in out

    def test_list_phase_filter_excludes(self, project_dir):
        _run_vnx(
            ["track", "new", "track-01", "--title", "T1", "--goal", "G1"],
            project_dir,
        )
        rc, out, err = _run_vnx(["track", "list", "--phase", "done"], project_dir)
        assert rc == 0
        assert "track-01" not in out


# ---------------------------------------------------------------------------
# vnx track activate / park / unpark
# ---------------------------------------------------------------------------

class TestTrackPhaseTransitions:
    def _create_track(self, project_dir, track_id="track-01"):
        _run_vnx(
            ["track", "new", track_id, "--title", "T", "--goal", "G"],
            project_dir,
        )

    def test_activate_exit_0(self, project_dir):
        self._create_track(project_dir)
        rc, out, err = _run_vnx(["track", "activate", "track-01"], project_dir)
        assert rc == 0, f"stderr: {err}"

    def test_park_requires_reason(self, project_dir):
        self._create_track(project_dir)
        _run_vnx(["track", "activate", "track-01"], project_dir)
        rc, out, err = _run_vnx(["track", "park", "track-01", "--reason", "blocked"], project_dir)
        assert rc == 0, f"stderr: {err}"

    def test_unpark_after_park(self, project_dir):
        self._create_track(project_dir)
        _run_vnx(["track", "activate", "track-01"], project_dir)
        _run_vnx(["track", "park", "track-01", "--reason", "temp"], project_dir)
        rc, out, err = _run_vnx(["track", "unpark", "track-01"], project_dir)
        assert rc == 0, f"stderr: {err}"

    def test_invalid_track_exit_nonzero(self, project_dir):
        rc, out, err = _run_vnx(["track", "activate", "track-nope"], project_dir)
        assert rc != 0


# ---------------------------------------------------------------------------
# vnx track show
# ---------------------------------------------------------------------------

class TestTrackShow:
    def test_show_existing(self, project_dir):
        _run_vnx(
            ["track", "new", "track-01", "--title", "Sub Escape", "--goal", "tmux default"],
            project_dir,
        )
        rc, out, err = _run_vnx(["track", "show", "track-01"], project_dir)
        assert rc == 0
        assert "track-01" in out
        assert "Sub Escape" in out

    def test_show_nonexistent_exit_nonzero(self, project_dir):
        rc, out, err = _run_vnx(["track", "show", "track-nope"], project_dir)
        assert rc != 0


# ---------------------------------------------------------------------------
# vnx track dispatch — NDJSON audit (ADR-005 / Finding 3)
# ---------------------------------------------------------------------------

class TestTrackDispatchAudit:
    def _create_track(self, project_dir, track_id="track-01"):
        _run_vnx(
            ["track", "new", track_id, "--title", "T", "--goal", "G"],
            project_dir,
        )

    def test_dispatch_exit_0(self, project_dir):
        self._create_track(project_dir)
        rc, out, err = _run_vnx(
            ["track", "dispatch", "track-01", "--pr", "PR-FUT-1", "--terminal", "T1"],
            project_dir,
        )
        assert rc == 0, f"stdout: {out}\nstderr: {err}"

    def test_dispatch_row_created_in_db(self, project_dir):
        self._create_track(project_dir)
        _run_vnx(
            ["track", "dispatch", "track-01", "--pr", "PR-FUT-1", "--terminal", "T1"],
            project_dir,
        )
        db_path = project_dir / ".vnx-data" / "state" / "runtime_coordination.db"
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT state, track, pr_ref FROM dispatches WHERE track = 'track-01'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "proposed"
        assert row[2] == "PR-FUT-1"

    def test_dispatch_ndjson_emitted(self, project_dir):
        """dispatch_register.ndjson must contain a dispatch_created event after dispatch."""
        self._create_track(project_dir)
        _run_vnx(
            ["track", "dispatch", "track-01", "--pr", "PR-FUT-1", "--terminal", "T1"],
            project_dir,
        )
        register_path = project_dir / ".vnx-data" / "state" / "dispatch_register.ndjson"
        assert register_path.exists(), "dispatch_register.ndjson not created"

        events = []
        for line in register_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))

        dispatch_created = [e for e in events if e.get("event") == "dispatch_created"]
        assert len(dispatch_created) >= 1, f"No dispatch_created event found; events={events}"

    def test_dispatch_ndjson_contains_dispatch_id(self, project_dir):
        """NDJSON event must reference the created dispatch_id."""
        self._create_track(project_dir)
        _run_vnx(
            ["track", "dispatch", "track-01", "--pr", "PR-FUT-1", "--terminal", "T1"],
            project_dir,
        )
        register_path = project_dir / ".vnx-data" / "state" / "dispatch_register.ndjson"
        events = [
            json.loads(ln) for ln in register_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        dispatch_ids = [e.get("dispatch_id", "") for e in events if e.get("event") == "dispatch_created"]
        assert any("track-01" in did for did in dispatch_ids), f"dispatch_id not in events: {dispatch_ids}"

    def test_dispatch_invalid_track_exit_nonzero(self, project_dir):
        rc, out, err = _run_vnx(
            ["track", "dispatch", "track-nope", "--pr", "PR-FUT-1", "--terminal", "T1"],
            project_dir,
        )
        assert rc != 0
