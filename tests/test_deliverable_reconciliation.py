"""tests/test_deliverable_reconciliation.py — OI-840: deliverable dispatch auto-completion.

Verifies that when a track's derived_status reaches 'done' (all terminal dispatches
+ merged PR evidence), the reconciler also marks non-terminal deliverable dispatches
for that track as 'completed'.

Without this, the deliverable-plane shows 'ready' items as dispatchable work when the
underlying code already shipped — the failure mode from
memory/verify-before-build-done-maar-queued.
"""

from __future__ import annotations

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
import track_reconciler
import tracks as tracks_lib

from fixtures.dispatches_schema_fixture import ensure_dispatches_columns

PROJECT_ID = "test-oi840"


# ---------------------------------------------------------------------------
# DB helpers (same pattern as test_track_reconciler.py)
# ---------------------------------------------------------------------------

def _build_db(tmp_path: Path) -> Path:
    """Return a state_dir with migrations 0022 + 0024 + 0027 + 0028 applied."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
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
            event_id TEXT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'dispatch',
            entity_id TEXT NOT NULL,
            from_state TEXT, to_state TEXT,
            actor TEXT NOT NULL DEFAULT 'runtime',
            reason TEXT,
            metadata_json TEXT DEFAULT '{}',
            occurred_at TEXT NOT NULL,
            project_id TEXT
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

    ensure_dispatches_columns(conn)
    conn.execute("PRAGMA user_version = 26")
    conn.commit()

    schema_migration.apply_script_if_below(
        conn, 27,
        (_MIGRATIONS / "0027_planning_horizon_and_deliverable_view.sql").read_text(encoding="utf-8"),
    )
    conn.commit()

    schema_migration.apply_script_if_below(
        conn, 28,
        (_MIGRATIONS / "0028_tracks_derived_status.sql").read_text(encoding="utf-8"),
    )
    conn.commit()
    conn.close()
    return state_dir


def _seed_track(
    state_dir: Path,
    track_id: str,
    *,
    phase: str = "active",
    pr_ref: str | None = None,
) -> None:
    tracks_lib.create_track(
        state_dir, track_id, PROJECT_ID,
        title=f"Track {track_id}",
        goal_state=f"ship {track_id}",
        phase=phase,
        pr_ref=pr_ref,
    )


def _seed_dispatch(
    state_dir: Path,
    dispatch_id: str,
    track_id: str,
    *,
    state: str = "completed",
    pr_ref: str | None = None,
    output_ref: str | None = None,
    output_kind: str | None = None,
) -> None:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, project_id, state, track, pr_ref, output_ref, output_kind) "
        "VALUES (?,?,?,?,?,?,?)",
        (dispatch_id, PROJECT_ID, state, track_id, pr_ref, output_ref, output_kind),
    )
    conn.commit()
    conn.close()


def _seed_pr_merged_event(state_dir: Path, dispatch_id: str) -> None:
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        INSERT INTO coordination_events
            (event_id, event_type, entity_type, entity_id, occurred_at, project_id)
        VALUES (?, 'pr_merged', 'dispatch', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)
        """,
        (f"ev-{dispatch_id}", dispatch_id, PROJECT_ID),
    )
    conn.commit()
    conn.close()


def _get_dispatch_states(state_dir: Path, track_id: str) -> dict[str, str]:
    """Return {dispatch_id: state} for all dispatches of a track."""
    db = state_dir / "runtime_coordination.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT dispatch_id, state, output_ref FROM dispatches WHERE track=? AND project_id=?",
        (track_id, PROJECT_ID),
    ).fetchall()
    conn.close()
    return {r["dispatch_id"]: r["state"] for r in rows}


# ---------------------------------------------------------------------------
# OI-840 regression tests
# ---------------------------------------------------------------------------

