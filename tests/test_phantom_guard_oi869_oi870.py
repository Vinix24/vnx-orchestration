"""test_phantom_guard_oi869_oi870.py — reproduce OI-869 + OI-870 fail paths.

OI-869 — self-referencing base_ref: when base_ref resolves to the same commit as the
branch head (because the worker pushed onto the SAME branch that base_ref names), the
diff is zero by definition. Falling back to origin/main yields the real diff.

OI-870 — branch derivation from dispatch-id: a fix-forward dispatch pushes onto a
pre-existing PR branch, not its own ``dispatch/<id>`` branch. Deriving the branch name
from dispatch_id looks for a branch that was never pushed. Reading the branch from
the worktree itself finds the real push target.

Every test in this file MUST fail against origin/main (which derives the branch from
dispatch_id and does not detect self-referencing base_ref) and pass on the fix branch.
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
# Helpers
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


@pytest.fixture()
def git_fixture(tmp_path: Path):
    """Bare origin remote + working checkout. Mirrors real fabric shape."""
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


# ---------------------------------------------------------------------------
# OI-869 — self-referencing base_ref
# ---------------------------------------------------------------------------


def test_oi869_self_referencing_base_ref_produces_zero_diff_on_main(git_fixture):
    """When base_ref names the SAME branch the worker pushed to, the diff is zero
    because base_ref and the branch head resolve to the same commit — "diff against
    yourself" is not evidence of a phantom.

    Without the fix: compute_branch_diff("origin/<branch>", base_ref=<branch>)
    → merge-base(branch, branch-head) == branch-head → diff == empty.
    The fix detects the self-reference and falls back to origin/main as the base.
    """
    consumer_repo = git_fixture
    dispatch_id = "20260731-oi869-selfref"
    from dispatch_worktree_isolation import _sanitize_dispatch_id
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch = f"dispatch/{safe_id}"

    # Create a worktree from origin/main
    wt_dir = consumer_repo / ".vnx-data" / "worktrees" / f"dispatch-{safe_id}"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(consumer_repo, "worktree", "add", str(wt_dir), "-b", branch, "origin/main")

    # Worker commits and pushes real work to this branch
    _run_git(wt_dir, "config", "user.email", "test@example.com")
    _run_git(wt_dir, "config", "user.name", "Test")
    (wt_dir / "oi869_work.py").write_text("def fix():\n    return 'done'\n", encoding="utf-8")
    _run_git(wt_dir, "add", "oi869_work.py")
    _run_git(wt_dir, "commit", "-m", "real fix-forward work")
    _run_git(wt_dir, "push", "-u", "origin", branch)

    # base_ref is the SAME branch name — self-referencing
    self_ref_base = branch

    # Fetch so the consumer repo can see origin/<branch>
    _run_git(consumer_repo, "fetch", "origin", branch)

    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref=self_ref_base,
        wt_path=wt_dir,
        repo=consumer_repo,
    )

    # The worker produced real work — the diff MUST be non-empty
    assert result is not None, (
        "OI-869: guard returned None (abstain) instead of the real diff — "
        "self-referencing base_ref produced a dead-end path"
    )
    assert "oi869_work.py" in result, (
        "OI-869: diff is missing the pushed file — "
        f"self-referencing base_ref {self_ref_base!r} caused a zero-diff"
    )
    assert "def fix" in result

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_dir), "--force")


# ---------------------------------------------------------------------------
# OI-870 — branch derived from dispatch-id mismatches actual push target
# ---------------------------------------------------------------------------


def test_oi870_fix_forward_branch_differs_from_dispatch_id_derived(git_fixture):
    """A fix-forward dispatch pushes onto an existing PR branch whose name does NOT
    match the dispatch/<id> pattern. Deriving the branch from dispatch_id (old code)
    looks for a branch that was never pushed → has_upstream=False → falls back to
    worktree diff. Combined with a self-referencing base_ref (OI-869), the worktree
    diff is also empty, and the guard falsely rejects real work.

    The fix reads the branch from the worktree (git -C <wt_path> rev-parse
    --abbrev-ref HEAD), finds the real push target on the remote, and uses that.
    """
    consumer_repo = git_fixture
    # The dispatch-id produces a branch the worker DID NOT push to
    dispatch_id = "20260731-ff1256-symlink-scan-scope"

    # The ACTUAL branch the worker pushed to (a pre-existing PR branch)
    actual_branch = "dispatch/20260730-ci-out-of-repo-tests-h"

    # Create a worktree on the actual PR branch (simulating the worker checking
    # out the PR branch inside its dispatch worktree)
    wt_dir = consumer_repo / ".vnx-data" / "worktrees" / "dispatch-ff1256-worktree"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(consumer_repo, "worktree", "add", str(wt_dir), "-b", actual_branch, "origin/main")

    # Worker commits and pushes real work to the ACTUAL branch
    _run_git(wt_dir, "config", "user.email", "test@example.com")
    _run_git(wt_dir, "config", "user.name", "Test")
    (wt_dir / "fix_forward_feature.py").write_text(
        "def fix_forward():\n    return 'applied'\n", encoding="utf-8",
    )
    _run_git(wt_dir, "add", "fix_forward_feature.py")
    _run_git(wt_dir, "commit", "-m", "fix-forward onto existing PR branch")
    _run_git(wt_dir, "push", "-u", "origin", actual_branch)

    # Simulate the real scenario: base_ref is the PR branch name (self-referencing,
    # because the plan stored it and the worker just pushed onto the same branch).
    # Combined with OI-870 (wrong branch derived from dispatch_id), the old code
    # has TWO reasons to produce an empty diff:
    #  1. ls-remote for derived branch → not found → has_upstream=False
    #  2. worktree fallback with self-referencing base_ref → diff == empty
    base_ref = actual_branch

    # Fetch the actual branch so the consumer repo can see it
    _run_git(consumer_repo, "fetch", "origin", actual_branch)

    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref=base_ref,
        wt_path=wt_dir,
        repo=consumer_repo,
    )

    # Real work was pushed — the diff MUST be non-empty
    assert result is not None, (
        "OI-870+OI-869: guard returned None (abstain) instead of the real diff — "
        f"derived branch dispatch/<sanitized-id> was never pushed and the worktree "
        f"fallback hit a self-referencing base_ref {base_ref!r}"
    )
    assert "fix_forward_feature.py" in result, (
        "OI-870+OI-869: diff is missing the pushed file — "
        f"wrong branch derived from dispatch_id={dispatch_id!r}, "
        f"actual branch={actual_branch!r}, base_ref={base_ref!r}"
    )
    assert "def fix_forward" in result

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_dir), "--force")


# ---------------------------------------------------------------------------
# OI-870 isolation — the branch is read from the worktree, not derived
# ---------------------------------------------------------------------------


def test_oi870_branch_read_from_worktree_not_derived_from_dispatch_id(git_fixture, monkeypatch):
    """The function reads the actual branch from the worktree via
    ``git -C <wt_path> rev-parse --abbrev-ref HEAD`` instead of deriving it from
    dispatch_id. When the worktree branch differs from the dispatch-id-derived
    name, the function still finds the pushed content.

    Without the fix: the derived branch is checked on the remote → not found →
    falls back to worktree diff which hits self-referencing base_ref → empty.
    """
    consumer_repo = git_fixture
    # dispatch-id that derives a different branch than the one in the worktree
    dispatch_id = "20260731-dispatch-xyz-999"

    # Actual branch in the worktree
    actual_branch = "dispatch/20260730-existing-pr-branch"

    # Create worktree on the actual branch
    wt_dir = consumer_repo / ".vnx-data" / "worktrees" / "dispatch-oi870-isolation"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(consumer_repo, "worktree", "add", str(wt_dir), "-b", actual_branch, "origin/main")

    # Worker pushes to the actual branch
    _run_git(wt_dir, "config", "user.email", "test@example.com")
    _run_git(wt_dir, "config", "user.name", "Test")
    (wt_dir / "isolation_work.py").write_text("# OI-870 isolation test\n", encoding="utf-8")
    _run_git(wt_dir, "add", "isolation_work.py")
    _run_git(wt_dir, "commit", "-m", "OI-870 isolation work")
    _run_git(wt_dir, "push", "-u", "origin", actual_branch)

    # Fetch so consumer repo can see the remote branch
    _run_git(consumer_repo, "fetch", "origin", actual_branch)

    # Self-referencing base_ref to force the old code's worktree fallback to fail
    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref=actual_branch,
        wt_path=wt_dir,
        repo=consumer_repo,
    )

    assert result is not None, (
        "OI-870 isolation: guard returned None — "
        f"dispatch_id={dispatch_id!r} derives a different branch than "
        f"the worktree's actual branch={actual_branch!r}"
    )
    assert "isolation_work.py" in result, (
        "OI-870 isolation: diff is missing the pushed file — "
        "branch was derived from dispatch_id instead of read from the worktree"
    )

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_dir), "--force")


# ---------------------------------------------------------------------------
# Regression: normal case still works (branch derived == actual branch)
# ---------------------------------------------------------------------------


def test_normal_case_branch_derived_equals_actual_still_works(git_fixture):
    """When the dispatch-id-derived branch IS the branch the worktree is on
    (the normal non-fix-forward case), the function still produces the correct
    diff. This is a regression guard — the worktree-branch-reading logic must
    not break the standard path.
    """
    consumer_repo = git_fixture
    dispatch_id = "20260731-normal-dispatch-case"
    from dispatch_worktree_isolation import _sanitize_dispatch_id
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch = f"dispatch/{safe_id}"

    wt_dir = consumer_repo / ".vnx-data" / "worktrees" / f"dispatch-{safe_id}"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(consumer_repo, "worktree", "add", str(wt_dir), "-b", branch, "origin/main")

    _run_git(wt_dir, "config", "user.email", "test@example.com")
    _run_git(wt_dir, "config", "user.name", "Test")
    (wt_dir / "normal.py").write_text("# normal dispatch work\n", encoding="utf-8")
    _run_git(wt_dir, "add", "normal.py")
    _run_git(wt_dir, "commit", "-m", "normal work")
    _run_git(wt_dir, "push", "-u", "origin", branch)

    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref="origin/main",
        wt_path=wt_dir,
        repo=consumer_repo,
    )

    assert result is not None
    assert "normal.py" in result, (
        "regression: normal case where dispatch-id-derived branch matches "
        "the actual worktree branch should still produce a correct non-empty diff"
    )

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_dir), "--force")


# ---------------------------------------------------------------------------
# Edge case: worktree rev-parse fails → still falls back to derived branch
# ---------------------------------------------------------------------------


def test_worktree_rev_parse_failure_falls_back_to_derived_branch(git_fixture, monkeypatch):
    """When ``git -C <wt_path> rev-parse --abbrev-ref HEAD`` fails (e.g. worktree
    is detached or git is broken), the function falls back to the dispatch-id-derived
    branch name — existing behaviour is preserved, not dropped.
    """
    consumer_repo = git_fixture
    dispatch_id = "20260731-fallback-derived"
    from dispatch_worktree_isolation import _sanitize_dispatch_id
    safe_id = _sanitize_dispatch_id(dispatch_id)
    branch = f"dispatch/{safe_id}"

    wt_dir = consumer_repo / ".vnx-data" / "worktrees" / f"dispatch-{safe_id}"
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_git(consumer_repo, "worktree", "add", str(wt_dir), "-b", branch, "origin/main")

    # Worker pushes real work
    _run_git(wt_dir, "config", "user.email", "test@example.com")
    _run_git(wt_dir, "config", "user.name", "Test")
    (wt_dir / "fallback.py").write_text("# fallback work\n", encoding="utf-8")
    _run_git(wt_dir, "add", "fallback.py")
    _run_git(wt_dir, "commit", "-m", "fallback work")
    _run_git(wt_dir, "push", "-u", "origin", branch)

    # Make ALL git commands inside the worktree fail, simulating a broken worktree
    original_run = subprocess.run

    def _stubbed_run(cmd, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
        # Let rev-parse in the worktree fail
        if "-C" in cmd_str and str(wt_dir) in cmd_str and "rev-parse" in cmd_str:
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal: not a git repository")
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", _stubbed_run)

    result = envelope._resolve_phantom_diff(
        dispatch_id,
        base_ref="origin/main",
        wt_path=wt_dir,
        repo=consumer_repo,
    )

    # Should still work via the dispatch-id-derived branch (which is correct here)
    assert result is not None, (
        "fallback: when worktree rev-parse fails, the dispatch-id-derived "
        "branch must still be used — guard must not abstain"
    )
    assert "fallback.py" in result

    # Cleanup
    _run_git(consumer_repo, "worktree", "remove", str(wt_dir), "--force")
