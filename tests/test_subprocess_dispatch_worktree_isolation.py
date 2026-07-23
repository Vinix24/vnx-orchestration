"""test_subprocess_dispatch_worktree_isolation.py — VNX_ISOLATED_WORKTREE tests.

Verifies:
1. When VNX_ISOLATED_WORKTREE unset: _repo_root() uses file-based path (no .vnx-data).
2. _set_active_worktree / _get_active_worktree / _repo_root() override mechanism.
3. create_dispatch_worktree runs git fetch + worktree add, returns correct path.
4. remove_dispatch_worktree calls git worktree remove --force + prune; idempotent.
5. Failure path: worktree is removed even when dispatch raises.
6. Two concurrent dispatch IDs → distinct worktree paths (no shared HEAD/index).
7. delivery._resolve_agent_cwd_and_log_profile returns worktree path when active.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))


# ─── git_helpers override API ─────────────────────────────────────────────────

class TestGitHelpersWorktreeOverride:
    def setup_method(self):
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree
        _set_active_worktree(None)  # clean state before each test

    def teardown_method(self):
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree
        _set_active_worktree(None)  # ensure cleanup

    def test_repo_root_no_override(self):
        from subprocess_dispatch_internals.git_helpers import _repo_root
        result = _repo_root()
        assert result.is_absolute()
        assert ".vnx-data" not in str(result)

    def test_set_worktree_changes_repo_root(self, tmp_path):
        from subprocess_dispatch_internals.git_helpers import (
            _repo_root, _set_active_worktree,
        )
        _set_active_worktree(tmp_path)
        assert _repo_root() == tmp_path

    def test_clear_worktree_restores_repo_root(self, tmp_path):
        from subprocess_dispatch_internals.git_helpers import (
            _repo_root, _set_active_worktree,
        )
        original = _repo_root()
        _set_active_worktree(tmp_path)
        assert _repo_root() == tmp_path
        _set_active_worktree(None)
        assert _repo_root() == original

    def test_get_active_worktree_returns_set_path(self, tmp_path):
        from subprocess_dispatch_internals.git_helpers import (
            _get_active_worktree, _set_active_worktree,
        )
        _set_active_worktree(tmp_path)
        assert _get_active_worktree() == tmp_path

    def test_get_active_worktree_none_by_default(self):
        from subprocess_dispatch_internals.git_helpers import _get_active_worktree
        assert _get_active_worktree() is None


# ─── dispatch_worktree_isolation module ───────────────────────────────────────

class TestDispatchWorktreeDir:
    def test_paths_are_distinct_for_different_dispatch_ids(self, tmp_path):
        from dispatch_worktree_isolation import _dispatch_worktree_dir
        path_a = _dispatch_worktree_dir(tmp_path, "20260529-dispatch-A")
        path_b = _dispatch_worktree_dir(tmp_path, "20260529-dispatch-B")
        assert path_a != path_b

    def test_path_contains_dispatch_id_fragment(self, tmp_path):
        from dispatch_worktree_isolation import _dispatch_worktree_dir, _sanitize_dispatch_id
        dispatch_id = "20260529-141916-worktree-isolation"
        path = _dispatch_worktree_dir(tmp_path, dispatch_id)
        assert _sanitize_dispatch_id(dispatch_id) in str(path)
        assert ".vnx-data/worktrees" in str(path)

    def test_sanitize_strips_unsafe_chars(self):
        from dispatch_worktree_isolation import _sanitize_dispatch_id
        result = _sanitize_dispatch_id("foo:bar/baz.qux")
        assert ":" not in result
        assert "/" not in result
        assert "." not in result


class TestCreateDispatchWorktree:
    def test_calls_git_fetch_then_worktree_add(self, tmp_path):
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            _dispatch_worktree_dir,
            _sanitize_dispatch_id,
        )
        dispatch_id = "20260529-test-create"
        safe_id = _sanitize_dispatch_id(dispatch_id)
        expected_wt = _dispatch_worktree_dir(tmp_path, dispatch_id)

        called_cmds = []

        def fake_run(cmd, **kwargs):
            called_cmds.append(list(cmd))
            if "worktree" in cmd and "add" in cmd:
                expected_wt.mkdir(parents=True, exist_ok=True)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
            result = create_dispatch_worktree(dispatch_id, project_root=tmp_path)

        cmd_strs = [" ".join(c) for c in called_cmds]
        assert any("fetch" in s and "origin" in s and "main" in s for s in cmd_strs), (
            f"git fetch origin main not called; got: {cmd_strs}"
        )
        assert any("worktree" in s and "add" in s for s in cmd_strs), (
            f"git worktree add not called; got: {cmd_strs}"
        )
        assert any(f"dispatch/{safe_id}" in s for s in cmd_strs), (
            f"branch dispatch/{safe_id} not in worktree add call; got: {cmd_strs}"
        )
        assert result == expected_wt.resolve()

    def test_raises_on_worktree_add_failure(self, tmp_path):
        import subprocess
        from dispatch_worktree_isolation import create_dispatch_worktree

        def fake_run(cmd, **kwargs):
            if "worktree" in cmd and "add" in cmd:
                raise subprocess.CalledProcessError(128, cmd, stderr="already exists")
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with pytest.raises(RuntimeError, match="create_dispatch_worktree failed"):
            with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
                create_dispatch_worktree("fail-id", project_root=tmp_path)

    def test_continues_when_fetch_fails(self, tmp_path):
        import subprocess
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            _dispatch_worktree_dir,
        )
        expected_wt = _dispatch_worktree_dir(tmp_path, "fetch-fail-id")

        def fake_run(cmd, **kwargs):
            if "fetch" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="network error")
            if "worktree" in cmd and "add" in cmd:
                expected_wt.mkdir(parents=True, exist_ok=True)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
            result = create_dispatch_worktree("fetch-fail-id", project_root=tmp_path)

        assert result == expected_wt.resolve()


class TestCentralInstallGuard:
    """P0 provider-worktree-root-fix: a dispatch worktree must NEVER be created
    (or removed) inside the shared VNX central install tree
    (``~/.vnx-system/...``) — that would run `git worktree` against the fabric
    checkout every central-install consumer (SC/MC/SEO/...) reads from,
    colliding across unrelated consumers. Resolution must fail loud instead.
    """

    def test_create_raises_when_project_root_is_central_install(self):
        from dispatch_worktree_isolation import (
            CentralInstallWorktreeError,
            create_dispatch_worktree,
        )

        central_install_path = Path.home() / ".vnx-system" / "versions" / "v1.9.9"

        with patch("dispatch_worktree_isolation.subprocess.run") as mock_run:
            with pytest.raises(CentralInstallWorktreeError):
                create_dispatch_worktree("central-install-guard-test", project_root=central_install_path)

        assert not mock_run.called, "guard must fire before any git subprocess call"

    def test_remove_raises_when_project_root_is_central_install(self):
        from dispatch_worktree_isolation import (
            CentralInstallWorktreeError,
            remove_dispatch_worktree,
        )

        central_install_path = Path.home() / ".vnx-system" / "current"

        with pytest.raises(CentralInstallWorktreeError):
            remove_dispatch_worktree("central-install-guard-remove-test", project_root=central_install_path)

    def test_consumer_project_root_outside_central_install_is_unaffected(self, tmp_path):
        """A normal consumer project_root (not under ~/.vnx-system) must resolve cleanly."""
        from dispatch_worktree_isolation import _resolve_project_root

        assert _resolve_project_root(tmp_path) == tmp_path.resolve()


class TestRemoveDispatchWorktree:
    def test_calls_remove_force_and_prune(self, tmp_path):
        from dispatch_worktree_isolation import remove_dispatch_worktree, _dispatch_worktree_dir

        dispatch_id = "20260529-test-remove"
        wt_path = _dispatch_worktree_dir(tmp_path, dispatch_id)
        wt_path.mkdir(parents=True, exist_ok=True)

        called_cmds = []

        def fake_run(cmd, **kwargs):
            called_cmds.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
            remove_dispatch_worktree(dispatch_id, project_root=tmp_path)

        cmd_strs = [" ".join(c) for c in called_cmds]
        assert any("remove" in s and "--force" in s for s in cmd_strs), (
            f"git worktree remove --force not called; got: {cmd_strs}"
        )
        assert any("prune" in s for s in cmd_strs), (
            f"git worktree prune not called; got: {cmd_strs}"
        )

    def test_idempotent_when_worktree_absent(self, tmp_path):
        from dispatch_worktree_isolation import remove_dispatch_worktree

        with patch("dispatch_worktree_isolation.subprocess.run") as mock_run:
            remove_dispatch_worktree("nonexistent-dispatch", project_root=tmp_path)

        mock_run.assert_not_called()


# ─── delivery._resolve_agent_cwd_and_log_profile ─────────────────────────────

class TestDeliveryWorktreeCwd:
    def setup_method(self):
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree
        _set_active_worktree(None)

    def teardown_method(self):
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree
        _set_active_worktree(None)

    def test_returns_worktree_path_when_active(self, tmp_path):
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree
        from subprocess_dispatch_internals.delivery import _resolve_agent_cwd_and_log_profile

        _set_active_worktree(tmp_path)

        with patch("subprocess_dispatch._resolve_agent_cwd", return_value=None):
            result = _resolve_agent_cwd_and_log_profile(role=None)

        assert result == tmp_path

    def test_returns_agent_cwd_when_no_worktree_active(self, tmp_path):
        from subprocess_dispatch_internals.delivery import _resolve_agent_cwd_and_log_profile

        agent_dir = tmp_path / "agents" / "backend-developer"
        agent_dir.mkdir(parents=True)

        with patch("subprocess_dispatch._resolve_agent_cwd", return_value=agent_dir):
            result = _resolve_agent_cwd_and_log_profile(role="backend-developer")

        assert result == agent_dir

    def test_worktree_takes_precedence_over_agent_cwd(self, tmp_path):
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree
        from subprocess_dispatch_internals.delivery import _resolve_agent_cwd_and_log_profile

        worktree = tmp_path / "wt"
        worktree.mkdir()
        agent_dir = tmp_path / "agents" / "backend-developer"
        agent_dir.mkdir(parents=True)

        _set_active_worktree(worktree)

        with patch("subprocess_dispatch._resolve_agent_cwd", return_value=agent_dir):
            result = _resolve_agent_cwd_and_log_profile(role="backend-developer")

        assert result == worktree


# ─── full lifecycle: create → active → cleanup ────────────────────────────────

class TestIsolationLifecycle:
    def setup_method(self):
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree
        _set_active_worktree(None)

    def teardown_method(self):
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree
        _set_active_worktree(None)

    def test_worktree_active_during_dispatch_cleared_after(self, tmp_path):
        """Active worktree is set before and cleared after delivery."""
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )
        from subprocess_dispatch_internals.git_helpers import (
            _get_active_worktree, _repo_root, _set_active_worktree,
        )
        from dispatch_worktree_isolation import _dispatch_worktree_dir

        dispatch_id = "lifecycle-test-001"
        wt_path = _dispatch_worktree_dir(tmp_path, dispatch_id)

        def fake_run(cmd, **kwargs):
            if "worktree" in cmd and "add" in cmd:
                wt_path.mkdir(parents=True, exist_ok=True)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
            resolved = create_dispatch_worktree(dispatch_id, project_root=tmp_path)
            _set_active_worktree(resolved)

            active_during = _get_active_worktree()
            root_during = _repo_root()

            _set_active_worktree(None)
            remove_dispatch_worktree(dispatch_id, project_root=tmp_path)

        assert active_during == wt_path.resolve()
        assert root_during == wt_path.resolve()
        assert _get_active_worktree() is None

    def test_cleanup_runs_on_exception(self, tmp_path):
        """Worktree is removed even when the dispatch raises an exception."""
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
            _dispatch_worktree_dir,
        )
        from subprocess_dispatch_internals.git_helpers import _set_active_worktree

        dispatch_id = "exception-cleanup-test"
        wt_path = _dispatch_worktree_dir(tmp_path, dispatch_id)
        removed = []

        def fake_run(cmd, **kwargs):
            if "worktree" in cmd and "add" in cmd:
                wt_path.mkdir(parents=True, exist_ok=True)
            if "worktree" in cmd and "remove" in cmd:
                removed.append(True)
            r = MagicMock()
            r.returncode = 0
            r.stderr = ""
            return r

        with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
            wt = create_dispatch_worktree(dispatch_id, project_root=tmp_path)
            _set_active_worktree(wt)
            try:
                raise RuntimeError("simulated dispatch failure")
            except RuntimeError:
                pass
            finally:
                _set_active_worktree(None)
                remove_dispatch_worktree(dispatch_id, project_root=tmp_path)

        assert len(removed) == 1, "worktree remove must be called even on failure"

    def test_two_dispatch_ids_get_distinct_paths(self, tmp_path):
        """Concurrency: two dispatches never share the same worktree directory."""
        from dispatch_worktree_isolation import _dispatch_worktree_dir

        path_a = _dispatch_worktree_dir(tmp_path, "dispatch-2026-A")
        path_b = _dispatch_worktree_dir(tmp_path, "dispatch-2026-B")

        assert path_a.resolve() != path_b.resolve()
        # Each worktree has its own HEAD/index — no shared state possible.
        assert str(path_a) != str(path_b)
