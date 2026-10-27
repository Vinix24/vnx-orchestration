"""worktree_salvage.py — preserve a worktree's unpushed commits (WT-2a1a).

Signatures only: the red-run commit. Every function raises NotImplementedError
until the implementation lands in the commit that follows.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "MANIFEST.txt"
BUNDLE_SUFFIX = ".bundle"
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


def _populated_submodules(worktree_path: Path) -> list[str]:
    raise NotImplementedError("populated submodules are not read yet")


def salvage(
    worktree_path: "Path | str", dest_dir: "Path | str", *, repo_root: "Path | str",
    base_ref: str = "origin/main", dry_run: bool = True,
) -> SalvageResult:
    raise NotImplementedError("salvage is not implemented yet")


def verify(
    result: SalvageResult, dest_dir: "Path | str", *, repo_root: "Path | str"
) -> list[str]:
    raise NotImplementedError("verify is not implemented yet")
