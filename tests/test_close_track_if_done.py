"""tests/test_close_track_if_done.py — shared close helper + close-time revalidation.

Verifies track_reconciler.close_track_if_done:
- evidence=None path: derived-done -> walks; derived!=done -> noop_not_terminal;
  parked without include_parked -> rejected_parked.
- revalidation: snapshot pr_ref differs from current row -> stale_candidate, no write.
- revalidation: blocker OI appeared after nomination -> stale_candidate, no write.
- revalidation clean -> closes, track_phase_history rows carry given actor + approval_id.
- mid-walk failure resumability (monkeypatch transition_phase on second step).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"
_MIGRATIONS = _ROOT / "schemas" / "migrations"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import schema_migration  # noqa: E402
import track_reconciler  # noqa: E402
import tracks as tracks_lib  # noqa: E402

PROJECT_ID = "test-close-proj"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path) -> Path:
    """State dir with migrations 0022 + 0024 + 0027 + 0028 + 0030 applied."""
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch', entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT, actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT, metadata_json TEXT DEFAULT '{}', occurred_at TEXT NOT NULL,
            project_id TEXT
        )
        """
    )
    conn.commit()

    for ver, fname in ((22, "0022_track_layer.sql"), (24, "0024_tracks_tenant_scoping.sql")):
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
        (30, "0030_track_oi_resolved_at.sql"),
    ):
        schema_migration.apply_script_if_below(
            conn, ver, (_MIGRATIONS / fname).read_text(encoding="utf-8")
        )
        conn.commit()

    conn.close()
    return state_dir


def _seed_done_track(state_dir: Path, track_id: str, *, phase: str) -> None:
    """Track whose work is terminal (completed dispatch, no pr_ref) => derived 'done'."""
    tracks_lib.create_track(
        state_dir, track_id, PROJECT_ID, title=track_id, goal_state="ship", phase=phase
    )
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track) VALUES (?,?,?,?)",
        (f"D-{track_id}", PROJECT_ID, "completed", track_id),
    )
    conn.commit()
    conn.close()


