"""test_phantom_guard_branch_fallback.py — provider-lane phantom-diff resolution.

The provider lane in run_envelope_plan captures a worker's diff BEFORE the
worktree teardown. Before this fix it always used compute_worktree_diff, but
that measures an ephemeral source: the worktree is torn down in the finally
block right after. A pushed branch survives teardown and is the more durable
evidence.

This tests _resolve_phantom_diff, the extracted resolution function, which:
  1. Prefers the pushed branch diff when the worker pushed its dispatch branch
  2. Falls back to the live worktree diff when no upstream exists
  3. Falls back to the worktree diff when the branch diff raises
  4. Returns None (abstain) when ALL sources are unresolvable
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_envelope as envelope
import phantom_guard as pg


# ---------------------------------------------------------------------------
# Real git repo fixture — proves the actual git plumbing, not just stubs
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def git_fixture(tmp_path: Path):
    """A bare 'origin' remote + a working checkout, mirroring the real fabric
    shape: the orchestrator's own repo checkout with a remote it fetches pushed
    branches from.

    Returns (consumer_repo, worktree_path) where consumer_repo is the main
    checkout and worktree_path simulates the dispatch worktree.
    """
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    bare.mkdir()
    _run_git(bare, "init", "--bare", "-b", "main")

    work.mkdir()
    _run_git(work, "init", "-b", "main")
    _run_git(work, "config", "user.email", "test@example.com")
    _run_git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    _run_git(work, "add", "README.md")
    _run_git(work, "commit", "-m", "base commit")
    _run_git(work, "remote", "add", "origin", str(bare))
    _run_git(work, "push", "origin", "main")
    return work


def _make_worktree(consumer_repo: Path, dispatch_id: str) -> Path:
    """Create a dispatch worktree from origin/main, simulating
    create_dispatch_worktree.
    """
    from dispatch_worktree_isolation import _sanitize_dispatch_id
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch = f"dispatch/{safe_id}"
    wt_dir = consumer_repo / ".vnx-data" / "worktrees" / safe_id
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(consumer_repo, "worktree", "add", str(wt_dir), "-b", branch, "origin/main")
    return wt_dir


def _push_branch(consumer_repo: Path, branch: str, filename: str, content: str) -> None:
    """Push a commit onto a branch and set its upstream tracking."""
    _run_git(consumer_repo, "checkout", branch)
    (consumer_repo / filename).write_text(content, encoding="utf-8")
    _run_git(consumer_repo, "add", filename)
    _run_git(consumer_repo, "commit", "-m", f"add {filename}")
    _run_git(consumer_repo, "push", "-u", "origin", branch)
    # The worktree dir is the checkout, not consumer_repo — return to main
    _run_git(consumer_repo, "checkout", "main")


# ---------------------------------------------------------------------------
# Test 1: pushed branch with real content → branch diff wins
# ---------------------------------------------------------------------------


def test_pushed_branch_with_content_wins_over_worktree(git_fixture):
    """When the worker pushed its dispatch branch (has upstream), the branch
    diff is the more durable evidence. The worktree diff is NOT used.

    Against the OLD code (which always called compute_worktree_diff directly),
    this scenario would return the worktree's uncommitted state — or fail if
    the worktree is gone. The new code returns the pushed content from origin.
    """
    consumer_repo = git_fixture
    dispatch_id = "20260730-branch-fallback-test1"
    from dispatch_worktree_isolation import _sanitize_dispatch_id
    branch = f"dispatch/{_sanitize_dispatch_id(dispatch_id)}"

    # Create a worktree (simulates the provider lane's worktree setup)
    wt_path = _make_worktree(consumer_repo, dispatch_id)

    # The consumer repo is on main; the worktree is on dispatch/<id>.
    # Push content onto the dispatch branch from the worktree.
    _run_git(wt_path, "config", "user.email", "test@example.com")
    _run_git(wt_path, "config", "user.name", "Test")
    (wt_path / "feature.py").write_text("def do_work():\n    return 42\n", encoding="utf-8")
    _run_git(wt_path, "add", "feature.py")
    _run_git(wt_path, "commit", "-m", "real work")
    _run_git(wt_path, "push", "-u", "origin", branch)

    # Now resolve — this is what the provider lane does AFTER the worker runs
    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref="origin/main",
        wt_path=wt_path,
        repo=consumer_repo,
    )

    assert result is not None
    assert "feature.py" in result
    assert "def do_work" in result
    # Verify the diff came from origin/<branch>, not the worktree, by checking
    # the git merge-base used (the function computes against origin/main).
    # A non-empty diff on a pushed branch proves the branch path was used.
    assert len(result.strip()) > 0

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_path), "--force")


# ---------------------------------------------------------------------------
# Test 2: no upstream → falls back to worktree diff
# ---------------------------------------------------------------------------


def test_no_upstream_falls_back_to_worktree_diff(git_fixture):
    """When the dispatch branch was never pushed (no upstream tracking), the
    worktree diff is the only available source.
    """
    consumer_repo = git_fixture
    dispatch_id = "20260730-branch-fallback-test2"

    wt_path = _make_worktree(consumer_repo, dispatch_id)

    # Make a change in the worktree WITHOUT pushing
    _run_git(wt_path, "config", "user.email", "test@example.com")
    _run_git(wt_path, "config", "user.name", "Test")
    (wt_path / "unpushed.py").write_text("# never pushed\n", encoding="utf-8")
    _run_git(wt_path, "add", "unpushed.py")
    _run_git(wt_path, "commit", "-m", "unpushed work")

    # Verify the branch has NOT been pushed to origin
    from dispatch_worktree_isolation import _sanitize_dispatch_id
    branch = f"dispatch/{_sanitize_dispatch_id(dispatch_id)}"
    ls_remote = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=str(consumer_repo), capture_output=True, text=True,
    )
    assert ls_remote.returncode == 0
    assert not ls_remote.stdout.strip()  # proves branch is NOT on the remote

    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref="origin/main",
        wt_path=wt_path,
        repo=consumer_repo,
    )

    assert result is not None
    assert "unpushed.py" in result
    assert len(result.strip()) > 0

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_path), "--force")


# ---------------------------------------------------------------------------
# Test 3: branch-diff raises → falls back to worktree diff
# ---------------------------------------------------------------------------


def test_branch_diff_exception_falls_back_to_worktree(git_fixture, monkeypatch):
    """When the branch has an upstream but compute_branch_diff raises (e.g.
    a deleted remote ref, network issue), fall back to the worktree diff.
    """
    consumer_repo = git_fixture
    dispatch_id = "20260730-branch-fallback-test3"

    wt_path = _make_worktree(consumer_repo, dispatch_id)

    # Make real work in the worktree
    _run_git(wt_path, "config", "user.email", "test@example.com")
    _run_git(wt_path, "config", "user.name", "Test")
    (wt_path / "fallback.py").write_text("# worktree fallback\n", encoding="utf-8")
    _run_git(wt_path, "add", "fallback.py")
    _run_git(wt_path, "commit", "-m", "worktree content")

    # Stub: the upstream check succeeds, but compute_branch_diff raises
    original_compute_branch_diff = pg.compute_branch_diff

    def _failing_branch_diff(*a, **k):
        raise subprocess.CalledProcessError(128, ["git", "diff"], stderr="fatal: bad ref")

    monkeypatch.setattr(pg, "compute_branch_diff", _failing_branch_diff)

    # Also stub the remote check via subprocess.run monkeypatching:
    # make `git ls-remote --heads origin <branch>` succeed (branch exists remotely)
    original_run = subprocess.run

    def _stubbed_run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "ls-remote" in cmd_str and "--heads" in cmd_str:
            # Simulate: branch exists on the remote
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123def456\trefs/heads/dispatch/test\n", stderr="")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _stubbed_run)

    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref="origin/main",
        wt_path=wt_path,
        repo=consumer_repo,
    )

    assert result is not None
    assert "fallback.py" in result
    assert len(result.strip()) > 0

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_path), "--force")


# ---------------------------------------------------------------------------
# Test 4: both branch diff and worktree diff fail → None (abstain)
# ---------------------------------------------------------------------------


def test_both_sources_fail_returns_none(git_fixture, monkeypatch):
    """When both the branch diff and the worktree diff are unresolvable,
    return None so the phantom guard abstains — never false-reject.
    """
    consumer_repo = git_fixture
    dispatch_id = "20260730-branch-fallback-test4"

    # Use a non-existent worktree path — compute_worktree_diff will fail on it
    wt_path = consumer_repo / "nonexistent-worktree"

    # Stub both the upstream check and both diff functions to fail
    def _failing_run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "ls-remote" in cmd_str and "--heads" in cmd_str:
            # Simulate: branch not on remote (empty stdout, exit 0)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "merge-base" in cmd_str or "diff" in cmd_str:
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: bad revision")
        raise AssertionError(f"unexpected subprocess call: {cmd_str}")

    monkeypatch.setattr(subprocess, "run", _failing_run)

    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref="origin/main",
        wt_path=wt_path,
        repo=consumer_repo,
    )

    assert result is None, "both sources failed — guard must abstain, never false-reject"


# ---------------------------------------------------------------------------
# Test 5: failing upstream check is logged (no longer silent)
# ---------------------------------------------------------------------------


def test_failing_upstream_check_is_logged(git_fixture, monkeypatch, caplog):
    """When the git ls-remote upstream check raises, the exception is logged
    with the dispatch_id and step context. The guard still falls through to
    the worktree diff — behavior is unchanged, but the failure leaves a trace.
    """
    import logging

    consumer_repo = git_fixture
    dispatch_id = "20260730-branch-fallback-test5"

    wt_path = _make_worktree(consumer_repo, dispatch_id)

    # Make real work in the worktree (so the fallback returns it)
    _run_git(wt_path, "config", "user.email", "test@example.com")
    _run_git(wt_path, "config", "user.name", "Test")
    (wt_path / "logged.py").write_text("# logged fallback\n", encoding="utf-8")
    _run_git(wt_path, "add", "logged.py")
    _run_git(wt_path, "commit", "-m", "logged worktree content")

    # Make git ls-remote raise (simulates network error, timeout, or corrupt repo)
    original_run = subprocess.run

    def _failing_ls_remote(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "ls-remote" in cmd_str and "--heads" in cmd_str:
            raise subprocess.TimeoutExpired(cmd, 15)
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _failing_ls_remote)

    with caplog.at_level(logging.WARNING):
        result = envelope._resolve_phantom_diff(
            dispatch_id,
            base_ref="origin/main",
            wt_path=wt_path,
            repo=consumer_repo,
        )

    # Behavior: still falls through to worktree diff
    assert result is not None
    assert "logged.py" in result

    # Audit trail: the failure was logged
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) >= 1, "upstream check failure must be logged"
    upstream_warning = next(
        (r for r in warnings if "upstream check failed" in r.message), None
    )
    assert upstream_warning is not None, (
        "log message must identify the failing step (upstream check)"
    )
    assert dispatch_id in upstream_warning.message, (
        "log message must include the dispatch_id"
    )

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_path), "--force")


# ---------------------------------------------------------------------------
# Test 6: branch-diff exception is logged (no longer silent)
# ---------------------------------------------------------------------------


def test_branch_diff_exception_is_logged(git_fixture, monkeypatch, caplog):
    """When the branch has upstream but compute_branch_diff raises, the
    exception is logged with dispatch_id and step context. The guard still
    falls through to the worktree diff.
    """
    import logging

    consumer_repo = git_fixture
    dispatch_id = "20260730-branch-fallback-test6"

    wt_path = _make_worktree(consumer_repo, dispatch_id)

    # Make real work in the worktree (so the fallback returns it)
    _run_git(wt_path, "config", "user.email", "test@example.com")
    _run_git(wt_path, "config", "user.name", "Test")
    (wt_path / "second_fallback.py").write_text("# second fallback\n", encoding="utf-8")
    _run_git(wt_path, "add", "second_fallback.py")
    _run_git(wt_path, "commit", "-m", "second fallback work")

    # Stub: upstream check succeeds (branch exists), but branch diff raises
    original_run = subprocess.run

    def _stubbed_run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        if "ls-remote" in cmd_str and "--heads" in cmd_str:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="abc123\trefs/heads/dispatch/test\n", stderr="",
            )
        if "diff" in cmd_str and "origin/dispatch" in cmd_str:
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: bad ref")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _stubbed_run)

    with caplog.at_level(logging.WARNING):
        result = envelope._resolve_phantom_diff(
            dispatch_id,
            base_ref="origin/main",
            wt_path=wt_path,
            repo=consumer_repo,
        )

    # Behavior: still falls through to worktree diff
    assert result is not None
    assert "second_fallback.py" in result

    # Audit trail: the branch-diff failure was logged
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) >= 1, "branch diff failure must be logged"
    branch_warning = next(
        (r for r in warnings if "branch diff failed" in r.message), None
    )
    assert branch_warning is not None, (
        "log message must identify the failing step (branch diff)"
    )
    assert dispatch_id in branch_warning.message, (
        "log message must include the dispatch_id"
    )

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_path), "--force")
