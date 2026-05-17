"""pool_worktree_manager.py — Per-worker git worktree create/reap.

Wave 6 PR-6.5b — Each pool worker gets an isolated git worktree so
concurrent subprocess workers operate on independent file trees.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _resolve_project_root() -> Path:
    try:
        from project_root import resolve_project_root  # type: ignore[attr-defined]
        return Path(resolve_project_root(__file__))
    except Exception:
        return Path.cwd()


def _worktree_dir(project_root: Path, terminal_id: str) -> Path:
    return project_root / ".vnx-data" / "worktrees" / f"pool-{terminal_id}"


def create_worker_worktree(
    terminal_id: str,
    base_branch: str = "main",
    *,
    project_root: Optional[Path] = None,
) -> Path:
    """Create an isolated git worktree for a pool worker.

    Idempotent: returns existing worktree path if already present.
    """
    root = (project_root or _resolve_project_root()).resolve()
    wt_path = _worktree_dir(root, terminal_id)

    if wt_path.is_dir():
        log.info("worktree already exists: %s", wt_path)
        return wt_path

    wt_path.parent.mkdir(parents=True, exist_ok=True)

    branch_name = f"pool/{terminal_id}"

    try:
        subprocess.run(
            [
                "git", "worktree", "add",
                str(wt_path),
                "-b", branch_name,
                f"origin/{base_branch}",
            ],
            cwd=str(root),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if "already exists" in (exc.stderr or ""):
            try:
                subprocess.run(
                    [
                        "git", "worktree", "add",
                        str(wt_path),
                        branch_name,
                    ],
                    cwd=str(root),
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc2:
                raise RuntimeError(
                    f"git worktree add failed for {terminal_id}: {exc2.stderr}"
                ) from exc2
        else:
            raise RuntimeError(
                f"git worktree add failed for {terminal_id}: {exc.stderr}"
            ) from exc

    log.info("worktree created: %s (branch %s)", wt_path, branch_name)
    return wt_path.resolve()


def reap_worker_worktree(
    terminal_id: str,
    *,
    project_root: Optional[Path] = None,
) -> None:
    """Remove a pool worker's git worktree. Idempotent."""
    root = (project_root or _resolve_project_root()).resolve()
    wt_path = _worktree_dir(root, terminal_id)

    if not wt_path.exists():
        log.info("worktree already absent: %s", wt_path)
        return

    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=str(root),
            check=True,
            capture_output=True,
            text=True,
        )
        log.info("worktree removed: %s", wt_path)
    except subprocess.CalledProcessError as exc:
        log.warning(
            "git worktree remove failed: %s; cleaning up directory",
            (exc.stderr or "").strip(),
        )
        shutil.rmtree(str(wt_path), ignore_errors=True)
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(root),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            log.warning("worktree prune failed: %s", exc)

    _delete_worktree_branch(terminal_id, root)


def _delete_worktree_branch(terminal_id: str, project_root: Path) -> None:
    """Best-effort delete the pool/{terminal_id} branch after worktree removal."""
    branch_name = f"pool/{terminal_id}"
    try:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(project_root),
            check=True,
            capture_output=True,
            text=True,
        )
        log.info("branch deleted: %s", branch_name)
    except subprocess.CalledProcessError as exc:
        log.warning("branch deletion failed for %s: %s", branch_name, exc)
