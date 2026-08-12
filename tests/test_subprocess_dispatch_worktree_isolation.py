"""test_subprocess_dispatch_worktree_isolation.py — worktree isolation tests (default-on since OI-1090).

Verifies:
1. _set_active_worktree / _get_active_worktree / _repo_root() override mechanism.
2. create_dispatch_worktree runs git fetch + worktree add, returns correct path.
3. remove_dispatch_worktree classifies before removal; idempotent.
4. Failure path: worktree is removed even when dispatch raises.
5. Two concurrent dispatch IDs → distinct worktree paths (no shared HEAD/index).
6. delivery._resolve_agent_cwd_and_log_profile returns worktree path when active.
7. OI-975: HEAD-jump detection stores main_head_sha in claim and warns on mismatch.
"""

from __future__ import annotations

import subprocess
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
        # _repo_root() resolves to a git-managed directory (may include
        # .vnx-data when running from a worktree — the dispatch worktree is
        # also a git checkout, so the assertion below holds regardless).
        assert (result / ".git").exists(), (
            f"_repo_root() must resolve to a git root; got {result}"
        )

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
            # stdout must be a real string: patching subprocess.run patches the
            # actual stdlib function, and subprocess.check_output is implemented
            # in terms of run() — so the claim-path's check_output call (via
            # resolve_data_root -> _project_id_from_git_remote) collects THIS
            # mock's .stdout too. Left as an auto-MagicMock it breaks .strip()
            # downstream with a TypeError unrelated to what this test exercises.
            r.stdout = ""
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
            r.stdout = ""
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
            r.stdout = ""
            return r

        with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
            result = create_dispatch_worktree("fetch-fail-id", project_root=tmp_path)

        assert result == expected_wt.resolve()


# ─── explicit base_ref (dispatch 20260812h-a-headless-baseref) ───────────────


