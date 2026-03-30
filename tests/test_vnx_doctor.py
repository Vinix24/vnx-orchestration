#!/usr/bin/env python3
"""Tests for VNX Doctor — Python-led health validator (PR-1).

Tests static installation checks (tools, paths, directories, settings,
database) and worktree detection. Runtime checks are tested via
integration with vnx_doctor_runtime.py separately.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_doctor import (
    PASS, WARN, FAIL,
    CheckResult,
    check_tools,
    check_directories,
    check_templates,
    check_settings,
    check_hooks,
    check_database,
    check_write_access,
    check_worktree,
    check_path_resolution,
    run_doctor,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vnx_env(tmp_path):
    """Create a fully initialized VNX project for doctor checks."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    vnx_home = tmp_path / "vnx-system"
    vnx_home.mkdir()
    data_dir = project_root / ".vnx-data"

    # Create full runtime layout
    for sub in ["state", "logs", "pids", "locks", "unified_reports", "database",
                "dispatches/pending", "dispatches/active", "dispatches/completed"]:
        (data_dir / sub).mkdir(parents=True)

    # Config
    (project_root / ".vnx").mkdir()
    (project_root / ".vnx" / "config.yml").write_text("project_root: test\n")

    # Templates
    (vnx_home / "templates" / "terminals").mkdir(parents=True)
    for tid in ["T0", "T1", "T2", "T3"]:
        (vnx_home / "templates" / "terminals" / f"{tid}.md").write_text(f"# {tid}\n")

    # Skills
    skills_dir = project_root / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "skills.yaml").write_text("skills: []\n")

    # Hooks
    hooks_dir = project_root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "sessionstart.sh").write_text("#!/bin/bash\n")

    # Settings
    settings = {
        "hooks": {"SessionStart": []},
        "permissions": {"allow": []},
    }
    (project_root / ".claude" / "settings.json").write_text(json.dumps(settings))

    # Database
    db_path = data_dir / "state" / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    for i in range(12):
        conn.execute(f"CREATE TABLE IF NOT EXISTS table_{i} (id INTEGER PRIMARY KEY)")
    conn.close()

    paths = {
        "PROJECT_ROOT": str(project_root),
        "VNX_HOME": str(vnx_home),
        "VNX_DATA_DIR": str(data_dir),
        "VNX_STATE_DIR": str(data_dir / "state"),
        "VNX_DISPATCH_DIR": str(data_dir / "dispatches"),
        "VNX_LOGS_DIR": str(data_dir / "logs"),
        "VNX_PIDS_DIR": str(data_dir / "pids"),
        "VNX_LOCKS_DIR": str(data_dir / "locks"),
        "VNX_REPORTS_DIR": str(data_dir / "unified_reports"),
        "VNX_DB_DIR": str(data_dir / "database"),
        "VNX_SKILLS_DIR": str(skills_dir),
    }
    return paths


# ---------------------------------------------------------------------------
# Tool checks
# ---------------------------------------------------------------------------

class TestToolChecks:
    def test_required_tools_present(self):
        results = check_tools()
        required = [r for r in results if "Required" in r.message]
        # bash and python3 should be available in test environment
        assert all(r.status == PASS for r in required)

    def test_detects_missing_tool(self):
        with patch("vnx_doctor.shutil.which", side_effect=lambda t: None if t == "bash" else "/usr/bin/" + t):
            results = check_tools()
            bash_check = [r for r in results if "bash" in r.message]
            assert bash_check[0].status == FAIL


# ---------------------------------------------------------------------------
# Directory checks
# ---------------------------------------------------------------------------

class TestDirectoryChecks:
    def test_all_dirs_present(self, vnx_env):
        results = check_directories(vnx_env)
        assert all(r.status == PASS for r in results)

    def test_missing_state_dir(self, vnx_env):
        import shutil
        shutil.rmtree(vnx_env["VNX_STATE_DIR"])
        results = check_directories(vnx_env)
        failed = [r for r in results if r.status == FAIL]
        assert len(failed) >= 1
        assert any("State" in r.message for r in failed)

    def test_missing_config(self, vnx_env):
        config = Path(vnx_env["PROJECT_ROOT"]) / ".vnx" / "config.yml"
        config.unlink()
        results = check_directories(vnx_env)
        failed = [r for r in results if r.status == FAIL]
        assert any("config" in r.message.lower() for r in failed)


