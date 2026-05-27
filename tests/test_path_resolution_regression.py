#!/usr/bin/env python3
"""Path-resolution regression tests (PR-7).

Comprehensive tests for vnx_paths.py covering:
  - Default resolution from script location
  - Environment variable overrides
  - Cross-project contamination prevention
  - Legacy .claude/vnx-system layout detection
  - Worktree isolation (VNX_DATA_DIR override)
  - Skills directory fallback chain
  - Intelligence directory portability

These regressions guard A-R4 (path resolution must stay deterministic
across main repo and worktrees).
"""

import os
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import vnx_paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_env(monkeypatch, keys=None):
    """Remove all VNX env vars so tests start clean."""
    keys = keys or [
        "VNX_HOME", "VNX_BIN", "VNX_EXECUTABLE", "PROJECT_ROOT",
        "VNX_DATA_DIR", "VNX_DATA_DIR_EXPLICIT", "VNX_STATE_DIR", "VNX_DISPATCH_DIR",
        "VNX_LOGS_DIR", "VNX_PIDS_DIR", "VNX_LOCKS_DIR",
        "VNX_REPORTS_DIR", "VNX_DB_DIR", "VNX_SKILLS_DIR",
        "VNX_CANONICAL_ROOT", "VNX_INTELLIGENCE_DIR",
        # State-root resolution inputs (PR-PIP-2): keep tests deterministic by
        # clearing the identity + user-data-dir env so resolution is driven only
        # by the test's own setup, never the runner's ambient project.
        "VNX_DATA_HOME", "XDG_DATA_HOME",
        "VNX_PROJECT_ID", "VNX_OPERATOR_ID", "VNX_ORCHESTRATOR_ID", "VNX_AGENT_ID",
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# Default resolution
# ---------------------------------------------------------------------------

class TestDefaultResolution:
    """vnx_paths.py resolves from its own location when no env vars set."""

    def test_resolve_returns_all_required_keys(self, monkeypatch):
        _clean_env(monkeypatch)
        paths = vnx_paths.resolve_paths()
        required = {
            "VNX_HOME", "PROJECT_ROOT", "VNX_DATA_DIR", "VNX_STATE_DIR",
            "VNX_DISPATCH_DIR", "VNX_LOGS_DIR", "VNX_PIDS_DIR",
            "VNX_LOCKS_DIR", "VNX_REPORTS_DIR", "VNX_DB_DIR",
            "VNX_SKILLS_DIR", "VNX_CANONICAL_ROOT", "VNX_INTELLIGENCE_DIR",
        }
        assert required.issubset(set(paths.keys())), f"Missing: {required - set(paths.keys())}"

    def test_all_paths_are_absolute(self, monkeypatch):
        _clean_env(monkeypatch)
        paths = vnx_paths.resolve_paths()
        for key, value in paths.items():
            assert os.path.isabs(value), f"{key}={value} is not absolute"

    def test_vnx_home_is_ancestor_of_this_script(self, monkeypatch):
        _clean_env(monkeypatch)
        paths = vnx_paths.resolve_paths()
        vnx_home = Path(paths["VNX_HOME"])
        this_file = Path(vnx_paths.__file__).resolve()
        assert str(this_file).startswith(str(vnx_home)), \
            f"vnx_paths.py ({this_file}) not under VNX_HOME ({vnx_home})"

    def test_data_dir_resolves_to_user_data_dir_when_state_home_set(self, tmp_path, monkeypatch):
        """PR-PIP-2: with a project_id and VNX_DATA_HOME, state lives OUTSIDE the
        project map (clean footprint), not under PROJECT_ROOT."""
        _clean_env(monkeypatch)
        data_home = tmp_path / "state-home"
        data_home.mkdir()
        monkeypatch.setenv("VNX_DATA_HOME", str(data_home))
        monkeypatch.setenv("VNX_PROJECT_ID", "vnx-dev")
        paths = vnx_paths.resolve_paths()
        data_dir = Path(paths["VNX_DATA_DIR"])
        project_root = Path(paths["PROJECT_ROOT"])
        assert data_dir == (data_home / "vnx-dev").resolve()
        assert not str(data_dir).startswith(str(project_root)), \
            f"VNX_DATA_DIR ({data_dir}) should NOT be under PROJECT_ROOT ({project_root})"


# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------

class TestEnvOverrides:
    """Explicit env vars take precedence over defaults."""

    def test_vnx_home_override(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "custom-vnx"
        fake_home.mkdir()
        monkeypatch.setenv("VNX_HOME", str(fake_home))
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_HOME"] == str(fake_home.resolve())

    def test_vnx_data_dir_override(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_data = tmp_path / "custom-data"
        fake_data.mkdir()
        monkeypatch.setenv("VNX_DATA_DIR", str(fake_data))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_DATA_DIR"] == str(fake_data.resolve())

    def test_state_dir_override(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_state = tmp_path / "custom-state"
        monkeypatch.setenv("VNX_STATE_DIR", str(fake_state))
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_STATE_DIR"] == str(fake_state)

    def test_vnx_bin_resolves_home(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "vnx-dist"
        (fake_home / "bin").mkdir(parents=True)
        fake_bin = fake_home / "bin" / "vnx"
        fake_bin.write_text("#!/bin/bash\n")
        monkeypatch.setenv("VNX_BIN", str(fake_bin))
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_HOME"] == str(fake_home.resolve())

    def test_vnx_executable_resolves_home(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "vnx-dist2"
        (fake_home / "bin").mkdir(parents=True)
        fake_exec = fake_home / "bin" / "vnx"
        fake_exec.write_text("#!/bin/bash\n")
        monkeypatch.setenv("VNX_EXECUTABLE", str(fake_exec))
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_HOME"] == str(fake_home.resolve())


# ---------------------------------------------------------------------------
# Cross-project contamination prevention
# ---------------------------------------------------------------------------

class TestCrossProjectContamination:
    """PROJECT_ROOT from a different project must not leak in."""

    def test_project_root_ignored_when_vnx_home_not_under_it(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        # PROJECT_ROOT points to projectA, but vnx_paths.py is under projectB
        project_a = tmp_path / "projectA"
        project_a.mkdir()
        monkeypatch.setenv("PROJECT_ROOT", str(project_a))
        # vnx_paths.py location determines real VNX_HOME; PROJECT_ROOT should
        # be ignored because VNX_HOME is not under projectA
        paths = vnx_paths.resolve_paths()
        vnx_home = Path(paths["VNX_HOME"])
        project_root = Path(paths["PROJECT_ROOT"])
        # VNX_HOME must be under PROJECT_ROOT
        assert str(vnx_home).startswith(str(project_root)), \
            f"Cross-project leak: VNX_HOME={vnx_home} not under PROJECT_ROOT={project_root}"

    def test_data_dir_not_inherited_from_wrong_project(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        wrong_data = tmp_path / "other-project" / ".vnx-data"
        wrong_data.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(wrong_data))
        paths = vnx_paths.resolve_paths()
        # Explicit VNX_DATA_DIR override IS respected (by design — worktree isolation)
        # but PROJECT_ROOT still derives from VNX_HOME, not from VNX_DATA_DIR
        vnx_home = Path(paths["VNX_HOME"])
        project_root = Path(paths["PROJECT_ROOT"])
        assert str(vnx_home).startswith(str(project_root))


# ---------------------------------------------------------------------------
# Legacy layout detection
# ---------------------------------------------------------------------------

class TestLegacyLayout:
    """VNX_HOME under .claude/vnx-system layout resolves PROJECT_ROOT correctly."""

    def test_legacy_layout_project_root(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        project = tmp_path / "my-project"
        vnx_home = project / ".claude" / "vnx-system"
        vnx_home.mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))
        paths = vnx_paths.resolve_paths()
        assert paths["PROJECT_ROOT"] == str(project.resolve())
        assert paths["VNX_CANONICAL_ROOT"] == str(vnx_home.resolve())
        assert paths["VNX_INTELLIGENCE_DIR"] == str((vnx_home / ".vnx-intelligence").resolve())

    def test_non_legacy_layout_project_root(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        project = tmp_path / "my-project"
        vnx_home = project / ".vnx"
        vnx_home.mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))
        paths = vnx_paths.resolve_paths()
        assert paths["PROJECT_ROOT"] == str(project.resolve())


# ---------------------------------------------------------------------------
# Worktree isolation
# ---------------------------------------------------------------------------

class TestWorktreeIsolation:
    """Each worktree should get its own .vnx-data when VNX_DATA_DIR is overridden."""

    def test_explicit_data_dir_for_worktree(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        wt_data = tmp_path / "worktree-feature" / ".vnx-data"
        wt_data.mkdir(parents=True)
        monkeypatch.setenv("VNX_DATA_DIR", str(wt_data))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_DATA_DIR"] == str(wt_data.resolve())
        # Derived dirs should be under the worktree data dir
        assert str(Path(paths["VNX_STATE_DIR"])).startswith(str(wt_data.resolve()))

    def test_derived_dirs_follow_data_dir(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        custom_data = tmp_path / "custom-data"
        custom_data.mkdir()
        monkeypatch.setenv("VNX_DATA_DIR", str(custom_data))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        paths = vnx_paths.resolve_paths()
        for key in ["VNX_STATE_DIR", "VNX_DISPATCH_DIR", "VNX_LOGS_DIR",
                     "VNX_PIDS_DIR", "VNX_LOCKS_DIR", "VNX_REPORTS_DIR", "VNX_DB_DIR"]:
            assert str(Path(paths[key])).startswith(str(custom_data.resolve())), \
                f"{key} not under VNX_DATA_DIR"


# ---------------------------------------------------------------------------
# Skills directory fallback
# ---------------------------------------------------------------------------

class TestSkillsDirectory:
    """Skills dir follows: env > .claude/skills > VNX_HOME/skills."""

    def test_env_override(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        custom_skills = tmp_path / "custom-skills"
        custom_skills.mkdir()
        monkeypatch.setenv("VNX_SKILLS_DIR", str(custom_skills))
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_SKILLS_DIR"] == str(custom_skills)

    def test_claude_skills_preferred(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        project = tmp_path / "proj"
        vnx_home = project / ".vnx"
        vnx_home.mkdir(parents=True)
        claude_skills = project / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))
        monkeypatch.setenv("PROJECT_ROOT", str(project))
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_SKILLS_DIR"] == str(claude_skills)

    def test_fallback_to_vnx_home_skills(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        project = tmp_path / "proj"
        vnx_home = project / ".vnx"
        (vnx_home / "skills").mkdir(parents=True)
        monkeypatch.setenv("VNX_HOME", str(vnx_home))
        monkeypatch.setenv("PROJECT_ROOT", str(project))
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_SKILLS_DIR"] == str(vnx_home / "skills")


# ---------------------------------------------------------------------------
# Intelligence directory
# ---------------------------------------------------------------------------

class TestIntelligenceDirectory:
    """VNX_INTELLIGENCE_DIR defaults to canonical repo root, not runtime root."""

    def test_default_location(self, monkeypatch):
        _clean_env(monkeypatch)
        paths = vnx_paths.resolve_paths()
        expected_suffix = ".vnx-intelligence"
        assert paths["VNX_INTELLIGENCE_DIR"].endswith(expected_suffix)
        assert paths["VNX_INTELLIGENCE_DIR"].startswith(paths["VNX_CANONICAL_ROOT"])

    def test_env_override(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        custom_intel = tmp_path / "intel"
        custom_intel.mkdir()
        monkeypatch.setenv("VNX_INTELLIGENCE_DIR", str(custom_intel))
        paths = vnx_paths.resolve_paths()
        assert paths["VNX_INTELLIGENCE_DIR"] == str(custom_intel)


class TestStateRootResolver:
    """PR-PIP-2: _resolve_state_root ordered resolution + collision-safety.

    Each branch is exercised in isolation with a real (tmp) HOME so the live
    machine's ~/.vnx-data and ~/.local/share never leak into the assertions.
    """

    def test_explicit_override_wins(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        explicit = tmp_path / "explicit-data"
        explicit.mkdir()
        monkeypatch.setenv("VNX_DATA_DIR", str(explicit))
        monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")
        # Even with a project_id and VNX_DATA_HOME set, explicit override wins.
        monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "ignored"))
        result = vnx_paths._resolve_state_root("vnx-dev", tmp_path / "proj")
        assert result == explicit.resolve()

    def test_data_home_per_project_subdir(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        data_home = tmp_path / "data-home"
        data_home.mkdir()
        monkeypatch.setenv("VNX_DATA_HOME", str(data_home))
        result = vnx_paths._resolve_state_root("seocrawler-v2", tmp_path / "proj")
        assert result == (data_home / "seocrawler-v2").resolve()

    def test_existing_central_install_preserved(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        central = fake_home / ".vnx-data" / "vnx-dev"
        central.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        result = vnx_paths._resolve_state_root("vnx-dev", tmp_path / "proj")
        assert result == central.resolve()

    def test_existing_dev_checkout_preserved(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        project_root = tmp_path / "checkout"
        local = project_root / ".vnx-data"
        local.mkdir(parents=True)
        # No ~/.vnx-data/<id> exists → existing project-local dir wins over XDG.
        result = vnx_paths._resolve_state_root("vnx-dev", project_root)
        assert result == local.resolve()

    def test_fresh_install_uses_xdg_default(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        project_root = tmp_path / "fresh"
        project_root.mkdir()
        result = vnx_paths._resolve_state_root("vnx-dev", project_root)
        assert result == (fake_home / ".local" / "share" / "vnx" / "vnx-dev").resolve()
        assert not str(result).startswith(str(project_root))

    def test_fresh_install_honors_xdg_data_home(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        result = vnx_paths._resolve_state_root("vnx-dev", tmp_path / "fresh")
        assert result == (xdg / "vnx" / "vnx-dev").resolve()

    def test_collision_safety_no_project_id_stays_local(self, tmp_path, monkeypatch):
        """Unresolvable project_id must NEVER collapse to a shared default id."""
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        project_root = tmp_path / "noid"
        project_root.mkdir()
        # No project_id, no existing dirs → legacy project-local, no guess.
        result = vnx_paths._resolve_state_root(None, project_root)
        assert result == (project_root / ".vnx-data").resolve()

    def test_no_project_id_ignores_data_home(self, tmp_path, monkeypatch):
        """Without a project_id, VNX_DATA_HOME cannot form a per-project dir."""
        _clean_env(monkeypatch)
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "data-home"))
        project_root = tmp_path / "noid"
        project_root.mkdir()
        result = vnx_paths._resolve_state_root(None, project_root)
        assert result == (project_root / ".vnx-data").resolve()


def _init_git_repo(repo_root: Path) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_root, check=True, capture_output=True)
    (repo_root / "README.md").write_text("root\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_root, check=True, capture_output=True)


class TestGitWorktreeLayout:
    """Standalone git worktree repos keep runtime local and intelligence canonical."""

    def test_standalone_worktree_uses_local_runtime_and_canonical_intelligence(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        project_root = tmp_path / "project"
        canonical_root = project_root / ".claude" / "vnx-system"
        _init_git_repo(canonical_root)

        worktree_root = tmp_path / "vnx-system-wt-upgrade"
        subprocess.run(
            ["git", "-C", str(canonical_root), "worktree", "add", "-b", "feature/test-paths", str(worktree_root)],
            check=True,
            capture_output=True,
        )

        monkeypatch.setenv("VNX_HOME", str(worktree_root))
        paths = vnx_paths.resolve_paths()

        assert paths["VNX_HOME"] == str(worktree_root.resolve())
        assert paths["PROJECT_ROOT"] == str(worktree_root.resolve())
        assert paths["VNX_DATA_DIR"] == str((worktree_root / ".vnx-data").resolve())
        assert paths["VNX_CANONICAL_ROOT"] == str(canonical_root.resolve())
        assert paths["VNX_INTELLIGENCE_DIR"] == str((canonical_root / ".vnx-intelligence").resolve())

    def test_parent_project_root_env_is_rejected_for_standalone_worktree(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        project_root = tmp_path / "project"
        canonical_root = project_root / ".claude" / "vnx-system"
        _init_git_repo(canonical_root)

        worktree_root = tmp_path / "vnx-system-wt-upgrade"
        subprocess.run(
            ["git", "-C", str(canonical_root), "worktree", "add", "-b", "feature/test-leakage", str(worktree_root)],
            check=True,
            capture_output=True,
        )

        monkeypatch.setenv("VNX_HOME", str(worktree_root))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
        paths = vnx_paths.resolve_paths()

        assert paths["PROJECT_ROOT"] == str(worktree_root.resolve())
        assert paths["VNX_DATA_DIR"] == str((worktree_root / ".vnx-data").resolve())
        assert paths["VNX_INTELLIGENCE_DIR"] == str((canonical_root / ".vnx-intelligence").resolve())


# ---------------------------------------------------------------------------
# ensure_env populates os.environ
# ---------------------------------------------------------------------------

class TestEnsureEnv:
    """ensure_env() should set missing env vars without overwriting existing."""

    def test_populates_missing_keys(self, monkeypatch):
        _clean_env(monkeypatch)
        paths = vnx_paths.ensure_env()
        for key in paths:
            assert os.environ.get(key) == paths[key]

    def test_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        _clean_env(monkeypatch)
        custom = str(tmp_path / "custom-state")
        monkeypatch.setenv("VNX_STATE_DIR", custom)
        vnx_paths.ensure_env()
        assert os.environ["VNX_STATE_DIR"] == custom


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    """Same inputs must produce same outputs across calls."""

    def test_idempotent(self, monkeypatch):
        _clean_env(monkeypatch)
        p1 = vnx_paths.resolve_paths()
        p2 = vnx_paths.resolve_paths()
        assert p1 == p2

    def test_no_trailing_slashes(self, monkeypatch):
        _clean_env(monkeypatch)
        paths = vnx_paths.resolve_paths()
        for key, value in paths.items():
            assert not value.endswith("/"), f"{key} ends with slash: {value}"
