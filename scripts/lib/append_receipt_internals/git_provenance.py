"""Git provenance metadata builder for receipt enrichment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from .common import _safe_subprocess


def _extract_shortstat_value(shortstat: str, token: str) -> int:
    # Example: "12 files changed, 342 insertions(+), 87 deletions(-)"
    for part in shortstat.split(","):
        chunk = part.strip().lower()
        if token in chunk:
            digits = "".join(ch for ch in chunk if ch.isdigit())
            if digits:
                try:
                    return int(digits)
                except ValueError:
                    return 0
    return 0


def _parse_porcelain_paths(status_raw: str) -> List[str]:
    """Parse `git status --porcelain` into a flat list of changed paths.

    ADR-035 §9 PR-5 paths-fix: `git diff --name-only` alone only sees
    unstaged tracked changes — a receipt with staged code + unstaged docs
    would show only the docs path, and the doc-only invariant (§3.1.1) would
    wrongly accept a change that includes staged code. `--porcelain` already
    reports staged, unstaged, AND untracked entries in one pass.
    """
    paths: List[str] = []
    for line in status_raw.splitlines():
        if not line.strip():
            continue
        # Porcelain v1: 2 status chars + 1 space + path ("old -> new" for renames).
        entry = line[3:] if len(line) > 3 else line[2:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip()
        if len(entry) >= 2 and entry.startswith('"') and entry.endswith('"'):
            entry = entry[1:-1]
        if entry:
            paths.append(entry)
    return paths


def _resolve_git_root(repo_root: Path) -> Path:
    """Resolve the PROJECT root using CLAUDE_PROJECT_DIR if set."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_dir:
        probe = _safe_subprocess(["git", "rev-parse", "--show-toplevel"], cwd=Path(project_dir))
        if probe:
            return Path(project_dir)
    return repo_root


def _build_git_provenance(repo_root: Path) -> Dict[str, Any]:
    repo_root = _resolve_git_root(repo_root)
    git_root_raw = _safe_subprocess(["git", "rev-parse", "--show-toplevel"], cwd=repo_root)

    if not git_root_raw:
        return {
            "git_ref": "not_a_repo",
            "branch": "unknown",
            "is_dirty": False,
            "dirty_files": 0,
            "diff_summary": None,
        }

    git_root = Path(git_root_raw)
    git_ref = _safe_subprocess(["git", "rev-parse", "HEAD"], cwd=git_root) or "unknown"
    branch = _safe_subprocess(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root) or "unknown"
    # strip=False: porcelain's fixed-column status codes are position-sensitive
    # (see _parse_porcelain_paths) — a blob-level strip() would eat the first
    # line's leading space and shift every path by one column.
    status_raw = _safe_subprocess(["git", "status", "--porcelain"], cwd=git_root, strip=False) or ""
    dirty_files = len([line for line in status_raw.splitlines() if line.strip()])
    is_dirty = dirty_files > 0

    diff_summary = None
    if is_dirty:
        shortstat = _safe_subprocess(["git", "diff", "--shortstat"], cwd=git_root) or ""
        diff_summary = {
            "files_changed": _extract_shortstat_value(shortstat, "file"),
            "insertions": _extract_shortstat_value(shortstat, "insertion"),
            "deletions": _extract_shortstat_value(shortstat, "deletion"),
        }
        # ADR-035 §3.1.1/§3.2 (r2 HIGH-4, PR-5 paths-fix): the changed-file
        # list the doc-only invariant needs. Parsed from the SAME
        # `git status --porcelain` call already used for dirty_files above —
        # no new git-invocation class — so staged, unstaged, AND untracked
        # paths are all covered (`git diff --name-only` alone would have
        # missed staged and untracked entries).
        diff_summary["paths"] = _parse_porcelain_paths(status_raw)

    git_dir = _safe_subprocess(["git", "rev-parse", "--git-dir"], cwd=git_root) or ""
    git_common_dir = _safe_subprocess(["git", "rev-parse", "--git-common-dir"], cwd=git_root) or ""
    in_worktree = bool(git_dir and git_common_dir and git_dir != git_common_dir)

    provenance = {
        "git_ref": git_ref,
        "branch": branch,
        "is_dirty": is_dirty,
        "dirty_files": dirty_files,
        "diff_summary": diff_summary,
        "in_worktree": in_worktree,
    }
    if in_worktree:
        provenance["worktree_path"] = str(git_root)
    return provenance