# ---------------------------------------------------------------------------
# Template checks
# ---------------------------------------------------------------------------

class TestTemplateChecks:
    def test_all_templates_present(self, vnx_env):
        results = check_templates(vnx_env)
        assert all(r.status == PASS for r in results)

    def test_missing_template(self, vnx_env):
        tmpl = Path(vnx_env["VNX_HOME"]) / "templates" / "terminals" / "T0.md"
        tmpl.unlink()
        results = check_templates(vnx_env)
        failed = [r for r in results if r.status == FAIL]
        assert len(failed) >= 1


# ---------------------------------------------------------------------------
# Settings checks
# ---------------------------------------------------------------------------

class TestSettingsChecks:
    def test_valid_settings(self, vnx_env):
        results = check_settings(vnx_env)
        # Should pass JSON validity and hooks/permissions checks
        passes = [r for r in results if r.status == PASS]
        assert len(passes) >= 3  # JSON valid, hooks, permissions

    def test_missing_settings(self, vnx_env):
        settings_path = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "settings.json"
        settings_path.unlink()
        results = check_settings(vnx_env)
        assert results[0].status == FAIL
        assert "Missing" in results[0].message

    def test_invalid_json(self, vnx_env):
        settings_path = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "settings.json"
        settings_path.write_text("{invalid json")
        results = check_settings(vnx_env)
        assert results[0].status == FAIL
        assert "Invalid JSON" in results[0].message

    def test_missing_hooks_section(self, vnx_env):
        settings_path = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {}}))
        results = check_settings(vnx_env)
        hooks_check = [r for r in results if "hooks" in r.message.lower()]
        assert any(r.status == FAIL for r in hooks_check)


# ---------------------------------------------------------------------------
# Hooks checks
# ---------------------------------------------------------------------------

class TestHooksChecks:
    def test_hook_present(self, vnx_env):
        results = check_hooks(vnx_env)
        assert results[0].status == PASS

    def test_hook_missing(self, vnx_env):
        hook = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "hooks" / "sessionstart.sh"
        hook.unlink()
        results = check_hooks(vnx_env)
        assert results[0].status == FAIL


# ---------------------------------------------------------------------------
# Database checks
# ---------------------------------------------------------------------------

class TestDatabaseChecks:
    def test_healthy_db(self, vnx_env):
        results = check_database(vnx_env)
        assert results[0].status == PASS
        assert "12 tables" in results[0].message

    def test_missing_db(self, vnx_env):
        db = Path(vnx_env["VNX_STATE_DIR"]) / "quality_intelligence.db"
        db.unlink()
        results = check_database(vnx_env)
        assert results[0].status == WARN

    def test_incomplete_db(self, vnx_env):
        db_path = Path(vnx_env["VNX_STATE_DIR"]) / "quality_intelligence.db"
        db_path.unlink()
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE single_table (id INTEGER)")
        conn.close()
        results = check_database(vnx_env)
        assert results[0].status == FAIL
        assert "incomplete" in results[0].message.lower()


# ---------------------------------------------------------------------------
# Write access checks
# ---------------------------------------------------------------------------

class TestWriteAccess:
    def test_writable(self, vnx_env):
        result = check_write_access(vnx_env)
        assert result.status == PASS

    def test_non_writable(self, vnx_env):
        state_dir = Path(vnx_env["VNX_STATE_DIR"])
        state_dir.chmod(0o444)
        try:
            result = check_write_access(vnx_env)
            assert result.status == FAIL
        finally:
            state_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# Path resolution checks
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_project_root_valid(self, vnx_env):
        results = check_path_resolution(vnx_env)
        root_check = [r for r in results if "Project root" in r.message]
        assert root_check[0].status == PASS


