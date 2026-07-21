"""gate_worktree.py — ephemeral git worktree checkout for gate execution (OI-708).

codex_gate and gemini_review spawn a real CLI agent (codex/gemini) that can run
its own shell tools (sed/rg/cat/...) against whatever `cwd` the subprocess
inherits. The gate's diff is fetched authoritatively via `gh pr diff`, but the
agent's OWN file reads previously hit the orchestrator's ambient working
directory — which can be stale relative to the PR branch (uncommitted local
drift, or simply not fast-forwarded to `origin/<branch>` HEAD).

This module checks out `origin/<branch>` into an isolated, detached-HEAD git
worktree so the gate subprocess's `cwd` matches the diff it is reviewing, and
removes the worktree unconditionally afterward (success or failure) so no
per-execution worktree leaks and the orchestrator's own checkout is never
touched (no `git checkout` in the caller's tree).
"""

from __future__ import annotations

import fcntl
import logging
import re
import secrets
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Collapse any char that is not alphanumeric, hyphen, or underscore.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_SAFE_LEN = 60

# Conservative git ref-name allowlist: must start with an alnum (never '-', so
# a branch cannot be mistaken for a git option), and contain only the chars
# real branch names use. Whitespace of any kind is excluded implicitly.
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

# Managed worktree directory prefix — must match _worktree_dir() below. Used
# by remove_gate_worktree to scope its shutil.rmtree fallback.
_MANAGED_WORKTREE_PREFIX = "gate-"


class GateWorktreeError(RuntimeError):
    """Raised when a gate's isolated worktree cannot be created.

    Callers MUST treat this as a gate execution failure — never fall back to
    running the gate subprocess in the orchestrator's ambient checkout, as
    that reintroduces the stale-checkout bug this module exists to fix.
    """


def _sanitize(value: str) -> str:
    return _UNSAFE_RE.sub("-", value or "")[:_MAX_SAFE_LEN] or "unknown"


def _resolve_project_root(project_root: Optional[Path]) -> Path:
    if project_root is not None:
        return project_root.resolve()
    try:
        from project_root import resolve_project_root  # type: ignore[attr-defined]
        return Path(resolve_project_root(__file__)).resolve()
    except Exception:
        return Path(__file__).resolve().parents[2]


def _worktree_dir(project_root: Path, gate: str, identifier: str) -> Path:
    token = secrets.token_hex(4)
    return (
        project_root / ".vnx-data" / "worktrees"
        / f"{_MANAGED_WORKTREE_PREFIX}{_sanitize(gate)}-{_sanitize(identifier)}-{token}"
    )


def _validate_branch(branch: str, *, gate: str, identifier: str) -> None:
    """Reject any branch that is not a safe git ref name.

    MUST run before any git subprocess call: a branch beginning with '-'
    (e.g. ``--upload-pack=...``) would otherwise be parsed by git as an
    option rather than a ref.
    """
    if not _SAFE_BRANCH_RE.match(branch):
        raise GateWorktreeError(
            f"create_gate_worktree: unsafe branch {branch!r} rejected before any git call "
            f"(gate={gate!r}, identifier={identifier!r})"
        )


@contextmanager
def _worktree_lock(root: Path):
    """Serialize `git worktree` add/remove via an exclusive fcntl lock.

    Uses the SAME lock path as dispatch_worktree_isolation / tmux_worktree
    (``<repo>/.git/worktrees/.vnx-lock``) so gate execution never races other
    lanes' concurrent ``git worktree add/remove`` against this repo.
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


def create_gate_worktree(
    *,
    branch: str,
    gate: str,
    identifier: str,
    project_root: Optional[Path] = None,
) -> Path:
    """Fetch origin/<branch> and check it out into an isolated detached worktree.

    Steps:
      1. git fetch origin <branch>
      2. git worktree add --detach <path> origin/<branch>

    Raises GateWorktreeError when branch is empty or either git step fails —
    callers must fail the gate rather than silently falling back to the
    orchestrator's (possibly stale) checkout.
    """
    if not branch:
        raise GateWorktreeError(
            "create_gate_worktree requires a non-empty branch "
            f"(gate={gate!r}, identifier={identifier!r})"
        )
    _validate_branch(branch, gate=gate, identifier=identifier)

    root = _resolve_project_root(project_root)
    wt_path = _worktree_dir(root, gate, identifier)
    wt_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=str(root), check=True, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise GateWorktreeError(
            f"create_gate_worktree: git fetch origin {branch!r} failed: {detail}"
        ) from exc

    try:
        with _worktree_lock(root):
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path), f"origin/{branch}"],
                cwd=str(root), check=True, capture_output=True, text=True, timeout=30,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise GateWorktreeError(
            f"create_gate_worktree: git worktree add failed for gate={gate!r} "
            f"branch={branch!r}: {detail}"
        ) from exc

    log.info("gate worktree created: %s (origin/%s, detached)", wt_path, branch)
    return wt_path.resolve()


def remove_gate_worktree(wt_path: Optional[Path], *, project_root: Optional[Path] = None) -> None:
    """Remove a gate worktree. Best-effort + idempotent — never raises.

    Called on both success and failure paths (including when the gate
    subprocess itself failed) so no worktree ever leaks past one gate
    execution.
    """
    if not wt_path:
        return
    wt_path = Path(wt_path)
    if not wt_path.exists():
        return

    root = _resolve_project_root(project_root)
    try:
        with _worktree_lock(root):
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=str(root), check=True, capture_output=True, text=True, timeout=30,
                )
                log.info("gate worktree removed: %s", wt_path)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                log.warning(
                    "git worktree remove failed: %s; falling back to shutil.rmtree", detail,
                )
                resolved = wt_path.resolve()
                if wt_path.is_symlink():
                    log.warning("refusing rmtree: %s is a symlink", wt_path)
                    return
                # Verify the STRUCTURE of the actual path being removed — a
                # managed gate worktree always looks like
                # ``.vnx-data/worktrees/gate-*`` — rather than reconstructing
                # an expected root from project_root (which may itself fall
                # back to a __file__-anchored resolution). All three checks
                # are plain string comparisons on `resolved`, a function
                # parameter, never a Path(...) join.
                grandparent_name = resolved.parent.parent.name
                parent_name = resolved.parent.name
                if not (
                    resolved.name.startswith(_MANAGED_WORKTREE_PREFIX)
                    and parent_name == "worktrees"
                    and grandparent_name == ".vnx-data"
                ):
                    log.warning(
                        "refusing rmtree: %s does not match the managed gate-worktree "
                        "structure ('.vnx-data/worktrees/%s*'); got parent=%r grandparent=%r",
                        resolved, _MANAGED_WORKTREE_PREFIX, parent_name, grandparent_name,
                    )
                    return
                shutil.rmtree(str(resolved), ignore_errors=True)

            try:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(root), check=True, capture_output=True, text=True, timeout=30,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                log.warning("git worktree prune failed: %s", detail)
    except Exception as exc:  # pragma: no cover - cleanup must never raise
        log.warning("remove_gate_worktree: unexpected error removing %s: %s", wt_path, exc)
