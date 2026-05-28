"""tmux_worktree.py — Ephemeral per-dispatch git worktree isolation (PR-TMUX-3).

allocate()  → create an isolated working tree for a single dispatch
classify()  → determine the tree's state at teardown (clean/committed/pushed/dirty)
reap()      → three-state cleanup based on classification
"""
from __future__ import annotations

import fcntl
import logging
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Analogous to pool_worktree_manager._TERMINAL_ID_RE; dispatch IDs are longer.
_DISPATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# Fetch cache: keyed by base_ref, value = monotonic timestamp of last successful cache-update.
_FETCH_CACHE: dict[str, float] = {}
_FETCH_CACHE_TTL = 30.0


class WorktreeAllocateError(RuntimeError):
    """Raised when git worktree add fails unrecoverably."""


@dataclass
class WorktreeHandle:
    path: Path
    branch: str
    base_sha: str
    base_ref: str
    dispatch_id: str


@dataclass
class ReapResult:
    removed: bool
    branch_kept_local: bool = False
    branch_kept_remote: bool = False
    preserved_path: Path | None = None
    errors: list[str] = field(default_factory=list)


def _resolve_repo_root(repo_root: Path | None) -> Path:
    if repo_root is not None:
        return repo_root.resolve()
    try:
        from project_root import resolve_project_root  # type: ignore[attr-defined]
        return resolve_project_root(__file__)
    except Exception:
        return Path.cwd().resolve()


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Thin subprocess wrapper — no shell=True."""
    return subprocess.run(args, capture_output=True, text=True, **kwargs)


def _git_common_dir(repo_root: Path) -> Path:
    """Return the git common dir (handles bare worktrees where .git is a file)."""
    result = _run(["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"])
    if result.returncode == 0:
        raw = result.stdout.strip()
        p = Path(raw)
        return p if p.is_absolute() else (repo_root / p).resolve()
    # Fallback: assume standard layout
    return (repo_root / ".git").resolve()


@contextmanager
def _flock_context(repo_root: Path):
    """Serialize worktree add/remove via an exclusive fcntl lock on <git-common-dir>/worktrees/.vnx-lock."""
    git_dir = _git_common_dir(repo_root)
    lock_dir = git_dir / "worktrees"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".vnx-lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _maybe_fetch(repo_root: Path, base_ref: str) -> None:
    """Fetch origin for base_ref if the cache entry is older than TTL."""
    now = time.monotonic()
    if now - _FETCH_CACHE.get(base_ref, 0.0) < _FETCH_CACHE_TTL:
        return
    _FETCH_CACHE[base_ref] = now
    remote_branch = base_ref[len("origin/"):] if base_ref.startswith("origin/") else base_ref
    result = _run(["git", "-C", str(repo_root), "fetch", "origin", remote_branch])
    if result.returncode != 0:
        logger.warning(
            "fetch origin %s failed (proceeding): %s",
            remote_branch,
            (result.stderr or "").strip(),
        )


def allocate(
    dispatch_id: str,
    *,
    base_ref: str = "origin/main",
    repo_root: Path | None = None,
) -> WorktreeHandle:
    """Create an ephemeral isolated git worktree for *dispatch_id*.

    Raises ValueError for invalid dispatch_id.
    Raises WorktreeAllocateError on unrecoverable git failures.
    """
    if not _DISPATCH_ID_RE.fullmatch(dispatch_id):
        raise ValueError(
            f"invalid dispatch_id {dispatch_id!r}: "
            "must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
        )

    root = _resolve_repo_root(repo_root)
    worktree_path = root / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    _maybe_fetch(root, base_ref)

    # Resolve base_sha BEFORE add — needed by classify/reap for push-detection.
    sha_result = _run(["git", "-C", str(root), "rev-parse", base_ref])
    if sha_result.returncode != 0:
        raise WorktreeAllocateError(
            f"cannot resolve {base_ref!r}: {(sha_result.stderr or '').strip()}"
        )
    base_sha = sha_result.stdout.strip()
    branch = f"dispatch/{dispatch_id}"

    with _flock_context(root):
        add_result = _run(
            [
                "git", "-C", str(root), "worktree", "add",
                "-b", branch,
                str(worktree_path),
                base_ref,
            ]
        )
        if add_result.returncode != 0:
            stderr = (add_result.stderr or "").strip()
            if "already exists" in stderr or "already checked out" in stderr:
                # Branch exists: attach it to the new worktree path without -b.
                attach_result = _run(
                    [
                        "git", "-C", str(root), "worktree", "add",
                        str(worktree_path),
                        branch,
                    ]
                )
                if attach_result.returncode != 0:
                    raise WorktreeAllocateError(
                        f"worktree attach failed for {dispatch_id!r}: "
                        f"{(attach_result.stderr or '').strip()}"
                    )
                # Verify the branch points at base_sha (or the same commit).
                bsha_result = _run(["git", "-C", str(root), "rev-parse", branch])
                branch_sha = bsha_result.stdout.strip() if bsha_result.returncode == 0 else ""
                if branch_sha and branch_sha != base_sha:
                    raise WorktreeAllocateError(
                        f"branch {branch!r} already exists at {branch_sha[:8]!r}, "
                        f"expected {base_sha[:8]!r}"
                    )
            else:
                raise WorktreeAllocateError(
                    f"git worktree add failed for {dispatch_id!r}: {stderr}"
                )

    resolved_path = worktree_path.resolve()
    logger.info(
        "worktree allocated: %s branch=%s base=%s",
        resolved_path,
        branch,
        base_sha[:8],
    )
    return WorktreeHandle(
        path=resolved_path,
        branch=branch,
        base_sha=base_sha,
        base_ref=base_ref,
        dispatch_id=dispatch_id,
    )


def classify(handle: WorktreeHandle) -> Literal["clean", "committed", "pushed", "dirty"]:
    """Determine the state of the worktree at teardown time."""
    wt = handle.path

    status_result = _run(
        [
            "git", "-c", "core.fileMode=false", "-c", "core.autocrlf=input",
            "-C", str(wt), "status", "--porcelain",
        ]
    )
    if status_result.returncode == 0 and status_result.stdout.strip():
        return "dirty"

    local_sha_result = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    if local_sha_result.returncode != 0:
        return "clean"
    local_sha = local_sha_result.stdout.strip()

    if local_sha == handle.base_sha:
        return "clean"

    # New commits exist — determine whether they've been pushed to origin.
    try:
        ls_result = _run(
            [
                "git", "-C", str(wt),
                "ls-remote", "origin", f"dispatch/{handle.dispatch_id}",
            ],
            timeout=10,
        )
        remote_output = ls_result.stdout.strip() if ls_result.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        logger.warning("ls-remote timed out for %s; treating as committed", handle.branch)
        return "committed"
    except Exception as exc:
        logger.warning(
            "ls-remote failed for %s (%s); treating as committed", handle.branch, exc
        )
        return "committed"

    if not remote_output:
        return "committed"

    remote_sha = remote_output.split()[0]
    return "pushed" if remote_sha == local_sha else "committed"


def _remove_worktree_with_fallback(root: Path, wt: Path) -> list[str]:
    """Remove worktree with --force; retry once; fall back to rmtree + prune."""
    errors: list[str] = []
    result = _run(["git", "-C", str(root), "worktree", "remove", "--force", str(wt)])
    if result.returncode != 0:
        time.sleep(0.3)
        result2 = _run(["git", "-C", str(root), "worktree", "remove", "--force", str(wt)])
        if result2.returncode != 0:
            errors.append(f"worktree remove failed: {(result2.stderr or '').strip()}")
            if not wt.is_symlink() and wt.is_dir():
                shutil.rmtree(str(wt), ignore_errors=True)
            else:
                logger.warning("refusing rmtree: %s is symlink or not a dir", wt)
            prune = _run(["git", "-C", str(root), "worktree", "prune"])
            if prune.returncode != 0:
                errors.append(f"worktree prune failed: {(prune.stderr or '').strip()}")
    return errors


def reap(handle: WorktreeHandle, classification: str) -> ReapResult:
    """Clean up the worktree based on its classification.

    clean     → remove worktree + delete local branch
    pushed    → remove worktree + delete local branch (remote ref preserved)
    committed → remove worktree disk only; keep local branch
    dirty     → lock worktree in place; preserve everything
    """
    # Reconstruct repo_root: handle.path = root/.vnx-data/worktrees/dispatch-<id>
    root = handle.path.parent.parent.parent
    branch = handle.branch
    wt = handle.path

    with _flock_context(root):
        if classification == "clean":
            errors = _remove_worktree_with_fallback(root, wt)
            br = _run(["git", "-C", str(root), "branch", "-D", branch])
            if br.returncode != 0:
                errors.append(f"branch delete failed: {(br.stderr or '').strip()}")
            return ReapResult(removed=True, errors=errors)

        if classification == "pushed":
            errors = _remove_worktree_with_fallback(root, wt)
            br = _run(["git", "-C", str(root), "branch", "-D", branch])
            if br.returncode != 0:
                errors.append(f"branch delete failed: {(br.stderr or '').strip()}")
            return ReapResult(removed=True, branch_kept_remote=True, errors=errors)

        if classification == "committed":
            errors = _remove_worktree_with_fallback(root, wt)
            return ReapResult(removed=True, branch_kept_local=True, errors=errors)

        if classification == "dirty":
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            lock_result = _run(
                [
                    "git", "-C", str(root), "worktree", "lock", str(wt),
                    "--reason", f"vnx preserve {ts}",
                ]
            )
            if lock_result.returncode != 0:
                logger.warning(
                    "worktree lock failed for %s: %s",
                    wt,
                    (lock_result.stderr or "").strip(),
                )
            return ReapResult(removed=False, preserved_path=wt)

    logger.warning(
        "unknown classification %r for %s; treating as dirty",
        classification,
        handle.dispatch_id,
    )
    return ReapResult(
        removed=False,
        preserved_path=wt,
        errors=[f"unknown classification: {classification}"],
    )
