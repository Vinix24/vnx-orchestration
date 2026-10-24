"""worktree_salvage.py — preserve a dirty worktree before any later removal.

Salvage is step one of the ``worktree-opruiming-structureel`` track. This
module writes the three artifacts a locked dispatch worktree may still hold,
plus a ``MANIFEST.txt`` line describing them, in exactly the format the
hand-run salvage of 27-09-2026 used under
``$VNX_DATA_DIR/salvage/worktrees-2026-09-27/``::

    <tree dir name> | <branch or (detached)> | head=<full sha> | niet-op-remote=<n> | <artifacts> 

Artifacts:

* ``<name>.bundle`` — only when ``head`` has commits not reachable from the
  remote named by ``base_ref`` (``origin`` for the default ``origin/main``).
* ``<name>.patch`` — only when the working tree or index differs from ``head``.
* ``<name>.untracked.tgz`` — only when there are untracked, non-ignored files,
  with every internal convenience symlink (``.venv`` style) left out.

There is no caller yet: a later PR removes a worktree only after
:func:`verify` returns an empty list.

A ``--not --remotes=<remote>`` bundle is *thin*: it records the remote tip as
a prerequisite, so it restores with ``git fetch <bundle>`` in a clone that
already has the remote's history, and it cannot be cloned on its own. That is
deliberate — bundling the full ancestry instead would write tens of megabytes
per tree.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class SalvageError(RuntimeError):
    """A git command failed; the salvage is incomplete and must not be trusted."""


@dataclass
class SalvageResult:
    """What :func:`salvage` found (and, for a real run, wrote)."""

    name: str
    branch: str
    head: str
    unpushed: int
    artifacts: list[str] = field(default_factory=list)
    manifest_line: str = ""
    written: bool = False


def salvage(
    worktree_path,
    dest_dir,
    *,
    repo_root,
    base_ref: str = "origin/main",
    dry_run: bool = True,
) -> SalvageResult:
    """Compute (and unless ``dry_run``, write) the salvage artifacts."""
    raise NotImplementedError("salvage")


def verify(result: SalvageResult, dest_dir) -> list[str]:
    """Return the problems found; an empty list means the salvage is complete."""
    raise NotImplementedError("verify")
