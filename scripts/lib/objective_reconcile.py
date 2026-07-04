#!/usr/bin/env python3
"""objective_reconcile.py — batch git-grounded auto-close for the tracks layer (D3).

Called by planning_cli's ``vnx objective reconcile`` subcommand.

Steps per run (executed in order):
  1. Provenance sweep — best-effort; failure never blocks remaining steps.
  2. Derived refresh  — reconcile_all_tracks persists derived_status for every
                        track in the project (both check and apply modes).
  3. Nomination       — tracks with non-empty pr_ref, declared phase not in
                        {done, parked}.  Does NOT gate on derived_status.
  4. Verification     — ``gh pr view <n> --json state,mergedAt`` per PR number.
                        MERGED results cached persistently (pr_state_cache.json);
                        non-MERGED states are re-checked on every run.
  5. Close            — close_track_if_done for CONFIRMED candidates
                        (--apply only; skipped in check mode).
  6. Summary          — reconcile_summary.json (atomic write) +
                        reconcile_history.ndjson (NDJSON append).

Exit codes:
  0 — clean (gh ok, zero unverified)
  2 — usage / state error (unknown project, unreadable state dir)
  3 — degraded (gh absent / auth-failed / timed out, or ≥1 unverified skip)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import tracks as tracks_lib  # same package; importable when scripts/lib/ is in sys.path
import track_reconciler
from track_reconciler import _parse_pr_numbers, EvidenceSnapshot

log = logging.getLogger(__name__)

_PR_STATE_CACHE_FILE = "pr_state_cache.json"
_SUMMARY_FILE = "reconcile_summary.json"
_HISTORY_FILE = "reconcile_history.ndjson"
_SKIP_PHASES: FrozenSet[str] = frozenset({"done", "parked"})


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    token = os.urandom(3).hex()
    return f"{ts}-{token}"


# ---------------------------------------------------------------------------
# Persistent MERGED cache
# ---------------------------------------------------------------------------

def _load_pr_state_cache(state_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load pr_state_cache.json: str(pr_number) → {state, mergedAt}. Errors → {}."""
    path = state_dir / _PR_STATE_CACHE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {}


