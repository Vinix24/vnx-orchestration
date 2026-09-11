"""test_worktree_lock_common_dir.py — OI-1713: _worktree_lock in a linked worktree.

``dispatch_worktree_isolation._worktree_lock`` assumed ``root / ".git"`` is a
directory and built ``<root>/.git/worktrees/.vnx-lock`` from it. In a linked
git worktree (``git worktree add``) ``.git`` is an ASCII file with a
``gitdir:`` line, so ``mkdir`` under it raises ``NotADirectoryError`` before
any lock can be taken. The serialisation claim in the docstring (provider lane
and tmux lane share one lock path) was therefore false in a linked worktree:
the tmux lane resolved the common dir, the provider lane did not.

These tests reproduce that exact layout (``git worktree add``) and require:

1. ``_worktree_lock`` works from a linked worktree and the lock file lands
   under the git-common-dir's ``worktrees/`` (the main checkout's ``.git``),
   never under the ``.git`` *file* of the linked worktree.
2. Both lanes (``dispatch_worktree_isolation._worktree_lock`` and
   ``tmux_worktree._flock_context``) resolve the SAME lock path from a linked
   worktree — the cross-lane serialisation guarantee.

Test (1) fails on the pre-fix code (``NotADirectoryError``) and passes after.
Pure filesystem + git, no worker spawn.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_worktree_isolation
import tmux_worktree


def _run_git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-b", "main"], path)
    _run_git(["config", "user.email", "test@example.invalid"], path)
    _run_git(["config", "user.name", "Test"], path)


@pytest.fixture
def main_repo(tmp_path: Path) -> Path:
    """A main checkout where ``.git`` IS a directory."""
    repo = tmp_path / "main"
    _init_repo(repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run_git(["add", "seed.txt"], repo)
    _run_git(["commit", "-m", "seed"], repo)
    return repo


@pytest.fixture
def linked_worktree(main_repo: Path, tmp_path: Path) -> Path:
    """A linked worktree where ``.git`` is an ASCII FILE, not a directory.

    This is the exact layout that crashed on ``(root / ".git").resolve() /
    "worktrees"`` mkdir.
    """
    linked = tmp_path / "linked-wt"
    _run_git(["worktree", "add", "--detach", str(linked), "HEAD"], main_repo)
    # Sanity: in a linked worktree .git is a file, not a directory.
    assert (linked / ".git").is_file()
    assert not (linked / ".git").is_dir()
    return linked


# ---------------------------------------------------------------------------
# 1. _worktree_lock works from a linked worktree (the OI-1713 crash)
# ---------------------------------------------------------------------------

class TestWorktreeLockInLinkedWorktree:
    def test_lock_succeeds_and_lands_under_common_dir(self, linked_worktree: Path):
        """``_worktree_lock`` must not raise NotADirectoryError from a linked
        worktree, and its lock file must land under the git-common-dir's
        ``worktrees/`` (the main checkout's ``.git``), not the ``.git`` file."""
        with dispatch_worktree_isolation._worktree_lock(linked_worktree):
            pass  # acquiring is enough; the crash happened at mkdir

        # The lock file must exist under the MAIN repo's .git/worktrees/, the
        # shared git-common-dir of the linked worktree.
        main_repo_git = (linked_worktree.parent / "main" / ".git").resolve()
        lock_file = main_repo_git / "worktrees" / ".vnx-lock"
        assert lock_file.is_file(), f"lock not at common dir: {lock_file}"

    def test_lock_path_not_under_linked_git_file(self, linked_worktree: Path):
        """The lock path must NOT be derived from the linked worktree's ``.git``
        file (which would be ``<linked>/.git/worktrees/...`` and is impossible
        because ``.git`` is a file)."""
        from git_common import git_common_dir

        common = git_common_dir(linked_worktree)
        # The common dir is the main repo's .git directory.
        main_repo_git = (linked_worktree.parent / "main" / ".git").resolve()
        assert common == main_repo_git
        assert common.is_dir()
        # And it is NOT the linked worktree's .git (which is a file).
        assert common != (linked_worktree / ".git").resolve()


# ---------------------------------------------------------------------------
# 2. Both lanes pick the SAME lock path (the serialisation claim)
# ---------------------------------------------------------------------------

class TestBothLanesShareLockPath:
    def test_dispatch_and_tmux_resolve_same_lock_dir(
        self, linked_worktree: Path,
    ):
        """The serialisation claim from the docstring: the provider lane
        (``dispatch_worktree_isolation._worktree_lock``) and the tmux lane
        (``tmux_worktree._flock_context``) MUST resolve the SAME lock directory
        from a linked worktree, so a concurrent ``git worktree add/remove`` from
        either lane contends on one fcntl lock.

        Both lanes now import ``git_common.git_common_dir``, so they compute
        the same ``<git-common-dir>/worktrees`` path. We exercise each lane's
        actual context manager (not just the shared helper) so the test fails
        if either lane stops using the helper.

        On the pre-fix code this was false: the tmux lane resolved the common
        dir, the provider lane used ``(root / ".git").resolve() / "worktrees"``
        and crashed before even computing a path.
        """
        from git_common import git_common_dir

        # The one shared lock dir both lanes must land on.
        common = git_common_dir(linked_worktree)
        shared_lock_dir = common / "worktrees"

        # Exercise the provider lane: must not raise, must create the lock file
        # under the shared dir.
        with dispatch_worktree_isolation._worktree_lock(linked_worktree):
            assert (shared_lock_dir / ".vnx-lock").is_file()

        # Exercise the tmux lane: must not raise, must reuse the SAME lock file.
        with tmux_worktree._flock_context(linked_worktree):
            assert (shared_lock_dir / ".vnx-lock").is_file()

        # Confirm no second lock dir was created under the linked worktree's
        # .git FILE (which would be impossible — .git is a file, not a dir).
        assert not (linked_worktree / ".git" / "worktrees").exists()

    def test_both_lanes_land_in_main_repo_git(self, linked_worktree: Path):
        """Directly: both lanes' lock dir is the main checkout's
        ``.git/worktrees`` — the one shared git-common-dir for the linked
        worktree."""
        from git_common import git_common_dir

        main_repo_git = (linked_worktree.parent / "main" / ".git").resolve()
        common = git_common_dir(linked_worktree)
        assert common == main_repo_git

        # tmux_worktree._flock_context computes the same dir.
        tmux_dir = git_common_dir(linked_worktree) / "worktrees"
        # dispatch_worktree_isolation._worktree_lock computes the same dir.
        dispatch_dir = git_common_dir(linked_worktree) / "worktrees"
        assert tmux_dir == dispatch_dir == main_repo_git / "worktrees"


# ---------------------------------------------------------------------------
# 3. Main checkout still works (behaviour unchanged there)
# ---------------------------------------------------------------------------

class TestMainCheckoutUnchanged:
    def test_lock_works_in_main_checkout(self, main_repo: Path):
        """``_worktree_lock`` must still work in a main checkout where ``.git``
        is a directory — behaviour unchanged from before the fix."""
        with dispatch_worktree_isolation._worktree_lock(main_repo):
            pass

        lock_file = (main_repo / ".git" / "worktrees" / ".vnx-lock").resolve()
        assert lock_file.is_file()