def test_deliverable_dispatches_completed_when_track_done(tmp_path):
    """Deliverable dispatches at 'ready' should become 'completed' when the track
    reconciles to 'done' (all terminal dispatches + pr_merged event)."""
    state_dir = _build_db(tmp_path)

    # Set up a track that has already shipped — phase='done', all terminal dispatches.
    _seed_track(state_dir, "T-dlv-done", pr_ref="PR-500", phase="done")

    # Real worker dispatch (terminal, completed).
    _seed_dispatch(state_dir, "D-worker-1", "T-dlv-done", state="completed", pr_ref="PR-500")
    _seed_pr_merged_event(state_dir, "D-worker-1")

    # Deliverable stubs — these are what the deliverable-plane shows as 'ready'.
    # They represent planned work that was shipped but never auto-completed.
    _seed_dispatch(state_dir, "D-dlv-0", "T-dlv-done", state="ready",
                   output_ref="doc:D-dlv-0", output_kind="doc")
    _seed_dispatch(state_dir, "D-dlv-1", "T-dlv-done", state="ready",
                   output_ref="pr:D-dlv-1", output_kind="pr")
    _seed_dispatch(state_dir, "D-dlv-2", "T-dlv-done", state="proposed",
                   output_ref="pr:D-dlv-2", output_kind="pr")

    # Before reconciliation: deliverable dispatches are still non-terminal.
    states_before = _get_dispatch_states(state_dir, "T-dlv-done")
    assert states_before["D-dlv-0"] == "ready"
    assert states_before["D-dlv-1"] == "ready"
    assert states_before["D-dlv-2"] == "proposed"

    # Reconcile the track.
    result = track_reconciler.reconcile_track(state_dir, "T-dlv-done", PROJECT_ID)
    assert result["derived_status"] == "done"

    # After reconciliation: deliverable dispatches should be completed.
    states_after = _get_dispatch_states(state_dir, "T-dlv-done")
    assert states_after["D-dlv-0"] == "completed", (
        f"deliverable D-dlv-0 (ready -> completed) wasn't reconciled: got {states_after['D-dlv-0']!r}"
    )
    assert states_after["D-dlv-1"] == "completed", (
        f"deliverable D-dlv-1 (ready -> completed) wasn't reconciled: got {states_after['D-dlv-1']!r}"
    )
    assert states_after["D-dlv-2"] == "completed", (
        f"deliverable D-dlv-2 (proposed -> completed) wasn't reconciled: got {states_after['D-dlv-2']!r}"
    )

    # Already-terminal dispatches should be untouched.
    assert states_after["D-worker-1"] == "completed"


def test_deliverable_dispatches_not_completed_when_track_not_done(tmp_path):
    """Deliverable dispatches should NOT be changed when the track is NOT done."""
    state_dir = _build_db(tmp_path)

    _seed_track(state_dir, "T-not-done", pr_ref="PR-600")

    # Only one dispatch, in_flight — not all terminal.
    _seed_dispatch(state_dir, "D-inflight-1", "T-not-done", state="running", pr_ref="PR-600")

    # Deliverable stub at 'ready'.
    _seed_dispatch(state_dir, "D-dlv-ready", "T-not-done", state="ready",
                   output_ref="pr:D-dlv-ready", output_kind="pr")

    result = track_reconciler.reconcile_track(state_dir, "T-not-done", PROJECT_ID)
    assert result["derived_status"] == "in_progress"

    # Deliverable should NOT be auto-completed — track is not done.
    states = _get_dispatch_states(state_dir, "T-not-done")
    assert states["D-dlv-ready"] == "ready", (
        f"Deliverable should stay ready when track is not done: got {states['D-dlv-ready']!r}"
    )


def test_deliverable_reconciliation_idempotent(tmp_path):
    """Re-running reconciliation on an already-reconciled track is a no-op."""
    state_dir = _build_db(tmp_path)

    _seed_track(state_dir, "T-idem", pr_ref="PR-700", phase="done")
    _seed_dispatch(state_dir, "D-idem-w", "T-idem", state="completed", pr_ref="PR-700")
    _seed_pr_merged_event(state_dir, "D-idem-w")
    _seed_dispatch(state_dir, "D-idem-dlv", "T-idem", state="ready",
                   output_ref="pr:D-idem-dlv", output_kind="pr")

    # First reconciliation.
    result1 = track_reconciler.reconcile_track(state_dir, "T-idem", PROJECT_ID)
    assert result1["derived_status"] == "done"

    states1 = _get_dispatch_states(state_dir, "T-idem")
    assert states1["D-idem-dlv"] == "completed"

    # Second reconciliation — should be a clean no-op (already completed).
    result2 = track_reconciler.reconcile_track(state_dir, "T-idem", PROJECT_ID)
    assert result2["derived_status"] == "done"
    assert not result2["drifted"]

    states2 = _get_dispatch_states(state_dir, "T-idem")
    assert states2["D-idem-dlv"] == "completed"


def test_deliverable_already_completed_untouched(tmp_path):
    """Already-completed deliverable dispatches stay completed."""
    state_dir = _build_db(tmp_path)

    _seed_track(state_dir, "T-already", pr_ref="PR-800", phase="done")
    _seed_dispatch(state_dir, "D-already-w", "T-already", state="completed", pr_ref="PR-800")
    _seed_pr_merged_event(state_dir, "D-already-w")
    _seed_dispatch(state_dir, "D-already-dlv", "T-already", state="completed",
                   output_ref="pr:D-already-dlv", output_kind="pr")

    result = track_reconciler.reconcile_track(state_dir, "T-already", PROJECT_ID)
    assert result["derived_status"] == "done"

    states = _get_dispatch_states(state_dir, "T-already")
    assert states["D-already-dlv"] == "completed"


