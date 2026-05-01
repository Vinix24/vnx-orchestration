"""git_helpers — git-introspection helpers used during dispatch."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    """Resolve repo root: scripts/lib/subprocess_dispatch_internals/<this> -> ../../../."""
    return Path(__file__).resolve().parents[3]


def _get_commit_hash() -> str:
    """Return current HEAD commit hash, or empty string on failure."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=_repo_root(),
        )
        return proc.stdout.strip()
    except Exception as exc:
        logger.debug("_get_commit_hash failed: %s", exc)
        return ""


def _get_current_branch() -> str:
    """Return current branch name, or empty string on failure."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=_repo_root(),
        )
        return proc.stdout.strip()
    except Exception as exc:
        logger.debug("_get_current_branch failed: %s", exc)
        return ""


def _count_lines_changed_since_sha(
    pre_sha: str, paths: "list[str] | None" = None
) -> int:
    """Count lines added+removed between pre_sha and HEAD via git diff --numstat.

    Replaces the time-window-based ``_count_lines_changed`` from
    dispatch_parameter_tracker, which over-counted unrelated commits made
    by parallel dispatches in the same window.

    paths, when provided, restricts the diff to specific pathspecs so the
    count attributes only files inside the dispatch's declared scope.
    Returns 0 on any failure (never raises).
    """
    if not pre_sha:
        return 0
    try:
        cwd = _repo_root()
        cmd = ["git", "diff", "--numstat", f"{pre_sha}..HEAD"]
        if paths:
            cmd.append("--")
            cmd.extend(paths)
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, cwd=cwd,
        )
        total = 0
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                try:
                    total += int(parts[0]) + int(parts[1])
                except ValueError:
                    # Binary files report "-\t-\t<path>" — skip silently.
                    pass
        return total
    except Exception as exc:
        logger.debug("_count_lines_changed_since_sha failed: %s", exc)
        return 0


def _commit_belongs_to_dispatch(commit_hash: str, dispatch_id: str) -> bool:
    """Return True if the given commit's message contains the dispatch_id marker.

    Used by deliver_with_recovery to determine whether a HEAD change between
    pre-dispatch and post-dispatch was actually produced by THIS dispatch
    (vs. a concurrent commit from another terminal in a shared worktree).

    Never raises.  Returns False on any error or empty input.
    """
    if not commit_hash or not dispatch_id:
        return False
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--pretty=%B", commit_hash],
            capture_output=True, text=True, timeout=10,
            cwd=_repo_root(),
        )
        if proc.returncode != 0:
            return False
        return f"Dispatch-ID: {dispatch_id}" in proc.stdout
    except Exception as exc:
        logger.debug(
            "_commit_belongs_to_dispatch failed for %s/%s: %s",
            commit_hash, dispatch_id, exc,
        )
        return False


def _check_commit_since(dispatch_start_ts: str, dispatch_id: str | None = None) -> bool:
    """Return True if no commits attributable to this dispatch are found.

    When ``dispatch_id`` is provided, the check is *dispatch-scoped*: only
    commits whose message contains ``Dispatch-ID: <dispatch_id>`` count as
    "this dispatch committed".  This prevents another terminal's commit
    landing during the dispatch window from being mis-attributed to this
    dispatch — a real correctness risk in shared worktrees where multiple
    headless workers operate concurrently.

    When ``dispatch_id`` is None, falls back to the legacy time-window check
    (any commit since ``dispatch_start_ts``) for backward compatibility with
    callers that haven't been updated.

    Never raises.
    """
    if dispatch_id:
        scoped = _check_dispatch_scoped_commit(dispatch_start_ts, dispatch_id)
        if scoped is not None:
            return scoped

    enforcer_result = _check_via_governance_enforcer(dispatch_start_ts)
    if enforcer_result is not None:
        return enforcer_result

    return _check_via_direct_git_log(dispatch_start_ts)


def _check_dispatch_scoped_commit(
    dispatch_start_ts: str, dispatch_id: str
) -> "bool | None":
    """Dispatch-scoped commit check via grep.  Returns None on failure."""
    try:
        proc = subprocess.run(
            [
                "git",
                "log",
                "--all",
                f"--since={dispatch_start_ts}",
                f"--grep=Dispatch-ID: {dispatch_id}",
                "--oneline",
                "-5",
            ],
            capture_output=True, text=True, timeout=10,
            cwd=_repo_root(),
        )
        dispatch_commits = [l for l in proc.stdout.splitlines() if l.strip()]
        if not dispatch_commits:
            logger.warning(
                "receipt_must_have_commit: no commits with 'Dispatch-ID: %s' "
                "found since %s (commit attribution scoped to this dispatch)",
                dispatch_id, dispatch_start_ts,
            )
            return True
        return False
    except Exception as exc:
        logger.debug(
            "dispatch-scoped commit check failed for %s: %s — falling back to time-window check",
            dispatch_id, exc,
        )
        return None


def _check_via_governance_enforcer(dispatch_start_ts: str) -> "bool | None":
    """Time-window commit check via GovernanceEnforcer.  Returns None on failure."""
    try:
        from governance_enforcer import GovernanceEnforcer, DEFAULT_CONFIG_PATH  # noqa: PLC0415
        enforcer = GovernanceEnforcer()
        if DEFAULT_CONFIG_PATH.exists():
            enforcer.load_config(DEFAULT_CONFIG_PATH)
        result = enforcer.check(
            "receipt_must_have_commit",
            {"dispatch_timestamp": dispatch_start_ts},
        )
        if not result.passed:
            logger.warning("receipt_must_have_commit: %s", result.message)
            return True
        return False
    except Exception as exc:
        logger.debug("commit check (enforcer path) failed: %s — using git directly", exc)
        return None


def _check_via_direct_git_log(dispatch_start_ts: str) -> bool:
    """Final fallback — direct git log.  Returns True on no commits or error."""
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", f"--since={dispatch_start_ts}", "-5"],
            capture_output=True, text=True, timeout=10,
        )
        commits = [l for l in proc.stdout.splitlines() if l.strip()]
        if not commits:
            logger.warning("receipt_must_have_commit: no commits found since %s", dispatch_start_ts)
            return True
    except Exception as exc:
        logger.debug("git log fallback failed: %s", exc)
    return False
