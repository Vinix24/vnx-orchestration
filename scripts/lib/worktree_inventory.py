"""worktree_inventory.py — one read-only inventory of every git worktree the
fabric owns.

Measured 24/25-10-2026 on the dev checkout: 35 worktrees under
``.vnx-data/worktrees/`` — dispatch trees (``dispatch-*``), gate trees
(``gate-*``, ``gate_worktree.create_gate_worktree``), pool trees
(``pool-*``, ``pool_worktree_manager``) — plus the bench trees under
``VNX_BENCH_WORKTREE_ROOT`` (``dispatch_worktree_isolation._dispatch_worktree_dir``).
``scripts/cleanup_reviewed_worktrees.py`` only ever sees LOCKED
``dispatch-*`` trees, so a leaked gate, pool or bench tree is invisible to it.
This module answers "what is there, and in what state?" for all of them at
once, and is what ``cleanup_reviewed_worktrees.py --all`` prints.

Read-only by construction: nothing here writes, locks, unlocks, removes a
worktree or deletes a branch. Every field is measured, never guessed:

* ``dirty_count`` — ``git status --porcelain -z`` inside the tree, read as
  bytes (a ``\\r`` or ``\\n`` in a filename stays that byte) and counted per
  path. An untracked symlink that resolves inside the repo is a convenience
  link, not work, and is not counted — the same rule
  ``cleanup_reviewed_worktrees.is_worktree_clean`` uses (one implementation,
  :func:`is_internal_symlink`, shared by both).
* ``unpushed`` — ``git rev-list --count <head> --not --remotes=origin``.
* ``age_days`` — whole days since the mtime of the tree's ``.git`` file.
* ``missing`` — the tree's path no longer exists. Such a tree is reported,
  never dropped: an entry git still knows about is exactly what a sweep must
  see.

Unknown evidence is ``None``, never ``0``: a failed ``git status`` does not
mean "clean", and an unreadable ``.git`` does not mean "age zero". Only a
failed ``git worktree list --porcelain`` raises (:class:`WorktreeInventoryError`)
— there is no partial inventory.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

KIND_DISPATCH = "dispatch"
KIND_GATE = "gate"
KIND_POOL = "pool"
KIND_BENCH = "bench"
KIND_OTHER = "other"

BENCH_ROOT_ENV = "VNX_BENCH_WORKTREE_ROOT"

# Managed tree name prefixes, in ``_worktree_dir`` order of their owners:
# gate_worktree._MANAGED_WORKTREE_PREFIX, pool_worktree_manager, dispatch.
_NAME_KINDS = (("dispatch-", KIND_DISPATCH), ("gate-", KIND_GATE), ("pool-", KIND_POOL))

_SECONDS_PER_DAY = 86400.0


class WorktreeInventoryError(RuntimeError):
    """``git worktree list --porcelain`` failed; no inventory was produced."""


class WorktreePorcelainRecord(TypedDict, total=False):
    """One ``git worktree list --porcelain`` block. Every key is optional —
    presence tracks whether the corresponding line appeared at all."""

    path: str
    head: str
    branch: str
    locked: str
    detached: bool
    bare: bool
    prunable: bool


def parse_worktree_list_porcelain(text: str) -> list[WorktreePorcelainRecord]:
    """Parse ``git worktree list --porcelain`` into one dict per worktree
    block. Keys present only when the corresponding line was present:
    ``path``, ``head``, ``branch``, ``locked`` (reason, possibly ""),
    ``detached`` (bool)."""
    records: list[WorktreePorcelainRecord] = []
    current: WorktreePorcelainRecord = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):]
        elif line == "detached":
            current["detached"] = True
        elif line == "locked":
            current["locked"] = ""
        elif line.startswith("locked "):
            current["locked"] = line[len("locked "):]
        elif line == "bare":
            current["bare"] = True
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
    if current:
        records.append(current)
    return records


def is_internal_symlink(entry_path: Path, repo_root: Path) -> bool:
    """An untracked symlink whose target resolves inside ``repo_root`` (such as
    ``.venv -> <checkout>/.venv``) is a convenience link, not work."""
    if not entry_path.is_symlink():
        return False
    target = Path(os.path.realpath(entry_path))
    root = Path(os.path.realpath(repo_root))
    return target == root or root in target.parents


def git_status_bytes(wt_path: Path) -> bytes | None:
    """Raw ``git status --porcelain -z`` output. No text mode: universal
    newlines would rewrite a ``\\r`` in a filename and the record would no
    longer name the file on disk. ``None`` when git could not run or failed."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=str(wt_path), capture_output=True, check=False,
        )
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def count_dirty_paths(raw: bytes, wt_path: Path, repo_root: Path) -> int:
    """Number of changed paths in a ``status --porcelain -z`` stream.

    A rename/copy record is followed by its source path as a separate
    NUL-terminated field; that source is not a path of its own, so it is
    consumed instead of counted. An untracked internal symlink is not work."""
    records = raw.split(b"\0")
    count = 0
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status = record[:2]
        if status == b"??":
            try:
                name = os.fsdecode(record[3:])
            except (UnicodeError, ValueError):
                count += 1
                continue
            if name and is_internal_symlink(wt_path / name, repo_root):
                continue
        count += 1
        if b"R" in status or b"C" in status:
            index += 1
    return count


