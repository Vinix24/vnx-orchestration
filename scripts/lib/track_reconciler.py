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

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional

log = logging.getLogger(__name__)

DB_FILENAME = "runtime_coordination.db"

TERMINAL_DISPATCH_STATES = frozenset({"completed", "expired", "dead_letter"})
IN_FLIGHT_DISPATCH_STATES = frozenset({
    "queued", "claimed", "delivering", "accepted", "running", "active",
})


def _parse_pr_number(pr_ref: Optional[str]) -> Optional[int]:
    """Parse '#756', '756', or '  #42  ' -> integer. Returns None on failure."""
    if not pr_ref:
        return None
    try:
        return int(str(pr_ref).strip().lstrip("#").strip())
    except (TypeError, ValueError):
        return None


def _load_merged_pr_numbers(state_dir: str | Path) -> FrozenSet[int]:
    """Load confirmed-merged PR numbers from local sources only (no network).

    Sources (all optional; errors silently ignored):
      1. {state_dir}/../events/pr_merged.ndjson  — ADR-005 event ledger
      2. {state_dir}/t0_receipts.ndjson           — receipt log
      3. {state_dir}/../../ROADMAP.yaml           — authoritative feature list
         pr_queue[*].status=merged entries cover recent PRs not yet in NDJSON files

    Returns frozenset[int] of confirmed-merged PR numbers.
    Deterministic for a given file state; never calls gh or any network.
    """
    merged: set = set()
    state_path = Path(state_dir)

    # Sources 1 + 2: scan NDJSON files for event_type='pr_merged' records with pr_number
    ndjson_candidates = [
        state_path.parent / "events" / "pr_merged.ndjson",
        state_path / "t0_receipts.ndjson",
    ]
    for path in ndjson_candidates:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = rec.get("event_type") or rec.get("event") or ""
                if event == "pr_merged":
                    pn = rec.get("pr_number")
                    if pn is not None:
                        try:
                            merged.add(int(pn))
                        except (TypeError, ValueError):
                            pass
        except OSError:
            pass

    # Source 3: ROADMAP.yaml — authoritative for recent PRs not yet backfilled to NDJSON.
    # Path: state_dir is .vnx-data/state; ROADMAP.yaml is at project root two levels up.
    roadmap_path = state_path.parent.parent / "ROADMAP.yaml"
    try:
        import yaml  # available in all VNX environments
        data = yaml.safe_load(roadmap_path.read_text(encoding="utf-8")) or {}
        for feat in (data.get("features") or []):
            for pr in (feat.get("pr_queue") or []):
                if (pr.get("status") or "") == "merged":
                    pn = _parse_pr_number(pr.get("pr_id"))
                    if pn is not None:
                        merged.add(pn)
    except OSError:
        pass
    except Exception:
        log.debug("_load_merged_pr_numbers: ROADMAP.yaml parse error (non-fatal)", exc_info=True)

    return frozenset(merged)


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
    merged_pr_numbers: FrozenSet[int] = frozenset(),
) -> str:
    """Compute derived_status for one track. Pure read — writes nothing.

    Returns one of: 'done', 'blocked', 'in_progress', 'queued'.

    merged_pr_numbers: confirmed-merged PR numbers from local sources. Used in
    the additive pr_ref evidence path — derives 'done' for tracks where the
    dispatch join yields no rows (historical dispatches used 'A'/'B'/'C' track
    labels instead of feature track_ids). The existing dispatch-based derivation
    is unchanged; this path only fires when dispatches is empty.
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

    # 3. Fetch track's pr_ref once (reused in both the zero-dispatch and all-terminal paths).
    track_row = conn.execute(
        "SELECT pr_ref FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    track_pr_ref = track_row["pr_ref"] if track_row else None

    # 4. Dispatch state aggregation.
    dispatches = conn.execute(
        "SELECT dispatch_id, state FROM dispatches WHERE track = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchall()

    if not dispatches:
        # pr_ref evidence path: covers tracks with no matching dispatches.
        # Historical dispatches stored 'A'/'B'/'C' in the track column instead of
        # feature track_ids, so the join above is empty for all pre-1.0 tracks.
        # If the track's own pr_ref is confirmed merged via local evidence (NDJSON
        # ledger or ROADMAP.yaml), derive 'done' without a dispatch match.
        pr_num = _parse_pr_number(track_pr_ref)
        if pr_num is not None and pr_num in merged_pr_numbers:
            return "done"
        return "queued"

    states = [d[0] for d in dispatches]  # d is (dispatch_id, state); note row_factory gives dict
    # With row_factory = sqlite3.Row, index by name or position:
    dispatch_ids = [row["dispatch_id"] for row in dispatches]
    state_values = [row["state"] for row in dispatches]

    all_terminal = all(s in TERMINAL_DISPATCH_STATES for s in state_values)

    if all_terminal:
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
    *,
    _merged_pr_numbers: Optional[FrozenSet[int]] = None,
) -> Dict[str, Any]:
    """Compute and persist derived_status for a single track.

    Returns a result dict with track_id, project_id, derived_status,
    declared_phase, and drifted flag.

    Raises RuntimeError if derived_status column is absent (migration 0028
    must be applied first).

    _merged_pr_numbers: internal kwarg — pre-loaded merged PR set. When None,
    _load_merged_pr_numbers(state_dir) is called. Pass a pre-loaded set when
    calling from reconcile_all_tracks to avoid per-track file I/O.
    """
    conn = _get_conn(state_dir)
    try:
        if not _has_col(conn, "tracks", "derived_status"):
            raise RuntimeError(
                "tracks.derived_status column absent; apply migration 0028 first."
            )

        merged = (
            _merged_pr_numbers
            if _merged_pr_numbers is not None
            else _load_merged_pr_numbers(state_dir)
        )
        derived = _compute_derived_status(conn, track_id, project_id, merged)
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

    merged_pr_numbers = _load_merged_pr_numbers(state_dir)

    results = []
    for track_id in track_ids:
        result = reconcile_track(
            state_dir, track_id, project_id,
            _merged_pr_numbers=merged_pr_numbers,
        )
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
