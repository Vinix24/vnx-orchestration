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
import re
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
_VALID_VERDICTS: FrozenSet[str] = frozenset({"ok", "false-candidate"})

# Number of consecutive clean advisory runs required before VNX_AUTO_CLOSE may flip.
FLIP_STREAK_REQUIRED = 7


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Operator review recording
# ---------------------------------------------------------------------------

def record_review(
    state_dir: Path,
    run_id: str,
    reviewer: str,
    verdict: str,
    note: Optional[str],
    ts: Optional[str] = None,
) -> None:
    """Append an operator review record to reconcile_history.ndjson.

    Raises ValueError when run_id does not appear as a reconcile-run record in
    the history file (callers translate this to CLI exit 2). Raises ValueError
    for an invalid verdict. Raises OSError on I/O failure.
    """
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"invalid verdict {verdict!r}: must be one of {sorted(_VALID_VERDICTS)}"
        )

    history_path = state_dir / _HISTORY_FILE

    run_found = False
    try:
        with open(history_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                # Review records themselves carry record="review"; skip them.
                if rec.get("record") == "review":
                    continue
                if rec.get("run_id") == run_id:
                    run_found = True
                    break
    except FileNotFoundError:
        pass

    if not run_found:
        raise ValueError(f"run_id {run_id!r} not found in reconcile history")

    review_record: Dict[str, Any] = {
        "record": "review",
        "run_id": run_id,
        "reviewer": reviewer,
        "verdict": verdict,
        "note": note or "",
        "ts": ts or _now_utc(),
    }

    try:
        with open(history_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(review_record) + "\n")
    except OSError as exc:
        raise OSError(f"cannot append review to {history_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Streak computation (flip criterion)
# ---------------------------------------------------------------------------

def compute_streak(
    state_dir: Path,
    project_id: str,
) -> Dict[str, Any]:
    """Compute the consecutive clean-run streak from reconcile_history.ndjson.

    A run enters the streak when:
      (a) evidence_source_health.gh == "ok" AND counts.unverified == 0
      (b) it has zero "false-candidate" reviews

    A degraded run (gh != "ok", unverified > 0, or any false-candidate review)
    breaks the streak.

    The VNX_AUTO_CLOSE flip criterion is met when the streak has at least
    FLIP_STREAK_REQUIRED (7) consecutive clean runs AND at least one run in
    the streak has a confirmed candidate that received an "ok" review.

    Returns:
      {
        "streak_length": int,
        "required_streak": int,    # always FLIP_STREAK_REQUIRED (7)
        "has_reviewed_confirmed": bool,
        "flip_criterion_met": bool,
        "runs": [...],     # run entries in streak (newest first)
        "project_id": str,
      }
    """
    history_path = state_dir / _HISTORY_FILE

    summaries: List[Dict[str, Any]] = []
    reviews_by_run: Dict[str, List[Dict[str, Any]]] = {}

    try:
        with open(history_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("record") == "review":
                    rid = rec.get("run_id", "")
                    reviews_by_run.setdefault(rid, []).append(rec)
                elif "run_id" in rec and "counts" in rec:
                    if rec.get("project_id") == project_id:
                        summaries.append(rec)
    except FileNotFoundError:
        pass

    # Walk from newest run to oldest
    summaries_newest_first = list(reversed(summaries))

    streak_runs: List[Dict[str, Any]] = []
    has_reviewed_confirmed = False

    for run in summaries_newest_first:
        run_id = run.get("run_id", "")
        gh_ok = run.get("evidence_source_health", {}).get("gh") == "ok"
        counts = run.get("counts") or {}
        unverified = counts.get("unverified", 0)
        confirmed = counts.get("confirmed", 0)
        is_clean = gh_ok and unverified == 0

        run_reviews = reviews_by_run.get(run_id, [])
        has_false_candidate = any(r.get("verdict") == "false-candidate" for r in run_reviews)
        has_ok_review = any(r.get("verdict") == "ok" for r in run_reviews)

        if not is_clean or has_false_candidate:
            break

        streak_runs.append({
            "run_id": run_id,
            "started_at": run.get("started_at"),
            "gh": run.get("evidence_source_health", {}).get("gh"),
            "confirmed": confirmed,
            "unverified": unverified,
            "reviews": run_reviews,
        })

        if confirmed > 0 and has_ok_review:
            has_reviewed_confirmed = True

    streak_length = len(streak_runs)
    flip_criterion_met = streak_length >= FLIP_STREAK_REQUIRED and has_reviewed_confirmed

    return {
        "streak_length": streak_length,
        "required_streak": FLIP_STREAK_REQUIRED,
        "has_reviewed_confirmed": has_reviewed_confirmed,
        "flip_criterion_met": flip_criterion_met,
        "runs": streak_runs,
        "project_id": project_id,
    }


# ---------------------------------------------------------------------------
# Tick command builder (testable helper)
# ---------------------------------------------------------------------------

def build_tick_command(
    vnx_dir: str,
    project_id: str,
    state_dir: str,
    *,
    auto_close: bool = False,
) -> List[str]:
    """Return the argv list the supervisor tick executes for `objective reconcile`.

    CHECK mode by default; --apply appended only when auto_close=True
    (i.e. VNX_AUTO_CLOSE=1 in the environment).
    """
    cmd = [
        "python3",
        str(Path(vnx_dir) / "scripts" / "planning_cli.py"),
        "objective", "reconcile",
        "--project-id", project_id,
        "--state-dir", state_dir,
    ]
    if auto_close:
        cmd.append("--apply")
    return cmd


def _make_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    token = os.urandom(3).hex()
    return f"{ts}-{token}"


# ---------------------------------------------------------------------------
# Persistent MERGED cache
# ---------------------------------------------------------------------------

def _get_repo_key(repo_root: Path) -> str:
    """Return a stable identifier for the repo at repo_root.

    Scopes the persistent PR-state cache so a cached entry from repo A cannot
    satisfy a lookup for the same PR number in repo B.

    Tries ``git remote get-url origin``; strips trailing .git and whitespace.
    Falls back to the resolved absolute path when the repo has no origin or
    when git is absent/times out.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_root),
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            if url:
                if url.endswith(".git"):
                    url = url[:-4]
                return url
    except (subprocess.TimeoutExpired, OSError):
        pass
    return str(repo_root.resolve())


def _load_full_cache_raw(path: Path) -> Dict[str, Any]:
    """Load and validate pr_state_cache.json.

    Returns {} on I/O error, JSON error, or when the file is in the old
    (flat PR-number-keyed) format — old format is treated as empty so the
    caller regenerates from live gh calls rather than migrating blindly.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        # Old format: top-level keys are PR numbers (pure digit strings).
        # New format: top-level keys are repo identifiers (URLs or paths).
        if any(str(k).strip().isdigit() for k in raw):
            return {}
        return raw
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _load_pr_state_cache(state_dir: Path, repo_key: str) -> Dict[str, Dict[str, Any]]:
    """Load the per-repo MERGED cache for repo_key: str(pr_number) → {state, mergedAt}.

    Returns {} on any error, on old-format files, or when repo_key is absent.
    """
    full = _load_full_cache_raw(state_dir / _PR_STATE_CACHE_FILE)
    repo_data = full.get(repo_key, {})
    return repo_data if isinstance(repo_data, dict) else {}


def _save_pr_state_cache(
    state_dir: Path, repo_key: str, repo_cache: Dict[str, Dict[str, Any]]
) -> None:
    """Atomically write/update pr_state_cache.json for repo_key.

    Preserves existing entries for other repo keys.
    Silently swallows I/O errors.
    """
    path = state_dir / _PR_STATE_CACHE_FILE
    full = _load_full_cache_raw(path)
    full[repo_key] = repo_cache
    tmp = path.parent / (path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(full, indent=2, sort_keys=True), encoding="utf-8")
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
# Re-close guard helpers (D6)
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
    except Exception as exc:
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
    # Re-close guard: skip tracks reopened from done→active when pr_ref
    # matches the stamp (unchanged) — reported as reopened_guard, not closed.
    # ------------------------------------------------------------------ step 3
    all_tracks = tracks_lib.list_tracks(state_dir, project_id)
    candidates: List[Dict[str, Any]] = []
    guarded_tracks: List[Dict[str, Any]] = []
    for t in all_tracks:
        phase = (t.get("phase") or "").strip()
        pr_ref = (t.get("pr_ref") or "").strip()
        if not pr_ref or phase in _SKIP_PHASES:
            continue
        pr_numbers = _parse_pr_numbers(pr_ref)
        if not pr_numbers:
            continue

        # Re-close guard: if a done→active reopen history row exists, compare
        # its stamped pr_ref against the current pr_ref.
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
        ] + guarded_tracks
        counts = {
            "tracks": total_tracks, "nominated": nominated, "confirmed": 0,
            "closed": 0, "closed_sibling": 0, "open_pr": 0,
            "unverified": len(candidates), "deferred": 0, "stale": 0,
            "reopened_guard": len(guarded_tracks),
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
    repo_key = _get_repo_key(repo_root)
    pr_cache = _load_pr_state_cache(state_dir, repo_key)
    gh_calls_used = 0
    counts = {
        "tracks": total_tracks, "nominated": nominated, "confirmed": 0,
        "closed": 0, "closed_sibling": 0, "open_pr": 0,
        "unverified": 0, "deferred": 0, "stale": 0,
        "reopened_guard": len(guarded_tracks),
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
        _save_pr_state_cache(state_dir, repo_key, pr_cache)

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
                "allow_closed_siblings": allow_closed_siblings,
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

    # Add re-close guarded tracks to the per-track report.
    per_track.extend(guarded_tracks)

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