def dirty_count(wt_path: Path, repo_root: Path) -> int | None:
    """Changed paths in *wt_path*; ``None`` when git status failed (unknown is
    not clean)."""
    raw = git_status_bytes(wt_path)
    if raw is None:
        return None
    return count_dirty_paths(raw, wt_path, repo_root)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _unpushed_count(repo_root: Path, head: str | None) -> int | None:
    """Commits on *head* that no ``origin/*`` ref reaches; ``None`` when the
    count cannot be read."""
    if not head:
        return None
    proc = _run_git(["rev-list", "--count", head, "--not", "--remotes=origin"], cwd=repo_root)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _age_days(wt_path: Path, now: float) -> int | None:
    try:
        mtime = os.stat(wt_path / ".git").st_mtime
    except OSError:
        return None
    return max(0, int((now - mtime) // _SECONDS_PER_DAY))


def _bench_worktree_root() -> Path | None:
    raw = os.environ.get(BENCH_ROOT_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _classify(path: Path, repo_root: Path, bench_root: Path | None) -> tuple[str, bool]:
    """``(kind, owned)`` by location, then by the name the owner gave it."""
    if bench_root is not None and _is_within(path, bench_root):
        return KIND_BENCH, True
    if not _is_within(path, (repo_root / ".vnx-data" / "worktrees").resolve()):
        return KIND_OTHER, False
    for prefix, kind in _NAME_KINDS:
        if path.name.startswith(prefix):
            return kind, True
    return KIND_OTHER, True


@dataclass(frozen=True)
class WorktreeEntry:
    """One worktree, measured. ``dirty_count``/``unpushed``/``age_days`` are
    ``None`` when the evidence could not be read — never a stand-in zero."""

    path: Path
    branch: str | None
    head: str | None
    locked: bool
    lock_reason: str | None
    dirty_count: int | None
    unpushed: int | None
    age_days: int | None
    kind: str
    owned: bool
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "branch": self.branch,
            "head": self.head,
            "locked": self.locked,
            "lock_reason": self.lock_reason,
            "dirty_count": self.dirty_count,
            "unpushed": self.unpushed,
            "age_days": self.age_days,
            "kind": self.kind,
            "owned": self.owned,
            "missing": self.missing,
        }


def _entry_from_record(
    record: WorktreePorcelainRecord,
    repo_root: Path,
    bench_root: Path | None,
    now: float,
) -> WorktreeEntry | None:
    raw_path = record.get("path")
    if not raw_path:
        return None
    path = Path(raw_path)
    try:
        path = path.resolve()
    except OSError:
        pass
    missing = not path.exists()
    branch_ref = record.get("branch")
    branch = branch_ref[len("refs/heads/"):] if branch_ref and branch_ref.startswith("refs/heads/") else branch_ref
    lock_reason = record.get("locked")
    kind, owned = _classify(path, repo_root, bench_root)
    head = record.get("head")
    return WorktreeEntry(
        path=path,
        branch=branch,
        head=head,
        locked=lock_reason is not None,
        lock_reason=lock_reason or None,
        dirty_count=None if missing else dirty_count(path, repo_root),
        unpushed=_unpushed_count(repo_root, head),
        age_days=_age_days(path, now),
        kind=kind,
        owned=owned,
        missing=missing,
    )


def inventory(repo_root: Path, *, now: float | None = None) -> list[WorktreeEntry]:
    """Every worktree git knows about in *repo_root*, in porcelain order.

    *now* is epoch seconds for the age column (tests pin it). Raises
    :class:`WorktreeInventoryError` when ``git worktree list`` itself fails —
    a partial inventory must never look complete."""
    root = Path(repo_root).resolve()
    proc = _run_git(["worktree", "list", "--porcelain"], cwd=root)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise WorktreeInventoryError(
            f"git worktree list --porcelain faalde (exit {proc.returncode}): {stderr or 'geen stderr'}"
        )
    at = time.time() if now is None else now
    bench_root = _bench_worktree_root()
    entries: list[WorktreeEntry] = []
    for record in parse_worktree_list_porcelain(proc.stdout):
        entry = _entry_from_record(record, root, bench_root, at)
        if entry is not None:
            entries.append(entry)
    return entries