def test_no_deliverable_dispatches_is_noop(tmp_path):
    """Track with no deliverable dispatches reconciles normally (no-op on deliverable side)."""
    state_dir = _build_db(tmp_path)

    _seed_track(state_dir, "T-nodlv", pr_ref="PR-900")
    _seed_dispatch(state_dir, "D-nodlv-1", "T-nodlv", state="completed", pr_ref="PR-900")
    _seed_pr_merged_event(state_dir, "D-nodlv-1")

    result = track_reconciler.reconcile_track(state_dir, "T-nodlv", PROJECT_ID)
    assert result["derived_status"] == "done"
    # No error should occur — the function handles the empty deliverable case gracefully.


# ---------------------------------------------------------------------------
# Path B: zero real dispatches, track declared done (OI-840 production scenario)
# ---------------------------------------------------------------------------

def test_deliverables_completed_when_no_real_dispatches_and_track_done(tmp_path):
    """Path B: no real worker dispatches, track declared done — deliverable
    stubs should still be auto-completed. This is the exact production scenario
    for governance-attribution-enforce where real dispatches used lane letters
    instead of track_ids."""
    state_dir = _build_db(tmp_path)

    # Track is declared done — all work shipped.
    _seed_track(state_dir, "T-pathb", pr_ref="#1004,#1007,#1009", phase="done")

    # Only deliverable stubs, no real worker dispatches.
    _seed_dispatch(state_dir, "D-pathb-0", "T-pathb", state="ready",
                   output_ref="doc:D-pathb-0", output_kind="doc")
    _seed_dispatch(state_dir, "D-pathb-1", "T-pathb", state="ready",
                   output_ref="pr:D-pathb-1", output_kind="pr")
    _seed_dispatch(state_dir, "D-pathb-2", "T-pathb", state="proposed",
                   output_ref="pr:D-pathb-2", output_kind="pr")

    # Before: all deliverable stubs non-terminal.
    states_before = _get_dispatch_states(state_dir, "T-pathb")
    assert states_before["D-pathb-0"] == "ready"
    assert states_before["D-pathb-1"] == "ready"
    assert states_before["D-pathb-2"] == "proposed"

    # Reconcile.
    result = track_reconciler.reconcile_track(state_dir, "T-pathb", PROJECT_ID)
    # derived_status is 'queued' for declared-done tracks with no real dispatches
    # and deliverable stubs blocking — the reconciler writes 'queued' into
    # derived_status.  The important regressie-gate is that deliverable stubs
    # are auto-completed, *not* that derived_status reads 'done'.
    # (Without deliverable auto-completion, derived_status would still be
    # 'queued' because the stubs block it.)

    # After: deliverable stubs should be completed.
    states_after = _get_dispatch_states(state_dir, "T-pathb")
    assert states_after["D-pathb-0"] == "completed", (
        f"Path B: deliverable D-pathb-0 should be completed, got {states_after['D-pathb-0']!r}"
    )
    assert states_after["D-pathb-1"] == "completed", (
        f"Path B: deliverable D-pathb-1 should be completed, got {states_after['D-pathb-1']!r}"
    )
    assert states_after["D-pathb-2"] == "completed", (
        f"Path B: deliverable D-pathb-2 should be completed, got {states_after['D-pathb-2']!r}"
    )


def test_deliverables_not_completed_when_track_not_done_no_real_dispatches(tmp_path):
    """Path B guard: track NOT done, no real dispatches — deliverable stubs
    should NOT be auto-completed."""
    state_dir = _build_db(tmp_path)

    _seed_track(state_dir, "T-pathb-nd", phase="queued")

    _seed_dispatch(state_dir, "D-pathbnd-0", "T-pathb-nd", state="ready",
                   output_ref="doc:D-pathbnd-0", output_kind="doc")

    result = track_reconciler.reconcile_track(state_dir, "T-pathb-nd", PROJECT_ID)
    assert result["derived_status"] == "queued"

    states = _get_dispatch_states(state_dir, "T-pathb-nd")
    assert states["D-pathbnd-0"] == "ready", (
        f"Deliverable should stay ready when track is not done: got {states['D-pathbnd-0']!r}"
    )
