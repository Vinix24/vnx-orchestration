"""test_tmux_worktree.py — Tests for per-dispatch ephemeral git worktree isolation.

Covers: allocate, classify, reap, _flock_context, _FETCH_CACHE logic.
Uses real git repos in tempdir fixtures, mirroring test_pool_worktree_manager.py.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import tmux_worktree
from tmux_worktree import (
    ReapResult,
    WorktreeAllocateError,
    WorktreeHandle,
    _flock_context,
    allocate,
    classify,
    reap,
)


# ---------------------------------------------------------------------------
# Real-git-repo fixture (mirrors test_pool_worktree_manager.py pattern)
# ---------------------------------------------------------------------------

def _init_git_repo_with_origin(tmp_path: Path) -> Path:
    """Create a bare origin + local clone with an initial commit.

    Returns the local clone path (the project root).
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(bare), str(local)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "checkout", "-b", "main"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@test.local"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )

    readme = local / "README.md"
    readme.write_text("init\n")
    subprocess.run(
        ["git", "-C", str(local), "add", "README.md"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )
    return local


# ---------------------------------------------------------------------------
# allocate tests
# ---------------------------------------------------------------------------

def test_allocate_creates_worktree_and_branch(tmp_path):
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("disp-abc-1", repo_root=local)

    assert handle.path.is_dir()
    assert (handle.path / "README.md").is_file()
    assert handle.branch == "dispatch/disp-abc-1"
    assert handle.dispatch_id == "disp-abc-1"
    assert handle.base_sha

    branches = subprocess.check_output(
        ["git", "-C", str(local), "branch", "--list", "dispatch/disp-abc-1"],
        text=True,
    ).strip()
    assert "dispatch/disp-abc-1" in branches


def test_allocate_attaches_existing_branch_on_collision(tmp_path):
    """If the branch already exists, allocate falls back to attach-without-b."""
    local = _init_git_repo_with_origin(tmp_path)
    subprocess.run(
        ["git", "-C", str(local), "branch", "dispatch/collide-1", "origin/main"],
        check=True, capture_output=True,
    )
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("collide-1", repo_root=local)

    assert handle.path.is_dir()
    assert (handle.path / "README.md").is_file()
    assert handle.branch == "dispatch/collide-1"


@pytest.mark.parametrize("bad_id", [
    "",
    "has space",
    "has/slash",
    "semi;colon",
    "pipe|char",
    "dollar$sign",
    "back`tick",
    "at@sign",
    "a" * 65,
    "../traversal",
    "-starts-with-hyphen",
])
def test_allocate_validates_dispatch_id(bad_id):
    """allocate() rejects dispatch_ids with shell metacharacters, slashes, or whitespace."""
    with pytest.raises(ValueError, match="invalid dispatch_id"):
        allocate(bad_id, repo_root=Path("/tmp"))


def test_allocate_fetches_with_cache(tmp_path):
    """Within TTL a second allocate for the same base_ref skips fetch; past TTL re-fetches."""
    local = _init_git_repo_with_origin(tmp_path)
    fetch_calls: list[list[str]] = []

    original_run = tmux_worktree._run

    def tracking_run(args, **kwargs):
        if "fetch" in args:
            fetch_calls.append(list(args))
        return original_run(args, **kwargs)

    with patch.object(tmux_worktree, "_run", tracking_run):
        with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
            allocate("cache-t1", base_ref="origin/main", repo_root=local)
            count_after_first = len(fetch_calls)

            # Second call within TTL: cache hit, no extra fetch.
            allocate("cache-t2", base_ref="origin/main", repo_root=local)
            count_after_second = len(fetch_calls)

            assert count_after_first == 1, f"expected 1 fetch, got {count_after_first}"
            assert count_after_second == 1, f"cache must suppress second fetch, got {count_after_second}"

            # Expire the cache entry manually.
            tmux_worktree._FETCH_CACHE["origin/main"] = 0.0

            allocate("cache-t3", base_ref="origin/main", repo_root=local)
            count_after_third = len(fetch_calls)

            assert count_after_third == 2, f"expired cache must trigger re-fetch, got {count_after_third}"


# ---------------------------------------------------------------------------
# classify tests
# ---------------------------------------------------------------------------

def test_classify_clean(tmp_path):
    """Worktree with no changes classifies as clean."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("cls-clean-1", repo_root=local)
    assert classify(handle) == "clean"


def test_classify_dirty(tmp_path):
    """Worktree with untracked/modified files classifies as dirty."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("cls-dirty-1", repo_root=local)
    (handle.path / "newfile.txt").write_text("untracked\n")
    assert classify(handle) == "dirty"


def test_classify_committed_local(tmp_path):
    """Worktree with local commit but no push classifies as committed."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("cls-committed-1", repo_root=local)

    (handle.path / "work.txt").write_text("work\n")
    subprocess.run(
        ["git", "-C", str(handle.path), "add", "work.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(handle.path), "commit", "-m", "worker commit"],
        check=True, capture_output=True,
    )

    assert classify(handle) == "committed"


def test_classify_pushed(tmp_path):
    """Worktree with commit pushed to origin classifies as pushed."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("cls-pushed-1", repo_root=local)

    (handle.path / "pushed.txt").write_text("pushed\n")
    subprocess.run(
        ["git", "-C", str(handle.path), "add", "pushed.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(handle.path), "commit", "-m", "worker push"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "-u", "origin", handle.branch],
        check=True, capture_output=True,
    )

    assert classify(handle) == "pushed"


# ---------------------------------------------------------------------------
# reap tests
# ---------------------------------------------------------------------------

def test_reap_clean_removes_worktree_and_branch(tmp_path):
    """reap clean: worktree directory gone, local branch deleted."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("reap-clean-1", repo_root=local)

    assert handle.path.is_dir()
    result = reap(handle, "clean")

    assert result.removed
    assert not result.branch_kept_local
    assert not result.branch_kept_remote
    assert not handle.path.exists()
    branches = subprocess.check_output(
        ["git", "-C", str(local), "branch", "--list", handle.branch],
        text=True,
    ).strip()
    assert branches == ""


def test_reap_committed_keeps_local_branch(tmp_path):
    """reap committed: disk removed, local branch preserved."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("reap-committed-1", repo_root=local)

    (handle.path / "w.txt").write_text("work\n")
    subprocess.run(
        ["git", "-C", str(handle.path), "add", "w.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(handle.path), "commit", "-m", "uncommitted work"],
        check=True, capture_output=True,
    )

    result = reap(handle, "committed")

    assert result.removed
    assert result.branch_kept_local
    assert not handle.path.exists()
    branches = subprocess.check_output(
        ["git", "-C", str(local), "branch", "--list", handle.branch],
        text=True,
    ).strip()
    assert handle.branch in branches


def test_reap_pushed_keeps_remote_branch(tmp_path):
    """reap pushed: disk removed, local branch deleted, remote branch intact."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("reap-pushed-1", repo_root=local)

    (handle.path / "p.txt").write_text("pushed\n")
    subprocess.run(
        ["git", "-C", str(handle.path), "add", "p.txt"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(handle.path), "commit", "-m", "pushed work"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(handle.path), "push", "-u", "origin", handle.branch],
        check=True, capture_output=True,
    )

    result = reap(handle, "pushed")

    assert result.removed
    assert result.branch_kept_remote
    assert not handle.path.exists()
    # Local branch gone
    local_branches = subprocess.check_output(
        ["git", "-C", str(local), "branch", "--list", handle.branch],
        text=True,
    ).strip()
    assert local_branches == ""
    # Remote ref still present
    remote_refs = subprocess.check_output(
        ["git", "-C", str(local), "ls-remote", "origin", handle.branch],
        text=True,
    ).strip()
    assert handle.branch in remote_refs


def test_reap_dirty_preserves_and_locks(tmp_path):
    """reap dirty: worktree locked in place, path not removed, preserved_path set."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("reap-dirty-1", repo_root=local)

    (handle.path / "dirty.txt").write_text("uncommitted\n")

    result = reap(handle, "dirty")

    assert not result.removed
    assert result.preserved_path == handle.path
    assert handle.path.is_dir()


def test_reap_remove_failure_force_fallback(tmp_path):
    """When both git worktree remove attempts fail, fallback to rmtree + prune."""
    local = _init_git_repo_with_origin(tmp_path)
    with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
        handle = allocate("reap-fallback-1", repo_root=local)

    remove_calls: list[list[str]] = []
    original_run = tmux_worktree._run

    def intercept_remove(args, **kwargs):
        if "worktree" in args and "remove" in args:
            remove_calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, "", "simulated lock")
        return original_run(args, **kwargs)

    with patch.object(tmux_worktree, "_run", intercept_remove):
        result = reap(handle, "clean")

    assert len(remove_calls) == 2, f"expected 2 remove attempts, got {remove_calls}"
    assert not handle.path.exists(), "rmtree fallback must have removed the directory"


# ---------------------------------------------------------------------------
# flock concurrency test
# ---------------------------------------------------------------------------

def test_flock_serializes_concurrent_allocate(tmp_path):
    """Two concurrent allocate() calls must not enter the flock section simultaneously."""
    local = _init_git_repo_with_origin(tmp_path)

    tracker_lock = threading.Lock()
    inside_count = [0]
    concurrent_max = [0]
    errors: list[Exception] = []

    original_flock_context = tmux_worktree._flock_context

    @contextmanager
    def counting_flock(root):
        with original_flock_context(root):
            with tracker_lock:
                inside_count[0] += 1
                concurrent_max[0] = max(concurrent_max[0], inside_count[0])
            try:
                yield
            finally:
                with tracker_lock:
                    inside_count[0] -= 1

    def do_allocate(dispatch_id: str) -> None:
        try:
            with patch.dict(tmux_worktree._FETCH_CACHE, {}, clear=True):
                with patch.object(tmux_worktree, "_flock_context", counting_flock):
                    allocate(dispatch_id, repo_root=local)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=do_allocate, args=("flock-t1",))
    t2 = threading.Thread(target=do_allocate, args=("flock-t2",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, f"unexpected errors: {errors}"
    assert concurrent_max[0] <= 1, (
        f"flock violation: {concurrent_max[0]} concurrent holders observed"
    )
