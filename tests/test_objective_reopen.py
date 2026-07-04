"""tests/test_objective_reopen.py — vnx objective reopen CLI command (D6).

Verifies:
- happy path: done→active, track_phase_history carries approval_id + stamped reason
- refusal without --approval-id (exit 2, no write)
- refusal without --reason (exit 2, no write)
- refusal when track is not in phase done (exit 2, no write)
- when track has no pr_ref, stamp uses '-'
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for _p in (_LIB, _SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import planning_cli  # noqa: E402
import schema_migration  # noqa: E402
import tracks as tracks_lib  # noqa: E402

PROJECT_ID = "test-reopen-proj"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path) -> Path:
    """State dir with migrations 0022 + 0024 + 0027 + 0028 applied."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir.parent / "events").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev', state TEXT NOT NULL DEFAULT 'queued',
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT, metadata_json TEXT DEFAULT '{}', occurred_at TEXT NOT NULL,
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
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_ref TEXT")
    conn.execute("ALTER TABLE dispatches ADD COLUMN output_kind TEXT")
    conn.execute("PRAGMA user_version = 26")
    conn.commit()
    for ver, fname in (
        (27, "0027_planning_horizon_and_deliverable_view.sql"),
        (28, "0028_tracks_derived_status.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()
    conn.close()
    return state_dir


def _args(
    state_dir: Path,
    track_id: str,
    *,
    approval_id: str = "",
    reason: str = "",
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        project_id=PROJECT_ID,
        track_id=track_id,
        approval_id=approval_id,
        reason=reason,
        json=False,
    )


def _phase(state_dir: Path, track_id: str) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT phase FROM tracks WHERE track_id=? AND project_id=?",
        (track_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _history(state_dir: Path, track_id: str) -> list:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    rows = conn.execute(
        "SELECT from_phase, to_phase, actor, reason, approval_id "
        "FROM track_phase_history "
        "WHERE track_id=? AND project_id=? ORDER BY rowid",
        (track_id, PROJECT_ID),
    ).fetchall()
    conn.close()
    return [
        {
            "from_phase": r[0],
            "to_phase": r[1],
            "actor": r[2],
            "reason": r[3],
            "approval_id": r[4],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reopen_happy_path(tmp_path):
    """Happy path: done→active, history row carries approval_id and stamped reason."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(
        sd, "T-reopen", PROJECT_ID, "Title", "Goal",
        phase="active", pr_ref="#42",
    )
    tracks_lib.transition_phase(sd, "T-reopen", PROJECT_ID, "done", actor="T0")
    assert _phase(sd, "T-reopen") == "done"

    rc = planning_cli.cmd_objective_reopen(
        _args(sd, "T-reopen", approval_id="appr-001", reason="follow-up work needed")
    )

    assert rc == 0, f"expected exit 0, got {rc}"
    assert _phase(sd, "T-reopen") == "active"

    hist = _history(sd, "T-reopen")
    reopen_row = next((h for h in hist if h["to_phase"] == "active"), None)
    assert reopen_row is not None
    assert reopen_row["actor"] == "operator"
    assert reopen_row["approval_id"] == "appr-001"
    assert reopen_row["reason"].startswith("reopen pr_ref=#42 | ")
    assert "follow-up work needed" in reopen_row["reason"]


def test_reopen_refusal_no_approval_id(tmp_path):
    """Refuse with exit 2 when --approval-id is absent; track stays done."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-noappr", PROJECT_ID, "T", "G", phase="active")
    tracks_lib.transition_phase(sd, "T-noappr", PROJECT_ID, "done", actor="T0")

    rc = planning_cli.cmd_objective_reopen(
        _args(sd, "T-noappr", approval_id="", reason="some reason")
    )

    assert rc == 2
    assert _phase(sd, "T-noappr") == "done"


def test_reopen_refusal_no_reason(tmp_path):
    """Refuse with exit 2 when --reason is absent; track stays done."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-noreason", PROJECT_ID, "T", "G", phase="active")
    tracks_lib.transition_phase(sd, "T-noreason", PROJECT_ID, "done", actor="T0")

    rc = planning_cli.cmd_objective_reopen(
        _args(sd, "T-noreason", approval_id="appr-002", reason="")
    )

    assert rc == 2
    assert _phase(sd, "T-noreason") == "done"


def test_reopen_refusal_not_done(tmp_path):
    """Refuse with exit 2 when track is not in phase done; track stays active."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-notdone", PROJECT_ID, "T", "G", phase="active")

    rc = planning_cli.cmd_objective_reopen(
        _args(sd, "T-notdone", approval_id="appr-003", reason="some reason")
    )

    assert rc == 2
    assert _phase(sd, "T-notdone") == "active"


def test_reopen_refusal_not_done_queued(tmp_path):
    """Refuse with exit 2 when track is in phase queued (not done)."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-queued", PROJECT_ID, "T", "G", phase="queued")

    rc = planning_cli.cmd_objective_reopen(
        _args(sd, "T-queued", approval_id="appr-004", reason="some reason")
    )

    assert rc == 2
    assert _phase(sd, "T-queued") == "queued"


def test_reopen_pr_ref_none_uses_dash(tmp_path):
    """When track has no pr_ref, the stamp uses '-' as the pr_ref value."""
    sd = _build_db(tmp_path)
    tracks_lib.create_track(sd, "T-noref", PROJECT_ID, "T", "G", phase="active")
    tracks_lib.transition_phase(sd, "T-noref", PROJECT_ID, "done", actor="T0")

    rc = planning_cli.cmd_objective_reopen(
        _args(sd, "T-noref", approval_id="appr-005", reason="reopening")
    )

    assert rc == 0
    hist = _history(sd, "T-noref")
    reopen_row = next((h for h in hist if h["to_phase"] == "active"), None)
    assert reopen_row is not None
    assert reopen_row["reason"].startswith("reopen pr_ref=- | ")
    assert "reopening" in reopen_row["reason"]
