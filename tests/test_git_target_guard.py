"""test_git_target_guard.py — OI-975 guard against dispatch git ops on the main checkout.

A dispatch must operate exclusively inside its own ephemeral worktree. A git
call that resolves its target to the MAIN checkout (the operator's checkout)
instead of the dispatch worktree can move the operator's HEAD onto a
``dispatch/<id>`` / PR branch without the operator asking.

This tests git_target_guard directly and its wiring into the two git-checkout
primitives that move a checkout onto a branch:

  1. ``regression_attribution.attribute_regression`` — checks out arbitrary
     refs during bisection (scripts/lib/regression_attribution.py:224).
  2. ``chain_origin_anchor._commit_and_push_anchor`` — ``git checkout -B
     <branch>`` on the seal target (scripts/lib/chain_origin_anchor.py:813).

Real git repos in tempdir fixtures (mirrors test_tmux_worktree.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "scripts" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import git_target_guard
from git_target_guard import (
    DispatchTargetsMainCheckoutError,
    guard_git_cwd,
    guard_git_target,
    is_dispatch_context,
    resolves_to_main_checkout,
)


# ---------------------------------------------------------------------------
# Real-git-repo fixtures
# ---------------------------------------------------------------------------

def _init_git_repo(tmp_path: Path) -> Path:
    """Create a bare origin + local clone with an initial commit on main.

    Returns the local clone path (the project root / main checkout).
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(bare)],
        check=True, capture_output=True,
    )
    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(bare), str(local)], check=True, capture_output=True)
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
    subprocess.run(["git", "-C", str(local), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"], check=True, capture_output=True
    )
    return local


def _add_dispatch_worktree(local: Path, dispatch_id: str) -> Path:
    """Create a linked worktree under ``<local>/.vnx-data/worktrees/dispatch-<id>``.

    Mirrors tmux_worktree.allocate's layout so the worktree path shape matches
    production dispatch worktrees.
    """
    wt = local / ".vnx-data" / "worktrees" / f"dispatch-{dispatch_id}"
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(local), "worktree", "add", "-b", f"dispatch/{dispatch_id}", str(wt), "origin/main"],
        check=True, capture_output=True,
    )
    return wt


@pytest.fixture
def dispatch_env(monkeypatch):
    """Simulate a dispatch worker context: the env vars the lanes export.

    Explicitly re-enables the guard (conftest sets VNX_GIT_TARGET_GUARD=0 for
    the suite) so these tests exercise the real refusal.
    """
    monkeypatch.setenv("VNX_CURRENT_DISPATCH_ID", "20260804-test-checkout-vastzetten")
    monkeypatch.setenv("VNX_DISPATCH_ID", "20260804-test-checkout-vastzetten")
    monkeypatch.setenv("VNX_GIT_TARGET_GUARD", "1")
    return "20260804-test-checkout-vastzetten"


@pytest.fixture
def clean_dispatch_env(monkeypatch):
    """Ensure no dispatch-context env vars leak into a test."""
    monkeypatch.delenv("VNX_CURRENT_DISPATCH_ID", raising=False)
    monkeypatch.delenv("VNX_DISPATCH_ID", raising=False)
    monkeypatch.delenv("VNX_GIT_TARGET_GUARD", raising=False)


# ---------------------------------------------------------------------------
# is_dispatch_context / resolves_to_main_checkout
# ---------------------------------------------------------------------------

def test_is_dispatch_context_true_when_dispatch_env_set(dispatch_env):
    assert is_dispatch_context() is True


def test_is_dispatch_context_false_when_unset(clean_dispatch_env):
    assert is_dispatch_context() is False


def test_resolves_to_main_checkout_true_for_main_checkout(tmp_path):
    local = _init_git_repo(tmp_path)
    assert resolves_to_main_checkout(local) is True


def test_resolves_to_main_checkout_true_for_subdir_of_main_checkout(tmp_path):
    local = _init_git_repo(tmp_path)
    subdir = local / "scripts"
    subdir.mkdir(exist_ok=True)
    assert resolves_to_main_checkout(subdir) is True


def test_resolves_to_main_checkout_false_for_dispatch_worktree(tmp_path):
    local = _init_git_repo(tmp_path)
    wt = _add_dispatch_worktree(local, "20260804-test-checkout-vastzetten")
    assert resolves_to_main_checkout(wt) is False


def test_resolves_to_main_checkout_false_for_non_git_dir(tmp_path):
    non_git = tmp_path / "not-a-repo"
    non_git.mkdir()
    assert resolves_to_main_checkout(non_git) is False


# ---------------------------------------------------------------------------
# guard_git_target
# ---------------------------------------------------------------------------

def test_guard_git_target_refuses_main_checkout_in_dispatch_context(tmp_path, dispatch_env):
    local = _init_git_repo(tmp_path)
    with pytest.raises(DispatchTargetsMainCheckoutError):
        guard_git_target(local)


def test_guard_git_target_refuses_subdir_of_main_checkout_in_dispatch_context(tmp_path, dispatch_env):
    local = _init_git_repo(tmp_path)
    subdir = local / "scripts"
    subdir.mkdir(exist_ok=True)
    with pytest.raises(DispatchTargetsMainCheckoutError):
        guard_git_target(subdir)


def test_guard_git_target_passes_dispatch_worktree_in_dispatch_context(tmp_path, dispatch_env):
    local = _init_git_repo(tmp_path)
    wt = _add_dispatch_worktree(local, "20260804-test-checkout-vastzetten")
    assert guard_git_target(wt) == wt.resolve()


