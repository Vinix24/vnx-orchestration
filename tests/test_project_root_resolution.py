"""Comprehensive tests for scripts/lib/project_root.py.

Covers all resolution paths: caller-file, cwd, symlink, env fallback,
explicit override, multi-worktree isolation.
"""
from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

from data_dir_guard import VNXDataDirMismatchWarning
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


def _fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a temp directory for the test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class TestResolveDataDirProjectIdGuard:
    """OI-899: the explicit VNX_DATA_DIR branch of resolve_data_dir must be
    covered by the data-dir/project-id guard.

    The guard adds a *signal* only — every path returned before the fix must
    still be returned byte-identical after it.
    """

    def _pin_explicit(self, monkeypatch: pytest.MonkeyPatch, data_dir: Path, pid: str | None) -> None:
        if pid is None:
            monkeypatch.delenv("VNX_PROJECT_ID", raising=False)
        else:
            monkeypatch.setenv("VNX_PROJECT_ID", pid)
        monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    def test_explicit_foreign_root_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The measured live case (OI-900): foreign data root pinned via the
        fleet-wide mitigation env vars (VNX_DATA_DIR + VNX_DATA_DIR_EXPLICIT=1)
        must produce a mismatch signal for the active project_id."""
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "warn")
        home = _fake_home(tmp_path, monkeypatch)
        (home / ".vnx-data" / "vnx-dev").mkdir(parents=True)
        foreign = tmp_path / "mission-control-root"
        foreign.mkdir()
        self._pin_explicit(monkeypatch, foreign, "vnx-dev")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_data_dir()

        assert result == foreign.resolve()
        mismatch = [w for w in caught if issubclass(w.category, VNXDataDirMismatchWarning)]
        assert len(mismatch) == 1
        assert "vnx-dev" in str(mismatch[0].message)
        assert str(foreign.resolve()) in str(mismatch[0].message)

    def test_explicit_foreign_root_enforce_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "enforce")
        home = _fake_home(tmp_path, monkeypatch)
        (home / ".vnx-data" / "vnx-dev").mkdir(parents=True)
        foreign = tmp_path / "mission-control-root"
        foreign.mkdir()
        self._pin_explicit(monkeypatch, foreign, "vnx-dev")

        with pytest.raises(RuntimeError, match="VNX data-dir mismatch"):
            resolve_data_dir()

    @pytest.mark.parametrize("mode", ["off", "warn", "enforce"])
    def test_explicit_matching_central_dir_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", mode)
        home = _fake_home(tmp_path, monkeypatch)
        central = home / ".vnx-data" / "vnx-dev"
        central.mkdir(parents=True)
        self._pin_explicit(monkeypatch, central, "vnx-dev")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_data_dir()

        assert result == central.resolve()
        assert len(caught) == 0

    def test_guard_off_is_silent_on_mismatch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "off")
        _fake_home(tmp_path, monkeypatch)
        foreign = tmp_path / "mission-control-root"
        foreign.mkdir()
        self._pin_explicit(monkeypatch, foreign, "vnx-dev")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_data_dir()

        assert result == foreign.resolve()
        assert len(caught) == 0

    def test_unresolvable_project_id_stays_silent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unresolvable project_id means 'cannot verify' — no mismatch may
        be fabricated and resolution must not raise, even in enforce mode."""
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "enforce")
        _fake_home(tmp_path, monkeypatch)
        foreign = tmp_path / "some-root"
        foreign.mkdir()
        self._pin_explicit(monkeypatch, foreign, None)
        # No .vnx-project-id marker and no git repo anywhere up from cwd.
        monkeypatch.chdir(tmp_path)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_data_dir()

        assert result == foreign.resolve()
        assert len(caught) == 0

    def test_return_values_invariant_across_guard_modes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Return-value invariance: the guard is signal-only. For every guard
        mode the three resolvers must return the exact paths they returned
        before the guard existed."""
        home = _fake_home(tmp_path, monkeypatch)
        (home / ".vnx-data" / "vnx-dev").mkdir(parents=True)
        foreign = tmp_path / "mission-control-root"
        foreign.mkdir()
        self._pin_explicit(monkeypatch, foreign, "vnx-dev")

        expected_data = foreign.resolve()
        expected_state = expected_data / "state"
        expected_dispatch = expected_data / "dispatches"

        for mode in ("off", "warn"):  # enforce is covered separately (raises by design)
            monkeypatch.setenv("VNX_DATA_DIR_GUARD", mode)
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                assert resolve_data_dir() == expected_data
                assert resolve_state_dir() == expected_state
                assert resolve_dispatch_dir() == expected_dispatch

    def test_resolve_state_dir_fires_guard_at_most_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No double-fire: callers in the resolution chain must not multiply
        the guard signal — one resolution produces at most one warning."""
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "warn")
        home = _fake_home(tmp_path, monkeypatch)
        (home / ".vnx-data" / "vnx-dev").mkdir(parents=True)
        foreign = tmp_path / "mission-control-root"
        foreign.mkdir()
        self._pin_explicit(monkeypatch, foreign, "vnx-dev")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_state_dir()

        assert result == foreign.resolve() / "state"
        mismatch = [w for w in caught if issubclass(w.category, VNXDataDirMismatchWarning)]
        assert len(mismatch) == 1

    def test_default_branch_is_not_guarded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The repo-local default ($PROJECT_ROOT/.vnx-data) is legitimately not
        under ~/.vnx-data/<pid>; guarding it would flood every ordinary
        resolution. It must stay silent even in warn mode."""
        monkeypatch.setenv("VNX_DATA_DIR_GUARD", "warn")
        _fake_home(tmp_path, monkeypatch)
        repo = tmp_path / "plainrepo"
        repo.mkdir()
        _git_init(repo)
        monkeypatch.delenv("VNX_DATA_DIR", raising=False)
        monkeypatch.delenv("VNX_DATA_DIR_EXPLICIT", raising=False)
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        caller = repo / "script.py"
        caller.write_text("# s\n")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = resolve_data_dir(caller_file=str(caller))

        assert result == (repo / ".vnx-data").resolve()
        mismatch = [w for w in caught if issubclass(w.category, VNXDataDirMismatchWarning)]
        assert len(mismatch) == 0

    def test_project_root_imports_standalone_no_cycle(self) -> None:
        """project_root must stay importable standalone (deferred guard import
        must not create an import cycle), even with a clean sys.modules."""
        lib_dir = Path(__file__).resolve().parent.parent / "scripts" / "lib"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import project_root; print('standalone-ok')",
                str(lib_dir),
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        assert "standalone-ok" in proc.stdout

    def test_guard_import_failure_fails_open(self, tmp_path: Path) -> None:
        """When scripts/lib is not on sys.path the deferred ``import
        data_dir_guard`` fails; resolve_data_dir must still return the
        explicit path without raising (project_root stays usable standalone)."""
        repo_root = Path(__file__).resolve().parent.parent
        foreign = tmp_path / "foreign-root"
        foreign.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(
            {
                "VNX_DATA_DIR": str(foreign),
                "VNX_DATA_DIR_EXPLICIT": "1",
                "VNX_PROJECT_ID": "vnx-dev",
                "VNX_DATA_DIR_GUARD": "warn",
            }
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from scripts.lib import project_root; "
                "print(project_root.resolve_data_dir())",
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_path),
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        assert str(foreign.resolve()) in proc.stdout


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
