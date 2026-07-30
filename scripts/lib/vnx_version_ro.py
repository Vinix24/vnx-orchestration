#!/usr/bin/env python3
"""Version directory read-only enforcement.

After a successful install or update, pinned version directories under
``~/.vnx-system/versions/<v>/`` (every ``vX.Y.Z``) are made read-only so
that no worker, script, or tool can accidentally write into the shared
immutable code tree.  ``edge`` is always left writable (it is a living
checkout).  Install and update flows temporarily unlock the directory,
perform their work, then re-lock it — even when an error occurs part-way
through.

Why OS-level, not worker-permission-level
------------------------------------------
Two reasons, both measured:

1. The written path resolves INSIDE the consumer's worktree before
   symlink resolution, so path-prefix deny rules do not catch it.
2. The capability binding in ``.vnx/worker_permissions.yaml`` is
   claude-bound.  A kimi or codex worker via the native CLI never sees
   those rules; kimi is the fleet default build-worker.

A read-only bit is OS-enforced and effective across providers, tools,
and symlinks.

Public API
----------
- ``make_readonly(version_dir)`` — remove all write bits recursively.
- ``make_writable(version_dir)`` — restore user-write bits recursively.
- ``is_readonly(version_dir)`` — test whether the directory's user-write
  bit is cleared.
- ``writeable_version_dir(version_dir)`` — context manager that
  temporarily unlocks, yields, then re-locks.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def _is_pinned(version_dir: Path) -> bool:
    """True when ``version_dir`` is a pinned version, not ``edge``."""
    return version_dir.name != "edge"


def _chmod_one(path: Path, *, remove_write: bool) -> None:
    """Add or remove all write bits from *one* filesystem entry."""
    try:
        st = path.lstat()
    except OSError:
        return  # broken symlink or racy deletion
    mode = st.st_mode
    if remove_write:
        new_mode = mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    else:
        new_mode = mode | stat.S_IWUSR
    if new_mode != mode:
        try:
            os.chmod(str(path), new_mode, follow_symlinks=False)
        except OSError:
            pass  # defensive: never crash traversal on a single entry


def _chmod_recursive(root: Path, *, remove_write: bool) -> None:
    """Recursively add or remove write permissions under *root*."""
    for dirpath_str, dirnames, filenames in os.walk(str(root), followlinks=False):
        dirpath = Path(dirpath_str)
        _chmod_one(dirpath, remove_write=remove_write)
        for name in filenames:
            _chmod_one(dirpath / name, remove_write=remove_write)


def make_readonly(version_dir: Path) -> None:
    """Recursively remove all write bits from ``version_dir``.

    No-op for ``edge`` (a living checkout must stay writable) and for
    non-existent or non-directory paths.

    Raises nothing — chmod failures on individual entries are silently
    skipped so traversal never aborts the whole tree.  If the final
    state is wrong the caller will detect that and fail loud.
    """
    if not _is_pinned(version_dir):
        return
    if not version_dir.is_dir():
        return
    _chmod_recursive(version_dir, remove_write=True)


def make_writable(version_dir: Path) -> None:
    """Recursively add user-write bit to every entry under ``version_dir``.

    No-op for ``edge`` and for non-existent/non-directory paths.
    See ``make_readonly`` for the error-handling rationale.
    """
    if not _is_pinned(version_dir):
        return
    if not version_dir.is_dir():
        return
    _chmod_recursive(version_dir, remove_write=False)


def is_readonly(version_dir: Path) -> bool:
    """True when the directory's user-write bit is cleared.

    Tests only the directory itself (not recursive).  Returns False for
    ``edge`` (conceptually never read-only) and for non-existent paths.
    """
    if not _is_pinned(version_dir):
        return False
    if not version_dir.is_dir():
        return False
    try:
        mode = version_dir.stat().st_mode
    except OSError:
        return False
    return not (mode & stat.S_IWUSR)


@contextmanager
def writeable_version_dir(version_dir: Path) -> Iterator[None]:
    """Context manager: temporarily unlock a version dir, re-lock on exit.

    Usage::

        with writeable_version_dir(version_dir):
            subprocess.run(["git", "-C", str(version_dir), "pull"])

    The directory is unlocked before the body runs and re-locked in the
    ``finally`` block.  ``edge`` is never touched.  If the directory was
    already writable on entry it is left writable on exit (a nested
    unlock inside another writer's context manager).
    """
    was_readonly = is_readonly(version_dir)
    if was_readonly:
        make_writable(version_dir)
    try:
        yield
    finally:
        if was_readonly:
            make_readonly(version_dir)
