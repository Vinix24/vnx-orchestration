#!/usr/bin/env python3
"""objective_reconcile.py — batch git-grounded auto-close for the tracks layer (D3).

Called by planning_cli's ``vnx objective reconcile`` subcommand.

Steps per run (executed in order):
  1. Provenance sweep — best-effort; failure never blocks remaining steps.
  2. Derived refresh  — reconcile_all_tracks persists derived_status for every
                        track in the project (both check and apply modes).
  3. Nomination       — tracks with non-empty pr_ref, declared phase not in
                        {done, parked}.  Does NOT gate on derived_status.
                        OI-1098: a track with ANY currently-linked PR
                        explicitly marked delivery_kind != 'complete' is held
                        (verdict 'delivery_hold', with reason) — never
                        nominated, never silently dropped.
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
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import tracks as tracks_lib  # same package; importable when scripts/lib/ is in sys.path
import track_reconciler
from track_reconciler import _parse_pr_numbers, EvidenceSnapshot

# Steps 2, 2b, 3, 4a and 4b of run_reconcile, moved verbatim to their own
# module (S4, then S5). _parse_reopen_stamp is imported here as a re-export:
# the reopen-guard tests import it from this module.
from objective_reconcile_steps import (
    _parse_reopen_stamp,
    gh_unavailable_result,
    nominate_candidates,
    recompute_dispatch_outcomes,
    refresh_derived,
    verify_candidates,
)

log = logging.getLogger(__name__)

_PR_STATE_CACHE_FILE = "pr_state_cache.json"
_SUMMARY_FILE = "reconcile_summary.json"
_HISTORY_FILE = "reconcile_history.ndjson"
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

# Absolute fallback locations for the gh binary. A launchd/cron background or a
# minimal PATH (non-interactive shell) often omits the Homebrew bin dir even
# though gh is installed and works in an interactive terminal. Resolving these
# explicitly prevents a live gh from being misreported as "absent".
_GH_FALLBACK_PATHS: Tuple[str, ...] = (
    "/opt/homebrew/bin/gh",
    "/usr/local/bin/gh",
    "/usr/bin/gh",
)


def _resolve_gh_binary() -> Optional[str]:
    """Resolve the gh binary to an absolute path, or None when truly absent.

    First honors the calling PATH via shutil.which (interactive shells), then a
    short list of absolute fallback paths for environments whose PATH omits the
    gh directory. Never raises.
    """
    found = shutil.which("gh")
    if found:
        return found
    for candidate in _GH_FALLBACK_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _detect_gh(repo_root: Path, gh_binary: Optional[str] = None) -> str:
    """Return 'ok', 'absent', 'auth_failed', or 'timeout'."""
    gh = gh_binary or _resolve_gh_binary()
    if not gh:
        return "absent"
    try:
        result = subprocess.run(
            [gh, "auth", "status"],
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


def _gh_pr_view(
    pr_number: int, repo_root: Path, timeout: int = 15, gh_binary: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Call ``gh pr view <n> --json state,mergedAt``. Returns dict or None on error."""
    gh = gh_binary or _resolve_gh_binary()
    if not gh:
        return None
    try:
        result = subprocess.run(
            [gh, "pr", "view", str(pr_number), "--json", "state,mergedAt"],
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
# Close-evidence gathering (shared by run_reconcile and the close verb)
# ---------------------------------------------------------------------------
#
# OI-1071: PR #1402 (OI-1064) threaded gh-confirmed merge evidence into the
# ``reconcile --apply`` close path (run_reconcile step 4c + close_track_if_done),
# but the operator-facing ``objective close`` verb did NOT call that path — it
# peeked/reconciled with the local-only set, fell back to _load_merged_pr_numbers
# (whose gh source is default-ON since OI-1155, opt out via VNX_RECONCILE_GIT=0), and
# re-derived 'queued' for a track whose PRs merged via a bare ``gh pr merge``.
#
# The two helpers below are the SINGLE source of the evidence set both code
# paths consume, so the bulk reconcile sweep and the standalone close verb
# cannot drift apart:
#
#   merge_evidence_pr_numbers — the UNION primitive. Takes a set of
#     gh-confirmed MERGED PR numbers and returns the union with the
#     locally-loaded set (NDJSON receipts). run_reconcile calls this after its
#     own gh sweep; gather_close_evidence calls this after the per-track sweep.
#
#   gather_close_evidence — the per-track gather the close verb uses. Resolves
#     the track's pr_ref, fetches only THAT track's PRs (bounded by max_gh_calls,
#     cache-first), and returns the union set + a health report so the verb can
#     degrade honestly when gh is unreachable.

def merge_evidence_pr_numbers(
    gh_confirmed_merged: "FrozenSet[int]",
    state_dir: Path,
    repo_root: Optional[Path] = None,
) -> FrozenSet[int]:
    """Return the UNION of gh-confirmed MERGED PR numbers and the local set.

    gh evidence is additive: it never replaces local NDJSON evidence,
    only adds what a bare ``gh pr merge`` never wrote locally. This is the
    exact union run_reconcile builds (step 4c) — extracted so the standalone
    close verb and the bulk sweep reach it through the SAME function and
    cannot drift.

    Local loading reuses track_reconciler._load_merged_pr_numbers (sources
    1-3 are local and deterministic; source 4 — ``gh pr list``, default-ON
    since OI-1155 — is cache-first so it reuses the 10-min cache the
    reconciler already warmed and adds no per-PR ``gh pr view`` calls).
    """
    local_merged = track_reconciler._load_merged_pr_numbers(state_dir, repo_root)
    return frozenset(set(gh_confirmed_merged) | set(local_merged))


def gather_close_evidence(
    state_dir: Path,
    project_id: str,
    track_id: str,
    *,
    repo_root: Optional[Path] = None,
    max_gh_calls: int = 50,
    pr_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Gather the same merge evidence run_reconcile does, for ONE track.

    Returns::

        {
          "merged_pr_numbers": FrozenSet[int],  # gh-confirmed UNION local
          "gh_health": "ok" | "absent" | "auth_failed" | "timeout",
          "consulted_gh": bool,                  # False when gh unreachable / no PRs
          "pr_results": List[Dict],              # per-PR {number, state, mergedAt}
          "pr_ref": str,                          # the pr_ref resolved for the track
          "pr_numbers": FrozenSet[int],           # parsed PR numbers from pr_ref
        }

    Resolves the track's ``pr_ref`` (unless ``pr_ref`` is passed), fetches only
    that track's PRs through the per-PR cache (so a PR already verified this run
    or recently cached is not re-fetched), and bounds live gh calls by
    ``max_gh_calls``. When gh is absent/unauthenticated/timed out OR the track
    has no pr_ref, falls back to the local-only set and reports
    ``consulted_gh=False`` so the caller can SAY it could not reach GitHub
    rather than silently behaving as if it checked.

    Never raises; gh failures degrade to local-only.
    """
    # Resolve the track's pr_ref from the DB when the caller did not pass it.
    if pr_ref is None:
        try:
            lib = tracks_lib.get_track(state_dir, track_id, project_id)
            if lib is not None:
                pr_ref = (lib.get("pr_ref") or "").strip()
        except Exception:  # vnx-silent-except: best-effort pr_ref resolve
            pr_ref = None
    pr_ref = (pr_ref or "").strip()
    pr_numbers: FrozenSet[int] = _parse_pr_numbers(pr_ref) if pr_ref else frozenset()

    pr_results: List[Dict[str, Any]] = []
    gh_confirmed_merged: set = set()

    # No PRs to verify → nothing to fetch; the union is the local set alone.
    # repo_root is left as-is (possibly None) so the local loader resolves it
    # exactly as the pre-OI-1071 peek/reconcile path did — no eager git probe.
    if not pr_numbers:
        return {
            "merged_pr_numbers": merge_evidence_pr_numbers(frozenset(), state_dir, repo_root),
            "gh_health": "ok",
            "consulted_gh": False,
            "pr_results": [],
            "pr_ref": pr_ref,
            "pr_numbers": pr_numbers,
        }

    # gh probes need a concrete repo root for cwd=. Resolve once, only when
    # there are PRs to fetch (the no-pr_ref hot path above skips this).
    if repo_root is None:
        repo_root = _resolve_effective_repo_root()

    gh_binary = _resolve_gh_binary()
    gh_health = _detect_gh(repo_root, gh_binary)
    if gh_health != "ok":
        # gh unavailable: degrade honestly to local-only. Caller MUST surface
        # that gh could not be consulted instead of pretending it checked.
        return {
            "merged_pr_numbers": merge_evidence_pr_numbers(frozenset(), state_dir, repo_root),
            "gh_health": gh_health,
            "consulted_gh": False,
            "pr_results": [],
            "pr_ref": pr_ref,
            "pr_numbers": pr_numbers,
        }

    # Cache-first fetch, bounded by max_gh_calls. Reuses the same per-PR cache
    # run_reconcile uses so a PR already verified this run is not re-fetched.
    repo_key = _get_repo_key(repo_root)
    pr_cache = _load_pr_state_cache(state_dir, repo_key)
    gh_calls_used = 0

    per_pr: Dict[int, Optional[Dict[str, Any]]] = {}
    prs_to_fetch: List[int] = []
    for pn in pr_numbers:
        cached = pr_cache.get(str(pn))
        if cached and _is_merged(cached):
            per_pr[pn] = cached
        else:
            prs_to_fetch.append(pn)

    # Bound live calls. A single close handles ONE track, so the cap is the
    # track's PR count at most; if the cap is lower than that, fetch what fits
    # and report the rest as unverified (consulted_gh stays True — gh WAS
    # reached, just budget-capped).
    uncapped_remainder: List[int] = []
    if len(prs_to_fetch) > max_gh_calls:
        uncapped_remainder = prs_to_fetch[max_gh_calls:]
        prs_to_fetch = prs_to_fetch[:max_gh_calls]

    for pn in prs_to_fetch:
        gh_calls_used += 1
        data = _gh_pr_view(pn, repo_root, gh_binary=gh_binary)
        per_pr[pn] = data
        if data is not None and _is_merged(data):
            pr_cache[str(pn)] = data

    # Persist any newly cached MERGED results.
    _save_pr_state_cache(state_dir, repo_key, pr_cache)

    # Build the per-PR result list and the gh-confirmed MERGED set, mirroring
    # run_reconcile step 4c's extraction.
    for pn in sorted(pr_numbers):
        data = per_pr.get(pn)
        if data is None:
            if pn in uncapped_remainder:
                pr_results.append({"number": pn, "state": None, "mergedAt": None})
            else:
                pr_results.append({"number": pn, "state": None, "mergedAt": None})
            continue
        pr_results.append({
            "number": pn,
            "state": data.get("state"),
            "mergedAt": data.get("mergedAt"),
        })
        if _is_merged(data):
            gh_confirmed_merged.add(pn)

    return {
        "merged_pr_numbers": merge_evidence_pr_numbers(
            frozenset(gh_confirmed_merged), state_dir, repo_root
        ),
        "gh_health": gh_health,
        "consulted_gh": True,
        "pr_results": pr_results,
        "pr_ref": pr_ref,
        "pr_numbers": pr_numbers,
    }


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

def _resolve_effective_repo_root() -> Path:
    """Resolve a concrete project repo root for the gh merge-state lookups.

    Used when the caller passes ``repo_root=None``. Prefers the canonical
    PROJECT_ROOT (VNX_HOME + project-marker aware — never the keystone), then the
    CWD git-root, then the CWD. Always returns a real directory so ``gh``
    subprocess ``cwd=`` never receives ``None``.
    """
    try:
        from vnx_paths import resolve_paths
        candidate = Path(resolve_paths()["PROJECT_ROOT"])
        if candidate.is_dir():
            return candidate
    except Exception:  # vnx-silent-except: best-effort — fall through to the git-root candidate
        pass
    try:
        git_root = track_reconciler._git_toplevel(Path.cwd())
        if git_root is not None:
            return git_root
    except Exception:  # vnx-silent-except: best-effort — fall through to the CWD fallback
        pass
    return Path.cwd()


def _run_provenance_sweep(
    state_dir: Path, repo_root: Path, project_id: str,
) -> Dict[str, int]:
    """Scan recent commits for dispatch->commit provenance links.

    Best-effort at the scan/link level, exactly as before: a git or SQL error
    while reading/registering leaves ``provenance`` at the zero-default and is
    logged at debug, never blocking the rest of ``run_reconcile``.

    A COMMIT failure is a different class of problem. ``reconcile_commit_provenance``
    writes into ``prov_conn`` under sqlite3's implicit transaction — those rows are
    invisible outside this call until ``commit()`` succeeds. Folding a commit failure
    into the same debug-level catch-all would repeat the exact bug this function
    fixes: reporting non-zero ``linked`` counts for edges that never survived. So a
    commit failure is logged loudly (``log.error``) and the returned counts fall
    back to the zero-default rather than the pre-commit ``result``.
    """
    provenance: Dict[str, int] = {"scanned": 0, "linked": 0}
    try:
        from receipt_provenance import reconcile_commit_provenance  # noqa: PLC0415
        db_path = state_dir / track_reconciler.DB_FILENAME
        prov_conn = sqlite3.connect(str(db_path), timeout=10.0)
        prov_conn.row_factory = sqlite3.Row
        try:
            # Finding A (ADR-007 composite-safety): this reconcile pipeline
            # already operates on a single project_id's per-project store —
            # pass it through so the dispatch_metadata.pr_id backfill never
            # falls back to an ambiguous dispatch_id-only lookup.
            result = reconcile_commit_provenance(repo_root, prov_conn, project_id=project_id)
            try:
                prov_conn.commit()
            except sqlite3.Error:
                log.error(
                    "provenance sweep: commit failed — %d scanned/%d linked "
                    "edge(s) rolled back, not counted",
                    result.get("scanned", 0), result.get("linked", 0),
                    exc_info=True,
                )
            else:
                # ``result["linked_pending_commit"]`` was computed by
                # reconcile_commit_provenance BEFORE this commit — it mirrors
                # ``linked`` because prov_conn's implicit transaction was
                # still open at that point. Now that prov_conn.commit() has
                # actually succeeded, every one of those writes is durable:
                # the caller-visible count must reflect that, not the
                # pre-commit snapshot, or it repeats the exact "non-zero
                # pending after a successful commit" lie this field exists
                # to prevent.
                result["linked_pending_commit"] = 0
                provenance = result
        finally:
            prov_conn.close()
    except Exception as exc:  # noqa: BLE001
        log.debug("provenance sweep non-fatal: %s", exc)
    return provenance


def run_reconcile(
    state_dir: Path,
    project_id: str,
    *,
    repo_root: Optional[Path] = None,
    apply: bool = False,
    allow_closed_siblings: bool = False,
    max_gh_calls: int = 50,
) -> Tuple[Dict[str, Any], int]:
    """Run the full reconcile pipeline for project_id.

    Returns (summary_dict, exit_code) where exit_code is 0, 2, or 3.
    Never raises — all errors are captured in the summary and returned as exit 3.

    ``repo_root`` may be None: the gh PR lookups need a concrete project repo,
    so a None is resolved here to the canonical PROJECT_ROOT / CWD git-root /
    CWD (track_reconciler's ``None``-handling) rather than degrading gh to
    ``cwd="None"`` (= absent).
    """
    # None-tolerance: never pass None into gh (cwd="None") or downstream.
    if repo_root is None:
        repo_root = _resolve_effective_repo_root()

    run_id = _make_run_id()
    started_at = _now_utc()
    mode = "apply" if apply else "check"

    # ------------------------------------------------------------------ step 1
    # Provenance sweep — best-effort; never blocks the rest.
    provenance = _run_provenance_sweep(state_dir, repo_root, project_id)

    # ------------------------------------------------------------------ step 2
    # Derived refresh — persists derived_status for every track.
    reconcile_results, refresh_failure = refresh_derived(
        state_dir, project_id, repo_root,
        run_id=run_id, mode=mode, started_at=started_at, provenance=provenance,
    )
    if refresh_failure is not None:
        return refresh_failure

    total_tracks = len(reconcile_results)

    # ------------------------------------------------------------------ step 2b
    # Dispatch-outcome recompute (receipt-quality PR-B3) — same cadence as the
    # derived-status refresh above. Best-effort; failure never blocks the
    # track-close pipeline below.
    dispatch_outcomes_summary = recompute_dispatch_outcomes(
        state_dir, project_id, repo_root
    )

    # ------------------------------------------------------------------ step 3
    # Nomination: non-empty pr_ref + declared phase not in {done, parked}.
    # Re-close guard: skip tracks reopened from done→active when pr_ref
    # matches the stamp (unchanged) — reported as reopened_guard, not closed.
    candidates, guarded_tracks, held_tracks = nominate_candidates(state_dir, project_id)

    nominated = len(candidates)

    # ------------------------------------------------------------------ step 4a
    # gh detection — absent / auth-failed → all unverified, exit 3.
    # Resolve the binary once per run and reuse it for every PR lookup, so a
    # PATH that differs from an interactive shell (launchd/cron background)
    # cannot flip a live gh to "absent" partway through a run.
    degraded, gh_binary, gh_health = gh_unavailable_result(
        state_dir, project_id, repo_root,
        run_id=run_id, mode=mode, started_at=started_at, provenance=provenance,
        dispatch_outcomes_summary=dispatch_outcomes_summary,
        total_tracks=total_tracks, nominated=nominated, candidates=candidates,
        guarded_tracks=guarded_tracks, held_tracks=held_tracks,
    )
    if degraded is not None:
        return degraded

    # ------------------------------------------------------------------ step 4b
    # Per-candidate verification with persistent MERGED cache.
    verified = verify_candidates(
        state_dir, repo_root, candidates, guarded_tracks, held_tracks,
        gh_binary=gh_binary, total_tracks=total_tracks, nominated=nominated,
        max_gh_calls=max_gh_calls, allow_closed_siblings=allow_closed_siblings,
    )
    per_track = verified["per_track"]
    counts = verified["counts"]
    confirmed_candidates = verified["confirmed_candidates"]

    # ------------------------------------------------------------------ step 4c
    # OI-1064: thread the gh-confirmed merge evidence into the derived-status
    # derivation so it is not discarded. The bulk pass (step 2) ran BEFORE the
    # gh sweep, so a track whose pr_ref PRs were merged via a bare ``gh pr
    # merge`` (no local pr_merged receipt) derived 'queued' there — even though
    # reconcile itself just confirmed those PRs merged via gh. Threading the
    # evidence here lets the close path re-derive 'done' from evidence it
    # ALREADY gathered (source 3, default-ON since OI-1155, makes its own
    # ``gh pr list`` call; this threading adds no per-PR ``gh pr view`` calls).
    #
    # The injected set is a UNION of the gh-confirmed MERGED PR numbers and the
    # locally-loaded set (NDJSON receipts). Local evidence stays valid; gh
    # only ADDS what a bare ``gh pr merge`` never wrote locally. No new per-PR
    # ``gh pr view`` calls: only MERGED results already fetched in step 4b are
    # extracted, so the --max-gh-calls cap still holds for the whole run.
    # ------------------------------------------------------------------ step 4c
    gh_confirmed_merged: set = set()
    for cc in confirmed_candidates:
        for pr in cc["pr_results"]:
            if isinstance(pr, dict) and _is_merged({
                "state": pr.get("state"),
                "mergedAt": pr.get("mergedAt"),
            }):
                try:
                    gh_confirmed_merged.add(int(pr["number"]))
                except (TypeError, ValueError, KeyError):
                    pass
    # OI-1071: the UNION of gh-confirmed MERGED PR numbers and the local set is
    # built by the shared primitive so the standalone close verb and this bulk
    # sweep consume the SAME function and cannot drift apart.
    evidence_merged_pr_numbers: FrozenSet[int] = merge_evidence_pr_numbers(
        frozenset(gh_confirmed_merged), state_dir, repo_root
    )

    # Re-derive the confirmed tracks so the bulk-pass-derived_status (which ran
    # before the gh sweep and wrote 'queued' for bare-gh-merge tracks) is
    # corrected in the DB with the same evidence the close path will use. This
    # is idempotent (reconcile_track rewrites derived_status) and adds zero gh
    # calls. Non-apply mode also benefits: the derived_status persisted for
    # reporting matches the evidence reconcile actually gathered.
    for cc in confirmed_candidates:
        track_reconciler.reconcile_track(
            state_dir, cc["track_id"], project_id,
            repo_root=repo_root,
            _merged_pr_numbers=evidence_merged_pr_numbers,
        )

    # ------------------------------------------------------------------ step 5
    # Close confirmed candidates (--apply only). The evidence set from step 4c
    # is forwarded so reconcile_track inside close_track_if_done sees the same
    # complete evidence (gh-confirmed UNION local) instead of falling back to
    # the local-only set.
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
                repo_root=repo_root,
                merged_pr_numbers=evidence_merged_pr_numbers,
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

    # Add re-close guarded and delivery-held tracks to the per-track report.
    # OI-1098: held tracks MUST stay visible — a track that silently drops
    # out of the report is as wrong as one that closes too early.
    per_track.extend(guarded_tracks)
    per_track.extend(held_tracks)

    # ------------------------------------------------------------------ step 6
    # Summary + history.
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
        "dispatch_outcomes": dispatch_outcomes_summary,
        "counts": counts,
        "per_track": per_track,
    }
    _persist_summary(state_dir, summary)
    return summary, exit_code
