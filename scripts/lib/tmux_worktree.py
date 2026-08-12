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

# OI-1124: both name-forms a dispatch identity travels in — the canonical
# branch form ``dispatch/<id>`` (this module and the provider-lane allocator)
# and the worktree-DIRECTORY form ``dispatch-<id>`` (which git itself would
# mint as a branch from the path basename, and which a confused worker can
# copy as a branch name). A checked-out branch matching this pattern with a
# DIFFERENT embedded id is a cross-dispatch identity compromise.
_DISPATCH_BRANCH_RE = re.compile(r"^dispatch[-/](?P<id>[A-Za-z0-9][A-Za-z0-9_-]{0,63})$")

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


def _current_branch(wt: "Path | str") -> str:
    """The branch checked out at *wt* ('' on git failure, 'HEAD' when detached)."""
    result = _run(["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else ""


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

        # OI-1124: the worktree MUST come up on its own dispatch branch. git
        # mints a branch from the PATH BASENAME (dash form ``dispatch-<id>``)
        # when ``worktree add`` runs without ``-b``/committish, so any code
        # path that regresses into that form silently builds a wrong
        # worktree→branch mapping — the failure that surfaces later as one
        # dispatch's diff merging under another's name. Assert at creation,
        # fail loud. Fail-open on '' (git itself unreachable): the add above
        # already succeeded, so an unreadable HEAD is a probe failure, not
        # evidence of a wrong branch.
        actual_branch = _current_branch(worktree_path)
        if actual_branch and actual_branch != branch:
            raise WorktreeAllocateError(
                f"worktree for {dispatch_id!r} came up on branch "
                f"{actual_branch!r}, expected {branch!r} — branch/worktree "
                f"identity mismatch at creation (OI-1124)"
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
    """Determine the state of the worktree at teardown time.

    Thin wrapper over :func:`classify_path` — the single canonical git-state
    classification, shared by the tmux lane (which has a WorktreeHandle) and the
    envelope lanes (which have only a worktree path + branch). The decision logic
    lives in classify_path so it is never duplicated across lanes.
    """
    return classify_path(
        wt=handle.path,
        branch=handle.branch,
        dispatch_id=handle.dispatch_id,
        base_sha=handle.base_sha,
    )


def classify_path(
    *,
    wt: "Path | str",
    branch: str,
    dispatch_id: str,
    base_sha: "str | None" = None,
) -> Literal["clean", "committed", "pushed", "dirty"]:
    """Classify a dispatch worktree's git state from a path alone.

    This is the one canonical classification, reused by every lane's PR
    enforcement (rij-7 of the lane-matrix) so the per-state decision in
    pr_enforcement.enforce_pr_exists runs against a single verdict source.

    ``base_sha`` is the worktree's base commit (origin/main at allocation time).
    The envelope lanes resolve it from ``base_ref`` (mirroring the tmux lane's
    ``WorktreeHandle.base_sha``) and pass it here. When it is still unknown, the
    worktree's merge-base with origin/main is tried as a fallback. If THAT fails
    too (shallow PR-clone in CI without a local ``origin/main``), a clean tree is
    classified ``clean`` rather than guessed ``committed`` — a brand-new dispatch
    branch always has an empty ``ls-remote`` result, so the old "empty remote →
    committed" inference turned a commit-less worktree into a false push+PR
    rejection (OI-1011 regression, faler-2 of dispatch
    20260809-fix1419-census-headless). A false ``clean`` misses enforcement on
    stranded work; a false ``committed`` breaks a dispatch that committed nothing.
    The former is recoverable (T0 salvages), the latter is a hard regression.

    States:
    - ``dirty``     : uncommitted tracked changes in the worktree.
    - ``clean``     : no new commits (HEAD == base), or base unresolvable and clean.
    - ``committed`` : new local commits, not yet on origin (or origin unknown).
    - ``pushed``    : new commits and the remote dispatch branch matches HEAD.
    """
    # ── OI-1124: cross-dispatch branch-identity drift ──────────────────────
    # Measured 2026-08-10: a worker that received a crossed instruction (the
    # pre-#1451 shared tmux paste buffer) ran ``git checkout -b
    # dispatch-<OTHER-id>`` inside its own worktree — dispatch D's worktree
    # ended up on a branch carrying dispatch B's id, and B's PR shipped from
    # D's tree. Allocation was correct; the drift happened mid-run. Everything
    # downstream (reap's branch delete, PR enforcement, T0's review) identifies
    # work by branch name, so silently continuing risks merging one dispatch's
    # diff under another's name with green CI. A worktree whose checked-out
    # branch names a DIFFERENT dispatch id is therefore classified ``dirty`` —
    # reap preserves and locks it for identity review instead of reaping or
    # branch-deleting under the wrong identity. Worker-chosen non-dispatch
    # branches (``fix/...``) and a re-formed branch carrying the OWN id keep
    # their normal verdicts: identity is intact there, only the form differs.
    current = _current_branch(wt)
    if current and current != branch:
        drift = _DISPATCH_BRANCH_RE.match(current)
        expected_ids = {dispatch_id}
        expected = _DISPATCH_BRANCH_RE.match(branch)
        if expected:
            expected_ids.add(expected.group("id"))
        if drift and drift.group("id") not in expected_ids:
            logger.warning(
                "OI-1124 BRANCH-IDENTITY DRIFT: worktree %s (dispatch %s) is "
                "checked out on %r, which names a DIFFERENT dispatch (%r); "
                "expected %r. Classifying 'dirty' so the worktree is preserved "
                "for identity review instead of reaped or merged under the "
                "wrong name.",
                wt, dispatch_id, current, drift.group("id"), branch,
            )
            return "dirty"

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

    ref_sha = base_sha
    if ref_sha is None:
        # The envelope lanes normally pass base_sha (resolved from base_ref). When
        # they do not, fall back to the merge-base: a clean tree's HEAD equals it.
        mb = _run(
            ["git", "-C", str(wt), "merge-base", "origin/main", "HEAD"],
        )
        ref_sha = mb.stdout.strip() if mb.returncode == 0 else None
    if ref_sha and local_sha == ref_sha:
        return "clean"
    if ref_sha is None:
        # Base unresolvable (no base_sha passed AND origin/main absent locally).
        # A clean tree here must NOT be guessed ``committed`` from an empty
        # ls-remote: every brand-new dispatch branch has an empty remote, so that
        # inference is a false positive on commit-less worktrees. Treat as clean
        # and surface the degraded resolution so it is visible, not silent.
        logger.warning(
            "classify_path: base unresolvable for dispatch=%s branch=%s "
            "(no base_sha, origin/main absent) — clean tree classified 'clean' "
            "(degraded; push+PR enforcement skips). Investigate if work was expected.",
            dispatch_id, branch,
        )
        return "clean"

    # New commits exist (HEAD diverged from a known base) — determine whether
    # they've been pushed to origin.
    remote_ref = branch if branch.startswith("refs/") else f"refs/heads/{branch}"
    try:
        ls_result = _run(
            ["git", "-C", str(wt), "ls-remote", "origin", remote_ref],
            timeout=10,
        )
        remote_output = ls_result.stdout.strip() if ls_result.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        logger.warning("ls-remote timed out for %s; treating as committed", branch)
        return "committed"
    except Exception as exc:
        logger.warning("ls-remote failed for %s (%s); treating as committed", branch, exc)
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

    Before removing the worktree, kills any processes still running inside it
    (OI-873): a SessionStart hook spawned from the worktree can survive the
    dispatch and hold the coordination DB write lock.  Process cleanup runs
    OUTSIDE the flock context so it never blocks concurrent allocations.
    """
    # Reconstruct repo_root: handle.path = root/.vnx-data/worktrees/dispatch-<id>
    root = handle.path.parent.parent.parent
    branch = handle.branch
    wt = handle.path

    # OI-873: kill processes still running inside this worktree before removal.
    # Only needed when the worktree WILL be removed (clean/pushed/committed);
    # dirty worktrees are preserved — their processes may still be doing useful
    # work and killing them would lose uncommitted state.
    if classification in ("clean", "pushed", "committed"):
        try:
            from worktree_process_cleanup import kill_worktree_processes  # noqa: PLC0415
            kill_worktree_processes(wt)
        except Exception as _proc_exc:
            logger.warning(
                "reap: process cleanup failed for %s: %s — "
                "continuing with worktree removal",
                handle.dispatch_id, _proc_exc,
            )
        # OI-877: also reap process groups recorded for this dispatch at spawn
        # time.  A dispatch process whose repo-root resolved to the MAIN
        # checkout has nothing open inside the worktree, so the lsof scan
        # cannot see it — the recorded PGID is the only handle that still
        # reaches it (even after it was reparented to launchd / PPID 1).  The
        # worktree scan above stays; this is a second membership source, not
        # a replacement.
        try:
            from dispatch_process_registry import (  # noqa: PLC0415
                clear_dispatch_pgids,
                kill_dispatch_pgids,
            )
            kill_dispatch_pgids(handle.dispatch_id, repo_root=root)
            clear_dispatch_pgids(handle.dispatch_id, repo_root=root)
        except Exception as _pgid_exc:
            logger.warning(
                "reap: pgid-based process cleanup failed for %s: %s — "
                "continuing with worktree removal",
                handle.dispatch_id, _pgid_exc,
            )

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