class TestCreateDispatchWorktreeExplicitBaseRef:
    """create_dispatch_worktree() previously always built on origin/main,
    ignoring any base_ref the caller wanted — the headless-lane blocker this
    dispatch fixes. Real git repos (two branches, distinct HEADs) so the
    assertion is on the worktree's actual committed content, not a mocked
    subprocess call. These tests fail on the pre-fix signature (no base_ref
    kwarg accepted at all).
    """

    @staticmethod
    def _init_repo_two_branches(tmp_path: Path) -> "tuple[Path, str, str]":
        """Bare origin + local clone with `main` and `feature` at DIFFERENT commits.

        Returns (local_checkout, main_sha, feature_sha).
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
        (local / "README.md").write_text("init\n")
        subprocess.run(["git", "-C", str(local), "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(local), "commit", "-m", "initial"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(local), "push", "-u", "origin", "main"], check=True, capture_output=True)
        main_sha = subprocess.check_output(
            ["git", "-C", str(local), "rev-parse", "main"], text=True
        ).strip()

        subprocess.run(["git", "-C", str(local), "checkout", "-b", "feature"], check=True, capture_output=True)
        (local / "feature.txt").write_text("feature work\n")
        subprocess.run(["git", "-C", str(local), "add", "feature.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(local), "commit", "-m", "feature commit"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(local), "push", "-u", "origin", "feature"], check=True, capture_output=True)
        feature_sha = subprocess.check_output(
            ["git", "-C", str(local), "rev-parse", "feature"], text=True
        ).strip()

        subprocess.run(["git", "-C", str(local), "checkout", "main"], check=True, capture_output=True)
        assert main_sha != feature_sha, "fixture bug: main and feature must diverge"
        return local, main_sha, feature_sha

    def test_explicit_base_ref_sets_worktree_on_that_ref(self, tmp_path, monkeypatch):
        """An explicit base_ref=origin/feature builds the worktree on feature's
        HEAD, not origin/main — the core fix this dispatch delivers."""
        monkeypatch.delenv("VNX_BENCH_WORKTREE_BASE_REF", raising=False)
        from dispatch_worktree_isolation import create_dispatch_worktree

        local, main_sha, feature_sha = self._init_repo_two_branches(tmp_path)

        wt_path = create_dispatch_worktree(
            "explicit-base-ref-test", project_root=local, base_ref="origin/feature",
        )

        wt_head = subprocess.check_output(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"], text=True
        ).strip()
        assert wt_head == feature_sha, (
            f"worktree HEAD ({wt_head}) must equal the explicit base_ref's commit "
            f"({feature_sha}, feature), not origin/main's ({main_sha}) — a dispatch "
            f"with base_ref=origin/dispatch/<branch> must build on THAT branch"
        )
        assert (wt_path / "feature.txt").exists(), (
            "worktree must contain the feature branch's file — proof it was built "
            "on feature, not silently on main"
        )

    def test_no_explicit_base_ref_defaults_to_origin_main(self, tmp_path, monkeypatch):
        """Omitting base_ref preserves the pre-existing default: origin/main."""
        monkeypatch.delenv("VNX_BENCH_WORKTREE_BASE_REF", raising=False)
        from dispatch_worktree_isolation import create_dispatch_worktree

        local, main_sha, _feature_sha = self._init_repo_two_branches(tmp_path)

        wt_path = create_dispatch_worktree("default-base-ref-test", project_root=local)

        wt_head = subprocess.check_output(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"], text=True
        ).strip()
        assert wt_head == main_sha
        assert not (wt_path / "feature.txt").exists()

    def test_bench_override_still_works_without_explicit_base_ref(self, tmp_path, monkeypatch):
        """VNX_BENCH_WORKTREE_BASE_REF remains a working fallback for callers
        that pass no explicit base_ref (the provider-lane benchmark harness)."""
        from dispatch_worktree_isolation import create_dispatch_worktree

        local, _main_sha, feature_sha = self._init_repo_two_branches(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "feature")

        wt_path = create_dispatch_worktree("bench-override-test", project_root=local)

        wt_head = subprocess.check_output(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"], text=True
        ).strip()
        assert wt_head == feature_sha, (
            "VNX_BENCH_WORKTREE_BASE_REF must still redirect the worktree when the "
            "caller passes no explicit base_ref"
        )

    def test_explicit_base_ref_wins_over_bench_override(self, tmp_path, monkeypatch):
        """An explicit base_ref takes priority over VNX_BENCH_WORKTREE_BASE_REF.

        The bench env var is a fallback for callers with no explicit base_ref
        (see the precedence note in create_dispatch_worktree's docstring), not an
        override of a caller's own explicit request — silently letting the env
        win would reproduce the exact silent-wrong-base defect this fix closes,
        just from a different source.
        """
        from dispatch_worktree_isolation import create_dispatch_worktree

        local, main_sha, _feature_sha = self._init_repo_two_branches(tmp_path)
        monkeypatch.setenv("VNX_BENCH_WORKTREE_BASE_REF", "feature")

        wt_path = create_dispatch_worktree(
            "explicit-wins-test", project_root=local, base_ref="origin/main",
        )

        wt_head = subprocess.check_output(
            ["git", "-C", str(wt_path), "rev-parse", "HEAD"], text=True
        ).strip()
        assert wt_head == main_sha

    def test_nonexistent_base_ref_fails_loud_with_ref_named(self, tmp_path, monkeypatch):
        """A base_ref that doesn't exist raises RuntimeError naming the ref —
        no silent fallback to origin/main (OI-class: a stale/silent fallback is
        worse than a loud error here)."""
        monkeypatch.delenv("VNX_BENCH_WORKTREE_BASE_REF", raising=False)
        from dispatch_worktree_isolation import create_dispatch_worktree

        local, _main_sha, _feature_sha = self._init_repo_two_branches(tmp_path)

        with pytest.raises(RuntimeError, match="origin/does-not-exist"):
            create_dispatch_worktree(
                "bad-base-ref-test", project_root=local, base_ref="origin/does-not-exist",
            )


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
    def test_remove_delegates_to_tmux_worktree_reap(self, tmp_path):
        """remove_dispatch_worktree delegates to tmux_worktree.classify()+reap()
        (L3 provider-lane reap, 2026-08-08).  The reap call invokes
        git worktree remove --force for clean/pushed/committed classifications."""
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
            r.stdout = ""
            return r

        with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
            remove_dispatch_worktree(dispatch_id, project_root=tmp_path)

        cmd_strs = [" ".join(c) for c in called_cmds]
        assert any("worktree remove" in s and "--force" in s for s in cmd_strs), (
            f"git worktree remove --force not called (via reap); got: {cmd_strs}"
        )

    def test_idempotent_when_worktree_absent(self, tmp_path):
        """remove_dispatch_worktree exits early without git calls when worktree
        is absent.  Verifies the function completes without raising."""
        from dispatch_worktree_isolation import remove_dispatch_worktree

        # The absent-worktree early-exit path goes through _clear_claim →
        # vnx_paths.resolve_data_root, which internally calls subprocess.
        # Patch subprocess.run in dispatch_worktree_isolation to return a
        # well-formed CompletedProcess so the vnx_paths resolver (which uses
        # subprocess.check_output → subprocess.run) doesn't choke on a
        # MagicMock.
        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 128  # not a git repo → non-zero
            r.stderr = "fatal: not a git repository"
            r.stdout = ""
            return r

        with patch("dispatch_worktree_isolation.subprocess.run", side_effect=fake_run):
            # Must not raise — idempotent when worktree is absent
            remove_dispatch_worktree("nonexistent-dispatch", project_root=tmp_path)


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
            r.stdout = ""
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
            r.stdout = ""
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


# ─── OI-975 HEAD-jump detection ──────────────────────────────────────────────


class TestHeadJumpDetection:
    """OI-975: detect when the main checkout HEAD changes during a dispatch.

    The claim stores main_head_sha at creation time; teardown compares the
    current main checkout HEAD against it and logs a loud WARNING when they
    differ, naming the dispatch-id so the event is traceable.
    """

    def test_main_head_sha_stored_in_claim(self, tmp_path):
        """create_dispatch_worktree stores main_head_sha in the claim."""
        import json
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            _sanitize_dispatch_id,
            _claim_path,
        )

        local = self._init_repo(tmp_path)
        dispatch_id = "head-jump-store-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        safe_id = _sanitize_dispatch_id(dispatch_id)
        claim_path = _claim_path(safe_id, local)
        claim = json.loads(claim_path.read_text())

        assert "main_head_sha" in claim, (
            "OI-975: claim must include main_head_sha"
        )
        main_head_sha = claim["main_head_sha"]
        assert len(main_head_sha) == 40, (
            f"main_head_sha must be a full SHA, got {main_head_sha!r}"
        )
        # Must match the actual HEAD of the main checkout.
        actual_head = subprocess.check_output(
            ["git", "-C", str(local), "rev-parse", "HEAD"], text=True
        ).strip()
        assert main_head_sha == actual_head

    def test_remove_detects_head_jump(self, tmp_path, caplog):
        """remove_dispatch_worktree logs WARNING when main checkout HEAD changed."""
        import logging
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
            _sanitize_dispatch_id,
            _claim_path,
        )
        import json

        local = self._init_repo(tmp_path)
        dispatch_id = "head-jump-detect-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        # Simulate a HEAD jump: change the claim's main_head_sha to a different
        # (but valid) SHA that doesn't match the current HEAD.
        safe_id = _sanitize_dispatch_id(dispatch_id)
        claim_path = _claim_path(safe_id, local)
        claim = json.loads(claim_path.read_text())
        # Use a fake SHA that is 40 hex chars — definitely not the current HEAD.
        claim["main_head_sha"] = "0" * 40
        claim_path.write_text(json.dumps(claim) + "\n")

        with caplog.at_level(logging.WARNING, logger="dispatch_worktree_isolation"):
            remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        head_jump_logs = [
            r for r in caplog.records
            if "HEAD-JUMP DETECTED" in (r.message or "")
        ]
        assert len(head_jump_logs) == 1, (
            f"OI-975: expected exactly 1 HEAD-JUMP warning, got {len(head_jump_logs)}"
        )
        assert dispatch_id in head_jump_logs[0].message, (
            "HEAD-JUMP warning must include the dispatch_id"
        )

    def test_no_warning_when_head_unchanged(self, tmp_path, caplog):
        """No false alarm: when HEAD hasn't changed, no HEAD-JUMP warning is logged."""
        import logging
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
        )

        local = self._init_repo(tmp_path)
        dispatch_id = "head-jump-clean-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        with caplog.at_level(logging.WARNING, logger="dispatch_worktree_isolation"):
            remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        head_jump_logs = [
            r for r in caplog.records
            if "HEAD-JUMP DETECTED" in (r.message or "")
        ]
        assert len(head_jump_logs) == 0, (
            "no HEAD-JUMP warning when HEAD is unchanged"
        )

    def test_missing_main_head_sha_skips_check(self, tmp_path, caplog):
        """Pre-existing claims without main_head_sha do not trigger a warning."""
        import logging
        import json
        from dispatch_worktree_isolation import (
            create_dispatch_worktree,
            remove_dispatch_worktree,
            _sanitize_dispatch_id,
            _claim_path,
        )

        local = self._init_repo(tmp_path)
        dispatch_id = "head-jump-old-claim-1"
        wt_path = create_dispatch_worktree(dispatch_id, project_root=local)

        # Remove main_head_sha to simulate a pre-OI-975 claim.
        safe_id = _sanitize_dispatch_id(dispatch_id)
        claim_path = _claim_path(safe_id, local)
        claim = json.loads(claim_path.read_text())
        del claim["main_head_sha"]
        claim_path.write_text(json.dumps(claim) + "\n")

        with caplog.at_level(logging.WARNING, logger="dispatch_worktree_isolation"):
            remove_dispatch_worktree(dispatch_id, project_root=local, terminal_id="T1")

        head_jump_logs = [
            r for r in caplog.records
            if "HEAD-JUMP DETECTED" in (r.message or "")
        ]
        assert len(head_jump_logs) == 0, (
            "no HEAD-JUMP warning when claim has no main_head_sha field"
        )

    @staticmethod
    def _init_repo(tmp_path: Path) -> Path:
        """Create a bare origin + local clone with an initial commit."""
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
            ["git", "-C", str(local), "config", "user.email", "test@test.local"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(local), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        (local / "README.md").write_text("init\n")
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
