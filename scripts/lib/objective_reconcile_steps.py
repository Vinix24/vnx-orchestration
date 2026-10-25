#!/usr/bin/env python3
"""objective_reconcile_steps.py — steps 2, 2b and 3 of ``run_reconcile``.

Extracted verbatim from ``objective_reconcile.run_reconcile`` (S4 of the
slop-refactor track) so the pipeline reads as a pipeline: the derived-status
refresh, the dispatch-outcome recompute and the nomination sweep each keep
their own body, their own error handling and their own connection lifetime.

Nothing here decides policy on its own — every branch below is the same
branch ``run_reconcile`` took before the move, including the fail-closed
edges (delivery hold, unparseable reopen stamp, unchanged pr_ref).

Import direction: ``objective_reconcile`` imports this module, never the
other way at module level. The two helpers still owned by
``objective_reconcile`` (``_now_utc`` and ``_persist_summary`` — its
remaining steps use them too) are imported lazily inside ``refresh_derived``.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import tracks as tracks_lib  # same package; importable when scripts/lib/ is in sys.path
import track_reconciler
from track_reconciler import _parse_pr_numbers

log = logging.getLogger(__name__)

_SKIP_PHASES: FrozenSet[str] = frozenset({"done", "parked"})


def refresh_derived(
    state_dir: Path,
    project_id: str,
    repo_root: Optional[Path],
    *,
    run_id: str,
    mode: str,
    started_at: str,
    provenance: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[Tuple[Dict[str, Any], int]]]:
    """Step 2 — derived refresh; persists derived_status for every track.

    Returns ``(results, None)`` on success. On a ``RuntimeError`` from the
    reconciler it persists the error summary exactly as ``run_reconcile`` did
    and returns ``([], (summary, 2))`` so the caller can return exit 2.
    """
    # Lazy: objective_reconcile imports this module at module level, so these
    # two helpers (still owned there — its remaining steps use them) can only
    # be imported here, at call time, without a circular import.
    from objective_reconcile import _now_utc, _persist_summary

    try:
        return track_reconciler.reconcile_all_tracks(
            state_dir, project_id, repo_root=repo_root
        ), None
    except RuntimeError as exc:
        summary: Dict[str, Any] = {
            "run_id": run_id, "project_id": project_id, "mode": mode,
            "started_at": started_at, "finished_at": _now_utc(),
            "error": str(exc),
            "evidence_source_health": {"gh": "unknown"},
            "provenance": provenance,
            "counts": {k: 0 for k in (
                "tracks", "nominated", "confirmed", "closed",
                "closed_sibling", "open_pr", "unverified", "deferred", "stale",
                "delivery_hold",
            )},
            "per_track": [],
        }
        _persist_summary(state_dir, summary)
        return [], (summary, 2)


def recompute_dispatch_outcomes(
    state_dir: Path, project_id: str, repo_root: Optional[Path],
) -> Dict[str, Any]:
    """Step 2b — dispatch-outcome recompute (receipt-quality PR-B3).

    Same cadence as the derived-status refresh above. Best-effort: a failure is
    captured in the returned ``error`` field and never blocks the track-close
    pipeline. The abandoned sweep is not a separate pass — it is one branch of
    the same per-dispatch ``classify_outcome()`` call this makes for every
    dispatch in the project.
    """
    summary: Dict[str, Any] = {"closed": 0, "total": 0, "error": None}
    try:
        # Lazy: the classifier puts scripts/lib on sys.path and imports the
        # receipts stack; keeping it off module import makes it an advisory
        # dependency of this sweep rather than of objective_reconcile itself.
        import dispatch_outcome_classifier
        outcome_results = dispatch_outcome_classifier.reconcile_all_dispatch_outcomes(
            state_dir, state_dir.parent, project_id, repo_root=repo_root,
        )
        summary["total"] = len(outcome_results)
        summary["closed"] = sum(
            1 for r in outcome_results if r.get("outcome") is not None
        )
    except Exception as exc:  # vnx-silent-except: advisory sweep, never blocks reconcile
        log.debug("dispatch-outcome recompute non-fatal: %s", exc)
        summary["error"] = str(exc)
    return summary


def nominate_candidates(
    state_dir: Path, project_id: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Step 3 — nomination: non-empty pr_ref + declared phase not in {done, parked}.

    Returns ``(candidates, guarded_tracks, held_tracks)``.

    Re-close guard: skip tracks reopened from done→active when pr_ref matches
    the stamp (unchanged) — reported as reopened_guard, not closed. An
    unparseable stamp is guarded too (fail-closed).

    OI-1098: a track with ANY currently-linked PR explicitly marked
    delivery_kind != 'complete' is held (verdict 'delivery_hold') — never
    nominated, and never silently dropped from the report.
    """
    all_tracks = tracks_lib.list_tracks(state_dir, project_id)
    candidates: List[Dict[str, Any]] = []
    guarded_tracks: List[Dict[str, Any]] = []
    held_tracks: List[Dict[str, Any]] = []
    # OI-1098: one shared read-only connection for the per-track delivery-hold
    # check (the nomination loop previously did no DB reads of its own).
    hold_conn = track_reconciler._get_conn(state_dir)
    try:
        for t in all_tracks:
            phase = (t.get("phase") or "").strip()
            pr_ref = (t.get("pr_ref") or "").strip()
            if not pr_ref or phase in _SKIP_PHASES:
                continue
            pr_numbers = _parse_pr_numbers(pr_ref)
            if not pr_numbers:
                continue

            # OI-1098 fail-closed + visible: an explicit non-'complete'
            # delivery marking on ANY currently-linked PR vetoes the
            # nomination — and the track must stay VISIBLE in the report with
            # the reason, never silently drop out of the candidate set.
            hold = track_reconciler._delivery_hold(
                hold_conn, t["track_id"], project_id, pr_ref
            )
            if hold is not None:
                held_tracks.append({
                    "track_id": t["track_id"],
                    "verdict": "delivery_hold",
                    "pr_ref": pr_ref,
                    "reason": hold["reason"],
                })
                continue

            # Re-close guard: if a done→active reopen history row exists,
            # compare its stamped pr_ref against the current pr_ref.
            reopen_hist = _get_latest_done_to_active_history(state_dir, t["track_id"], project_id)
            if reopen_hist is not None:
                stamped_pr_ref = _parse_reopen_stamp(reopen_hist.get("reason") or "")
                if stamped_pr_ref is None:
                    # Unparseable stamp — fail-closed.
                    guarded_tracks.append({
                        "track_id": t["track_id"],
                        "verdict": "reopened_guard",
                        "pr_ref": pr_ref,
                        "reason": "unparseable_reopen_stamp",
                    })
                    continue
                if stamped_pr_ref == pr_ref:
                    # pr_ref unchanged since reopen — skip.
                    guarded_tracks.append({
                        "track_id": t["track_id"],
                        "verdict": "reopened_guard",
                        "pr_ref": pr_ref,
                        "reason": "pr_ref_unchanged_since_reopen",
                    })
                    continue
                # pr_ref changed — re-armed; fall through to normal nomination.

            candidates.append({
                "track_id": t["track_id"],
                "pr_ref": pr_ref,
                "pr_numbers": pr_numbers,
                "phase": phase,
            })
    finally:
        hold_conn.close()
    return candidates, guarded_tracks, held_tracks


