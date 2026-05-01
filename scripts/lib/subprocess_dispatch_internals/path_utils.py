"""path_utils — file-path normalization and tool-event extraction helpers."""

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
