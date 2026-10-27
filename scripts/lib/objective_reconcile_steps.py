#!/usr/bin/env python3
"""objective_reconcile_steps.py — steps 2, 2b, 3, 4a and 4b of ``run_reconcile``.

Extracted verbatim from ``objective_reconcile.run_reconcile`` (S4, then S5 of
the slop-refactor track) so the pipeline reads as a pipeline: each step keeps
its own body, its own error handling and its own connection lifetime.

Nothing here decides policy on its own — every branch below is the same
branch ``run_reconcile`` took before the move, including the fail-closed
edges (delivery hold, unparseable reopen stamp, unchanged pr_ref) and the
order of every write to the PR-state cache, the summary and the counts.

Import direction: ``objective_reconcile`` imports this module, never the
other way at module level. The helpers still owned by ``objective_reconcile``
(``_now_utc``/``_persist_summary``, its gh probes and its cache/decision
primitives — its remaining steps use them too) are imported lazily inside the
functions below, which also keeps monkeypatching them on the owning module
effective.
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


def gh_unavailable_result(
    state_dir: Path, project_id: str, repo_root: Path, *,
    run_id: str, mode: str, started_at: str, provenance: Dict[str, Any],
    dispatch_outcomes_summary: Dict[str, Any], total_tracks: int, nominated: int,
    candidates: List[Dict[str, Any]], guarded_tracks: List[Dict[str, Any]],
    held_tracks: List[Dict[str, Any]],
) -> Tuple[Optional[Tuple[Dict[str, Any], int]], Optional[str], str]:
    """Step 4a — gh detection; the degraded summary when gh is unusable.

    Returns ``(degraded, gh_binary, gh_health)``. The binary is resolved once
    per run and handed back so every later PR lookup reuses it (a stripped PATH
    must not flip a live gh to "absent" partway through a run). ``degraded`` is
    the ``(summary, 3)`` the caller returns when gh is absent / auth-failed /
    timed out — every candidate unverified, summary persisted first, exactly as
    ``run_reconcile`` did — and ``None`` when gh is usable.
    """
    # Lazy: objective_reconcile imports this module at module level, so these
    # helpers (still owned there) can only be imported here, without a cycle.
    from objective_reconcile import (
        _detect_gh, _now_utc, _persist_summary, _resolve_gh_binary,
    )

    gh_binary = _resolve_gh_binary()
    gh_health = _detect_gh(repo_root, gh_binary)
    if gh_health not in ("absent", "auth_failed", "timeout"):
        return None, gh_binary, gh_health

    per_track = [
        {"track_id": c["track_id"], "verdict": "unverified",
         "pr_ref": c["pr_ref"], "reason": gh_health}
        for c in candidates
    ] + guarded_tracks + held_tracks
    counts = {
        "tracks": total_tracks, "nominated": nominated, "confirmed": 0,
        "closed": 0, "closed_sibling": 0, "open_pr": 0,
        "unverified": len(candidates), "deferred": 0, "stale": 0,
        "reopened_guard": len(guarded_tracks),
        "delivery_hold": len(held_tracks),
    }
    summary = {
        "run_id": run_id, "project_id": project_id, "mode": mode,
        "started_at": started_at, "finished_at": _now_utc(),
        "evidence_source_health": {"gh": gh_health},
        "provenance": provenance,
        "dispatch_outcomes": dispatch_outcomes_summary,
        "counts": counts,
        "per_track": per_track,
    }
    _persist_summary(state_dir, summary)
    return (summary, 3), gh_binary, gh_health


def verify_candidates(
    state_dir: Path, repo_root: Path, candidates: List[Dict[str, Any]],
    guarded_tracks: List[Dict[str, Any]], held_tracks: List[Dict[str, Any]], *,
    gh_binary: Optional[str], total_tracks: int, nominated: int,
    max_gh_calls: int, allow_closed_siblings: bool,
) -> Dict[str, Any]:
    """Step 4b — per-candidate verification with the persistent MERGED cache.

    Returns ``per_track``, ``counts``, ``confirmed_candidates`` and
    ``gh_calls_used`` for the steps that follow (4c, 5, 6). A candidate whose
    lookups would cross ``max_gh_calls`` is reported ``deferred``, costs no call.
    """
    # Lazy: same circular-import reason as gh_unavailable_result above.
    from objective_reconcile import _get_repo_key, _load_pr_state_cache

    repo_key = _get_repo_key(repo_root)
    pr_cache = _load_pr_state_cache(state_dir, repo_key)
    gh_calls_used = 0
    counts = {
        "tracks": total_tracks, "nominated": nominated, "confirmed": 0,
        "closed": 0, "closed_sibling": 0, "open_pr": 0,
        "unverified": 0, "deferred": 0, "stale": 0,
        "reopened_guard": len(guarded_tracks),
        "delivery_hold": len(held_tracks),
    }
    confirmed_candidates: List[Dict[str, Any]] = []
    per_track: List[Dict[str, Any]] = []

    for cand in candidates:
        entry, confirmed, gh_calls_used = _verify_one_candidate(
            cand, state_dir=state_dir, repo_root=repo_root, repo_key=repo_key,
            pr_cache=pr_cache, gh_binary=gh_binary,
            allow_closed_siblings=allow_closed_siblings,
            gh_calls_used=gh_calls_used, max_gh_calls=max_gh_calls,
        )
        per_track.append(entry)
        verdict = entry["verdict"]
        if confirmed is not None:
            counts["confirmed"] += 1
            confirmed_candidates.append(confirmed)
        else:
            counts[verdict] = counts.get(verdict, 0) + 1

    return {
        "per_track": per_track, "counts": counts,
        "confirmed_candidates": confirmed_candidates, "gh_calls_used": gh_calls_used,
    }


def _verify_one_candidate(
    cand: Dict[str, Any], *, state_dir: Path, repo_root: Path, repo_key: str,
    pr_cache: Dict[str, Dict[str, Any]], gh_binary: Optional[str],
    allow_closed_siblings: bool, gh_calls_used: int, max_gh_calls: int,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], int]:
    """Verify ONE candidate; returns ``(per_track_entry, confirmed, calls_used)``.

    ``confirmed`` is set only for a CONFIRMED verdict (what 4c and step 5 act
    on); ``gh_calls_used`` is unchanged on a deferral — the cap is checked first.
    """
    # Lazy: same circular-import reason as gh_unavailable_result above.
    from objective_reconcile import (
        _decide_candidate, _gh_pr_view, _is_merged, _save_pr_state_cache,
    )

    track_id = cand["track_id"]
    pr_numbers: FrozenSet[int] = cand["pr_numbers"]
    pr_ref = cand["pr_ref"]

    # Split: cached MERGED vs needs a live gh call.
    pr_results: Dict[int, Optional[Dict[str, Any]]] = {}
    prs_to_fetch: List[int] = []
    for pn in pr_numbers:
        cached = pr_cache.get(str(pn))
        if cached and _is_merged(cached):
            pr_results[pn] = cached
        else:
            prs_to_fetch.append(pn)

    # Defer if fetching would exceed the cap.
    if prs_to_fetch and gh_calls_used + len(prs_to_fetch) > max_gh_calls:
        return {"track_id": track_id, "verdict": "deferred", "pr_ref": pr_ref}, None, gh_calls_used

    # Fetch live states; cache any MERGED results.
    for pn in prs_to_fetch:
        gh_calls_used += 1
        data = _gh_pr_view(pn, repo_root, gh_binary=gh_binary)
        pr_results[pn] = data
        if data is not None and _is_merged(data):
            pr_cache[str(pn)] = data

    # Persist cache after each candidate's batch so timeouts don't lose cached merges.
    _save_pr_state_cache(state_dir, repo_key, pr_cache)

    verdict, pr_list = _decide_candidate(pr_numbers, pr_results, allow_closed_siblings)

    entry: Dict[str, Any] = {
        "track_id": track_id,
        "verdict": verdict,
        "pr_ref": pr_ref,
        "pr_results": pr_list,
    }
    if verdict != "CONFIRMED":
        return entry, None, gh_calls_used
    return entry, {"track_id": track_id, "pr_ref": pr_ref, "pr_results": pr_list}, gh_calls_used


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