# ---------------------------------------------------------------------------
# Re-close guard helpers (D6) — used by nominate_candidates above, which was
# their only caller; objective_reconcile re-exports _parse_reopen_stamp.
# ---------------------------------------------------------------------------

def _get_latest_done_to_active_history(
    state_dir: Path, track_id: str, project_id: str
) -> Optional[Dict[str, Any]]:
    """Return the latest track_phase_history row (from_phase=done, to_phase=active)
    for this track, project-scoped. Returns None when no such row exists or on error."""
    db_path = state_dir / track_reconciler.DB_FILENAME
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT reason, approval_id, occurred_at
                FROM track_phase_history
                WHERE track_id = ? AND project_id = ?
                  AND from_phase = 'done' AND to_phase = 'active'
                ORDER BY occurred_at DESC
                LIMIT 1
                """,
                (track_id, project_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as exc:  # best-effort read: an unreadable row logs at debug and reads as absent
        log.debug("reopen guard DB lookup failed for %s: %s", track_id, exc)
        return None


_REOPEN_STAMP_PREFIX = "reopen pr_ref="
_JSON_STRING_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"')


def _parse_reopen_stamp(reason: str) -> Optional[str]:
    """Parse the stamped pr_ref from a reopen history reason string.

    New format: 'reopen pr_ref="<json-string>" | <operator text>'
    The value is a JSON string literal so embedded pipes/quotes are safe.

    Backwards-compat: old-format raw stamps (no leading quote) and garbled
    stamps return None — callers treat None as GUARDED (fail-closed).

    Returns the decoded pr_ref string ('' for the '-' sentinel), or None
    when the format is not the new JSON format.
    """
    if not reason:
        return None
    if not reason.startswith(_REOPEN_STAMP_PREFIX):
        return None
    rest = reason[len(_REOPEN_STAMP_PREFIX):]
    # New format: JSON string literal starting with '"'
    m = _JSON_STRING_RE.match(rest)
    if not m:
        # Old-format (no leading quote) or malformed → fail-closed
        return None
    try:
        val = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    # Remainder must be empty or start with ' | ' — anything else is garbled.
    remainder = rest[m.end():]
    if remainder and not remainder.startswith(" | "):
        return None
    # '-' is the sentinel for empty pr_ref
    return "" if val == "-" else val
