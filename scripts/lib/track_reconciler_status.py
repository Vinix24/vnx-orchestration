#!/usr/bin/env python3
"""track_reconciler_status.py — derived-status computation for a single track.

Pure move (fase 1 PR 1, track file-size-refactor-debt): _compute_derived_status
lived in track_reconciler.py (lines 228-371 on main) and moved here unchanged.
track_reconciler.py re-exports the name so no consumer has to change.

The helper imports sit at the BOTTOM of this module, after the function
definition, to break the import cycle with track_reconciler.py (which imports
this module for the re-export). Function globals resolve at call time, not at
definition time, so the names need not exist while this module is first being
imported — only when _compute_derived_status is actually called, by which
point track_reconciler is fully initialised.
"""

from __future__ import annotations

import sqlite3
from typing import FrozenSet


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
    # 1. Blocker open-item check: any link_type='blocks' row with resolved_at IS NULL → blocked.
    #    Migration 0030 adds resolved_at; when present, only unresolved rows are counted.
    #    Pre-0030 databases have no resolved_at column — fall back to presence-only check.
    has_project_id_col = _has_col(conn, "track_open_items", "project_id")
    has_resolved_at_col = _has_col(conn, "track_open_items", "resolved_at")
    if has_project_id_col and has_resolved_at_col:
        blocker = conn.execute(
            """
            SELECT 1 FROM track_open_items
            WHERE track_id = ? AND project_id = ? AND link_type = 'blocks'
              AND resolved_at IS NULL
            LIMIT 1
            """,
            (track_id, project_id),
        ).fetchone()
    elif has_project_id_col:
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

    # 3. Fetch track's pr_ref and declared phase once (reused below).
    track_row = conn.execute(
        "SELECT pr_ref, phase FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    track_pr_ref = track_row["pr_ref"] if track_row else None
    track_phase = track_row["phase"] if track_row else None

    # OI-1098: delivery hold. One explicit non-'complete' delivery marking on
    # any currently-linked PR vetoes every PR-EVIDENCE-based 'done' below
    # (merged coordination event, pr_ref merged-subset). Declared-phase
    # evidence is NOT vetoed: a human-declared 'done' stays authoritative, and
    # the no-pr_ref path has nothing to mark. Unmarked legacy PRs (no row)
    # do not hold — see _delivery_hold for the measured motivation.
    hold = _delivery_hold(conn, track_id, project_id, track_pr_ref)

    # 4. Dispatch state aggregation.
    dispatches = conn.execute(
        "SELECT dispatch_id, state FROM dispatches WHERE track = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchall()

    if not dispatches:
        # pr_ref evidence path: covers tracks with no matching dispatches.
        # Historical dispatches stored 'A'/'B'/'C' in the track column instead of
        # feature track_ids, so the join above is empty for all pre-1.0 tracks.
        # If the track's own pr_ref (single or a '#911,#912' multi-PR list) is
        # confirmed merged via all evidence sources, derive 'done' without a
        # dispatch match. ALL parsed PRs must be merged; partial merge = not done.
        nums = _parse_pr_numbers(track_pr_ref)
        if nums and nums <= merged_pr_numbers and hold is None:
            return "done"
        # Absence of evidence is not evidence of queued. Historical dispatches may
        # be archived, so defer to declared phase (2026-06-15 migration panel).
        if track_phase == "done":
            return "done"
        if track_phase == "active":
            return "in_progress"
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
        if merged_event and hold is None:
            return "done"

        # Declared-done stability: a declared-done track with all terminal dispatches
        # stays done even when PR evidence is incomplete (partial multi-PR merge or
        # no coordination event). Blocker/dependency checks still win (run above).
        if track_phase == "done":
            return "done"

        # Also accept the track's own pr_ref being confirmed merged via all
        # evidence sources (NDJSON / ROADMAP / git) — same as the no-dispatch path.
        # ALL parsed PRs must be merged; partial merge = not done.
        nums = _parse_pr_numbers(track_pr_ref)
        if nums and nums <= merged_pr_numbers and hold is None:
            return "done"

        # All dispatches terminal but PR not confirmed merged yet (or a
        # delivery hold vetoed the PR-evidence 'done' — OI-1098).
        return "in_progress"

    # Some dispatches still in flight.
    if any(s in IN_FLIGHT_DISPATCH_STATES for s in state_values):
        return "in_progress"

    # Remaining dispatches are in planned states (proposed, ready).
    return "queued"


# Late binding of the helpers this function uses from its old home module.
# Deliberately after the function definition (see module docstring): importing
# them at the top would deadlock the re-export cycle with track_reconciler.py.
from track_reconciler import (  # noqa: E402
    IN_FLIGHT_DISPATCH_STATES,
    TERMINAL_DISPATCH_STATES,
    _delivery_hold,
    _has_col,
    _parse_pr_numbers,
)
