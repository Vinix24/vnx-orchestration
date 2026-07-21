#!/usr/bin/env python3
"""Tests for gate_worktree.py — isolated git worktree checkout for gate execution (OI-708).

Uses real, disposable git repos under tmp_path (never touches the actual
vnx-orchestration checkout) so the create/remove lifecycle is verified
end-to-end: origin/<branch> content lands in the worktree even when the
local (caller) repo's own working tree is stale or has never fetched that
branch — the exact scenario that made codex's own file reads (sed/rg/cat)
unreliable when run with cwd at the orchestrator's ambient checkout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

VNX_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from gate_worktree import GateWorktreeError, create_gate_worktree, remove_gate_worktree


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
def origin_and_local(tmp_path):
    """Build an 'origin' repo with a feature branch pushed only there, and a
    'local' clone that never fetched it — mirroring an orchestrator checkout
    that is stale relative to the PR branch under review.
    """
    origin = tmp_path / "origin"
    _init_repo(origin)
    (origin / "marker.txt").write_text("BASE\n")
    _run_git(["add", "marker.txt"], origin)
    _run_git(["commit", "-m", "base"], origin)

    local = tmp_path / "local"
    _run_git(["clone", str(origin), str(local)], tmp_path)
    _run_git(["config", "user.email", "test@example.invalid"], local)
    _run_git(["config", "user.name", "Test"], local)

    # Push a feature branch to origin with content the local clone never sees.
    _run_git(["checkout", "-b", "feature/oi-708"], origin)
    (origin / "marker.txt").write_text("FRESH_FROM_PR_BRANCH\n")
    _run_git(["add", "marker.txt"], origin)
    _run_git(["commit", "-m", "feature work"], origin)
    _run_git(["checkout", "main"], origin)

    return {"origin": origin, "local": local}


class TestCreateGateWorktree:
    def test_worktree_content_matches_origin_branch_not_local_stale_tree(self, origin_and_local):
        """The created worktree must reflect origin/<branch> HEAD, even though
        the local repo's own working tree never fetched that branch before."""
        local = origin_and_local["local"]

        wt_path = create_gate_worktree(
            branch="feature/oi-708", gate="codex_gate", identifier="99",
            project_root=local,
        )
        try:
            assert wt_path.exists()
            assert (wt_path / "marker.txt").read_text() == "FRESH_FROM_PR_BRANCH\n"
            # local's own checked-out working tree is untouched (still on main/BASE).
            assert (local / "marker.txt").read_text() == "BASE\n"
            local_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], local).strip()
            assert local_branch == "main"
        finally:
            remove_gate_worktree(wt_path, project_root=local)

    def test_worktree_is_detached_head(self, origin_and_local):
        """--detach avoids colliding on a named branch across concurrent gates."""
        local = origin_and_local["local"]
        wt_path = create_gate_worktree(
            branch="feature/oi-708", gate="gemini_review", identifier="7",
            project_root=local,
        )
        try:
            branch_name = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], wt_path).strip()
            assert branch_name == "HEAD"
        finally:
            remove_gate_worktree(wt_path, project_root=local)

    def test_empty_branch_raises_without_touching_git(self, origin_and_local):
        local = origin_and_local["local"]
        with pytest.raises(GateWorktreeError, match="non-empty branch"):
            create_gate_worktree(branch="", gate="codex_gate", identifier="1", project_root=local)

    def test_nonexistent_branch_raises_gate_worktree_error(self, origin_and_local):
        local = origin_and_local["local"]
        with pytest.raises(GateWorktreeError):
            create_gate_worktree(
                branch="does/not/exist", gate="codex_gate", identifier="1", project_root=local,
            )
        # No leaked worktree directory for a failed creation.
        worktrees_dir = local / ".vnx-data" / "worktrees"
        if worktrees_dir.exists():
            assert list(worktrees_dir.iterdir()) == []


class TestRemoveGateWorktree:
    def test_remove_deletes_worktree_and_updates_git_metadata(self, origin_and_local):
        local = origin_and_local["local"]
        wt_path = create_gate_worktree(
            branch="feature/oi-708", gate="codex_gate", identifier="5", project_root=local,
        )
        assert wt_path.exists()

        remove_gate_worktree(wt_path, project_root=local)

        assert not wt_path.exists()
        listing = _run_git(["worktree", "list"], local)
        assert str(wt_path) not in listing

    def test_remove_is_idempotent(self, origin_and_local):
        local = origin_and_local["local"]
        wt_path = create_gate_worktree(
            branch="feature/oi-708", gate="codex_gate", identifier="6", project_root=local,
        )
        remove_gate_worktree(wt_path, project_root=local)
        # Second call on an already-removed path must be a silent no-op.
        remove_gate_worktree(wt_path, project_root=local)

    def test_remove_none_is_a_noop(self):
        remove_gate_worktree(None)  # must not raise

    def test_remove_nonexistent_path_is_a_noop(self, tmp_path):
        remove_gate_worktree(tmp_path / "never-existed")  # must not raise


class TestConcurrentGatesDoNotCollide:
    def test_two_worktrees_for_same_branch_get_distinct_paths(self, origin_and_local):
        """Two concurrent gate executions on the same PR (e.g. codex + gemini)
        must not collide on the same worktree path."""
        local = origin_and_local["local"]
        wt_a = create_gate_worktree(
            branch="feature/oi-708", gate="codex_gate", identifier="10", project_root=local,
        )
        wt_b = create_gate_worktree(
            branch="feature/oi-708", gate="gemini_review", identifier="10", project_root=local,
        )
        try:
            assert wt_a != wt_b
            assert wt_a.exists() and wt_b.exists()
        finally:
            remove_gate_worktree(wt_a, project_root=local)
            remove_gate_worktree(wt_b, project_root=local)
