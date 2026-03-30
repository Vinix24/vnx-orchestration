#!/usr/bin/env python3
"""Tests for VNX Install Validator — prerequisite and installation checks (PR-4).

Tests prerequisite detection, layout detection, installation validation,
path sanity checks, and invocation mode documentation.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_install import (
    PASS, WARN, FAIL,
    CheckResult,
    check_prerequisites,
    detect_layout,
    check_layout,
    validate_installation,
    check_path_sanity,
    run_checks,
    _version_ge,
    INVOCATION_MODES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vnx_project(tmp_path):
    """Create a minimal VNX project for layout/install tests."""
    project = tmp_path / "project"
    project.mkdir()

    vnx_home = project / ".vnx"
    (vnx_home / "bin").mkdir(parents=True)
    vnx_bin = vnx_home / "bin" / "vnx"
    vnx_bin.write_text("#!/bin/bash\necho vnx\n")
    vnx_bin.chmod(0o755)

    (vnx_home / "scripts" / "lib").mkdir(parents=True)
    (vnx_home / "scripts" / "vnx_init.py").write_text("# init\n")
    (vnx_home / "scripts" / "vnx_doctor.py").write_text("# doctor\n")
    (vnx_home / "scripts" / "lib" / "vnx_paths.py").write_text("# paths\n")
    (vnx_home / "scripts" / "lib" / "vnx_paths.sh").write_text("# paths\n")
    (vnx_home / "scripts" / "lib" / "vnx_mode.py").write_text("# mode\n")
    (vnx_home / "templates" / "terminals").mkdir(parents=True)
    for tid in ["T0", "T1", "T2", "T3"]:
        (vnx_home / "templates" / "terminals" / f"{tid}.md").write_text(f"# {tid}\n")

    (vnx_home / ".layout").write_text("vnx")

    return project


@pytest.fixture
def vnx_project_claude(tmp_path):
    """Create a project with .claude/vnx-system/ layout."""
    project = tmp_path / "project"
    project.mkdir()

    vnx_home = project / ".claude" / "vnx-system"
    (vnx_home / "bin").mkdir(parents=True)
    vnx_bin = vnx_home / "bin" / "vnx"
    vnx_bin.write_text("#!/bin/bash\necho vnx\n")
    vnx_bin.chmod(0o755)

    return project


@pytest.fixture
def vnx_project_initialized(vnx_project):
    """A VNX project that has been initialized (has .vnx-data)."""
    data_dir = vnx_project / ".vnx-data"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "dispatches" / "pending").mkdir(parents=True)
    (data_dir / "dispatches" / "active").mkdir(parents=True)
    (data_dir / "dispatches" / "completed").mkdir(parents=True)
    (data_dir / "logs").mkdir(parents=True)
    (data_dir / "pids").mkdir(parents=True)
    (data_dir / "locks").mkdir(parents=True)
    (data_dir / "unified_reports").mkdir(parents=True)

    mode_data = {"mode": "starter", "set_at": "2026-03-29T00:00:00Z", "schema_version": 1}
    (data_dir / "mode.json").write_text(json.dumps(mode_data))

    return vnx_project


# ---------------------------------------------------------------------------
# Version comparison tests
# ---------------------------------------------------------------------------

class TestVersionGe:
    def test_equal_versions(self):
        assert _version_ge("3.11.0", "3.11.0") is True

    def test_greater_major(self):
        assert _version_ge("4.0.0", "3.11.0") is True

    def test_greater_minor(self):
        assert _version_ge("3.12.0", "3.11.0") is True

    def test_greater_patch(self):
        assert _version_ge("3.11.1", "3.11.0") is True

    def test_less_than(self):
        assert _version_ge("3.8.0", "3.9.0") is False

    def test_short_version(self):
        assert _version_ge("3.11", "3.9.0") is True

    def test_unparseable_returns_true(self):
        assert _version_ge("unknown", "3.9.0") is True


# ---------------------------------------------------------------------------
# Layout detection tests
# ---------------------------------------------------------------------------

class TestDetectLayout:
    def test_vnx_layout(self, vnx_project):
        layout, vnx_home = detect_layout(vnx_project)
        assert layout == "vnx"
        assert vnx_home == vnx_project / ".vnx"

    def test_claude_layout(self, vnx_project_claude):
        layout, vnx_home = detect_layout(vnx_project_claude)
        assert layout == "claude"
        assert vnx_home == vnx_project_claude / ".claude" / "vnx-system"

    def test_no_layout(self, tmp_path):
        layout, vnx_home = detect_layout(tmp_path)
        assert layout is None
        assert vnx_home is None

    def test_vnx_preferred_over_claude(self, tmp_path):
        """If both layouts exist, .vnx/ wins (checked first)."""
        project = tmp_path / "both"
        project.mkdir()
        (project / ".vnx" / "bin").mkdir(parents=True)
        (project / ".vnx" / "bin" / "vnx").write_text("#!/bin/bash\n")
        (project / ".claude" / "vnx-system" / "bin").mkdir(parents=True)
        (project / ".claude" / "vnx-system" / "bin" / "vnx").write_text("#!/bin/bash\n")

        layout, _ = detect_layout(project)
        assert layout == "vnx"


# ---------------------------------------------------------------------------
# Layout check tests
# ---------------------------------------------------------------------------

class TestCheckLayout:
    def test_layout_found(self, vnx_project):
        results = check_layout(vnx_project)
        statuses = [r.status for r in results]
        assert PASS in statuses
        assert FAIL not in statuses

    def test_layout_not_found(self, tmp_path):
        results = check_layout(tmp_path)
        assert results[0].status == FAIL
        assert "No VNX installation" in results[0].message

    def test_layout_marker_matches(self, vnx_project):
        results = check_layout(vnx_project)
        # Should have layout PASS and marker PASS
        names = [r.name for r in results]
        assert "layout" in names
        assert "layout-marker" in names
        marker_result = [r for r in results if r.name == "layout-marker"][0]
        assert marker_result.status == PASS


# ---------------------------------------------------------------------------
# Installation validation tests
# ---------------------------------------------------------------------------

class TestValidateInstallation:
    def test_validates_complete_install(self, vnx_project):
        results = validate_installation(vnx_project)
        fails = [r for r in results if r.status == FAIL]
        assert len(fails) == 0, f"Unexpected failures: {[f.message for f in fails]}"

    def test_detects_missing_scripts(self, vnx_project):
        (vnx_project / ".vnx" / "scripts" / "vnx_init.py").unlink()
        results = validate_installation(vnx_project)
        fail_names = [r.name for r in results if r.status == FAIL]
        assert any("vnx_init" in n for n in fail_names)

    def test_no_install_found(self, tmp_path):
        results = validate_installation(tmp_path)
        assert results[0].status == FAIL
        assert "No VNX installation" in results[0].message

    def test_runtime_data_warning_before_init(self, vnx_project):
        results = validate_installation(vnx_project)
        runtime_results = [r for r in results if r.name == "runtime-data"]
        assert runtime_results[0].status == WARN
        assert "not yet initialized" in runtime_results[0].message

    def test_runtime_data_pass_after_init(self, vnx_project_initialized):
        results = validate_installation(vnx_project_initialized)
        runtime_results = [r for r in results if r.name == "runtime-data"]
        assert runtime_results[0].status == PASS

    def test_mode_detected(self, vnx_project_initialized):
        results = validate_installation(vnx_project_initialized)
        mode_results = [r for r in results if r.name == "mode"]
        assert mode_results[0].status == PASS
        assert "starter" in mode_results[0].message

    def test_executable_check(self, vnx_project):
        results = validate_installation(vnx_project)
        exec_results = [r for r in results if r.name == "executable"]
        assert exec_results[0].status == PASS


# ---------------------------------------------------------------------------
# Path sanity tests
# ---------------------------------------------------------------------------

class TestPathSanity:
    def test_normal_path(self, tmp_path):
        results = check_path_sanity(tmp_path)
        fails = [r for r in results if r.status == FAIL]
        assert len(fails) == 0

    def test_write_access(self, tmp_path):
        results = check_path_sanity(tmp_path)
        access_results = [r for r in results if r.name == "write-access"]
        assert access_results[0].status == PASS


# ---------------------------------------------------------------------------
# Prerequisites tests
# ---------------------------------------------------------------------------

class TestPrerequisites:
    def test_finds_python(self):
        """python3 should always be found in test environment."""
        results = check_prerequisites()
        python_results = [r for r in results if r.name == "python3"]
        assert python_results[0].status == PASS

    def test_finds_bash(self):
        results = check_prerequisites()
        bash_results = [r for r in results if r.name == "bash"]
        # macOS ships bash 3.2 which meets minimum; should pass
        assert bash_results[0].status == PASS

    def test_finds_git(self):
        results = check_prerequisites()
        git_results = [r for r in results if r.name == "git"]
        assert git_results[0].status == PASS

    @patch("vnx_install.shutil.which")
    def test_missing_required_tool(self, mock_which):
        mock_which.return_value = None
        results = check_prerequisites()
        fails = [r for r in results if r.status == FAIL]
        assert len(fails) > 0


# ---------------------------------------------------------------------------
# Run checks integration tests
# ---------------------------------------------------------------------------

class TestRunChecks:
    def test_prereqs_only(self):
        results = run_checks(prereqs_only=True)
        categories = {r.category for r in results}
        assert categories == {"prereq"}

    def test_full_check_with_project(self, vnx_project):
        results = run_checks(project_root=vnx_project, validate=True)
        categories = {r.category for r in results}
        assert "prereq" in categories
        assert "path" in categories
        assert "install" in categories


# ---------------------------------------------------------------------------
# CheckResult model tests
# ---------------------------------------------------------------------------

class TestCheckResult:
    def test_to_dict_minimal(self):
        r = CheckResult("test", "prereq", PASS, "OK")
        d = r.to_dict()
        assert d["name"] == "test"
        assert d["status"] == "pass"
        assert "remediation" not in d
        assert "details" not in d

    def test_to_dict_full(self):
        r = CheckResult("test", "prereq", FAIL, "bad", "fix it", ["detail1"])
        d = r.to_dict()
        assert d["remediation"] == "fix it"
        assert d["details"] == ["detail1"]


# ---------------------------------------------------------------------------
# Invocation modes documentation test
# ---------------------------------------------------------------------------

class TestInvocationModes:
    def test_documentation_exists(self):
        assert "vnx install-shell-helper" in INVOCATION_MODES
        assert ".vnx/bin/vnx" in INVOCATION_MODES
        assert "vnx setup" in INVOCATION_MODES
