"""Git provenance metadata builder for receipt enrichment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from .common import _safe_subprocess, _utc_now_iso


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
    captured_at = _utc_now_iso()

    if not git_root_raw:
        return {
            "git_ref": "not_a_repo",
            "branch": "unknown",
            "is_dirty": False,
            "dirty_files": 0,
            "diff_summary": None,
            "captured_at": captured_at,
            "captured_by": "append_receipt",
        }

    git_root = Path(git_root_raw)
    git_ref = _safe_subprocess(["git", "rev-parse", "HEAD"], cwd=git_root) or "unknown"
    branch = _safe_subprocess(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_root) or "unknown"
    status_raw = _safe_subprocess(["git", "status", "--porcelain"], cwd=git_root) or ""
    dirty_files = len([line for line in status_raw.splitlines() if line.strip()])
    is_dirty = dirty_files > 0

    diff_summary = None
    if is_dirty:
        shortstat = _safe_subprocess(["git", "diff", "--shortstat"], cwd=git_root) or ""
        if shortstat:
            diff_summary = {
                "files_changed": _extract_shortstat_value(shortstat, "file"),
                "insertions": _extract_shortstat_value(shortstat, "insertion"),
                "deletions": _extract_shortstat_value(shortstat, "deletion"),
            }

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
        "captured_at": captured_at,
        "captured_by": "append_receipt",
    }
    if in_worktree:
        provenance["worktree_path"] = str(git_root)
    return provenance
