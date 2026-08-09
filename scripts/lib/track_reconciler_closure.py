#!/usr/bin/env python3
"""track_reconciler_closure.py — human-gated track closure for the track layer.

Pure move (fase 1 PR 2, track file-size-refactor-debt): close_track_if_done
lived in track_reconciler.py (lines 799-1143 on main before PR 1) and moved
here unchanged. track_reconciler.py re-exports the name so no consumer has
to change.

NOT advisory — this module is the write side that the "ADVISORY ONLY" hard
contract of track_reconciler.py explicitly excludes:

  - Writes tracks.phase (plus phase_changed_at / completed_at) and inserts
    track_phase_history rows, via tracks_lib.transition_phase (tracks.py).
  - Advances tracks along the ALLOWED_TRANSITIONS phase graph to 'done',
    walking the shortest legal path one transition_phase call per step.

Human gates on every close:

  - actor: required keyword argument; transition_phase enforces it is one of
    'operator', 'T0', or 'system', so every close is attributable.
  - approval_id: threaded through to transition_phase and recorded in
    track_phase_history for every step of the walk.
  - a declared phase of 'parked' is refused (rejected_parked) unless the
    caller explicitly passes include_parked=True.
  - derived_status must be terminal ('done'), unless gh nomination evidence
    authorizes the close directly (see the function docstring for the full
    revalidation contract).

The helper imports sit at the BOTTOM of this module, after the function
definition, to break the import cycle with track_reconciler.py (which imports
this module for the re-export). Function globals resolve at call time, not at
definition time, so the names need not exist while this module is first being
imported — same pattern as track_reconciler_status.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional

import tracks as tracks_lib  # same package; importable whenever scripts/lib/ is in sys.path

def close_track_if_done(
    state_dir: "str | Path",
    track_id: str,
    project_id: str,
    *,
    actor: str,
    evidence: Optional[EvidenceSnapshot] = None,
    approval_id: Optional[str] = None,
    include_parked: bool = False,
    repo_root: "str | Path | None" = None,
    merged_pr_numbers: Optional[FrozenSet[int]] = None,
) -> Dict[str, Any]:
    """Attempt to close a track by walking its declared phase to 'done'.

    Reconciles derived_status, gates on it being terminal ('done'), then walks
    the shortest legal phase path to 'done' via transition_phase.

    evidence (optional nomination snapshot): when provided, performs CLOSE-TIME
    REVALIDATION as the very first step — BEFORE reconcile_track — so that a
    stale candidate causes zero DB writes (reconcile_track persists
    derived_status; it must not run on a track that will return stale_candidate).
    Fresh DB read checks:
      (a) track's pr_ref unchanged vs evidence['pr_ref'],
      (b) no unresolved blocker OI (link_type='blocks' AND resolved_at IS NULL),
      (c) declared phase still eligible (queued/active; parked only with include_parked),
      (d) gh evidence authority (dependency/merge/closed-sibling checks — only
          when evidence['pr_results'] is non-empty),
      (e) delivery completeness — OI-829 fail-closed auto-close: when the
          fresh pr_ref parses to >=1 PR number, at least one of them must be
          marked delivery_kind='complete' in track_pr_delivery for this
          (project_id, track_id). A merged PR alone is not evidence the whole
          plan shipped. Absence of any 'complete' marking (or only 'partial'
          markings) returns action='noop_incomplete_delivery', applied=False
          — this is not staleness, it's an incomplete delivery, so it gets
          its own action value rather than 'stale_candidate'. A track with no
          pr_ref at all has nothing to gate on and is unaffected. An
          unrecognized delivery_kind on an existing row is logged at ERROR
          (project_id, track_id, pr_number, the value) and fails closed with
          action='noop_incomplete_delivery' — loud but non-escaping, so one
          corrupt row cannot abort a reconcile sweep over other candidates.
    Any mismatch on (a)-(d) returns action='stale_candidate', applied=False,
    BEFORE reconcile_track — so a stale candidate causes zero DB writes,
    derived_status included. (e) uses its own action value but the same
    zero-writes-before-reconcile_track placement.

    When evidence is None (human objective-close path), no revalidation is done
    and the flow is byte-for-byte identical to the pre-revalidation behaviour:
    reconcile_track first, then the derived/declared/parked gates, then the walk.

    The walk is NOT atomic with the checks: transition_phase (tracks.py) opens its
    own connection and commits per step. A mid-walk failure leaves the track at an
    intermediate phase; re-calling this function re-walks from the current declared
    phase (bounded TOCTOU-narrowing, not atomicity).

    repo_root: optional project repo root, forwarded to the internal
    reconcile_track call for its ROADMAP.yaml (Source-3) evidence path; falls
    back to the CWD git-root then the legacy layout (see reconcile_track).

    merged_pr_numbers: optional pre-established merged-PR set (OI-1064). When
    provided, forwarded to the internal reconcile_track call so a caller that
    has ALREADY established merge state (run_reconcile's gh sweep) does not
    discard it — the set must be the UNION of the caller's gh-confirmed numbers
    and the locally-loaded set (run_reconcile performs that union). Default
    None keeps every existing caller behaviour-identical: reconcile_track loads
    the local sources itself. See reconcile_track's _merged_pr_numbers contract.

    Returns a dict with keys: track_id, project_id, action, applied, declared_phase,
    derived_status, path (when applicable), evidence (when computed), error (on failure).

    Possible action values:
      noop_not_terminal     derived != 'done', nothing to close
      noop_already_closed   declared already 'done'
      rejected_parked       declared='parked' and include_parked=False
      stale_candidate       revalidation mismatch (evidence path only); no write
      noop_incomplete_delivery  no linked PR marked delivery_kind='complete'
                                (evidence path only); no write
      rejected_no_path      no legal phase-graph path from declared to 'done'
      rejected_not_found    track deleted during walk
      rejected_walk_failed  transition failed mid-walk; declared_phase=stop-phase
      closed                walk completed; declared_phase updated to 'done'
    """
    # Close-time revalidation runs FIRST when evidence is provided — before
    # reconcile_track — so a stale candidate causes zero DB writes (reconcile_track
    # persists derived_status; it must not run for a track that will be rejected).
    # _skip_derived_gate is set True when gh evidence authorizes the close directly,
    # making derived_status advisory rather than a hard gate.
    _skip_derived_gate = False

    if evidence is not None:
        conn = _get_conn(state_dir)
        try:
            track_row = conn.execute(
                "SELECT pr_ref, phase FROM tracks WHERE track_id = ? AND project_id = ?",
                (track_id, project_id),
            ).fetchone()

            # (a) pr_ref must match the nomination snapshot.
            current_pr_ref = track_row["pr_ref"] if track_row else None
            if current_pr_ref != evidence.get("pr_ref"):
                return {
                    "track_id": track_id,
                    "project_id": project_id,
                    "declared_phase": track_row["phase"] if track_row else None,
                    "derived_status": None,
                    "action": "stale_candidate",
                    "applied": False,
                }

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
                return {
                    "track_id": track_id,
                    "project_id": project_id,
                    "declared_phase": track_row["phase"] if track_row else None,
                    "derived_status": None,
                    "action": "stale_candidate",
                    "applied": False,
                }

            # (c) declared phase still eligible after fresh read.
            fresh_phase = track_row["phase"] if track_row else None
            eligible = fresh_phase in ("queued", "active") or (
                fresh_phase == "parked" and include_parked
            )
            if not eligible:
                return {
                    "track_id": track_id,
                    "project_id": project_id,
                    "declared_phase": fresh_phase,
                    "derived_status": None,
                    "action": "stale_candidate",
                    "applied": False,
                }

            # (d) gh evidence authority — only when pr_results is non-empty.
            # gh pr view is the SOLE merge authority on this path; derived_status
            # becomes advisory (reconcile_track still runs for the derived refresh).
            pr_results_list = evidence.get("pr_results") or []
            if pr_results_list:
                # Dependency check: every dep must have declared phase 'done'.
                dep_rows = conn.execute(
                    """
                    SELECT t.phase
                    FROM track_dependencies td
                    JOIN tracks t
                      ON t.track_id = td.to_track_id AND t.project_id = td.to_project_id
                    WHERE td.from_track_id = ? AND td.from_project_id = ?
                    """,
                    (track_id, project_id),
                ).fetchall()
                for dep_row in dep_rows:
                    if dep_row[0] != "done":
                        return {
                            "track_id": track_id,
                            "project_id": project_id,
                            "declared_phase": track_row["phase"] if track_row else None,
                            "derived_status": None,
                            "action": "stale_candidate",
                            "applied": False,
                        }

                # gh evidence check: every parsed PR from pr_ref must appear in
                # pr_results as MERGED (with mergedAt) or CLOSED (closed sibling).
                # At least one must be MERGED. OPEN or unknown → stale.
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
                        return {
                            "track_id": track_id,
                            "project_id": project_id,
                            "declared_phase": track_row["phase"] if track_row else None,
                            "derived_status": None,
                            "action": "stale_candidate",
                            "applied": False,
                        }
                    state = (entry.get("state") or "")
                    if state == "MERGED" and entry.get("mergedAt"):
                        has_merged_pr = True
                    elif state == "CLOSED" and evidence.get("allow_closed_siblings") is True:
                        pass  # closed sibling — allowed only when caller opted in
                    else:
                        return {
                            "track_id": track_id,
                            "project_id": project_id,
                            "declared_phase": track_row["phase"] if track_row else None,
                            "derived_status": None,
                            "action": "stale_candidate",
                            "applied": False,
                        }
                if parsed_pns and not has_merged_pr:
                    return {
                        "track_id": track_id,
                        "project_id": project_id,
                        "declared_phase": track_row["phase"] if track_row else None,
                        "derived_status": None,
                        "action": "stale_candidate",
                        "applied": False,
                    }

                _skip_derived_gate = True

            # (e) delivery completeness — OI-829 fail-closed auto-close gate.
            # A merged PR is not evidence the track's whole plan shipped
            # (worker-provider-free-choice closed after PR-1 of 5 merged). When
            # the fresh pr_ref parses to >=1 PR number, at least one of them
            # must be marked delivery_kind='complete' in track_pr_delivery.
            # Absence of any 'complete' marking — including no rows at all, or
            # the table not yet migrated — fails closed with its own action
            # value (not 'stale_candidate': this is an incomplete delivery, not
            # staleness). A track with no pr_ref at all (closing on dispatch-
            # completion evidence, not PR evidence) has nothing to gate on and
            # is unaffected. An unrecognized delivery_kind on an existing row
            # must never read as "not complete" silently — it is logged at
            # ERROR and fails closed (noop_incomplete_delivery), but must not
            # raise: this function is called per-candidate from a sweep loop
            # with no per-track try/except, so an escaping exception would
            # abort every remaining candidate behind the corrupt row.
            parsed_pns_for_delivery = _parse_pr_numbers(current_pr_ref)
            if parsed_pns_for_delivery:
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
                        return {
                            "track_id": track_id,
                            "project_id": project_id,
                            "declared_phase": track_row["phase"] if track_row else None,
                            "derived_status": None,
                            "action": "noop_incomplete_delivery",
                            "applied": False,
                        }
                    delivery_by_pr[int(row["pr_number"])] = kind

                has_complete_delivery = any(
                    delivery_by_pr.get(pn) == "complete"
                    for pn in parsed_pns_for_delivery
                )
                if not has_complete_delivery:
                    return {
                        "track_id": track_id,
                        "project_id": project_id,
                        "declared_phase": track_row["phase"] if track_row else None,
                        "derived_status": None,
                        "action": "noop_incomplete_delivery",
                        "applied": False,
                    }
        finally:
            conn.close()

    # Revalidation passed (or evidence=None path): reconcile derived_status now.
    # When _skip_derived_gate is True, reconcile_track still runs for the derived
    # refresh (persists derived_status for reporting) but its result does not gate
    # the walk — gh evidence already authorized the close.
    result = reconcile_track(
        state_dir, track_id, project_id,
        repo_root=repo_root,
        _merged_pr_numbers=merged_pr_numbers,
    )
    derived = result["derived_status"]
    declared = result["declared_phase"]
    target = "done"

    payload: Dict[str, Any] = {
        "track_id": track_id,
        "project_id": project_id,
        "declared_phase": declared,
        "derived_status": derived,
        "action": None,
        "applied": False,
    }

    if derived != target and not _skip_derived_gate:
        payload["action"] = "noop_not_terminal"
        return payload

    if declared == target:
        payload["action"] = "noop_already_closed"
        return payload

    if declared == "parked" and not include_parked:
        payload["action"] = "rejected_parked"
        return payload

    payload["evidence"] = _close_evidence(state_dir, track_id, project_id, repo_root=repo_root)

    path = _phase_path_to(declared, target)
    if path is None:
        payload["action"] = "rejected_no_path"
        return payload
    payload["path"] = [declared, *path]

    cur = declared
    try:
        for step in path:
            tracks_lib.transition_phase(
                state_dir, track_id, project_id, step,
                actor=actor,
                reason=f"close-the-loop ({declared}->{target}, derived={derived})",
                approval_id=approval_id,
            )
            cur = step
    except tracks_lib.TrackNotFoundError as exc:
        payload["action"] = "rejected_not_found"
        payload["error"] = str(exc)
        return payload
    except Exception as exc:
        payload["declared_phase"] = cur
        payload["action"] = "rejected_walk_failed"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    payload["declared_phase"] = target
    payload["action"] = "closed"
    payload["applied"] = True
    return payload


# Late binding of the helpers this function uses from its old home module.
# Deliberately after the function definition (see module docstring): importing
# them at the top would deadlock the re-export cycle with track_reconciler.py.
# `log` binds the same logger object, so emitted records keep the
# 'track_reconciler' logger name they had before the move.
from track_reconciler import (  # noqa: E402
    EvidenceSnapshot,
    _close_evidence,
    _get_conn,
    _has_col,
    _parse_pr_numbers,
    _phase_path_to,
    log,
    reconcile_track,
)
