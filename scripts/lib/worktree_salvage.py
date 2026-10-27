"""worktree_salvage.py — preserve a worktree's commits and tracked work.

Every salvage gets its own new directory under ``dest_dir``::

    dest_dir/<name>-<UTC %Y%m%dT%H%M%SZ>-<head[:8]>/

and appends one line to ``dest_dir/MANIFEST.txt`` in the format of the
27-09-2026 hand salvage::

    <tree dir name> | <branch or (detached)> | head=<sha> | niet-op-remote=<n> | <artifact paths> 

* ``<name>.bundle`` — commits not reachable from the remote named by
  ``base_ref`` (``origin`` for the default ``origin/main``). Thin: it restores
  with ``git fetch <bundle> HEAD`` in a clone that has that remote's history;
  a clone of the bundle alone needs those prerequisite objects.
* ``<name>.staged.patch`` — HEAD to index (``git diff --binary --cached HEAD``).
* ``<name>.patch`` — index to worktree (``git diff --binary``). Two patches, not
  one ``git diff HEAD``: a staged change the tree undoes again cancels out in
  that single diff and the index content would be lost. ``git status``, not an
  empty diff, decides whether there is tracked work.

Untracked files are not salvaged here (PR-2b): :func:`salvage` reports them as
``untracked_pending`` and :func:`verify` refuses a tree that has any, so a
removal gated on :func:`verify` can never drop them.

Nothing is removed by this module: dry-run by default, and no caller yet.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "MANIFEST.txt"
BUNDLE_SUFFIX = ".bundle"
STAGED_SUFFIX = ".staged.patch"
PATCH_SUFFIX = ".patch"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class SalvageError(RuntimeError):
    """A git command failed; the salvage is incomplete and must not be trusted."""


@dataclass
class SalvageResult:
    """What :func:`salvage` found (and, for a real run, wrote)."""

    name: str
    branch: str
    head: str
    unpushed: int
    # The directory this salvage wrote into (computed, never written, on a dry run).
    salvage_dir: Path
    # Every artifact, as a path relative to ``dest_dir``.
    artifacts: list[str] = field(default_factory=list)
    untracked_pending: bool = False
    manifest_line: str = ""
    written: bool = False


def _utc_stamp() -> str:
    """The UTC timestamp of this salvage, as it appears in the directory name."""
    return time.strftime(_STAMP_FORMAT, time.gmtime())


def salvage(
    worktree_path: Path | str,
    dest_dir: Path | str,
    *,
    repo_root: Path | str,
    base_ref: str = "origin/main",
    dry_run: bool = True,
) -> SalvageResult:
    """Compute (and unless ``dry_run``, write) the salvage of ``worktree_path``."""
    raise NotImplementedError("salvage is not implemented yet")


def verify(
    result: SalvageResult, dest_dir: Path | str, *, repo_root: Path | str | None = None
) -> list[str]:
    """Every problem that keeps this salvage from being complete."""
    raise NotImplementedError("verify is not implemented yet")
