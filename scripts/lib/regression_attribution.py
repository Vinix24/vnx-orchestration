#!/usr/bin/env python3
"""Regression attribution primitive — name the commit that broke a check.

Given a shell command that currently fails (``bad_ref``, default ``HEAD``)
but is known to have passed at some earlier point (``good_ref``), this finds
the exact commit that introduced the failure via ``git bisect`` and reports
its author, date, subject, and changed files.

This is the CORE PRIMITIVE only (track: regression-attribution-canary,
PR-1). No scheduler, no open-item wiring, no notifications — those are
later PRs. `attribute_regression()` is pure and testable: point it at any
git repo, get back an `AttributionResult`.

Mechanism: `git bisect` (via `git bisect run`), not a reimplementation.

Safety:
  - Refuses to run against a dirty working tree (raises DirtyWorkingTreeError)
    so uncommitted work can never be lost or silently checked out over.
  - Always restores the original HEAD/branch in a `finally` block, even on
    exceptions raised from inside the bisect run.

BILLING SAFETY: No Anthropic SDK. No LLM calls. Pure git/subprocess.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUS_ATTRIBUTED = "attributed"
STATUS_INCONCLUSIVE = "inconclusive"

_FIRST_BAD_RE = re.compile(r"\b([0-9a-f]{7,40}) is the first bad commit\b")
_DIFFSTAT_LINE_RE = re.compile(r"^\s*(.+?)\s+\|\s+\d+")


class RegressionAttributionError(RuntimeError):
    """Raised when git/bisect fails in a way attribution cannot recover from."""


class DirtyWorkingTreeError(RegressionAttributionError):
    """Raised when the target working tree has uncommitted changes.

    Attribution must checkout arbitrary refs during bisection; running
    against a dirty tree risks losing uncommitted work, so it is refused
    outright rather than stashed automatically.
    """


@dataclass
class AttributionResult:
    """Result of an `attribute_regression()` call."""

    status: str  # "attributed" | "inconclusive"
    check_cmd: str
    good_ref: str
    bad_ref: str
    good_sha: Optional[str] = None
    bad_sha: Optional[str] = None
    reason: Optional[str] = None
    commit_sha: Optional[str] = None
    author: Optional[str] = None
    author_email: Optional[str] = None
    date: Optional[str] = None
    subject: Optional[str] = None
    changed_files: List[str] = field(default_factory=list)
    stat_summary: Optional[str] = None

    @property
    def is_attributed(self) -> bool:
        return self.status == STATUS_ATTRIBUTED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "check_cmd": self.check_cmd,
            "good_ref": self.good_ref,
            "bad_ref": self.bad_ref,
            "good_sha": self.good_sha,
            "bad_sha": self.bad_sha,
            "reason": self.reason,
            "commit_sha": self.commit_sha,
            "author": self.author,
            "author_email": self.author_email,
            "date": self.date,
            "subject": self.subject,
            "changed_files": self.changed_files,
            "stat_summary": self.stat_summary,
        }


# ---------------------------------------------------------------------------
# git plumbing helpers
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RegressionAttributionError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result


def _rev_parse(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", ref).stdout.strip()


def _current_ref(root: Path) -> str:
    """Return the branch name if HEAD is on one, else the detached commit sha."""
    result = _git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return _rev_parse(root, "HEAD")


def _checkout(root: Path, ref: str) -> None:
    _git(root, "checkout", "--quiet", ref)


def _require_clean_worktree(root: Path) -> None:
    result = _git(root, "status", "--porcelain")
    if result.stdout.strip():
        raise DirtyWorkingTreeError(
            f"refusing to run regression attribution on a dirty working tree "
            f"at {root} — commit or stash your changes first"
        )


def _run_check(root: Path, check_cmd: str) -> bool:
    result = subprocess.run(
        ["bash", "-c", check_cmd],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _run_bisect(root: Path, check_cmd: str, good_sha: str, bad_sha: str) -> str:
    """Run `git bisect` between good_sha (passes) and bad_sha (fails).

    Returns the full sha of the first commit at which check_cmd fails.
    Assumes bisect state is clean on entry; caller is responsible for
    `git bisect reset` in a finally block.
    """
    _git(root, "bisect", "start", bad_sha, good_sha)

    run_result = subprocess.run(
        ["git", "bisect", "run", "bash", "-c", check_cmd],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    output = f"{run_result.stdout}\n{run_result.stderr}"
    match = _FIRST_BAD_RE.search(output)
    if not match:
        raise RegressionAttributionError(
            f"git bisect run did not converge to a single commit:\n{output.strip()}"
        )
    return _rev_parse(root, match.group(1))


def _commit_details(root: Path, sha: str) -> Dict[str, Any]:
    log_fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s"
    log_out = _git(root, "log", "-1", f"--format={log_fmt}", sha).stdout.strip()
    full_sha, author, author_email, date, subject = log_out.split("\x1f", 4)

    stat_out = _git(root, "show", "--stat", "--format=", sha).stdout

    changed_files: List[str] = []
    for line in stat_out.splitlines():
        stat_match = _DIFFSTAT_LINE_RE.match(line)
        if stat_match:
            changed_files.append(stat_match.group(1).strip())

    return {
        "commit_sha": full_sha,
        "author": author,
        "author_email": author_email,
        "date": date,
        "subject": subject,
        "changed_files": changed_files,
        "stat_summary": stat_out.strip(),
    }


# ---------------------------------------------------------------------------
# Public primitive
# ---------------------------------------------------------------------------

def attribute_regression(
    check_cmd: str,
    good_ref: str,
    bad_ref: str = "HEAD",
    repo_root: "str | Path | None" = None,
) -> AttributionResult:
    """Find the commit that made `check_cmd` start failing.

    Args:
        check_cmd: shell command run via `bash -c`; exit 0 = pass, nonzero = fail.
        good_ref: a ref known to pass check_cmd.
        bad_ref: a ref known to fail check_cmd (default: HEAD).
        repo_root: git repo to operate in (default: cwd).

    Returns:
        AttributionResult with status "attributed" (commit found) or
        "inconclusive" (bad_ref already passes, or good_ref already fails —
        not a regression in this range; no bisect is run in either case).

    Raises:
        DirtyWorkingTreeError: working tree has uncommitted changes.
        RegressionAttributionError: git/bisect failed unexpectedly.
    """
    root = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
    _require_clean_worktree(root)

    good_sha = _rev_parse(root, good_ref)
    bad_sha = _rev_parse(root, bad_ref)
    original_ref = _current_ref(root)

    try:
        _checkout(root, bad_sha)
        if _run_check(root, check_cmd):
            return AttributionResult(
                status=STATUS_INCONCLUSIVE,
                check_cmd=check_cmd,
                good_ref=good_ref,
                bad_ref=bad_ref,
                good_sha=good_sha,
                bad_sha=bad_sha,
                reason=(
                    f"check_cmd passed at bad_ref ({bad_ref} = {bad_sha[:12]}); "
                    "nothing to attribute"
                ),
            )

        _checkout(root, good_sha)
        if not _run_check(root, check_cmd):
            return AttributionResult(
                status=STATUS_INCONCLUSIVE,
                check_cmd=check_cmd,
                good_ref=good_ref,
                bad_ref=bad_ref,
                good_sha=good_sha,
                bad_sha=bad_sha,
                reason=(
                    f"check_cmd also failed at good_ref ({good_ref} = {good_sha[:12]}); "
                    "not a regression in this range"
                ),
            )

        commit_sha = _run_bisect(root, check_cmd, good_sha, bad_sha)
        details = _commit_details(root, commit_sha)
        return AttributionResult(
            status=STATUS_ATTRIBUTED,
            check_cmd=check_cmd,
            good_ref=good_ref,
            bad_ref=bad_ref,
            good_sha=good_sha,
            bad_sha=bad_sha,
            **details,
        )
    finally:
        _git(root, "bisect", "reset", check=False)
        _git(root, "checkout", "--quiet", original_ref, check=False)
