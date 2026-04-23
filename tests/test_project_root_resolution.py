"""Comprehensive tests for scripts/lib/project_root.py.

Covers all resolution paths: caller-file, cwd, symlink, env fallback,
explicit override, multi-worktree isolation.
"""
from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import pytest

from scripts.lib.project_root import (
    resolve_data_dir,
    resolve_dispatch_dir,
    resolve_project_root,
    resolve_state_dir,
)


def _git_init(path: Path) -> None:
    """Initialize a bare-minimum git repo at path."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )


def _git_initial_commit(path: Path) -> None:
    """Create an initial commit so worktrees can be added."""
    readme = path / "README.md"
    readme.write_text("test\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )


class TestResolveProjectRoot:
    def test_resolve_from_caller_file_in_git_repo(self, tmp_path: Path) -> None:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        _git_init(repo)
        caller = repo / "scripts" / "myscript.py"
        caller.parent.mkdir(parents=True)
        caller.write_text("# script\n")

        result = resolve_project_root(caller_file=str(caller))
        assert result == repo.resolve()

    def test_resolve_from_cwd_in_git_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "cwdrepo"
        repo.mkdir()
        _git_init(repo)
        monkeypatch.chdir(repo)

        result = resolve_project_root(caller_file=None)
        assert result == repo.resolve()

    def test_resolve_symlink_resolved_correctly(self, tmp_path: Path) -> None:
        repo = tmp_path / "targetrepo"
        repo.mkdir()
        _git_init(repo)
        script_real = repo / "scripts" / "real_script.py"
        script_real.parent.mkdir(parents=True)
        script_real.write_text("# real\n")

        link_dir = tmp_path / "links"
        link_dir.mkdir()
        link = link_dir / "linked_script.py"
        link.symlink_to(script_real)

        result = resolve_project_root(caller_file=str(link))
        assert result == repo.resolve()

    def test_resolve_env_fallback_emits_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        non_git = tmp_path / "notarepo"
        non_git.mkdir()
        fake_root = tmp_path / "fake_root"
        fake_root.mkdir()

        monkeypatch.chdir(non_git)
        monkeypatch.setenv("VNX_CANONICAL_ROOT", str(fake_root))

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_project_root(caller_file=None)

        assert result == fake_root.resolve()
        assert len(caught) == 1
        assert issubclass(caught[0].category, DeprecationWarning)
        assert "VNX_CANONICAL_ROOT" in str(caught[0].message)

    def test_resolve_no_git_no_env_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        non_git = tmp_path / "notarepo2"
        non_git.mkdir()
        monkeypatch.chdir(non_git)
        monkeypatch.delenv("VNX_CANONICAL_ROOT", raising=False)

        with pytest.raises(RuntimeError, match="Cannot resolve project root"):
            resolve_project_root(caller_file=None)

    def test_resolve_prefers_git_over_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "gitrepo"
        repo.mkdir()
        _git_init(repo)
        fake_env_root = tmp_path / "fake_env_root"
        fake_env_root.mkdir()

        monkeypatch.setenv("VNX_CANONICAL_ROOT", str(fake_env_root))
        caller = repo / "script.py"
        caller.write_text("# s\n")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_project_root(caller_file=str(caller))

        assert result == repo.resolve()
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) == 0, "Should not warn when git resolution succeeds"


class TestResolveDataDir:
    def test_resolve_data_dir_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "proj"
        repo.mkdir()
        _git_init(repo)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        caller = repo / "script.py"
        caller.write_text("# s\n")

        result = resolve_data_dir(caller_file=str(caller))
        assert result == (repo / ".vnx-data").resolve()

    def test_resolve_data_dir_explicit_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "proj2"
        repo.mkdir()
        _git_init(repo)
        other = tmp_path / "other_data"
        other.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(other))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        caller = repo / "script.py"
        caller.write_text("# s\n")

        result = resolve_data_dir(caller_file=str(caller))
        assert result == other.resolve()

    def test_resolve_data_dir_env_without_explicit_flag_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "proj3"
        repo.mkdir()
        _git_init(repo)
        other = tmp_path / "other_data2"
        other.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(other))
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        caller = repo / "script.py"
        caller.write_text("# s\n")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_data_dir(caller_file=str(caller))

        assert result == (repo / ".vnx-data").resolve()
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) == 1
        assert "VNX_DATA_DIR_EXPLICIT=1" in str(dep_warnings[0].message)


class TestResolveStateDirAndDispatchDir:
    def test_resolve_state_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "staterepo"
        repo.mkdir()
        _git_init(repo)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        caller = repo / "script.py"
        caller.write_text("# s\n")

        result = resolve_state_dir(caller_file=str(caller))
        assert result == (repo / ".vnx-data" / "state").resolve()

    def test_resolve_dispatch_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "dispatchrepo"
        repo.mkdir()
        _git_init(repo)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        caller = repo / "script.py"
        caller.write_text("# s\n")

        result = resolve_dispatch_dir(caller_file=str(caller))
        assert result == (repo / ".vnx-data" / "dispatches").resolve()


class TestDeprecationWarningRegression:
    def test_env_fallback_emits_deprecation_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VNX_CANONICAL_ROOT fallback must emit DeprecationWarning (regression for #225)."""
        non_git = tmp_path / "notarepo_reg"
        non_git.mkdir()
        fake_root = tmp_path / "fake_root_reg"
        fake_root.mkdir()

        monkeypatch.chdir(non_git)
        monkeypatch.setenv("VNX_CANONICAL_ROOT", str(fake_root))
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_project_root(caller_file=None)

        assert result == fake_root.resolve()
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) == 1
        assert "VNX_CANONICAL_ROOT" in str(dep_warnings[0].message)

    def test_explicit_env_override_no_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VNX_DATA_DIR_EXPLICIT=1 must suppress all DeprecationWarnings (regression for #225)."""
        override_dir = tmp_path / "explicit_data"
        override_dir.mkdir()

        monkeypatch.setenv("VNX_DATA_DIR", str(override_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        monkeypatch.delenv("VNX_CANONICAL_ROOT", raising=False)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_data_dir(caller_file=None)

        assert result == override_dir.resolve()
        dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) == 0, f"Expected no DeprecationWarnings, got: {dep_warnings}"


class TestWorktreeIsolation:
    def test_resolve_worktree_isolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two git worktrees of the same repo each return their own worktree root."""
        main_repo = tmp_path / "mainrepo"
        main_repo.mkdir()
        _git_init(main_repo)
        _git_initial_commit(main_repo)

        # Create a branch for the worktree
        subprocess.run(
            ["git", "-C", str(main_repo), "branch", "wt-branch"],
            check=True,
            capture_output=True,
        )

        worktree_path = tmp_path / "my_worktree"
        subprocess.run(
            ["git", "-C", str(main_repo), "worktree", "add", str(worktree_path), "wt-branch"],
            check=True,
            capture_output=True,
        )

        try:
            script_main = main_repo / "script_main.py"
            script_main.write_text("# main\n")
            result_main = resolve_project_root(caller_file=str(script_main))

            script_wt = worktree_path / "script_wt.py"
            script_wt.write_text("# wt\n")
            result_wt = resolve_project_root(caller_file=str(script_wt))

            assert result_main == main_repo.resolve()
            assert result_wt == worktree_path.resolve()
            assert result_main != result_wt, "Each worktree should resolve to its own root"
        finally:
            subprocess.run(
                ["git", "-C", str(main_repo), "worktree", "remove", "--force", str(worktree_path)],
                capture_output=True,
            )
