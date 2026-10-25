#!/usr/bin/env python3
"""track_reconciler_revalidation.py — close-time revalidation for a nominated track.

Extracted unchanged (track slop-refactor-panel-review) from
track_reconciler_closure.close_track_if_done, which now only opens the
connection and returns the rejection. Checks run in this order on a fresh DB
read: (a) pr_ref unchanged vs the nomination snapshot, (b) no unresolved
blocker OI, (c) declared phase still eligible (queued/active; parked only with
include_parked), (d) gh evidence authority (dependencies, merge state, closed
siblings — only when evidence['pr_results'] is non-empty), (e) delivery
completeness — OI-829: when the pr_ref names at least one PR, one of them must
be delivery_kind='complete' in track_pr_delivery. A rejection happens before
any DB write: the caller returns it before reconcile_track, so a stale
candidate causes zero DB writes. Helper imports sit at the BOTTOM to break the
import cycle with track_reconciler.py; globals resolve at call time.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional, Tuple


def _rejection(
    track_id: str,
    project_id: str,
    declared_phase: Optional[str],
    action: str,
) -> Dict[str, Any]:
    """The single early-return payload shape; no DB write happens before it."""
    return {"track_id": track_id, "project_id": project_id,
            "declared_phase": declared_phase, "derived_status": None,
            "action": action, "applied": False}


def revalidate_close_evidence(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
    evidence: Optional[EvidenceSnapshot],
    include_parked: bool = False,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Revalidate a close nomination; return (rejection, skip_derived_gate).

    rejection is None when every check (a)-(e) passes; skip_derived_gate is
    True when gh evidence in (d) authorizes the close directly.
    """
    track_row = conn.execute(
        "SELECT pr_ref, phase FROM tracks WHERE track_id = ? AND project_id = ?",
        (track_id, project_id),
    ).fetchone()
    declared_phase = track_row["phase"] if track_row else None

    # (a) pr_ref must match the nomination snapshot.
    current_pr_ref = track_row["pr_ref"] if track_row else None
    if current_pr_ref != evidence.get("pr_ref"):
        return _rejection(track_id, project_id, declared_phase, "stale_candidate"), False

    # (b) no unresolved blocker OI.
    has_resolved = _has_col(conn, "track_open_items", "resolved_at")
    has_pid = _has_col(conn, "track_open_items", "project_id")
    if has_pid and has_resolved:
        blocker = conn.execute(
            "SELECT 1 FROM track_open_items WHERE track_id=? AND project_id=? "
            "AND link_type='blocks' AND resolved_at IS NULL LIMIT 1",
            (track_id, project_id),
        ).fetchone()
    elif has_pid:
        blocker = conn.execute(
            "SELECT 1 FROM track_open_items WHERE track_id=? AND project_id=? "
            "AND link_type='blocks' LIMIT 1",
            (track_id, project_id),
        ).fetchone()
    else:
        blocker = conn.execute(
            "SELECT 1 FROM track_open_items WHERE track_id=? "
            "AND link_type='blocks' LIMIT 1",
            (track_id,),
        ).fetchone()
    if blocker:
        return _rejection(track_id, project_id, declared_phase, "stale_candidate"), False

    # (c) declared phase still eligible after fresh read.
    eligible = declared_phase in ("queued", "active") or (
        declared_phase == "parked" and include_parked
    )
    if not eligible:
        return _rejection(track_id, project_id, declared_phase, "stale_candidate"), False

    # (d) gh evidence authority — only when pr_results is non-empty.
    rejection, skip_derived_gate = _gh_evidence_rejection(
        conn, track_id, project_id, declared_phase, evidence
    )
    if rejection is not None:
        return rejection, False

    # (e) delivery completeness — OI-829 fail-closed auto-close gate.
    rejection = _delivery_rejection(
        conn, track_id, project_id, declared_phase, current_pr_ref
    )
    if rejection is not None:
        return rejection, False

    return None, skip_derived_gate