def _phase(state_dir: Path, track_id: str) -> str:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT phase FROM tracks WHERE track_id=? AND project_id=?",
        (track_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return row[0] if row else ""


def _derived_status(state_dir: Path, track_id: str) -> "str | None":
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    row = conn.execute(
        "SELECT derived_status FROM tracks WHERE track_id=? AND project_id=?",
        (track_id, PROJECT_ID),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _history(state_dir: Path, track_id: str) -> list:
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    rows = conn.execute(
        "SELECT from_phase, to_phase, actor, approval_id "
        "FROM track_phase_history "
        "WHERE track_id=? AND project_id=? ORDER BY rowid",
        (track_id, PROJECT_ID),
    ).fetchall()
    conn.close()
    return [
        {"from_phase": r[0], "to_phase": r[1], "actor": r[2], "approval_id": r[3]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# evidence=None path
# ---------------------------------------------------------------------------

def test_none_evidence_derived_done_walks_to_done(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-active", phase="active")
    result = track_reconciler.close_track_if_done(
        sd, "T-active", PROJECT_ID, actor="operator", approval_id="APR-1"
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-active") == "done"


def test_none_evidence_derived_not_done_noop_not_terminal(tmp_path):
    sd = _build_db(tmp_path)
    # No dispatches => derived='queued' (not terminal) => noop_not_terminal.
    tracks_lib.create_track(sd, "T-q", PROJECT_ID, title="x", goal_state="y", phase="queued")
    result = track_reconciler.close_track_if_done(
        sd, "T-q", PROJECT_ID, actor="operator", approval_id="X"
    )
    assert result["action"] == "noop_not_terminal"
    assert result["applied"] is False
    assert _phase(sd, "T-q") == "queued"


def test_none_evidence_parked_without_include_flag_rejected_parked(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-parked", phase="parked")
    result = track_reconciler.close_track_if_done(
        sd, "T-parked", PROJECT_ID, actor="operator", approval_id="X"
    )
    assert result["action"] == "rejected_parked"
    assert result["applied"] is False
    assert _phase(sd, "T-parked") == "parked"


# ---------------------------------------------------------------------------
# Revalidation: stale_candidate on pr_ref mismatch
# ---------------------------------------------------------------------------

def test_revalidation_pr_ref_changed_stale_candidate(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-pr", phase="active")

    # Set pr_ref="#994" AND add a pr_merged coordination event so reconcile still
    # derives "done" (all-terminal dispatches + pr_merged event = done).
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET pr_ref=? WHERE track_id=? AND project_id=?",
        ("#994", "T-pr", PROJECT_ID),
    )
    conn.execute(
        "INSERT INTO coordination_events "
        "(event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES ('ev-994','pr_merged','dispatch',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        ("D-T-pr", PROJECT_ID),
    )
    conn.commit()
    conn.close()

    # Snapshot carries a DIFFERENT pr_ref -> stale candidate.
    evidence = {"pr_ref": "#993", "verified_at": "2026-07-04T10:00:00Z"}
    result = track_reconciler.close_track_if_done(
        sd, "T-pr", PROJECT_ID, actor="system", evidence=evidence
    )
    assert result["action"] == "stale_candidate"
    assert result["applied"] is False
    assert _phase(sd, "T-pr") == "active"  # no write


# ---------------------------------------------------------------------------
# Revalidation: stale_candidate causes no derived_status write (pr_ref variant)
# ---------------------------------------------------------------------------

def test_revalidation_stale_causes_no_derived_write(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-pr-stale", phase="active")

    # Set pr_ref="#994" in DB + add pr_merged event so reconcile would derive "done".
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "UPDATE tracks SET pr_ref=? WHERE track_id=? AND project_id=?",
        ("#994", "T-pr-stale", PROJECT_ID),
    )
    conn.execute(
        "INSERT INTO coordination_events "
        "(event_id, event_type, entity_type, entity_id, occurred_at, project_id) "
        "VALUES ('ev-994','pr_merged','dispatch',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),?)",
        ("D-T-pr-stale", PROJECT_ID),
    )
    conn.commit()
    conn.close()

    # Evidence snapshot carries a different pr_ref -> stale candidate.
    evidence = {"pr_ref": "#993", "verified_at": "2026-07-04T10:00:00Z"}
    result = track_reconciler.close_track_if_done(
        sd, "T-pr-stale", PROJECT_ID, actor="system", evidence=evidence
    )
    assert result["action"] == "stale_candidate"
    assert result["applied"] is False
    assert _phase(sd, "T-pr-stale") == "active"
    assert _derived_status(sd, "T-pr-stale") is None   # reconcile_track was not called


# ---------------------------------------------------------------------------
# Revalidation: stale_candidate on blocker OI
# ---------------------------------------------------------------------------

def test_revalidation_blocker_oi_appeared_stale_candidate(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-blocked", phase="active")

    # Snapshot was taken before the blocker appeared (pr_ref=None matches current row).
    evidence = {"pr_ref": None, "verified_at": "2026-07-04T10:00:00Z"}

    # Blocker OI arrives AFTER nomination — post-nomination change.
    conn = sqlite3.connect(str(sd / "runtime_coordination.db"))
    conn.execute(
        "INSERT INTO track_open_items "
        "(track_id, project_id, oi_id, link_type, link_source) "
        "VALUES (?,?,?,'blocks','manual')",
        ("T-blocked", PROJECT_ID, "OI-001"),
    )
    conn.commit()
    conn.close()

    result = track_reconciler.close_track_if_done(
        sd, "T-blocked", PROJECT_ID, actor="system", evidence=evidence
    )
    assert result["action"] == "stale_candidate"
    assert result["applied"] is False
    assert _phase(sd, "T-blocked") == "active"          # phase unchanged
    assert _derived_status(sd, "T-blocked") is None     # reconcile_track was not called


# ---------------------------------------------------------------------------
# Revalidation: clean -> closes, records actor + approval_id
# ---------------------------------------------------------------------------

def test_revalidation_clean_closes_and_records_actor_and_approval(tmp_path):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-ok", phase="active")

    # Snapshot matches current DB state exactly (no pr_ref, no blockers).
    evidence = {"pr_ref": None, "verified_at": "2026-07-04T10:00:00Z"}
    result = track_reconciler.close_track_if_done(
        sd, "T-ok", PROJECT_ID,
        actor="T0",
        approval_id="AUTO-1",
        evidence=evidence,
    )
    assert result["action"] == "closed"
    assert result["applied"] is True
    assert _phase(sd, "T-ok") == "done"

    hist = _history(sd, "T-ok")
    assert len(hist) == 1
    assert hist[0]["actor"] == "T0"
    assert hist[0]["approval_id"] == "AUTO-1"


# ---------------------------------------------------------------------------
# Mid-walk failure resumability
# ---------------------------------------------------------------------------

def test_mid_walk_failure_leaves_intermediate_and_is_resumable(tmp_path, monkeypatch):
    sd = _build_db(tmp_path)
    _seed_done_track(sd, "T-q2", phase="queued")  # queued -> active -> done (two steps)

    real = tracks_lib.transition_phase

    def flaky(state_dir, tid, pid, to_phase, **kw):
        if to_phase == "done":
            raise tracks_lib.InvalidTransitionError("injected mid-walk failure")
        return real(state_dir, tid, pid, to_phase, **kw)

    monkeypatch.setattr(tracks_lib, "transition_phase", flaky)

    result = track_reconciler.close_track_if_done(
        sd, "T-q2", PROJECT_ID, actor="operator", approval_id="A"
    )
    assert result["action"] == "rejected_walk_failed"
    assert _phase(sd, "T-q2") == "active"  # non-atomic: stuck at intermediate phase

    # Recovery: re-call resumes walk from the current declared phase.
    monkeypatch.setattr(tracks_lib, "transition_phase", real)
    result2 = track_reconciler.close_track_if_done(
        sd, "T-q2", PROJECT_ID, actor="operator", approval_id="A"
    )
    assert result2["action"] == "closed"
    assert _phase(sd, "T-q2") == "done"
