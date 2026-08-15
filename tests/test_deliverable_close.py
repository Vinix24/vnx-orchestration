"""tests/test_deliverable_close.py — `vnx deliverable close` (OI-1151).

The missing half of the deliverable lifecycle: `promote` gates
proposed -> ready, but nothing moved a ready deliverable to a terminal state
once its work landed via a burn-PR. This suite verifies the afboeken verb:

- close WITH evidence succeeds: ready -> completed, pr_ref stamped, and the
  audit event (who/when/what) is readable back from disk
- close WITHOUT evidence fails (argparse required + invalid-value guard)
- close on an UNKNOWN deliverable id fails loud (exit 1), nothing written
- the state of a NOT-closed deliverable is left untouched when another closes
- a proposed deliverable (not yet promoted) refuses close with a hint
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import planning_cli  # noqa: E402
import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns  # noqa: E402

PROJECT_ID = "test-proj"


def _build_db(tmp_path: Path) -> Path:
    """Return a state_dir with migrations 0022 + 0024 + 0027 applied."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)

    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            state TEXT NOT NULL DEFAULT 'queued',
            terminal_id TEXT, track TEXT, priority TEXT DEFAULT 'P2', pr_ref TEXT,
            gate TEXT, attempt_count INTEGER NOT NULL DEFAULT 0, bundle_path TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            expires_after TEXT, metadata_json TEXT DEFAULT '{}',
            UNIQUE(dispatch_id, project_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coordination_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch',
            entity_id TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT,
            metadata_json TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            project_id TEXT
        )
        """
    )
    conn.commit()

    for ver, fname in (
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()

    schema_migration.apply_script_if_below(
        conn,
        27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8"),
    )
    conn.commit()
    conn.close()
    return state_dir


@pytest.fixture()
def state_with_track(tmp_path: Path) -> tuple[Path, str]:
    state_dir = _build_db(tmp_path)
    track_id = "feat-alpha"
    tracks_lib.create_track(
        state_dir,
        track_id,
        PROJECT_ID,
        title="Feature Alpha",
        goal_state="ship Feature Alpha",
        phase="queued",
        horizon="now",
    )
    return state_dir, track_id


def _dispatch_ids(state_dir: Path, state: str) -> list[str]:
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    rows = conn.execute(
        "SELECT dispatch_id FROM dispatches WHERE project_id = ? AND state = ? ORDER BY id",
        (PROJECT_ID, state),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _dispatch_row(state_dir: Path, dispatch_id: str) -> dict:
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
        (dispatch_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def _close_events(state_dir: Path, dispatch_id: str) -> list[dict]:
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM coordination_events WHERE entity_id = ? "
        "AND event_type = 'deliverable_completed' ORDER BY id",
        (dispatch_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _add_and_promote(state_dir: Path, track_id: str, title: str) -> str:
    """Add a deliverable and promote it to ready; return its dispatch_id."""
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "post",
        "--title", title,
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]
    rc = planning_cli.main([
        "deliverable", "promote", dispatch_id,
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    return dispatch_id


# ---------------------------------------------------------------------------
# close with evidence
# ---------------------------------------------------------------------------

def test_close_with_evidence_succeeds_and_readable_from_disk(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    dispatch_id = _add_and_promote(state_dir, track_id, "Q3 launch post")

    rc = planning_cli.main([
        "deliverable", "close", dispatch_id,
        "--evidence", "123",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ready -> completed" in out
    assert "#123" in out

    # The deliverable row itself is the source of truth: completed + pr_ref.
    row = _dispatch_row(state_dir, dispatch_id)
    assert row["state"] == "completed"
    assert row["pr_ref"] == "#123"

    # The audit event (who/when/what) is readable back from disk.
    events = _close_events(state_dir, dispatch_id)
    assert len(events) == 1
    ev = events[0]
    assert ev["event_type"] == "deliverable_completed"
    assert ev["from_state"] == "ready"
    assert ev["to_state"] == "completed"
    assert ev["actor"] == "operator"
    assert ev["project_id"] == PROJECT_ID
    assert ev["occurred_at"]
    meta = json.loads(ev["metadata_json"])
    assert meta["evidence_pr"] == 123
    assert meta["pr_ref"] == "#123"
    assert meta["attestation"] == "operator"

    # The deliverables VIEW now derives 'done' for this deliverable.
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    derived = conn.execute(
        "SELECT derived_status FROM deliverables "
        "WHERE project_id = ? AND deliverable_ref = ?",
        (PROJECT_ID, f"post:{dispatch_id}"),
    ).fetchone()
    conn.close()
    assert derived is not None and derived[0] == "done"


def test_close_is_idempotent_on_already_completed(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    dispatch_id = _add_and_promote(state_dir, track_id, "Close me twice")
    for _ in range(2):
        rc = planning_cli.main([
            "deliverable", "close", dispatch_id,
            "--evidence", "42",
            "--project-id", PROJECT_ID,
            "--state-dir", str(state_dir),
        ])
        assert rc == 0
        capsys.readouterr()
    # A single audit event: the second close was a no-op, not a second write.
    assert len(_close_events(state_dir, dispatch_id)) == 1


# ---------------------------------------------------------------------------
# evidence required
# ---------------------------------------------------------------------------

def test_close_without_evidence_fails(state_with_track: tuple[Path, str], capsys):
    state_dir, track_id = state_with_track
    dispatch_id = _add_and_promote(state_dir, track_id, "No evidence")

    with pytest.raises(SystemExit) as exc:
        planning_cli.main([
            "deliverable", "close", dispatch_id,
            "--project-id", PROJECT_ID,
            "--state-dir", str(state_dir),
        ])
    assert exc.value.code == 2  # argparse: --evidence is required
    capsys.readouterr()
    assert _dispatch_row(state_dir, dispatch_id)["state"] == "ready"
    assert _close_events(state_dir, dispatch_id) == []


def test_close_with_invalid_evidence_fails(state_with_track: tuple[Path, str], capsys):
    state_dir, track_id = state_with_track
    dispatch_id = _add_and_promote(state_dir, track_id, "Bad evidence")

    rc = planning_cli.main([
        "deliverable", "close", dispatch_id,
        "--evidence", "not-a-pr",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--evidence <pr> is REQUIRED" in err
    assert _dispatch_row(state_dir, dispatch_id)["state"] == "ready"
    assert _close_events(state_dir, dispatch_id) == []


# ---------------------------------------------------------------------------
# unknown id / wrong state
# ---------------------------------------------------------------------------

def test_close_unknown_deliverable_fails_loud(state_with_track: tuple[Path, str], capsys):
    state_dir, _ = state_with_track
    rc = planning_cli.main([
        "deliverable", "close", "no-such-deliverable",
        "--evidence", "123",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err


def test_close_proposed_deliverable_refused(state_with_track: tuple[Path, str], capsys):
    state_dir, track_id = state_with_track
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "doc",
        "--title", "Still proposed",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    capsys.readouterr()
    dispatch_id = _dispatch_ids(state_dir, "proposed")[-1]

    rc = planning_cli.main([
        "deliverable", "close", dispatch_id,
        "--evidence", "7",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "expected state 'ready'" in err
    assert "promote" in err
    assert _dispatch_row(state_dir, dispatch_id)["state"] == "proposed"


# ---------------------------------------------------------------------------
# untouched sibling
# ---------------------------------------------------------------------------

def test_close_leaves_other_deliverable_untouched(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    closed = _add_and_promote(state_dir, track_id, "Closed one")
    untouched = _add_and_promote(state_dir, track_id, "Untouched one")

    rc = planning_cli.main([
        "deliverable", "close", closed,
        "--evidence", "99",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    capsys.readouterr()

    assert _dispatch_row(state_dir, closed)["state"] == "completed"
    # The NOT-closed deliverable keeps its status and pr_ref untouched.
    untouched_row = _dispatch_row(state_dir, untouched)
    assert untouched_row["state"] == "ready"
    assert untouched_row["pr_ref"] is None
    assert _close_events(state_dir, untouched) == []


# ---------------------------------------------------------------------------
# project_id scoping (ADR-007)
# ---------------------------------------------------------------------------

def test_close_scoped_by_project_id(state_with_track: tuple[Path, str], capsys):
    """A deliverable id that exists in ANOTHER project is not found here."""
    state_dir, track_id = state_with_track
    dispatch_id = _add_and_promote(state_dir, track_id, "Other project's")

    rc = planning_cli.main([
        "deliverable", "close", dispatch_id,
        "--evidence", "5",
        "--project-id", "some-other-project",
        "--state-dir", str(state_dir),
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err
    # The deliverable under its real project_id is untouched.
    assert _dispatch_row(state_dir, dispatch_id)["state"] == "ready"
