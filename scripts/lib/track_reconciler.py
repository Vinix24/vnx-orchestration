#!/usr/bin/env python3
"""track_reconciler.py — advisory rollup reconciler for the track layer (Phase 3).

Reads dispatch/event state and computes a per-track derived_status.

ADVISORY ONLY — hard contract:
  - Writes ONLY tracks.derived_status (never tracks.phase).
  - Never touches ROADMAP.yaml.
  - Never auto-advances any track.

Idempotent and replay-safe:
  - Re-running over the same DB state produces the same derived_status.
  - A duplicate pr_merged coordination event cannot double-advance a track
    (presence check, not counter).
  - Terminal dispatch states are irreversible; they cannot regress.

VNX_ROADMAP_AUTOPILOT gate: this module is always callable; the gate lives
in roadmap_manager.RoadmapManager.reconcile_tracks() for autopilot integration.

ADR-007: all queries are (track_id, project_id)-scoped.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DB_FILENAME = "runtime_coordination.db"

TERMINAL_DISPATCH_STATES = frozenset({"completed", "expired", "dead_letter"})
IN_FLIGHT_DISPATCH_STATES = frozenset({
    "queued", "claimed", "delivering", "accepted", "running", "active",
})


def _get_conn(state_dir: str | Path) -> sqlite3.Connection:
    db_path = Path(state_dir) / DB_FILENAME
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _compute_derived_status(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
) -> str:
    """Compute derived_status for one track. Pure read — writes nothing.

    Returns one of: 'done', 'blocked', 'in_progress', 'queued'.
    """
    # 1. Blocker open-item check: any link_type='blocks' row → blocked.
    #    The presence of the link implies the OI is unresolved; removal clears it.
    if _has_col(conn, "track_open_items", "project_id"):
        blocker = conn.execute(
            """
            SELECT 1 FROM track_open_items
            WHERE track_id = ? AND project_id = ? AND link_type = 'blocks'
            LIMIT 1
            """,
            (track_id, project_id),
        ).fetchone()
    else:
        blocker = conn.execute(
            "SELECT 1 FROM track_open_items WHERE track_id = ? AND link_type = 'blocks' LIMIT 1",
            (track_id,),
        ).fetchone()
    if blocker:
        return "blocked"

    # 2. Dependency check: any dependency whose declared phase is not 'done' blocks this track.
    #    Uses declared phase (authoritative) to avoid circular dependency on derived_status.
    dep_phases = conn.execute(
        """
        SELECT t.phase
        FROM track_dependencies td
        JOIN tracks t
          ON t.track_id = td.to_track_id AND t.project_id = td.to_project_id
        WHERE td.from_track_id = ? AND td.from_project_id = ?
        """,
        (track_id, project_id),
    ).fetchall()
    for row in dep_phases:
        if row[0] != "done":
            return "blocked"

    # 3. Dispatch state aggregation.
    dispatches = conn.execute(
        "SELECT dispatch_id, state FROM dispatches WHERE track = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchall()

    if not dispatches:
        return "queued"

    states = [d[0] for d in dispatches]  # d is (dispatch_id, state); note row_factory gives dict
    # With row_factory = sqlite3.Row, index by name or position:
    dispatch_ids = [row["dispatch_id"] for row in dispatches]
    state_values = [row["state"] for row in dispatches]

    all_terminal = all(s in TERMINAL_DISPATCH_STATES for s in state_values)

    if all_terminal:
        track_row = conn.execute(
            "SELECT pr_ref FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        track_pr_ref = track_row["pr_ref"] if track_row else None

        if not track_pr_ref:
            # No PR to verify — all work terminal → done.
            return "done"

        # Check for a pr_merged coordination event on any dispatch in this track.
        placeholders = ",".join("?" * len(dispatch_ids))
        merged_event = conn.execute(
            f"""
            SELECT 1 FROM coordination_events
            WHERE event_type = 'pr_merged'
              AND entity_id IN ({placeholders})
            LIMIT 1
            """,
            dispatch_ids,
        ).fetchone()
        if merged_event:
            return "done"

        # All dispatches terminal but PR not confirmed merged yet.
        return "in_progress"

    # Some dispatches still in flight.
    if any(s in IN_FLIGHT_DISPATCH_STATES for s in state_values):
        return "in_progress"

    # Remaining dispatches are in planned states (proposed, ready).
    return "queued"


def _write_derived_status(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
    derived: str,
) -> None:
    """Write derived_status for one track. Raises if derived_status column absent."""
    conn.execute(
        "UPDATE tracks SET derived_status = ? WHERE track_id = ? AND project_id = ?",
        (derived, track_id, project_id),
    )


def _log_drift(
    track_id: str,
    project_id: str,
    declared: Optional[str],
    derived: str,
) -> None:
    if declared != derived:
        log.info(
            "track_drift: track=%s project=%s declared=%s derived=%s",
            track_id, project_id, declared, derived,
        )


def reconcile_track(
    state_dir: str | Path,
    track_id: str,
    project_id: str,
) -> Dict[str, Any]:
    """Compute and persist derived_status for a single track.

    Returns a result dict with track_id, project_id, derived_status,
    declared_phase, and drifted flag.

    Raises RuntimeError if derived_status column is absent (migration 0028
    must be applied first).
    """
    conn = _get_conn(state_dir)
    try:
        if not _has_col(conn, "tracks", "derived_status"):
            raise RuntimeError(
                "tracks.derived_status column absent; apply migration 0028 first."
            )

        derived = _compute_derived_status(conn, track_id, project_id)
        _write_derived_status(conn, track_id, project_id, derived)
        conn.commit()

        track_row = conn.execute(
            "SELECT phase FROM tracks WHERE track_id = ? AND project_id = ?",
            (track_id, project_id),
        ).fetchone()
        declared = track_row["phase"] if track_row else None

        _log_drift(track_id, project_id, declared, derived)

        return {
            "track_id": track_id,
            "project_id": project_id,
            "derived_status": derived,
            "declared_phase": declared,
            "drifted": declared != derived,
        }
    finally:
        conn.close()


def reconcile_all_tracks(
    state_dir: str | Path,
    project_id: str,
) -> List[Dict[str, Any]]:
    """Compute and persist derived_status for all tracks in project_id.

    Idempotent: re-running produces the same results for the same DB state.
    Returns list of per-track result dicts (see reconcile_track).

    Raises RuntimeError if derived_status column is absent (migration 0028
    must be applied first).
    """
    conn = _get_conn(state_dir)
    try:
        if not _has_col(conn, "tracks", "derived_status"):
            raise RuntimeError(
                "tracks.derived_status column absent; apply migration 0028 first."
            )

        tracks = conn.execute(
            "SELECT track_id FROM tracks WHERE project_id = ? ORDER BY sort_order ASC, track_id ASC",
            (project_id,),
        ).fetchall()
        track_ids = [r["track_id"] for r in tracks]
    finally:
        conn.close()

    results = []
    for track_id in track_ids:
        result = reconcile_track(state_dir, track_id, project_id)
        results.append(result)
        log.debug(
            "reconciled track=%s derived=%s declared=%s drift=%s",
            track_id, result["derived_status"], result["declared_phase"], result["drifted"],
        )

    log.info(
        "track_reconciler: project=%s tracks=%d drifted=%d",
        project_id, len(results), sum(1 for r in results if r["drifted"]),
    )
    return results