def _gh_evidence_rejection(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
    declared_phase: Optional[str],
    evidence: EvidenceSnapshot,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """(d) gh evidence authority: (rejection, skip_derived_gate)."""
    pr_results_list = evidence.get("pr_results") or []
    if not pr_results_list:
        return None, False

    # Dependency check: every blocking dep ('hard', or an unknown kind
    # fail-closed) must have declared phase 'done'. 'soft' and 'overlap' edges
    # are advice and never hold a close (D9).
    if _unfinished_dependencies(conn, track_id, project_id)["blocking"]:
        return _rejection(track_id, project_id, declared_phase, "stale_candidate"), False

    # gh evidence check: every parsed PR from pr_ref must appear in pr_results
    # as MERGED (with mergedAt) or CLOSED (closed sibling). At least one must
    # be MERGED. OPEN or unknown → stale.
    pr_ref_snap = evidence.get("pr_ref") or ""
    parsed_pns = _parse_pr_numbers(pr_ref_snap)
    pr_results_by_num = {
        int(r["number"]): r for r in pr_results_list
        if isinstance(r, dict) and r.get("number") is not None
    }
    has_merged_pr = False
    for pn in parsed_pns:
        entry = pr_results_by_num.get(pn)
        if entry is None:
            return _rejection(track_id, project_id, declared_phase, "stale_candidate"), False
        state = (entry.get("state") or "")
        if state == "MERGED" and entry.get("mergedAt"):
            has_merged_pr = True
        elif state == "CLOSED" and evidence.get("allow_closed_siblings") is True:
            pass  # closed sibling — allowed only when caller opted in
        else:
            return _rejection(track_id, project_id, declared_phase, "stale_candidate"), False
    if parsed_pns and not has_merged_pr:
        return _rejection(track_id, project_id, declared_phase, "stale_candidate"), False

    return None, True


def _delivery_rejection(
    conn: sqlite3.Connection,
    track_id: str,
    project_id: str,
    declared_phase: Optional[str],
    current_pr_ref: Optional[str],
) -> Optional[Dict[str, Any]]:
    """(e) delivery completeness — OI-829 fail-closed auto-close gate.

    A merged PR is not evidence the track's whole plan shipped
    (worker-provider-free-choice closed after PR-1 of 5 merged). When the fresh
    pr_ref parses to >=1 PR number, at least one of them must be marked
    delivery_kind='complete' in track_pr_delivery. Absence of any 'complete'
    marking — including no rows at all, or the table not yet migrated — fails
    closed with its own action value (not 'stale_candidate': this is an
    incomplete delivery, not staleness). A track with no pr_ref at all (closing
    on dispatch-completion evidence, not PR evidence) has nothing to gate on
    and is unaffected. An unrecognized delivery_kind on an existing row must
    never read as "not complete" silently — it is logged at ERROR and fails
    closed (noop_incomplete_delivery), but must not raise: this function is
    called per-candidate from a sweep loop with no per-track try/except, so an
    escaping exception would abort every remaining candidate behind the
    corrupt row.
    """
    parsed_pns_for_delivery = _parse_pr_numbers(current_pr_ref)
    if not parsed_pns_for_delivery:
        return None

    try:
        delivery_rows = conn.execute(
            "SELECT pr_number, delivery_kind FROM track_pr_delivery "
            "WHERE track_id=? AND project_id=?",
            (track_id, project_id),
        ).fetchall()
    except sqlite3.OperationalError:
        delivery_rows = []  # migration 0032 not yet applied to this DB

    delivery_by_pr: Dict[int, str] = {}
    for row in delivery_rows:
        kind = row["delivery_kind"]
        if kind not in ("partial", "complete"):
            log.error(
                "track_pr_delivery: unrecognized delivery_kind %r for "
                "project_id=%r track_id=%r pr_number=%r — failing closed "
                "(noop_incomplete_delivery), not raising: this runs inside a "
                "sweep loop with no per-track try/except",
                kind, project_id, track_id, row["pr_number"],
            )
            return _rejection(
                track_id, project_id, declared_phase, "noop_incomplete_delivery"
            )
        delivery_by_pr[int(row["pr_number"])] = kind

    has_complete_delivery = any(
        delivery_by_pr.get(pn) == "complete"
        for pn in parsed_pns_for_delivery
    )
    if not has_complete_delivery:
        return _rejection(
            track_id, project_id, declared_phase, "noop_incomplete_delivery"
        )
    return None


# Late binding of the helpers this module uses from track_reconciler (see the
# module docstring). `log` binds the same logger object.
from track_reconciler import (  # noqa: E402
    EvidenceSnapshot,
    _has_col,
    _parse_pr_numbers,
    _unfinished_dependencies,
    log,
)
