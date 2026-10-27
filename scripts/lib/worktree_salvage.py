"""worktree_salvage.py — preserve a worktree's unpushed commits (PR-2a1).

Salvage and verification are one allowlist. :func:`salvage` preserves exactly one
kind of tree state — the commits no ``origin`` ref reaches — and :func:`verify`
answers ``[]`` only when every piece of the tree's state is that kind, positively
checked. Any other status entry (tracked or staged change, submodule, unmerged
path, untracked file), a tree without a readable HEAD, and every unproven
artifact is a named problem, so a dirty tree never verifies clean.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "MANIFEST.txt"
BUNDLE_SUFFIX = ".bundle"
_DETACHED = "(detached)"
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class SalvageError(RuntimeError):
    """A git command failed; the salvage is incomplete and must not be trusted."""


@dataclass
class SalvageResult:
    """What :func:`salvage` found, and for a real run wrote."""
    name: str
    branch: str
    head: str
    unpushed: int
    salvage_dir: Path
    artifacts: list[str] = field(default_factory=list)
    manifest_line: str = ""
    written: bool = False
    uncovered: list[str] = field(default_factory=list)
    worktree: Path | None = None


def _utc_stamp() -> str:
    return time.strftime(_STAMP_FORMAT, time.gmtime())


def salvage(worktree_path, dest_dir, *, repo_root, base_ref="origin/main", dry_run=True):
    raise NotImplementedError("salvage")


def verify(result, dest_dir, *, repo_root):
    raise NotImplementedError("verify")
