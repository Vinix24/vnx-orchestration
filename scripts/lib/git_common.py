"""git_common.py — shared git-dir resolution helpers for worktree-aware code.

Several modules need the shared git dir (``.git`` of a main checkout, or the
common dir a linked worktree points at via its ``gitdir:`` file). Duplicating
that resolution caused OI-1713: ``dispatch_worktree_isolation._worktree_lock``
assumed ``root / ".git"`` is a directory and raised ``NotADirectoryError`` in
every linked worktree, while ``tmux_worktree._git_common_dir`` and
``worktree_release._git_common_dir`` already did it right. This module is the
single implementation all three (plus ``gate_worktree``) import.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def git_common_dir(repo_root: Path) -> Path:
    """Resolve the shared git dir for *repo_root*, handling linked worktrees.

    In a main checkout ``.git`` is a directory and ``git rev-parse
    --git-common-dir`` returns that path. In a linked worktree (``git worktree
    add``) ``.git`` is an ASCII file with a ``gitdir:`` line, and
    ``--git-common-dir`` returns the shared dir of the main checkout — the only
    place whose ``worktrees/`` subdir can safely host a lock file. Deriving a
    path from ``root / ".git"`` would raise ``NotADirectoryError`` there
    (OI-1713 / OI-905).

    Returns the resolved git-common-dir path. On git failure, falls back to
    ``(repo_root / ".git").resolve()`` so callers in a standard layout keep
    working; a linked worktree whose git lookup fails is already broken in a
    way that will surface elsewhere.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return (repo_root / ".git").resolve()
    if proc.returncode != 0:
        return (repo_root / ".git").resolve()
    raw = proc.stdout.strip()
    if not raw:
        return (repo_root / ".git").resolve()
    p = Path(raw)
    return p if p.is_absolute() else (repo_root / p).resolve()
