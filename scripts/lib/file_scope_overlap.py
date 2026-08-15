"""file_scope_overlap.py — warn when two open dispatch branches touch the same files.

OI-1091: nothing prevented two concurrent dispatches from claiming the same files. Three
dispatches collided in ONE worktree and a sibling reset another's branch pointer (OI-1232).
This module detects that overlap at the merge chokepoint and WARNS. It never blocks: a merge
still proceeds, but T0 sees the collision before it lands and can reconcile the two tracks.

Shape of the check:
  - ``open_dispatch_branches`` enumerates remote ``dispatch/*`` branches that are NOT yet
    merged into the base ref (an already-merged-but-not-deleted branch is not "open", so it
    cannot produce a spurious warning).
  - ``changed_files`` is the ``git diff --name-only`` of a branch against the base ref.
  - ``find_overlaps`` intersects the merging branch's files with every other open dispatch
    branch and returns the overlaps; ``warn_overlaps`` formats + emits them.

Everything is best-effort: any git/network failure degrades to "no overlaps found" and a debug
log line, never an exception that blocks the merge.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

_LOG = logging.getLogger(__name__)

DEFAULT_BASE_REF = "origin/main"

# A dispatch branch is a remote ref under refs/heads/dispatch/. A fix-forward branch keeps the
# same shape (it is the parent's dispatch/<id> branch), so the dispatch-id is the last path
# component after the dispatch/ prefix.
_DISPATCH_PREFIX = "dispatch/"


def dispatch_id_from_branch(branch: str) -> Optional[str]:
    """Extract the dispatch-id from a branch name, or None when it is not a dispatch branch.

    Accepts a bare name (``dispatch/20260815-foo``) or a prefixed one (``origin/...`` /
    ``refs/heads/...``). Returns None for a non-dispatch branch (e.g. ``main``).
    """
    name = (branch or "").strip()
    for prefix in ("refs/heads/", "refs/remotes/origin/", "origin/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if not name.startswith(_DISPATCH_PREFIX):
        return None
    rest = name[len(_DISPATCH_PREFIX):].strip("/")
    return rest or None


def _run(cmd: list[str], *, repo: Optional[Path], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(repo) if repo else None,
        capture_output=True,
        text=True,
        check=check,
    )


def _list_remote_dispatch_branches(repo: Optional[Path]) -> list[str]:
    """Return remote ``dispatch/*`` branch names (bare, no origin/ prefix) from the remote.

    Uses ``git ls-remote --heads origin`` so the result reflects what is actually pushed, not a
    possibly-stale local remote-tracking view.
    """
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", "refs/heads/dispatch/*"],
        cwd=str(repo) if repo else None,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return []
    branches: list[str] = []
    for line in proc.stdout.splitlines():
        # "<sha>\trefs/heads/dispatch/<id>" — the ref column is after the first tab.
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/"):])
    return branches


def changed_files(branch: str, *, base_ref: str = DEFAULT_BASE_REF, repo: Optional[Path] = None) -> set[str]:
    """Return the files ``branch`` changed relative to ``base_ref`` (``git diff --name-only``).

    Fetches the branch first so the comparison is against the PUSHED tip, not a stale local
    ref. Empty set on any failure (a missing branch, a bad ref, no network).
    """
    _run(["git", "fetch", "origin", branch], repo=repo, check=False)
    merge_base = _run(
        ["git", "merge-base", base_ref, f"origin/{branch}"], repo=repo, check=False,
    ).stdout.strip()
    if not merge_base:
        return set()
    diff = _run(
        ["git", "diff", "--name-only", f"{merge_base}..origin/{branch}"], repo=repo, check=False,
    ).stdout
    return {line for line in diff.splitlines() if line.strip()}


def _is_merged(branch: str, *, base_ref: str = DEFAULT_BASE_REF, repo: Optional[Path] = None) -> bool:
    """True when the branch's tip is already an ancestor of base_ref (i.e. fully merged)."""
    proc = _run(
        ["git", "merge-base", "--is-ancestor", f"origin/{branch}", base_ref],
        repo=repo, check=False,
    )
    return proc.returncode == 0


def open_dispatch_branches(
    *, base_ref: str = DEFAULT_BASE_REF, repo: Optional[Path] = None
) -> list[str]:
    """Remote ``dispatch/*`` branches that are not yet merged into ``base_ref``.

    An already-merged branch (merged but its remote ref not deleted) is excluded: its files are
    already on the base, so it cannot collide with a new merge.
    """
    open_branches: list[str] = []
    for branch in _list_remote_dispatch_branches(repo):
        # _is_merged needs origin/<branch> present; changed_files fetches it, so reuse that
        # side effect only when the branch is otherwise a candidate.
        _run(["git", "fetch", "origin", branch], repo=repo, check=False)
        if not _is_merged(branch, base_ref=base_ref, repo=repo):
            open_branches.append(branch)
    return open_branches


def find_overlaps(
    merging_branch: str,
    *,
    base_ref: str = DEFAULT_BASE_REF,
    repo: Optional[Path] = None,
) -> list[tuple[str, list[str]]]:
    """Return [(other_dispatch_id, [overlapping files])] for the branch being merged.

    ``merging_branch`` is the PR head branch (bare name, e.g. ``dispatch/20260815-foo``). Each
    entry names the OTHER open dispatch whose files intersect, with the shared files listed.
    Best-effort: on any failure returns [].
    """
    try:
        mine = changed_files(merging_branch, base_ref=base_ref, repo=repo)
        if not mine:
            return []
        overlaps: list[tuple[str, list[str]]] = []
        for other in open_dispatch_branches(base_ref=base_ref, repo=repo):
            if other == merging_branch:
                continue
            other_id = dispatch_id_from_branch(other)
            if not other_id:
                continue
            theirs = changed_files(other, base_ref=base_ref, repo=repo)
            shared = sorted(mine & theirs)
            if shared:
                overlaps.append((other_id, shared))
        return overlaps
    except Exception as exc:  # noqa: BLE001 — a warning must never block a merge
        _LOG.debug("file_scope_overlap: overlap check failed branch=%s: %s", merging_branch, exc)
        return []


def warn_overlaps(
    merging_branch: str,
    *,
    base_ref: str = DEFAULT_BASE_REF,
    repo: Optional[Path] = None,
    stream: Optional[Any] = None,
) -> list[tuple[str, list[str]]]:
    """Compute overlaps for ``merging_branch`` and emit a WARN line per colliding dispatch.

    Returns the overlaps so the caller can also surface them in its result payload. The warning
    names the other dispatch AND lists the shared files (OI-1091's contract). Emits to ``stream``
    (default stderr) and the logger; never raises.
    """
    overlaps = find_overlaps(merging_branch, base_ref=base_ref, repo=repo)
    if not overlaps:
        return overlaps
    out = sys.stderr if stream is None else stream
    for other_id, files in overlaps:
        message = (
            f"WARN: file-scope overlap — dispatch {other_id!r} (open) also touches "
            f"{len(files)} file(s) in this merge: {', '.join(files)}"
        )
        print(message, file=out)
        _LOG.warning("file_scope_overlap: %s", message)
    return overlaps