# ---------------------------------------------------------------------------
# Full doctor flow
# ---------------------------------------------------------------------------

class TestRunDoctor:
    def test_healthy_system_passes(self, vnx_env):
        results = run_doctor(vnx_env)
        fails = [r for r in results if r.status == FAIL]
        assert len(fails) == 0, f"Unexpected failures: {[(r.name, r.message) for r in fails]}"

    def test_json_output(self, vnx_env):
        results = run_doctor(vnx_env)
        json_out = [r.to_dict() for r in results]
        serialized = json.dumps(json_out)
        parsed = json.loads(serialized)
        assert isinstance(parsed, list)
        assert all("name" in r and "status" in r for r in parsed)

    def test_broken_system_reports_failures(self, vnx_env):
        # Remove state dir
        import shutil
        shutil.rmtree(vnx_env["VNX_STATE_DIR"])
        results = run_doctor(vnx_env)
        fails = [r for r in results if r.status == FAIL]
        assert len(fails) >= 1

    def test_doctor_does_not_crash_on_partial_setup(self, tmp_path):
        """Doctor should report failures, not crash, on a bare project."""
        paths = {
            "PROJECT_ROOT": str(tmp_path),
            "VNX_HOME": str(tmp_path / "nonexistent-vnx"),
            "VNX_DATA_DIR": str(tmp_path / ".vnx-data"),
            "VNX_STATE_DIR": str(tmp_path / ".vnx-data" / "state"),
            "VNX_DISPATCH_DIR": str(tmp_path / ".vnx-data" / "dispatches"),
            "VNX_LOGS_DIR": str(tmp_path / ".vnx-data" / "logs"),
            "VNX_PIDS_DIR": str(tmp_path / ".vnx-data" / "pids"),
            "VNX_LOCKS_DIR": str(tmp_path / ".vnx-data" / "locks"),
            "VNX_REPORTS_DIR": str(tmp_path / ".vnx-data" / "unified_reports"),
            "VNX_DB_DIR": str(tmp_path / ".vnx-data" / "database"),
            "VNX_SKILLS_DIR": str(tmp_path / ".claude" / "skills"),
        }
        # Should not raise
        results = run_doctor(paths)
        fails = [r for r in results if r.status == FAIL]
        assert len(fails) >= 1  # Many things should fail


# ---------------------------------------------------------------------------
# Worktree detection tests
# ---------------------------------------------------------------------------

class TestWorktreeChecks:
    def test_no_worktree_returns_empty(self, vnx_env):
        with patch("vnx_doctor.detect_worktree", return_value=(False, None, None)):
            results = check_worktree(vnx_env)
            assert results == []

    def test_worktree_with_snapshot(self, vnx_env, tmp_path):
        wt_root = tmp_path / "wt"
        wt_root.mkdir()
        wt_data = wt_root / ".vnx-data"
        wt_data.mkdir()
        (wt_data / ".snapshot_meta").write_text(
            "snapshot_date=2026-03-29T10:00:00Z\nsource_dir=/main\n"
        )
        (wt_data / ".env_override").write_text("VNX_DATA_DIR=/wt/.vnx-data\n")

        with patch("vnx_doctor.detect_worktree",
                    return_value=(True, str(wt_root), str(tmp_path / "main"))):
            results = check_worktree(vnx_env)
            assert len(results) >= 2
            assert results[0].status == PASS  # "Running in worktree"

    def test_worktree_missing_env_override(self, vnx_env, tmp_path):
        wt_root = tmp_path / "wt"
        wt_root.mkdir()
        wt_data = wt_root / ".vnx-data"
        wt_data.mkdir()
        (wt_data / ".snapshot_meta").write_text(
            "snapshot_date=2026-03-29T10:00:00Z\nsource_dir=/main\n"
        )

        with patch("vnx_doctor.detect_worktree",
                    return_value=(True, str(wt_root), str(tmp_path / "main"))):
            results = check_worktree(vnx_env)
            warns = [r for r in results if r.status == WARN]
            assert any(".env_override" in r.message for r in warns)
