"""dispatch_worktree_isolation.py — per-dispatch ephemeral git worktree.

Feature-flag gated: only active when VNX_ISOLATED_WORKTREE=1.
Each dispatch gets a fresh worktree rooted at origin/main under
.vnx-data/worktrees/dispatch-{safe_id}/.  The worktree is removed
(success OR failure) so no state leaks between dispatches.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Collapse any char that is not alphanumeric, hyphen, or underscore.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_SAFE_ID_LEN = 60


def _sanitize_dispatch_id(dispatch_id: str) -> str:
    """Return a filesystem- and git-branch-safe version of dispatch_id."""
    return _UNSAFE_RE.sub("-", dispatch_id)[:_MAX_SAFE_ID_LEN]


def _dispatch_worktree_dir(project_root: Path, dispatch_id: str) -> Path:
    safe_id = _sanitize_dispatch_id(dispatch_id)
    # VNX_BENCH_WORKTREE_ROOT: place worktrees OUTSIDE the main repo so an UNSANDBOXED
    # worker (claude -p, deepseek-harness) cannot reach the main checkout via repo-relative
    # navigation and leak its output into the committed seed. From-scratch / introspection
    # tasks (t3 07/08/09, t4) triggered exactly this when worktrees lived under
    # <repo>/.vnx-data/worktrees/. The GLM agentic runner is sandboxed and is unaffected.
    # Default (unset): the in-repo path — production dispatch behaviour is unchanged.
    root_override = os.environ.get("VNX_BENCH_WORKTREE_ROOT", "").strip()
    if root_override:
        return Path(root_override).expanduser().resolve() / f"dispatch-{safe_id}"
    return project_root / ".vnx-data" / "worktrees" / f"dispatch-{safe_id}"


def _resolve_project_root(project_root: Optional[Path]) -> Path:
    if project_root is not None:
        return project_root.resolve()
    try:
        from project_root import resolve_project_root  # type: ignore[attr-defined]
        return Path(resolve_project_root(__file__)).resolve()
    except Exception:
        return Path(__file__).resolve().parents[2]


@contextmanager
def _worktree_lock(root: Path):
    """Serialize `git worktree` add/remove via an exclusive fcntl lock.

    Uses the SAME lock path as tmux_worktree._flock_context
    (``<repo>/.git/worktrees/.vnx-lock``) so the provider lane and the tmux lane
    never run concurrent ``git worktree add/remove`` against one repo. Concurrent
    adds contend on git's internal index/HEAD locks and fail in ~0.8s, which under
    VNX_BENCH_REQUIRE_ISOLATION=1 cascades into spurious isolation DNFs (observed
    2026-06-18 at --parallel 8: every provider cell DNF'd at 0.8s).
    """
    lock_dir = (root / ".git").resolve() / "worktrees"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".vnx-lock"
    with open(lock_path, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def create_dispatch_worktree(
    dispatch_id: str,
    *,
    project_root: Optional[Path] = None,
) -> Path:
    """Create an ephemeral git worktree based on origin/main for one dispatch.

    Steps:
      1. git fetch origin main  (best-effort — warns on failure)
      2. git worktree add <path> -b dispatch/<safe_id> origin/main

    Returns the resolved worktree Path.
    Raises RuntimeError when worktree creation fails.
    """
    root = _resolve_project_root(project_root)
    wt_path = _dispatch_worktree_dir(root, dispatch_id)
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch_name = f"dispatch/{safe_id}"

    # VNX_BENCH_WORKTREE_BASE_REF: base the worktree on a given ref instead of origin/main.
    # The benchmark sets this to the bench checkout's HEAD so worktrees carry the bench
    # branch's committed task seeds (e.g. the t4_02 SWE-bench seed) without merging WIP
    # benchmark tasks to main. Default (unset) keeps origin/main — production unchanged.
    base_ref = os.environ.get("VNX_BENCH_WORKTREE_BASE_REF", "").strip() or "origin/main"
    is_remote = base_ref.startswith("origin/")

    wt_path.parent.mkdir(parents=True, exist_ok=True)

    if is_remote:
        try:
            subprocess.run(
                ["git", "fetch", "origin", base_ref[len("origin/"):]],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            log.warning(
                "create_dispatch_worktree: git fetch %s failed (continuing): %s",
                base_ref, (exc.stderr or "").strip(),
            )

    try:
        with _worktree_lock(root):
            subprocess.run(
                [
                    "git", "worktree", "add",
                    str(wt_path),
                    "-b", branch_name,
                    base_ref,
                ],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"create_dispatch_worktree failed for {dispatch_id!r}: {(exc.stderr or '').strip()}"
        ) from exc

    log.info("dispatch worktree created: %s (branch %s)", wt_path, branch_name)
    return wt_path.resolve()


def remove_dispatch_worktree(
    dispatch_id: str,
    *,
    project_root: Optional[Path] = None,
) -> None:
    """Remove the ephemeral dispatch worktree.  Idempotent.

    Called on both success and failure paths — the worker's pushed branch
    survives on origin; only the local working tree is removed.
    """
    root = _resolve_project_root(project_root)
    wt_path = _dispatch_worktree_dir(root, dispatch_id)

    if not wt_path.exists():
        log.debug("remove_dispatch_worktree: already absent: %s", wt_path)
        return

    with _worktree_lock(root):
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
            log.info("dispatch worktree removed: %s", wt_path)
        except subprocess.CalledProcessError as exc:
            log.warning(
                "git worktree remove failed: %s; falling back to shutil.rmtree",
                (exc.stderr or "").strip(),
            )
            resolved = wt_path.resolve()
            # Safety: refuse to rmtree a path outside the project root.
            resolved.relative_to(root)
            if wt_path.is_symlink():
                raise RuntimeError(
                    f"refusing rmtree: {wt_path} is a symlink"
                )
            shutil.rmtree(str(resolved), ignore_errors=True)

        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            log.warning("git worktree prune failed: %s", (exc.stderr or "").strip())

    # Best-effort: delete the local dispatch branch (it lives on origin).
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch_name = f"dispatch/{safe_id}"
    try:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        log.debug("dispatch branch deleted locally: %s", branch_name)
    except Exception as exc:
        log.debug("branch deletion failed for %s: %s", branch_name, exc)
