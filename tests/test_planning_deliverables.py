"""tests/test_planning_deliverables.py — deliverable plane (Phase 2) tests.

Covers:
- `deliverable add` creates a dispatch row in state='proposed' under a track
- `deliverable list` shows the proposed deliverable (raw fallback + view path)
- `deliverable promote` transitions proposed->ready, stamps operator_approved_at
- A proposed deliverable is NOT claimable (BrokerError with actionable message)
- A ready deliverable IS claimable via the dispatch broker
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

import schema_migration
import planning_cli
import tracks as tracks_lib


PROJECT_ID = "test-proj"


def _build_db(tmp_path: Path) -> Path:
    """Return a state_dir with migrations 0022 + 0024 + 0027 applied."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    # events dir for track audit ledger
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)

    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
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
    """)
    conn.execute("""
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
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispatch_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id TEXT NOT NULL UNIQUE,
            dispatch_id TEXT NOT NULL,
            terminal_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'started',
            actor TEXT NOT NULL DEFAULT 'broker',
            metadata_json TEXT DEFAULT '{}',
            started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            completed_at TEXT
        )
    """)
    conn.commit()

    for ver, fname in (
        (22, "0022_track_layer.sql"),
        (24, "0024_tracks_tenant_scoping.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    # Pre-add output_ref + output_kind (as structural-doctor would on live DB)
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
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
    """DB with migration applied + one seeded track. Returns (state_dir, track_id)."""
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


# ---------------------------------------------------------------------------
# deliverable add
# ---------------------------------------------------------------------------

def test_deliverable_add_creates_proposed(state_with_track: tuple[Path, str], capsys):
    state_dir, track_id = state_with_track
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "post",
        "--title", "Q3 launch post",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Output should contain dispatch_id and state info
    assert "proposed" in out

    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM dispatches WHERE project_id = ? AND state = 'proposed'",
        (PROJECT_ID,),
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    row = dict(rows[0])
    assert row["track"] == track_id
    assert row["output_kind"] == "post"
    assert row["operator_approved_at"] is None
    meta = json.loads(row["metadata_json"])
    assert meta["title"] == "Q3 launch post"


def test_deliverable_add_unknown_objective_exits_nonzero(state_with_track, capsys):
    state_dir, _ = state_with_track
    rc = planning_cli.main([
        "deliverable", "add",
        "--objective", "no-such-track",
        "--output-kind", "doc",
        "--title", "Test",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 1


# ---------------------------------------------------------------------------
# deliverable list
# ---------------------------------------------------------------------------

def test_deliverable_list_shows_proposed(state_with_track: tuple[Path, str], capsys):
    state_dir, track_id = state_with_track
    # Add two deliverables
    for title in ("Post One", "Post Two"):
        planning_cli.main([
            "deliverable", "add",
            "--objective", track_id,
            "--output-kind", "post",
            "--title", title,
            "--project-id", PROJECT_ID,
            "--state-dir", str(state_dir),
        ])
    capsys.readouterr()  # discard add output

    rc = planning_cli.main([
        "deliverable", "list",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert track_id in out


def test_deliverable_list_json(state_with_track: tuple[Path, str], capsys):
    state_dir, track_id = state_with_track
    planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "pr",
        "--title", "Implement alpha",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    capsys.readouterr()

    rc = planning_cli.main([
        "deliverable", "list",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
        "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["output_kind"] == "pr"
    assert data[0]["derived_status"] == "proposed"


def test_deliverable_list_filter_by_objective(state_with_track: tuple[Path, str], capsys):
    state_dir, track_id = state_with_track
    # Create a second track and add a deliverable to it
    tracks_lib.create_track(
        state_dir, "feat-beta", PROJECT_ID,
        title="Feature Beta", goal_state="ship Beta", phase="queued",
    )
    planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "post",
        "--title", "Alpha post",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    planning_cli.main([
        "deliverable", "add",
        "--objective", "feat-beta",
        "--output-kind", "doc",
        "--title", "Beta doc",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    capsys.readouterr()

    rc = planning_cli.main([
        "deliverable", "list",
        "--objective", track_id,
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
        "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert all(r["track"] == track_id for r in data)
    assert len(data) == 1


# ---------------------------------------------------------------------------
# deliverable promote
# ---------------------------------------------------------------------------

def test_deliverable_promote_sets_ready_and_stamps_approved_at(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    # Add deliverable
    planning_cli.main([
        "deliverable", "add",
        "--objective", track_id,
        "--output-kind", "post",
        "--title", "Promote me",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    capsys.readouterr()

    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT dispatch_id FROM dispatches WHERE project_id = ? AND state = 'proposed'",
        (PROJECT_ID,),
    ).fetchone()
    dispatch_id = row["dispatch_id"]
    conn.close()

    rc = planning_cli.main([
        "deliverable", "promote", dispatch_id,
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ready" in out

    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    conn.row_factory = sqlite3.Row
    updated = dict(conn.execute(
        "SELECT state, operator_approved_at FROM dispatches WHERE dispatch_id = ? AND project_id = ?",
        (dispatch_id, PROJECT_ID),
    ).fetchone())
    conn.close()

    assert updated["state"] == "ready"
    assert updated["operator_approved_at"] is not None


def test_deliverable_promote_wrong_state_exits_nonzero(
    state_with_track: tuple[Path, str], capsys
):
    state_dir, track_id = state_with_track
    # Insert a queued (non-proposed) dispatch directly
    conn = sqlite3.connect(str(state_dir / tracks_lib.DB_FILENAME))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?, ?, 'queued', ?)",
        ("already-queued-1", PROJECT_ID, track_id),
    )
    conn.commit()
    conn.close()

    rc = planning_cli.main([
        "deliverable", "promote", "already-queued-1",
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])
    assert rc == 1


# ---------------------------------------------------------------------------
# dispatch guard: proposed not claimable, ready is claimable
# ---------------------------------------------------------------------------

def _extract_dispatch_id(output: str) -> str:
    """Parse dispatch_id from planning_cli deliverable add output."""
    for line in output.splitlines():
        if line.startswith("Deliverable created:"):
            return line.split(": ", 1)[1].strip()
    raise ValueError(f"dispatch_id not found in output:\n{output}")


def test_proposed_dispatch_not_claimable(state_with_track: tuple[Path, str], tmp_path: Path):
    state_dir, track_id = state_with_track
    from dispatch_broker import DispatchBroker, BrokerError

    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        planning_cli.main([
            "deliverable", "add",
            "--objective", track_id,
            "--output-kind", "deal",
            "--title", "Proposed deal",
            "--project-id", PROJECT_ID,
            "--state-dir", str(state_dir),
        ])
    dispatch_id = _extract_dispatch_id(buf.getvalue())

    broker = DispatchBroker(state_dir, dispatch_dir)
    with pytest.raises(BrokerError, match="proposed"):
        broker.claim(dispatch_id, terminal_id="T1")


def test_ready_dispatch_is_claimable(state_with_track: tuple[Path, str], tmp_path: Path):
    state_dir, track_id = state_with_track
    from dispatch_broker import DispatchBroker

    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        planning_cli.main([
            "deliverable", "add",
            "--objective", track_id,
            "--output-kind", "post",
            "--title", "Ready post",
            "--project-id", PROJECT_ID,
            "--state-dir", str(state_dir),
        ])
    dispatch_id = _extract_dispatch_id(buf.getvalue())

    planning_cli.main([
        "deliverable", "promote", dispatch_id,
        "--project-id", PROJECT_ID,
        "--state-dir", str(state_dir),
    ])

    broker = DispatchBroker(state_dir, dispatch_dir)
    result = broker.claim(dispatch_id, terminal_id="T1")
    assert result.dispatch_row["state"] == "claimed"
    assert result.dispatch_row["dispatch_id"] == dispatch_id
