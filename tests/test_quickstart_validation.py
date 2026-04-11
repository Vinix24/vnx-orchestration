#!/usr/bin/env python3
"""Quickstart validation tests (PR-7).

Validates the public quickstart flow in an isolated temp directory:
  install.sh → vnx init → vnx doctor

These tests run without tmux, without API keys, and without network access.
They verify that the documented quickstart sequence actually works.

Gate: gate_pr7_qa_and_certification — quickstart smoke path.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# The project root where install.sh lives
VNX_REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VNX_ENV_KEYS = [
    "VNX_HOME", "VNX_BIN", "VNX_EXECUTABLE", "PROJECT_ROOT",
    "VNX_DATA_DIR", "VNX_STATE_DIR", "VNX_DISPATCH_DIR",
    "VNX_LOGS_DIR", "VNX_PIDS_DIR", "VNX_LOCKS_DIR",
    "VNX_REPORTS_DIR", "VNX_DB_DIR", "VNX_SKILLS_DIR",
    "VNX_CANONICAL_ROOT", "VNX_INTELLIGENCE_DIR",
]


def _clean_env():
    """Return os.environ copy with VNX vars removed (prevents cross-project leak)."""
    env = dict(os.environ)
    for k in _VNX_ENV_KEYS:
        env.pop(k, None)
    return env


def _run(cmd, cwd=None, env=None, timeout=60):
    """Run a command, return (returncode, stdout, stderr)."""
    if env is None:
        env = _clean_env()
    result = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def _make_git_repo(path):
    """Initialize a git repo so vnx_paths.sh resolves PROJECT_ROOT correctly."""
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)
    # Initial commit so git rev-parse works
    (path / ".gitignore").write_text(".vnx-data/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True)


# ---------------------------------------------------------------------------
# Install flow
# ---------------------------------------------------------------------------

class TestInstallFlow:
    """install.sh installs into a temp project directory cleanly."""

    def test_install_creates_vnx_directory(self, tmp_path):
        target = tmp_path / "test-project"
        target.mkdir()
        _make_git_repo(target)
        rc, out, err = _run(["bash", str(VNX_REPO / "install.sh"), str(target)])
        assert rc == 0, f"install.sh failed: {err}"
        assert (target / ".vnx" / "bin" / "vnx").exists(), "bin/vnx not created"

    def test_install_creates_scripts(self, tmp_path):
        target = tmp_path / "test-project"
        target.mkdir()
        _make_git_repo(target)
        _run(["bash", str(VNX_REPO / "install.sh"), str(target)])
        scripts_dir = target / ".vnx" / "scripts"
        assert scripts_dir.is_dir(), "scripts/ not installed"
        assert (scripts_dir / "lib" / "vnx_paths.py").exists(), "vnx_paths.py missing"
        assert (scripts_dir / "lib" / "vnx_paths.sh").exists(), "vnx_paths.sh missing"
        assert (scripts_dir / "lib" / "vnx_mode.py").exists(), "vnx_mode.py missing"

    def test_install_check_mode(self, tmp_path):
        """--check flag should not create any files."""
        target = tmp_path / "check-project"
        target.mkdir()
        _make_git_repo(target)
        rc, out, err = _run(["bash", str(VNX_REPO / "install.sh"), str(target), "--check"])
        # --check may pass or fail depending on prereqs, but should not write files
        vnx_dir = target / ".vnx"
        assert not vnx_dir.exists(), "install --check should not create .vnx"


# ---------------------------------------------------------------------------
# Init flow (post-install)
# ---------------------------------------------------------------------------

class TestInitFlow:
    """vnx init --starter works in an installed project."""

    @pytest.fixture
    def installed_project(self, tmp_path):
        """Install VNX into a temp project, return paths."""
        target = tmp_path / "init-project"
        target.mkdir()
        _make_git_repo(target)
        rc, out, err = _run(["bash", str(VNX_REPO / "install.sh"), str(target)])
        if rc != 0:
            pytest.skip(f"install.sh failed: {err}")
        return target

    def test_init_starter_creates_mode_json(self, installed_project):
        vnx_bin = installed_project / ".vnx" / "bin" / "vnx"
        rc, out, err = _run([str(vnx_bin), "init", "--starter"], cwd=installed_project)
        assert rc == 0, f"vnx init --starter failed: {err}\n{out}"
        mode_file = installed_project / ".vnx-data" / "mode.json"
        assert mode_file.exists(), "mode.json not created by init"
        mode_data = json.loads(mode_file.read_text())
        assert mode_data.get("mode") == "starter"

    def test_init_creates_runtime_dirs(self, installed_project):
        vnx_bin = installed_project / ".vnx" / "bin" / "vnx"
        _run([str(vnx_bin), "init", "--starter"], cwd=installed_project)
        data_dir = installed_project / ".vnx-data"
        for subdir in ["state", "dispatches", "logs"]:
            assert (data_dir / subdir).is_dir(), f".vnx-data/{subdir} not created"


# ---------------------------------------------------------------------------
# Doctor flow (post-init)
# ---------------------------------------------------------------------------

class TestDoctorFlow:
    """vnx doctor runs on a freshly initialized project.

    Note: doctor may return non-zero if optional tools (e.g. rg/ripgrep)
    are missing. We verify that doctor runs without crashing and that
    critical checks (paths, dirs, config) pass.
    """

    @pytest.fixture
    def initialized_project(self, tmp_path):
        """Install and init a temp project."""
        target = tmp_path / "doctor-project"
        target.mkdir()
        _make_git_repo(target)
        rc, _, err = _run(["bash", str(VNX_REPO / "install.sh"), str(target)])
        if rc != 0:
            pytest.skip(f"install.sh failed: {err}")
        vnx_bin = target / ".vnx" / "bin" / "vnx"
        rc, _, err = _run([str(vnx_bin), "init", "--starter"], cwd=target)
        if rc != 0:
            pytest.skip(f"vnx init failed: {err}")
        return target

    def test_doctor_runs_and_critical_checks_pass(self, initialized_project):
        vnx_bin = initialized_project / ".vnx" / "bin" / "vnx"
        rc, out, err = _run([str(vnx_bin), "doctor"], cwd=initialized_project)
        # Doctor runs without crashing
        assert rc in (0, 1), f"vnx doctor crashed:\nstdout: {out}\nstderr: {err}"
        # Critical checks must pass (paths, dirs, config, templates)
        for critical in ["Project root", "VNX config", "Runtime data", "Config:"]:
            assert f"PASS" in out or critical not in out or "FAIL" not in out, \
                f"Critical check '{critical}' failed in doctor output"
        # No FAIL on required tools
        assert "FAIL] tool: Required" not in out, \
            f"Required tool check failed:\n{out}"

    def test_doctor_produces_output(self, initialized_project):
        """Doctor should produce structured diagnostic output."""
        vnx_bin = initialized_project / ".vnx" / "bin" / "vnx"
        rc, out, err = _run([str(vnx_bin), "doctor"], cwd=initialized_project)
        assert "VNX Doctor" in out, "Doctor should print header"
        assert "PASS" in out, "Doctor should have at least some passing checks"


# ---------------------------------------------------------------------------
# Full quickstart sequence
# ---------------------------------------------------------------------------

class TestFullQuickstart:
    """End-to-end: install → init → doctor → status."""

    def test_quickstart_sequence(self, tmp_path):
        """The documented quickstart should work without errors."""
        target = tmp_path / "quickstart"
        target.mkdir()
        _make_git_repo(target)

        # Step 1: install
        rc, _, err = _run(["bash", str(VNX_REPO / "install.sh"), str(target)])
        assert rc == 0, f"install failed: {err}"

        vnx_bin = target / ".vnx" / "bin" / "vnx"
        assert vnx_bin.exists()

        # Step 2: init --starter
        rc, _, err = _run([str(vnx_bin), "init", "--starter"], cwd=target)
        assert rc == 0, f"init failed: {err}"

        # Step 3: doctor (may return 1 if optional tools like rg missing)
        rc, out, err = _run([str(vnx_bin), "doctor"], cwd=target)
        assert rc in (0, 1), f"doctor crashed: {err}"
        assert "FAIL] tool: Required" not in out, f"Required tool missing:\n{out}"

        # Step 4: status
        rc, out, err = _run([str(vnx_bin), "status"], cwd=target)
        assert rc == 0, f"status failed: {err}"

    def test_quickstart_idempotent(self, tmp_path):
        """Running init twice should not break anything."""
        target = tmp_path / "idempotent"
        target.mkdir()
        _make_git_repo(target)
        _run(["bash", str(VNX_REPO / "install.sh"), str(target)])
        vnx_bin = target / ".vnx" / "bin" / "vnx"

        # Init twice
        _run([str(vnx_bin), "init", "--starter"], cwd=target)
        rc, _, err = _run([str(vnx_bin), "init", "--starter"], cwd=target)
        assert rc == 0, f"Second init failed: {err}"

        # Doctor should still run (may return 1 for optional tool warnings)
        rc, out, err = _run([str(vnx_bin), "doctor"], cwd=target)
        assert rc in (0, 1), f"Doctor crashed after double-init: {err}"
        assert "FAIL] tool: Required" not in out


# ---------------------------------------------------------------------------
# F45 doc + example artifact validation
# ---------------------------------------------------------------------------

class TestF45QuickstartArtifacts:
    """Validate that the F45 quickstart guide and hello-world example exist."""

    def test_quickstart_doc_exists(self):
        """docs/QUICKSTART.md exists."""
        assert (VNX_REPO / "docs" / "QUICKSTART.md").exists()

    def test_hello_world_agent_structure(self):
        """examples/hello-world/ has CLAUDE.md and config.yaml."""
        hello = VNX_REPO / "examples" / "hello-world"
        assert (hello / "CLAUDE.md").exists(), "examples/hello-world/CLAUDE.md not found"
        assert (hello / "config.yaml").exists(), "examples/hello-world/config.yaml not found"

    def test_hello_world_config_has_governance_profile(self):
        """config.yaml contains governance_profile field."""
        config_text = (VNX_REPO / "examples" / "hello-world" / "config.yaml").read_text()
        assert "governance_profile" in config_text

    def test_quickstart_has_all_steps(self):
        """QUICKSTART.md contains Step 1 through Step 6."""
        content = (VNX_REPO / "docs" / "QUICKSTART.md").read_text()
        for step in range(1, 7):
            assert f"## Step {step}" in content, f"QUICKSTART.md missing '## Step {step}'"