def test_guard_git_target_passes_main_checkout_outside_dispatch_context(tmp_path, clean_dispatch_env):
    local = _init_git_repo(tmp_path)
    # Operator mode: the main checkout is a legitimate git target.
    assert guard_git_target(local) == local.resolve()


def test_guard_git_target_accepts_explicit_dispatch_id(tmp_path, monkeypatch):
    local = _init_git_repo(tmp_path)
    # Explicit dispatch_id + guard re-enabled (no ambient dispatch env needed).
    monkeypatch.setenv("VNX_GIT_TARGET_GUARD", "1")
    with pytest.raises(DispatchTargetsMainCheckoutError):
        guard_git_target(local, dispatch_id="20260804-test-checkout-vastzetten")


# ---------------------------------------------------------------------------
# guard_git_cwd
# ---------------------------------------------------------------------------

def test_guard_git_cwd_no_cwd_in_dispatch_context_raises(dispatch_env):
    with pytest.raises(DispatchTargetsMainCheckoutError):
        guard_git_cwd(None)


def test_guard_git_cwd_explicit_worktree_in_dispatch_context_ok(tmp_path, dispatch_env):
    local = _init_git_repo(tmp_path)
    wt = _add_dispatch_worktree(local, "20260804-test-checkout-vastzetten")
    assert guard_git_cwd(wt) == wt.resolve()


def test_guard_git_cwd_no_cwd_outside_dispatch_context_ok(tmp_path, clean_dispatch_env, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert guard_git_cwd(None) == tmp_path.resolve()


# ---------------------------------------------------------------------------
# Wiring: regression_attribution.attribute_regression
# ---------------------------------------------------------------------------

def test_attribute_regression_refuses_main_checkout_in_dispatch_context(tmp_path, dispatch_env):
    """The branch-switching primitive must refuse the main checkout in a dispatch context."""
    import regression_attribution

    local = _init_git_repo(tmp_path)
    with pytest.raises(DispatchTargetsMainCheckoutError):
        regression_attribution.attribute_regression(
            check_cmd="true",
            good_ref="HEAD~0",
            bad_ref="HEAD~0",
            repo_root=local,
        )


def test_attribute_regression_runs_outside_dispatch_context(tmp_path, clean_dispatch_env):
    """Same call outside a dispatch context still works (operator mode)."""
    import regression_attribution

    local = _init_git_repo(tmp_path)
    result = regression_attribution.attribute_regression(
        check_cmd="true",  # passes at bad_ref => inconclusive, no bisect
        good_ref="HEAD",
        bad_ref="HEAD",
        repo_root=local,
    )
    assert result.status == "inconclusive"
    # The main checkout must still be on main after the call.
    head = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "main"


# ---------------------------------------------------------------------------
# Wiring: chain_origin_anchor._commit_and_push_anchor
# ---------------------------------------------------------------------------

def test_chain_origin_commit_refuses_main_checkout_in_dispatch_context(tmp_path, dispatch_env):
    """``git checkout -B`` inside the seal path must refuse the main checkout in a dispatch context."""
    from chain_origin_anchor import _commit_and_push_anchor

    local = _init_git_repo(tmp_path)
    with pytest.raises(DispatchTargetsMainCheckoutError):
        _commit_and_push_anchor(
            local,
            "main",
            [],
            identity="test-identity",
            epoch=1,
        )
    # Nothing must have moved: the guard fired before any checkout.
    head = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "main"


def test_chain_origin_commit_targets_worktree_in_dispatch_context(tmp_path, dispatch_env):
    """The same seal call against a dispatch worktree is allowed and stays on the worktree."""
    from chain_origin_anchor import _commit_and_push_anchor

    local = _init_git_repo(tmp_path)
    wt = _add_dispatch_worktree(local, "20260804-test-checkout-vastzetten")

    # Patch out the network steps (push + PR) — we only assert the guard lets
    # the call reach the checkout against the worktree.
    import chain_origin_anchor

    pushed = {"seen": False}

    def _fake_push(*args, **kwargs):
        pushed["seen"] = True
        return subprocess.CompletedProcess(args=(), returncode=0, stdout="", stderr="")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(chain_origin_anchor, "_git_run_checked", _fake_push)
    try:
        # With a worktree target the guard passes and the checkout lands on the
        # worktree, never on the main checkout.
        _commit_and_push_anchor(
            wt,
            "main",
            [],
            identity="test-identity",
            epoch=1,
        )
    finally:
        monkeypatch.undo()

    assert pushed["seen"] is True
    main_head = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert main_head == "main"


# ---------------------------------------------------------------------------
# Green-after proof: the guarded git operation leaves the main checkout alone
# ---------------------------------------------------------------------------

def test_guarded_worktree_git_operation_leaves_main_checkout_untouched(tmp_path, dispatch_env):
    """A dispatch-context git op on the worktree must not move the main checkout's branch."""
    local = _init_git_repo(tmp_path)
    wt = _add_dispatch_worktree(local, "20260804-test-checkout-vastzetten")

    before = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Guarded checkout inside the worktree (the exact op that must stay scoped).
    guarded_wt = guard_git_target(wt)
    subprocess.run(
        ["git", "-C", str(guarded_wt), "checkout", "-b", "dispatch/worker-side-branch"],
        capture_output=True, check=True,
    )

    after = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert before == after == "main"
    # The worktree is the one that moved.
    wt_head = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert wt_head == "dispatch/worker-side-branch"