def _save_pr_state_cache(state_dir: Path, cache: Dict[str, Dict[str, Any]]) -> None:
    """Atomically write pr_state_cache.json. Silently swallows I/O errors."""
    path = state_dir / _PR_STATE_CACHE_FILE
    tmp = path.parent / (path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# gh helpers
# ---------------------------------------------------------------------------

def _detect_gh(repo_root: Path) -> str:
    """Return 'ok', 'absent', 'auth_failed', or 'timeout'."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_root),
        )
        return "ok" if result.returncode == 0 else "auth_failed"
    except FileNotFoundError:
        return "absent"
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "absent"


def _gh_pr_view(pr_number: int, repo_root: Path, timeout: int = 15) -> Optional[Dict[str, Any]]:
    """Call ``gh pr view <n> --json state,mergedAt``. Returns dict or None on error."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "state,mergedAt"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
        return data if isinstance(data, dict) else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _is_merged(pr_data: Optional[Dict[str, Any]]) -> bool:
    """True iff state=='MERGED' and mergedAt is non-null/non-empty."""
    if not pr_data:
        return False
    return pr_data.get("state") == "MERGED" and bool(pr_data.get("mergedAt"))


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def _decide_candidate(
    pr_numbers: FrozenSet[int],
    pr_results: Dict[int, Optional[Dict[str, Any]]],
    allow_closed_siblings: bool,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Classify a candidate's verdict from per-PR gh results.

    Priority order: unverified > open_pr > closed_sibling > CONFIRMED.

    Returns (verdict, pr_list) where verdict is one of:
      CONFIRMED, closed_sibling, open_pr, unverified
    """
    pr_list: List[Dict[str, Any]] = []
    has_merged = False
    has_open = False
    has_closed_unmerged = False
    has_error = False

    for pn in sorted(pr_numbers):
        data = pr_results.get(pn)
        if data is None:
            has_error = True
            pr_list.append({"number": pn, "state": None, "mergedAt": None})
            continue
        state = data.get("state")
        merged_at = data.get("mergedAt")
        pr_list.append({"number": pn, "state": state, "mergedAt": merged_at})
        if _is_merged(data):
            has_merged = True
        elif state == "CLOSED":
            has_closed_unmerged = True
        elif state == "OPEN":
            has_open = True
        else:
            has_error = True

    if has_error:
        return "unverified", pr_list
    if has_open:
        return "open_pr", pr_list
    if has_closed_unmerged:
        if allow_closed_siblings and has_merged:
            return "CONFIRMED", pr_list
        return "closed_sibling", pr_list
    # All PRs accounted for, none errored/open/closed-unmerged.
    if has_merged and all(_is_merged(pr_results.get(pn)) for pn in pr_numbers):
        return "CONFIRMED", pr_list
    return "unverified", pr_list


# ---------------------------------------------------------------------------
# Summary persistence
# ---------------------------------------------------------------------------

def _persist_summary(state_dir: Path, summary: Dict[str, Any]) -> None:
    """Write reconcile_summary.json (atomic) + append one record to reconcile_history.ndjson."""
    state_dir.mkdir(parents=True, exist_ok=True)

    # Atomic summary write
    summary_path = state_dir / _SUMMARY_FILE
    tmp = summary_path.parent / (summary_path.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, summary_path)
    except OSError as exc:
        log.warning("reconcile: cannot write %s: %s", summary_path, exc)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    # NDJSON append
    history_path = state_dir / _HISTORY_FILE
    try:
        with open(history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, default=str) + "\n")
    except OSError as exc:
        log.warning("reconcile: cannot append %s: %s", history_path, exc)


# ---------------------------------------------------------------------------
# Core run logic
# ---------------------------------------------------------------------------

def run_reconcile(
    state_dir: Path,
    project_id: str,
    *,
    repo_root: Path,
    apply: bool = False,
    allow_closed_siblings: bool = False,
    max_gh_calls: int = 50,
) -> Tuple[Dict[str, Any], int]:
    """Run the full reconcile pipeline for project_id.

    Returns (summary_dict, exit_code) where exit_code is 0, 2, or 3.
    Never raises — all errors are captured in the summary and returned as exit 3.
    """
    run_id = _make_run_id()
    started_at = _now_utc()
    mode = "apply" if apply else "check"

    # ------------------------------------------------------------------ step 1
    # Provenance sweep — best-effort; never blocks the rest.
    # ------------------------------------------------------------------ step 1
    provenance: Dict[str, int] = {"scanned": 0, "linked": 0}
    try:
        from receipt_provenance import reconcile_commit_provenance  # noqa: PLC0415
        db_path = state_dir / track_reconciler.DB_FILENAME
        prov_conn = sqlite3.connect(str(db_path), timeout=10.0)
        prov_conn.row_factory = sqlite3.Row
        try:
            provenance = reconcile_commit_provenance(repo_root, prov_conn)
        finally:
            prov_conn.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("provenance sweep non-fatal: %s", exc)

    # ------------------------------------------------------------------ step 2
    # Derived refresh — persists derived_status for every track.
    # ------------------------------------------------------------------ step 2
    try:
        reconcile_results = track_reconciler.reconcile_all_tracks(state_dir, project_id)
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
            )},
            "per_track": [],
        }
        _persist_summary(state_dir, summary)
        return summary, 2

    total_tracks = len(reconcile_results)

    # ------------------------------------------------------------------ step 3
    # Nomination: non-empty pr_ref + declared phase not in {done, parked}.
    # No gate on derived_status or aggregate merged-set.
    # ------------------------------------------------------------------ step 3
    all_tracks = tracks_lib.list_tracks(state_dir, project_id)
    candidates: List[Dict[str, Any]] = []
    for t in all_tracks:
        phase = (t.get("phase") or "").strip()
        pr_ref = (t.get("pr_ref") or "").strip()
        if not pr_ref or phase in _SKIP_PHASES:
            continue
        pr_numbers = _parse_pr_numbers(pr_ref)
        if not pr_numbers:
            continue
        candidates.append({
            "track_id": t["track_id"],
            "pr_ref": pr_ref,
            "pr_numbers": pr_numbers,
            "phase": phase,
        })

    nominated = len(candidates)

    # ------------------------------------------------------------------ step 4a
    # gh detection — absent / auth-failed → all unverified, exit 3.
    # ------------------------------------------------------------------ step 4a
    gh_health = _detect_gh(repo_root)
    if gh_health in ("absent", "auth_failed", "timeout"):
        per_track = [
            {
                "track_id": c["track_id"],
                "verdict": "unverified",
                "pr_ref": c["pr_ref"],
                "reason": gh_health,
            }
            for c in candidates
        ]
        counts = {
            "tracks": total_tracks, "nominated": nominated, "confirmed": 0,
            "closed": 0, "closed_sibling": 0, "open_pr": 0,
            "unverified": len(candidates), "deferred": 0, "stale": 0,
        }
        summary = {
            "run_id": run_id, "project_id": project_id, "mode": mode,
            "started_at": started_at, "finished_at": _now_utc(),
            "evidence_source_health": {"gh": gh_health},
            "provenance": provenance,
            "counts": counts,
            "per_track": per_track,
        }
        _persist_summary(state_dir, summary)
        return summary, 3

    # ------------------------------------------------------------------ step 4b
    # Per-candidate verification with persistent MERGED cache.
    # ------------------------------------------------------------------ step 4b
    pr_cache = _load_pr_state_cache(state_dir)
    gh_calls_used = 0
    counts = {
        "tracks": total_tracks, "nominated": nominated, "confirmed": 0,
        "closed": 0, "closed_sibling": 0, "open_pr": 0,
        "unverified": 0, "deferred": 0, "stale": 0,
    }
    confirmed_candidates: List[Dict[str, Any]] = []
    per_track: List[Dict[str, Any]] = []

    for cand in candidates:
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
            per_track.append({"track_id": track_id, "verdict": "deferred", "pr_ref": pr_ref})
            counts["deferred"] += 1
            continue

        # Fetch live states; cache any MERGED results.
        for pn in prs_to_fetch:
            gh_calls_used += 1
            data = _gh_pr_view(pn, repo_root)
            pr_results[pn] = data
            if data is not None and _is_merged(data):
                pr_cache[str(pn)] = data

        # Persist cache after each candidate's batch so timeouts don't lose cached merges.
        _save_pr_state_cache(state_dir, pr_cache)

        verdict, pr_list = _decide_candidate(pr_numbers, pr_results, allow_closed_siblings)

        entry: Dict[str, Any] = {
            "track_id": track_id,
            "verdict": verdict,
            "pr_ref": pr_ref,
            "pr_results": pr_list,
        }
        per_track.append(entry)

        if verdict == "CONFIRMED":
            counts["confirmed"] += 1
            confirmed_candidates.append({
                "track_id": track_id, "pr_ref": pr_ref, "pr_results": pr_list,
            })
        else:
            counts[verdict] = counts.get(verdict, 0) + 1

    # ------------------------------------------------------------------ step 5
    # Close confirmed candidates (--apply only).
    # ------------------------------------------------------------------ step 5
    if apply:
        verified_at = _now_utc()
        for cc in confirmed_candidates:
            track_id = cc["track_id"]
            pr_ref = cc["pr_ref"]
            pr_list = cc["pr_results"]

            evidence: EvidenceSnapshot = {
                "pr_ref": pr_ref,
                "pr_results": pr_list,
                "verified_at": verified_at,
            }
            close_result = track_reconciler.close_track_if_done(
                state_dir, track_id, project_id,
                actor="system",
                evidence=evidence,
                approval_id=f"auto-reconcile-{run_id}",
            )
            action = close_result.get("action", "")

            # Record outcome in the per_track entry.
            for pt in per_track:
                if pt["track_id"] == track_id:
                    pt["close_result"] = action
                    break

            if action == "closed":
                counts["closed"] += 1
            elif action == "stale_candidate":
                counts["stale"] += 1

    # ------------------------------------------------------------------ step 6
    # Summary + history.
    # ------------------------------------------------------------------ step 6
    finished_at = _now_utc()
    exit_code = 3 if counts["unverified"] > 0 else 0

    summary = {
        "run_id": run_id,
        "project_id": project_id,
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "evidence_source_health": {"gh": gh_health},
        "provenance": provenance,
        "counts": counts,
        "per_track": per_track,
    }
    _persist_summary(state_dir, summary)
    return summary, exit_code
