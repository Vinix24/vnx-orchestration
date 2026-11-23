"""blast_radius — pure core of the file-level blast radius (ADR-096 decisions 5-8 and 10).

Inputs are path lists and identities, outputs are values. It reads no configuration: the generic
directory list is a parameter. Sources (live work, open PRs) are B4a, the door is B4b.

Unknown is a value of its own: an incomplete scope never reads as "no overlap".
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

DEFAULT_GENERIC_DIRS: Tuple[str, ...] = ("tests", "changelog.d", "docs")

OVERLAP = "overlap"
CLEAR = "clear"
UNKNOWN = "unknown"

REASON_SCOPE_INCOMPLETE = "scope_incomplete"
REASON_OTHER_SCOPE_INCOMPLETE = "other_scope_incomplete"
REASON_OTHER_UNREADABLE = "other_unreadable"


class ScopeInvalid(ValueError):
    """A path is not a repo-relative path (``..`` or outside the repo)."""


@dataclass(frozen=True)
class Scope:
    paths: Tuple[str, ...]  # normalised; a directory keeps its trailing "/"
    incomplete: bool


@dataclass(frozen=True)
class Subject:
    """One side of a comparison: a deliverable (or PR) with its identity and paths."""
    deliverable_id: str
    paths: Optional[Sequence[str]]  # None: the paths could not be read
    branch: Optional[str] = None
    predecessors: Tuple[str, ...] = ()
    pr_number: Optional[int] = None  # set when this subject is an open PR (B5 names it as `#<pr>`)


@dataclass(frozen=True)
class Result:
    other: str
    outcome: str  # OVERLAP | CLEAR | UNKNOWN
    paths: Tuple[str, ...] = ()
    reason: Optional[str] = None


def _branch_of(deliverable_id: str) -> str:
    return f"dispatch/{deliverable_id}"


_ROUND_SUFFIX = re.compile(r"^(?P<base>.*?)(?:-[a-z])?(?:-r\d+)?$")


def _round_base(branch: str) -> str:
    """``<id>-r2``, ``<id>-b`` and ``<id>-c-r2`` are follow-up rounds of ``<id>`` (branch or dispatch id)."""
    return _ROUND_SUFFIX.match(branch).group("base")


def normalise(paths: Iterable[str], repo_root: "str | Path") -> Tuple[str, ...]:
    """Repo-relative POSIX paths; raise ScopeInvalid (``scope_invalid: <path>``) on a bad one."""
    root = posixpath.normpath(Path(repo_root).resolve().as_posix())
    out: List[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ScopeInvalid(f"scope_invalid: {raw!r}")
        text = raw.strip().replace("\\", "/")
        is_dir = text.endswith("/")
        if text.startswith("/"):
            norm = posixpath.normpath(text)
            if norm != root and not norm.startswith(root + "/"):
                raise ScopeInvalid(f"scope_invalid: {raw}")
            rel = posixpath.relpath(norm, root)
        else:
            if ".." in text.split("/"):
                raise ScopeInvalid(f"scope_invalid: {raw}")
            rel = posixpath.normpath(text)
        if rel == ".." or rel.startswith("../"):
            raise ScopeInvalid(f"scope_invalid: {raw}")
        if rel == ".":
            raise ScopeInvalid(f"scope_invalid: {raw}")
        out.append(rel + "/" if is_dir else rel)
    return tuple(out)


def scope_of(paths: Optional[Sequence[str]], repo_root: "str | Path",
             generic: Sequence[str]) -> Scope:
    """Normalise, drop whole generic directories; no paths left means ``incomplete``."""
    norm = normalise(paths or (), repo_root)
    generic_set = set(generic)
    kept = tuple(p for p in norm if not (p.endswith("/") and p.rstrip("/") in generic_set))
    return Scope(paths=kept, incomplete=not kept)


def _contains(directory: str, path: str) -> bool:
    return directory.endswith("/") and path.startswith(directory) and path != directory


def overlaps(a: Sequence[str], b: Sequence[str]) -> Tuple[str, ...]:
    """The paths of ``a`` that share a file with ``b`` or contain / sit in a directory of ``b``."""
    hits: List[str] = []
    for p in a:
        for q in b:
            if p == q or _contains(p, q) or _contains(q, p):
                hits.append(p)
                break
    return tuple(hits)


def is_self(candidate: Subject, other: Subject) -> bool:
    """Same deliverable, a follow-up round on the same branch, or the PR on that branch."""
    if candidate.deliverable_id and candidate.deliverable_id == other.deliverable_id:
        return True
    own = _branch_of(candidate.deliverable_id) if candidate.deliverable_id else None
    for branch in (candidate.branch, own):
        if branch and other.branch and _round_base(other.branch) == _round_base(branch):
            return True
    return False


def _is_predecessor(candidate: Subject, other: Subject) -> bool:
    for pred in candidate.predecessors:
        if other.deliverable_id == pred:
            return True
        if other.branch and _round_base(other.branch) == _branch_of(pred):
            return True
    return False


def check(candidate: Subject, others: Iterable[Subject], repo_root: "str | Path",
          generic: Sequence[str] = DEFAULT_GENERIC_DIRS) -> List[Result]:
    """One Result per non-excluded other; self and declared predecessors are left out."""
    mine = scope_of(candidate.paths, repo_root, generic)
    results: List[Result] = []
    for other in others:
        if is_self(candidate, other) or _is_predecessor(candidate, other):
            continue
        if mine.incomplete:
            results.append(Result(other.deliverable_id, UNKNOWN, reason=REASON_SCOPE_INCOMPLETE))
            continue
        if other.paths is None:
            results.append(Result(other.deliverable_id, UNKNOWN, reason=REASON_OTHER_UNREADABLE))
            continue
        try:
            theirs = scope_of(other.paths, repo_root, generic)
        except ScopeInvalid:
            results.append(Result(other.deliverable_id, UNKNOWN, reason=REASON_OTHER_UNREADABLE))
            continue
        if theirs.incomplete:
            results.append(Result(other.deliverable_id, UNKNOWN,
                                  reason=REASON_OTHER_SCOPE_INCOMPLETE))
            continue
        hits = overlaps(mine.paths, theirs.paths)
        results.append(Result(other.deliverable_id, OVERLAP, paths=hits) if hits
                       else Result(other.deliverable_id, CLEAR))
    return results
