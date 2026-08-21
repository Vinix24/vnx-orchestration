#!/usr/bin/env python3
"""worktree_release.py — Governed release path for locked worktrees (OI-1052).

Provides a dry-run-first, classification-driven release pipeline for locked git
worktrees: re-classify each one, rescue committable/unpushed work to origin
branches, then unlock and remove the worktree. Nothing is deleted without an
explicit ``--apply`` flag — the default is a dry-run report.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# ── release-specific classification ──────────────────────────────────────
# The four release classifications, mapped from classify_path's
# {dirty, clean, committed, pushed} with an extra distinction inside "dirty":
#   * committable   — unstaged/staged changes, no local-only commits
#   * unpushed      — local commits not on origin, working tree clean
#   * both          — both uncommitted changes AND local-only commits
#   * releasable    — clean (no changes, no local-only commits) or pushed

ReleaseClass = Literal[
    "releasable", "committable", "unpushed_commits", "both",
    "detached", "unreachable", "error",
]

_UNSAFE_BRANCH_CHARS = re.compile(r"[^A-Za-z0-9._/@-]")

# Exit code for a refused repo-root conflict (main() / OI-1389 fix-forward).
# Chosen not to collide with 0 (success), 1 (problems/partial cleanup, see
# main()'s final return), or 2 (argparse's own usage-error exit code).
EXIT_REPO_ROOT_CONFLICT = 3


class RepoRootConflictError(RuntimeError):
    """Raised by ``_resolve_repo_root`` when PROJECT_ROOT and the cwd-derived
    git root name different repositories during a destructive (--apply) run.

    See ``_resolve_repo_root`` for the full precedence rule this enforces.
    """

    def __init__(self, env_root: Path, cwd_root: Path):
        self.env_root = env_root
        self.cwd_root = cwd_root
        super().__init__(
            "Refusing --apply: PROJECT_ROOT and the current working "
            "directory's git root point at two different repositories.\n"
            f"  PROJECT_ROOT env root : {env_root}\n"
            f"  cwd git root          : {cwd_root}\n"
            "Pick one explicitly: pass --repo-root <path> to say which repo "
            "you mean, or unset/align PROJECT_ROOT with your cwd, then retry."
        )


@dataclass
class ReleaseEntry:
    """Outcome for a single locked worktree."""
    worktree: str
    branch: str
    locked: bool
    classification: ReleaseClass
    detail: str = ""
    rescued: bool = False
    rescue_branch: str = ""
    rescue_commit: str = ""
    removed: bool = False
    # OI-1109: the post-removal local branch delete is a separate step from
    # worktree removal. A worktree can be gone while its branch survives
    # (e.g. checked out in another worktree). The outcome of that delete MUST
    # be visible in the entry and the report, not swallowed silently.
    branch_deleted: bool = False
    branch_delete_error: str = ""
    error: str = ""


@dataclass
class ReleaseReport:
    """Aggregate outcome of a release run."""
    entries: list[ReleaseEntry] = field(default_factory=list)
    dry_run: bool = True
    timestamp: str = ""

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            counts[e.classification] = counts.get(e.classification, 0) + 1
        return counts


# ── git porcelain helpers ──────────────────────────────────────────────────

def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def _git_common_dir(repo_root: Path) -> Path:
    result = _run(["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"])
    if result.returncode == 0:
        raw = result.stdout.strip()
        p = Path(raw)
        return p if p.is_absolute() else (repo_root / p).resolve()
    return (repo_root / ".git").resolve()


def _resolve_repo_root(*, dry_run: bool = True) -> Path:
    """Resolve the git repo root, applying PROJECT_ROOT-over-cwd precedence.

    Precedence, in order:

    1. An explicit ``--repo-root`` CLI flag always wins outright — callers
       that already have one (``main()``, and anything passing a concrete
       ``repo_root`` into ``release_locked_worktrees``/``list_locked_worktrees``)
       never call this function at all, so this precedence rule is enforced by
       *not calling* rather than by a branch here.
    2. Failing that, the ``PROJECT_ROOT`` env var wins over the cwd-derived git
       root (``git rev-parse --show-toplevel``) — but ONLY once the two are
       confirmed to point at the SAME repository, compared as resolved,
       normalized paths (``Path.resolve()``, so a symlink like macOS's
       ``/tmp`` -> ``/private/tmp`` is not a false conflict), or when the cwd
       has no git root to compare against at all (e.g. cwd isn't inside a git
       repo). In both those cases behavior is unchanged from before this
       function did any conflict checking.
    3. When PROJECT_ROOT and the cwd git root resolve to DIFFERENT
       repositories, that disagreement is the exact defect this function used
       to hide: a worker whose shell carried a stale PROJECT_ROOT from a prior
       repo, but whose cwd was a throwaway repo, had PROJECT_ROOT win silently
       — and a subsequent ``--apply`` unlocked and removed seven live
       worktrees in the WRONG repository (OI-1389 fix-forward). To close that:
         - during a destructive run (``dry_run=False``, i.e. ``--apply``):
           raises ``RepoRootConflictError`` naming both paths instead of
           picking one silently.
         - during a dry-run: still returns PROJECT_ROOT (unchanged behavior,
           no new refusal), but logs the disagreement as a warning (which the
           default logging config sends to stderr) so an operator sees it
           before they ever type ``--apply``.
    4. With no PROJECT_ROOT set at all, falls back to the cwd git root, then
       to the bare cwd if that git lookup also fails — unchanged from before.
    """
    env_root: Path | None = None
    env_root_raw = os.environ.get("PROJECT_ROOT", "")
    if env_root_raw:
        p = Path(env_root_raw)
        if p.is_dir():
            env_root = p.resolve()

    cwd_result = _run(["git", "rev-parse", "--show-toplevel"])
    cwd_root: Path | None = None
    if cwd_result.returncode == 0:
        cwd_root = Path(cwd_result.stdout.strip()).resolve()

    if env_root is not None:
        if cwd_root is None or cwd_root == env_root:
            return env_root
        if not dry_run:
            raise RepoRootConflictError(env_root, cwd_root)
        logger.warning(
            "PROJECT_ROOT (%s) and the cwd git root (%s) point at different "
            "repositories. Proceeding with PROJECT_ROOT for this dry-run — "
            "pass --repo-root explicitly before using --apply.",
            env_root, cwd_root,
        )
        return env_root

    if cwd_root is not None:
        return cwd_root
    return Path.cwd().resolve()


# ── worktree listing ──────────────────────────────────────────────────────

def list_locked_worktrees(repo_root: Path | None = None) -> list[dict]:
    """Return porcelain records for every LOCKED worktree in the repo.

    Each record: {'worktree': str, 'HEAD': str, 'branch': str | None,
                  'locked': True, 'lock_reason': str | None}
    """
    root = repo_root or _resolve_repo_root()
    result = _run(["git", "-C", str(root), "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        logger.warning("git worktree list failed: %s", result.stderr.strip())
        return []

    records: list[dict] = []
    current: dict = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree"] = line[9:].strip()
        elif line.startswith("HEAD "):
            current["HEAD"] = line[5:].strip()
        elif line.startswith("branch "):
            current["branch"] = line[7:].strip()
        elif line == "detached":
            current["detached"] = True
        elif line == "locked":
            current["locked"] = True
        elif line.startswith("locked "):
            # locked followed by the reason is rare but possible in newer git
            current["locked"] = True
        elif line.startswith("prunable "):
            current["prunable"] = line[9:].strip()

    if current:
        records.append(current)

    return [r for r in records if r.get("locked")]


# ── release classification ────────────────────────────────────────────────

def classify_for_release(wt_path: str, branch: str) -> tuple[ReleaseClass, str]:
    """Classify a worktree for release purposes.

    Returns ``(classification, detail)`` where ``detail`` is a human-readable
    explanation of the state (e.g. "1 unpushed commit", "3 modified files").

    The classification distinguishes five states:
    - ``releasable`` — clean working tree, all commits pushed (or no commits)
    - ``committable`` — uncommitted changes but no local-only commits
    - ``unpushed_commits`` — local commits not on origin, working tree clean
    - ``both`` — both uncommitted changes AND local-only commits
    - ``detached`` — detached HEAD (no branch). The rescue path keys on a
      branch name; a detached worktree has none, so fabricating a pseudo-branch
      from a commit prefix would produce a confusing ``vnx-release/refs/heads/<sha8>``
      rescue name. A detached worktree is reported as such and left for the
      operator to handle explicitly rather than silently pseudo-branching it.
    - ``unreachable`` — worktree path doesn't exist
    - ``error`` — git operations failed
    """
    wt = Path(wt_path)
    if not wt.exists():
        return ("unreachable", "worktree directory does not exist")

    # Check if this is a git directory at all
    if not (wt / ".git").exists() and not (wt / ".git").is_file():
        return ("unreachable", "not a git worktree (no .git)")

    # 0. Detached HEAD? The rescue path keys on a branch name; a detached
    #    worktree has none. Rather than fabricate a pseudo-branch from a commit
    #    prefix (which produced confusing ``vnx-release/refs/heads/<sha8>``
    #    rescue names), report it explicitly and leave it for the operator.
    symref = _run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"])
    if symref.returncode == 0 and symref.stdout.strip() == "HEAD":
        return ("detached", "detached HEAD — no branch to rescue; handle manually")

    # 1. Working tree status — dirty?
    status_result = _run(
        [
            "git", "-c", "core.fileMode=false", "-c", "core.autocrlf=input",
            "-C", str(wt), "status", "--porcelain",
        ]
    )
    has_changes = False
    change_detail = ""
    if status_result.returncode == 0 and status_result.stdout.strip():
        has_changes = True
        lines = [l for l in status_result.stdout.strip().splitlines() if l.strip()]
        change_detail = f"{len(lines)} uncommitted change(s)"

    # 2. Local HEAD
    head_result = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    if head_result.returncode != 0:
        return ("error", f"git rev-parse HEAD failed: {head_result.stderr.strip()}")
    local_sha = head_result.stdout.strip()

    # 3. Check for local-only commits: compare HEAD vs merge-base with origin/main
    mb_result = _run(
        ["git", "-C", str(wt), "merge-base", "origin/main", "HEAD"]
    )
    if mb_result.returncode != 0:
        # Can't determine base — if there are changes, report them
        if has_changes:
            return ("committable", f"{change_detail} (base unresolvable)")
        return ("releasable", "base unresolvable; treating as clean")

    base_sha = mb_result.stdout.strip()

    has_commits = local_sha != base_sha

    # 4. If commits exist, check remote
    commit_detail = ""
    if has_commits:
        count_result = _run(
            ["git", "-C", str(wt), "rev-list", "--count", f"{base_sha}..HEAD"]
        )
        if count_result.returncode == 0:
            n = count_result.stdout.strip()
            commit_detail = f"{n} local commit(s)"

        # Check if these commits are on origin
        remote_ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
        try:
            ls_result = _run(
                ["git", "-C", str(wt), "ls-remote", "origin", remote_ref],
                timeout=10,
            )
            remote_output = ls_result.stdout.strip() if ls_result.returncode == 0 else ""
        except (subprocess.TimeoutExpired, Exception):
            remote_output = ""

        if remote_output:
            remote_sha = remote_output.split()[0]
            if remote_sha == local_sha:
                has_commits = False  # Already pushed

    # 5. Determine classification
    if has_changes and has_commits:
        parts = [change_detail, commit_detail] if commit_detail else [change_detail]
        detail = "; ".join(parts)
        return ("both", detail)
    if has_changes:
        return ("committable", change_detail)
    if has_commits:
        return ("unpushed_commits", commit_detail)
    return ("releasable", "clean")


# ── rescue ────────────────────────────────────────────────────────────────

def _sanitize_branch_name(name: str) -> str:
    """Replace unsafe characters in a branch name segment."""
    return _UNSAFE_BRANCH_CHARS.sub("-", name)


def rescue_worktree(
    wt_path: str,
    branch: str,
    classification: ReleaseClass,
    *,
    dry_run: bool = True,
) -> tuple[bool, str, str]:
    """Rescue work from a worktree to origin.

    Returns ``(success, rescue_branch, rescue_commit)``.

    - *committable*: stages everything, commits with a salvage message,
      pushes to ``vnx-release/<branch>`` on origin.
    - *unpushed_commits*: pushes the existing branch to origin.
    - *both*: commits uncommitted changes first, then pushes the branch.
    - *releasable*/*unreachable*/*error*: no-op, returns (True, "", "").
    """
    if classification in ("releasable", "unreachable", "error"):
        return (True, "", "")

    wt = Path(wt_path)

    if dry_run:
        if classification == "committable":
            safe = _sanitize_branch_name(branch.replace("refs/heads/", ""))
            return (True, f"vnx-release/{safe}", "[would create]")
        if classification == "unpushed_commits":
            return (True, branch, "[would push]")
        if classification == "both":
            return (True, branch, "[would commit+push]")
        return (True, "", "")

    # ── apply mode ──────────────────────────────────────────────────────
    try:
        if classification == "committable":
            # Stage everything, commit, push to rescue branch
            add = _run(["git", "-C", str(wt), "add", "-A"])
            if add.returncode != 0:
                return (False, "", f"git add failed: {add.stderr.strip()}")

            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            safe = _sanitize_branch_name(branch.replace("refs/heads/", ""))
            rescue_branch = f"vnx-release/{safe}"

            commit = _run(
                [
                    "git", "-C", str(wt),
                    "-c", "user.email=vnx@vnx-orchestration.local",
                    "-c", "user.name=VNX Worktree Release",
                    "commit", "-m",
                    f"vnx release: salvage uncommitted changes from {branch} [{ts}]",
                ]
            )
            if commit.returncode != 0:
                return (False, "", f"git commit failed: {commit.stderr.strip()}")

            rescue_commit = _run(
                ["git", "-C", str(wt), "rev-parse", "HEAD"]
            ).stdout.strip()

            push = _run(
                [
                    "git", "-C", str(wt),
                    "push", "origin", f"HEAD:refs/heads/{rescue_branch}",
                ]
            )
            if push.returncode != 0:
                return (False, "", f"git push to {rescue_branch} failed: {push.stderr.strip()}")

            return (True, rescue_branch, rescue_commit[:8])

        if classification == "unpushed_commits":
            push = _run(
                ["git", "-C", str(wt), "push", "-u", "origin", branch]
            )
            if push.returncode != 0:
                return (False, "", f"git push failed: {push.stderr.strip()}")

            rescue_commit = _run(
                ["git", "-C", str(wt), "rev-parse", "HEAD"]
            ).stdout.strip()
            return (True, branch, rescue_commit[:8])

        if classification == "both":
            # Check if there are uncommitted changes to stage
            status = _run(
                [
                    "git", "-c", "core.fileMode=false",
                    "-C", str(wt), "status", "--porcelain",
                ]
            )
            if status.returncode == 0 and status.stdout.strip():
                add = _run(["git", "-C", str(wt), "add", "-A"])
                if add.returncode != 0:
                    return (False, "", f"git add failed: {add.stderr.strip()}")

                ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                commit = _run(
                    [
                        "git", "-C", str(wt),
                        "-c", "user.email=vnx@vnx-orchestration.local",
                        "-c", "user.name=VNX Worktree Release",
                        "commit", "-m",
                        f"vnx release: salvage uncommitted changes from {branch} [{ts}]",
                    ]
                )
                if commit.returncode != 0:
                    return (False, "", f"git commit failed: {commit.stderr.strip()}")

            push = _run(
                ["git", "-C", str(wt), "push", "-u", "origin", branch]
            )
            if push.returncode != 0:
                return (False, "", f"git push failed: {push.stderr.strip()}")

            rescue_commit = _run(
                ["git", "-C", str(wt), "rev-parse", "HEAD"]
            ).stdout.strip()
            return (True, branch, rescue_commit[:8])

        return (True, "", "")

    except Exception as exc:
        return (False, "", str(exc))


# ── remove worktree ───────────────────────────────────────────────────────

def _unlock_worktree(repo_root: Path, wt_path: str) -> bool:
    """Unlock a locked worktree. Returns True on success."""
    result = _run(
        ["git", "-C", str(repo_root), "worktree", "unlock", str(wt_path)]
    )
    return result.returncode == 0


def _remove_worktree(repo_root: Path, wt_path: str) -> tuple[bool, str]:
    """Remove a worktree with --force, retry once. Returns (success, error)."""
    result = _run(
        ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(wt_path)]
    )
    if result.returncode != 0:
        time.sleep(0.3)
        result2 = _run(
            ["git", "-C", str(repo_root), "worktree", "remove", "--force", str(wt_path)]
        )
        if result2.returncode != 0:
            return (False, result2.stderr.strip())
    return (True, "")


# ── main orchestrator ─────────────────────────────────────────────────────

def release_locked_worktrees(
    *,
    repo_root: Path | None = None,
    dry_run: bool = True,
) -> ReleaseReport:
    """Release all locked worktrees in the repository.

    The default is dry-run: classify, report what would be done, do nothing.
    Pass ``dry_run=False`` to actually rescue, unlock, and remove.

    Returns a ``ReleaseReport`` with one ``ReleaseEntry`` per locked worktree.

    Raises ``RepoRootConflictError`` when ``repo_root`` is not given, PROJECT_ROOT
    and the cwd git root disagree, and ``dry_run`` is False — see
    ``_resolve_repo_root``.
    """
    root = repo_root or _resolve_repo_root(dry_run=dry_run)
    locked = list_locked_worktrees(root)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    report = ReleaseReport(dry_run=dry_run, timestamp=ts)

    for rec in locked:
        wt_path = rec.get("worktree", "")
        # A detached worktree carries no branch ref. Previously the code
        # fabricated ``refs/heads/<sha8>`` from a commit prefix, which produced
        # a confusing ``vnx-release/refs/heads/<sha8>`` rescue name. Detached
        # worktrees are now classified explicitly and left for the operator, so
        # there is no branch name to fabricate here.
        branch = rec.get("branch", "")

        entry = ReleaseEntry(
            worktree=wt_path,
            branch=branch,
            locked=rec.get("locked", False),
            classification="error",
        )

        # 1. Classify
        cls, detail = classify_for_release(wt_path, branch)
        entry.classification = cls
        entry.detail = detail

        if cls in ("unreachable", "error", "detached"):
            # detached/unreachable/error: nothing to rescue or remove here.
            # detached carries no branch and is left for the operator; the
            # others already record their reason as the entry error/detail.
            if cls in ("unreachable", "error"):
                entry.error = detail
            report.entries.append(entry)
            continue

        # 2. Rescue if needed
        if cls in ("committable", "unpushed_commits", "both"):
            success, rescue_branch, rescue_commit = rescue_worktree(
                wt_path, branch, cls, dry_run=dry_run,
            )
            entry.rescued = success
            entry.rescue_branch = rescue_branch
            entry.rescue_commit = rescue_commit
            if not success:
                entry.error = rescue_commit  # error message in rescue_commit on failure
                report.entries.append(entry)
                continue

        # 3. Unlock + remove (detached/unreachable/error skip removal above)
        if dry_run:
            entry.removed = False  # Would remove
        else:
            unlocked = _unlock_worktree(root, wt_path)
            if not unlocked:
                entry.error = "failed to unlock worktree"
                report.entries.append(entry)
                continue

            ok, err = _remove_worktree(root, wt_path)
            if ok:
                entry.removed = True
                # Delete the local branch too (it's been rescued to origin if
                # needed). OI-1109: this is a SEPARATE step from worktree
                # removal — a branch can survive its worktree (e.g. checked out
                # in another worktree). Read the outcome and record it; never
                # let a failed branch delete pass silently under removed=True.
                #
                # ``git branch -D`` wants the short branch name, but porcelain
                # reports the full ref (``refs/heads/<name>``). Strip the prefix
                # — without this, ``-D`` silently failed on every release
                # (``branch 'refs/heads/...' not found``) and the old code never
                # noticed because the returncode was never read.
                branch_to_delete = branch.replace("refs/heads/", "") if branch else ""
                if not branch_to_delete:
                    # No branch ref to delete (e.g. a branchless worktree that
                    # slipped past classification). Record it rather than crash.
                    entry.branch_deleted = False
                    entry.branch_delete_error = (
                        "worktree removed but no branch ref recorded to delete"
                    )
                else:
                    branch_result = _run(
                        ["git", "-C", str(root), "branch", "-D", branch_to_delete]
                    )
                    if branch_result.returncode == 0:
                        entry.branch_deleted = True
                    else:
                        msg = (branch_result.stderr.strip()
                               or "unknown branch -D error")
                        entry.branch_deleted = False
                        entry.branch_delete_error = (
                            f"worktree removed but branch -D failed: {msg}"
                        )
            else:
                entry.error = f"failed to remove: {err}"

        report.entries.append(entry)

    # Prune stale references
    _run(["git", "-C", str(root), "worktree", "prune"])

    return report


# ── reporting ─────────────────────────────────────────────────────────────

def format_report(report: ReleaseReport, repo_root: Path | None = None) -> str:
    """Format a ReleaseReport as a human-readable string.

    ``repo_root`` should be the SAME resolved root the report's entries were
    produced against — pass it through rather than letting this function
    re-resolve independently, or the displayed "Repository:" line can name a
    different repo than the one actually operated on (e.g. when an explicit
    --repo-root was used while PROJECT_ROOT still points elsewhere). Falls
    back to a fresh resolution only when the caller has no root on hand.
    """
    lines: list[str] = []
    mode = "[DRY-RUN]" if report.dry_run else "[APPLY]"
    lines.append(f"=== Worktree Release {mode} ===")
    lines.append(f"Timestamp: {report.timestamp}")
    resolved = repo_root if repo_root is not None else _resolve_repo_root()
    lines.append(f"Repository: {resolved}")
    lines.append(f"Total locked worktrees found: {len(report.entries)}")
    lines.append("")

    # Distribution summary
    counts = report.counts
    lines.append("Classification distribution:")
    for cls in ("releasable", "committable", "unpushed_commits", "both",
                "detached", "unreachable", "error"):
        n = counts.get(cls, 0)
        if n > 0:
            lines.append(f"  {cls:<25} {n}")
    lines.append("")

    # Per-worktree detail
    rescued_count = 0
    partial_count = 0
    for e in report.entries:
        status = _status_char(e)
        lines.append(f"  [{status}] {Path(e.worktree).name}")
        lines.append(f"       branch:     {e.branch}")
        lines.append(f"       class:      {e.classification}  ({e.detail})")
        if e.rescued:
            rescued_count += 1
            lines.append(f"       rescued:    yes → {e.rescue_branch} ({e.rescue_commit})")
        if e.removed:
            lines.append(f"       removed:    yes")
        # OI-1109: surface a half-finished cleanup — worktree gone, branch
        # survived — explicitly rather than letting removed=True hide it.
        if e.branch_delete_error:
            partial_count += 1
            lines.append(f"       removed:    yes (worktree)")
            lines.append(f"       branch:     NOT deleted — {e.branch_delete_error}")
        elif e.removed and e.branch_deleted:
            lines.append(f"       branch:     deleted")
        if e.error:
            lines.append(f"       error:      {e.error}")
        lines.append("")

    if partial_count:
        lines.append(
            f"WARNING: {partial_count} worktree(s) removed but branch delete failed — "
            "check branch list for survivors."
        )
        lines.append("")

    if rescued_count:
        lines.append(f"Work rescued: {rescued_count} worktree(s)")
    else:
        lines.append("No worktrees required rescue.")

    if report.dry_run:
        lines.append("")
        lines.append("DRY-RUN: nothing was changed. Re-run with --apply to release.")

    return "\n".join(lines)


def _status_char(entry: ReleaseEntry) -> str:
    if entry.error:
        return "!"
    if entry.classification == "releasable":
        return " "
    if entry.classification == "unreachable":
        return "?"
    if entry.classification == "detached":
        return "D"
    if entry.branch_delete_error:
        return "!"  # partial cleanup — worktree gone, branch survived
    if entry.rescued or entry.classification in ("committable", "unpushed_commits", "both"):
        return "~"
    return " "


def write_report_file(
    report: ReleaseReport,
    data_dir: str | None = None,
    repo_root: Path | None = None,
) -> Path:
    """Write the release report to the governed reports directory.

    ``repo_root``, when given, is both the default base for ``data_dir`` (when
    VNX_DATA_DIR is unset) and what gets threaded into ``format_report`` for
    the "Repository:" line — the same already-resolved root the caller used
    for the release itself, not a fresh, possibly-diverging re-resolution.

    Returns the path to the written report file.
    """
    if data_dir is None:
        data_dir = os.environ.get("VNX_DATA_DIR", "")
    if not data_dir:
        root = repo_root if repo_root is not None else _resolve_repo_root()
        data_dir = str(root / ".vnx-data")

    reports_dir = Path(data_dir) / "unified_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"worktree-release-{ts}.md"
    path = reports_dir / filename

    content = format_report(report, repo_root=repo_root)
    path.write_text(content + "\n", encoding="utf-8")
    return path


# ── CLI entry point ───────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Exit codes:
      0  success, no problems
      1  one or more entries had an error or a partial cleanup (see
         format_report's WARNING section)
      2  argparse usage error (argparse's own default)
      3  EXIT_REPO_ROOT_CONFLICT — refused to run --apply because PROJECT_ROOT
         and the cwd-derived git root name two different repositories and no
         explicit --repo-root was given to disambiguate. See
         RepoRootConflictError / _resolve_repo_root.
    """
    parser = argparse.ArgumentParser(
        description="Release locked git worktrees (OI-1052)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scripts/lib/worktree_release.py           # dry-run (default)
  python3 scripts/lib/worktree_release.py --apply   # actually release
  python3 scripts/lib/worktree_release.py --json    # machine-readable dry-run

Exit codes:
  0  success
  1  one or more entries had an error or a partial cleanup
  2  argparse usage error
  3  refused: --apply with PROJECT_ROOT and the cwd git root pointing at two
     different repositories (pass --repo-root explicitly to disambiguate)
""",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually unlock, rescue, and remove worktrees (default: dry-run)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output machine-readable JSON instead of human-readable text",
    )
    parser.add_argument(
        "--repo-root", type=str, default=None,
        help="Path to the git repository root (default: auto-detect)",
    )

    args = parser.parse_args(argv)
    dry_run = not args.apply
    repo_root_arg = Path(args.repo_root) if args.repo_root else None

    # Resolve the root ONCE here and thread it through everything below
    # (release, report formatting, report file placement) rather than letting
    # each of those independently re-resolve — a second/third re-resolution
    # can silently diverge from the root actually operated on and, worse,
    # would each re-trigger the conflict warning/refusal on their own.
    try:
        root = repo_root_arg if repo_root_arg is not None else _resolve_repo_root(dry_run=dry_run)
    except RepoRootConflictError as exc:
        if args.json:
            print(json.dumps(
                {
                    "error": "repo_root_conflict",
                    "message": str(exc),
                    "env_root": str(exc.env_root),
                    "cwd_root": str(exc.cwd_root),
                },
                indent=2,
            ))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_REPO_ROOT_CONFLICT

    report = release_locked_worktrees(
        repo_root=root,
        dry_run=dry_run,
    )

    # Write report file (always, unless it fails)
    report_path = ""
    try:
        report_path = str(write_report_file(report, repo_root=root))
    except Exception as exc:
        logger.warning("Could not write report file: %s", exc)

    if args.json:
        output = {
            "dry_run": report.dry_run,
            "timestamp": report.timestamp,
            "counts": report.counts,
            "report_path": report_path,
            "entries": [
                {
                    "worktree": e.worktree,
                    "branch": e.branch,
                    "classification": e.classification,
                    "detail": e.detail,
                    "rescued": e.rescued,
                    "rescue_branch": e.rescue_branch,
                    "rescue_commit": e.rescue_commit,
                    "removed": e.removed,
                    "branch_deleted": e.branch_deleted,
                    "branch_delete_error": e.branch_delete_error,
                    "error": e.error,
                }
                for e in report.entries
            ],
        }
        print(json.dumps(output, indent=2))
    else:
        print(format_report(report, repo_root=root))
        if report_path:
            print(f"\nReport written: {report_path}")

    # Return non-zero if any errors OR partial cleanups (OI-1109): a worktree
    # removed while its branch survived is a half-finished release, not a
    # success, and must surface as a non-zero exit so callers notice.
    problems = sum(
        1 for e in report.entries if e.error or e.branch_delete_error
    )
    return 1 if problems > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
