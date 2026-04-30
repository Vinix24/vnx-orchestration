#!/usr/bin/env python3
"""dispatch_git_query.py — Read-only git helpers for subprocess dispatch.

Provides tool-event path extraction, dirty-file detection, commit hash
lookups, branch name queries, and commit attribution checks.  All functions
are pure reads — no git writes happen here.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Tool names whose ``input`` block names a file path the worker is modifying.
# Read/Bash/Glob/Grep are deliberately excluded — they do not modify files
# (or, in Bash's case, may modify them but cannot be reliably parsed).  Workers
# that rely on Bash for file modifications must commit those changes manually.
_FILE_WRITING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


def _normalize_repo_path(path_str: str, repo_root: Path) -> str | None:
    """Convert a tool-event file_path to a repo-relative POSIX string.

    Returns None when ``path_str`` resolves outside ``repo_root`` or is empty
    or unparseable.  The result is suitable for matching against
    ``git status --porcelain`` output, which always uses POSIX-style
    repo-relative paths.

    Symlink-resolves both sides so a path like ``./foo/../bar.py`` collapses
    to ``bar.py`` and a worktree-relative path matches the repo root.
    """
    if not path_str:
        return None
    try:
        p = Path(path_str)
        if not p.is_absolute():
            p = repo_root / p
        try:
            resolved = p.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved = p
        try:
            root_resolved = repo_root.resolve(strict=False)
        except (OSError, RuntimeError):
            root_resolved = repo_root
        rel = resolved.relative_to(root_resolved)
        return rel.as_posix()
    except (ValueError, OSError):
        return None


def _extract_touched_paths_from_event(event: "StreamEvent | object") -> list[str]:  # type: ignore[name-defined]
    """Return raw ``file_path`` / ``notebook_path`` strings from a tool_use event.

    Accepts a ``StreamEvent`` (or any object exposing ``.type`` + ``.data``).
    Returns an empty list for non-tool_use events or tools that do not write
    files.  Path normalization (repo-relative, in-repo filtering) is performed
    by the caller via ``_normalize_repo_path``.
    """
    event_type = getattr(event, "type", None)
    if event_type != "tool_use":
        return []
    data = getattr(event, "data", {}) or {}
    name = data.get("name", "")
    if name not in _FILE_WRITING_TOOLS:
        return []
    tool_input = data.get("input") or {}
    if not isinstance(tool_input, dict):
        return []
    paths: list[str] = []
    if name == "NotebookEdit":
        candidate = tool_input.get("notebook_path") or tool_input.get("file_path")
        if isinstance(candidate, str):
            paths.append(candidate)
    else:
        candidate = tool_input.get("file_path")
        if isinstance(candidate, str):
            paths.append(candidate)
    return paths


def _parse_dirty_files(porcelain_output: str) -> frozenset:
    """Parse 'git status --porcelain' output into a frozenset of relative file paths."""
    files: set[str] = set()
    for line in porcelain_output.splitlines():
        if not line.strip():
            continue
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        files.add(path_part.strip())
    return frozenset(files)


def _get_dirty_files(cwd: Path) -> set[str]:
    """Return the set of dirty (modified/untracked) file paths from git status --porcelain.

    Handles rename lines ("old -> new") by capturing only the destination path.
    Returns an empty set on any failure so callers can safely subtract.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15,
            cwd=cwd,
        )
        files: set[str] = set()
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            # git status --porcelain: XY<space>filename (or XY<space>old -> new)
            path_part = line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1]
            files.add(path_part)
        return files
    except Exception as exc:
        logger.debug("_get_dirty_files failed: %s", exc)
        return set()


def _get_commit_hash() -> str:
    """Return current HEAD commit hash, or empty string on failure."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=Path(__file__).resolve().parents[2],
        )
        return proc.stdout.strip()
    except Exception as exc:
        logger.debug("_get_commit_hash failed: %s", exc)
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
        cwd = Path(__file__).resolve().parents[2]
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


def _get_current_branch() -> str:
    """Return current branch name, or empty string on failure."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=Path(__file__).resolve().parents[2],
        )
        return proc.stdout.strip()
    except Exception as exc:
        logger.debug("_get_current_branch failed: %s", exc)
        return ""


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
                cwd=Path(__file__).resolve().parents[2],
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

    # Legacy / fallback path: time-window check via GovernanceEnforcer
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

    # Direct fallback
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
            cwd=Path(__file__).resolve().parents[2],
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
