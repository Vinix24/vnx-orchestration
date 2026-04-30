#!/usr/bin/env python3
"""dispatch_git_ops.py — Git mutation helpers for subprocess dispatch.

Provides auto-commit and auto-stash operations for post-dispatch cleanup.
Read-only git helpers (dirty-file detection, commit lookups, etc.) live in
dispatch_git_query.py; functions are re-exported here for backward
compatibility.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from dispatch_git_query import (
    _FILE_WRITING_TOOLS,
    _normalize_repo_path,
    _extract_touched_paths_from_event,
    _parse_dirty_files,
    _get_dirty_files,
    _get_commit_hash,
    _count_lines_changed_since_sha,
    _get_current_branch,
    _check_commit_since,
    _commit_belongs_to_dispatch,
)

__all__ = [
    "_FILE_WRITING_TOOLS",
    "_normalize_repo_path",
    "_extract_touched_paths_from_event",
    "_parse_dirty_files",
    "_get_dirty_files",
    "_get_commit_hash",
    "_count_lines_changed_since_sha",
    "_get_current_branch",
    "_check_commit_since",
    "_commit_belongs_to_dispatch",
    "_auto_commit_changes",
    "_auto_stash_changes",
]

logger = logging.getLogger(__name__)


def _auto_commit_changes(
    dispatch_id: str,
    terminal_id: str,
    gate: str = "",
    pre_dispatch_dirty: "set[str] | None" = None,
    dispatch_touched_files: "frozenset[str] | set[str] | None" = None,
    manifest_paths: "list[str] | None" = None,
) -> bool:
    """Stage and commit changes introduced by this dispatch.

    Two safety filters compose to determine the file set staged:

    1. ``pre_dispatch_dirty`` — files dirty *before* the dispatch started.
       Excluded from staging so pre-existing operator/agent edits are never
       swept into this worker's commit.
    2. ``dispatch_touched_files`` — files this dispatch's worker explicitly
       wrote via structured tool calls (Write/Edit/MultiEdit/NotebookEdit).
       In a *shared* or *concurrently-edited* worktree, files that became
       dirty during the dispatch window may have been written by another
       terminal or by the operator, not by this worker.  Intersecting with
       this set prevents auto-commit from sweeping those concurrent edits.

    Both kwargs are REQUIRED.  Passing ``None`` for either causes the helper
    to refuse to commit (fail-safe) — better to leave changes uncommitted
    than to sweep unrelated work into this worker's commit.  An empty set is
    treated as "no eligible files" and is therefore also a no-op (correct: a
    worker that performed no structured file writes should not auto-commit).

    manifest_paths, when provided (CFX-1), further restricts the staged set
    to files inside the dispatch's declared mutation scope.  This protects
    parallel dispatches in shared worktrees from sweeping each other's
    changes.  When None, falls back to pre_dispatch_dirty-only scoping
    with a deprecation log.

    Returns True if a commit was made, False otherwise.
    Never raises — all exceptions are logged and swallowed.
    """
    if pre_dispatch_dirty is None:
        logger.warning(
            "auto_commit: pre_dispatch_dirty=None — refusing to commit for dispatch %s "
            "(would otherwise sweep unrelated dirty files via git add -A)",
            dispatch_id,
        )
        return False
    if dispatch_touched_files is None:
        logger.warning(
            "auto_commit: dispatch_touched_files=None — refusing to commit for dispatch %s "
            "(cannot distinguish this worker's writes from concurrent edits in a shared worktree)",
            dispatch_id,
        )
        return False
    if manifest_paths is None:
        logger.warning(
            "auto_commit: manifest_paths absent for dispatch %s — using legacy "
            "pre_dispatch_dirty scoping only (callers should declare paths via "
            "dispatch_paths.write_manifest for parallel-worktree safety)",
            dispatch_id,
        )
    try:
        cwd = Path(__file__).resolve().parents[2]
        # Check for uncommitted changes
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
            cwd=cwd,
        )
        dirty_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]
        if not dirty_lines:
            logger.debug("auto_commit: working tree clean for dispatch %s", dispatch_id)
            return False

        # Scope staging to (files that became dirty during this dispatch) ∩
        # (files this dispatch's worker explicitly wrote).  The intersection
        # is the deepest scoping signal available: it filters out both
        # pre-existing dirty files and concurrent-terminal edits that happen
        # to land within the dispatch window.
        current_dirty = _get_dirty_files(cwd)
        new_during_dispatch = current_dirty - pre_dispatch_dirty
        touched = set(dispatch_touched_files)
        files_to_stage = sorted(new_during_dispatch & touched)
        if manifest_paths is not None:
            from dispatch_paths import filter_paths
            before = list(files_to_stage)
            files_to_stage = filter_paths(before, manifest_paths)
            dropped = sorted(set(before) - set(files_to_stage))
            if dropped:
                logger.info(
                    "auto_commit: dispatch %s manifest excluded %d out-of-scope files: %s",
                    dispatch_id, len(dropped), dropped,
                )
        if not files_to_stage:
            ignored_dispatch_dirty = sorted(new_during_dispatch - touched)
            if ignored_dispatch_dirty:
                logger.warning(
                    "auto_commit: %d dispatch-window dirty file(s) not in "
                    "touched_files — refusing to commit (likely concurrent "
                    "edits from another terminal). dispatch=%s files=%s",
                    len(ignored_dispatch_dirty),
                    dispatch_id,
                    ignored_dispatch_dirty[:10],
                )
            else:
                logger.debug(
                    "auto_commit: no dispatch-touched files dirty for dispatch %s "
                    "(all dirty files pre-existed the dispatch or fell outside manifest)",
                    dispatch_id,
                )
            return False
        add_cmd = ["git", "add", "--"] + files_to_stage

        add_proc = subprocess.run(
            add_cmd,
            capture_output=True, text=True, timeout=15,
            cwd=cwd,
        )
        if add_proc.returncode != 0:
            logger.warning("auto_commit: git add failed for %s: %s", dispatch_id, add_proc.stderr)
            return False

        gate_tag = gate or dispatch_id[:12]
        commit_msg = (
            f"feat({gate_tag}): auto-commit from headless worker {terminal_id}\n\n"
            f"Dispatch-ID: {dispatch_id}"
        )
        commit_proc = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True, text=True, timeout=30,
            cwd=cwd,
        )
        if commit_proc.returncode == 0:
            logger.info(
                "Auto-committed uncommitted changes from dispatch %s (terminal=%s)",
                dispatch_id, terminal_id,
            )
            return True
        else:
            logger.warning(
                "auto_commit: git commit failed for %s: %s",
                dispatch_id, commit_proc.stderr,
            )
            return False
    except Exception as exc:
        logger.warning("auto_commit: unexpected error for dispatch %s: %s", dispatch_id, exc)
        return False


def _auto_stash_changes(
    dispatch_id: str,
    terminal_id: str,
    pre_dispatch_dirty: "set[str] | None" = None,
    dispatch_touched_files: "frozenset[str] | set[str] | None" = None,
    manifest_paths: "list[str] | None" = None,
) -> bool:
    """Stash changes introduced by this dispatch after a failure (preserves but does not commit).

    Two safety filters compose to determine the file set stashed:

    1. ``pre_dispatch_dirty`` — files dirty *before* the dispatch started.
       Excluded from the stash so pre-existing edits remain in the worktree
       and are not hidden from the operator or other terminals.
    2. ``dispatch_touched_files`` — files this dispatch's worker explicitly
       wrote via structured tool calls.  In a shared worktree, files that
       became dirty during the dispatch window may have been written by
       another terminal — those must NOT be stashed under this dispatch's
       name.

    Both kwargs are REQUIRED.  Passing ``None`` for either causes the helper
    to refuse to stash (fail-safe).  An empty ``dispatch_touched_files`` is
    a legitimate "no structured writes happened" signal and also yields a
    no-op stash.

    manifest_paths, when provided (CFX-1), further restricts the stash set to
    files inside the dispatch's declared mutation scope.  When None, falls
    back to pre_dispatch_dirty-only scoping with a deprecation log.

    Returns True if a stash was created, False otherwise.
    Never raises — all exceptions are logged and swallowed.
    """
    if pre_dispatch_dirty is None:
        logger.warning(
            "auto_stash: pre_dispatch_dirty=None — refusing to stash for dispatch %s "
            "(would otherwise sweep unrelated dirty files into a global stash)",
            dispatch_id,
        )
        return False
    if dispatch_touched_files is None:
        logger.warning(
            "auto_stash: dispatch_touched_files=None — refusing to stash for dispatch %s "
            "(cannot distinguish this worker's writes from concurrent edits in a shared worktree)",
            dispatch_id,
        )
        return False
    if manifest_paths is None:
        logger.warning(
            "auto_stash: manifest_paths absent for dispatch %s — using legacy "
            "pre_dispatch_dirty scoping only (callers should declare paths via "
            "dispatch_paths.write_manifest for parallel-worktree safety)",
            dispatch_id,
        )
    try:
        cwd = Path(__file__).resolve().parents[2]
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
            cwd=cwd,
        )
        dirty_lines = [l for l in status_proc.stdout.splitlines() if l.strip()]
        if not dirty_lines:
            return False

        stash_name = f"vnx-auto-stash-{dispatch_id}"

        current_dirty = _get_dirty_files(cwd)
        new_during_dispatch = current_dirty - pre_dispatch_dirty
        touched = set(dispatch_touched_files)
        files_to_stash = sorted(new_during_dispatch & touched)
        if manifest_paths is not None:
            from dispatch_paths import filter_paths
            before = list(files_to_stash)
            files_to_stash = filter_paths(before, manifest_paths)
            dropped = sorted(set(before) - set(files_to_stash))
            if dropped:
                logger.info(
                    "auto_stash: dispatch %s manifest excluded %d out-of-scope files: %s",
                    dispatch_id, len(dropped), dropped,
                )
        if not files_to_stash:
            ignored_dispatch_dirty = sorted(new_during_dispatch - touched)
            if ignored_dispatch_dirty:
                logger.warning(
                    "auto_stash: %d dispatch-window dirty file(s) not in "
                    "touched_files — refusing to stash (likely concurrent "
                    "edits from another terminal). dispatch=%s files=%s",
                    len(ignored_dispatch_dirty),
                    dispatch_id,
                    ignored_dispatch_dirty[:10],
                )
            else:
                logger.debug(
                    "auto_stash: no dispatch-touched files dirty for dispatch %s "
                    "(all dirty files pre-existed the dispatch or fell outside manifest)",
                    dispatch_id,
                )
            return False
        # -u includes untracked files matching the specified paths so
        # newly-created files from the failed dispatch are also captured.
        stash_cmd = ["git", "stash", "push", "-u", "-m", stash_name, "--"] + files_to_stash

        stash_proc = subprocess.run(
            stash_cmd,
            capture_output=True, text=True, timeout=30,
            cwd=cwd,
        )
        if stash_proc.returncode == 0:
            logger.info(
                "Stashed %d dispatch-produced file(s) from failed dispatch %s "
                "(terminal=%s, stash=%s)",
                len(files_to_stash), dispatch_id, terminal_id, stash_name,
            )
            return True
        else:
            logger.warning(
                "auto_stash: git stash failed for %s: %s",
                dispatch_id, stash_proc.stderr,
            )
            return False
    except Exception as exc:
        logger.warning("auto_stash: unexpected error for dispatch %s: %s", dispatch_id, exc)
        return False
