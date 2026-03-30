#!/usr/bin/env python3
"""Tests for VNX Init — Python-led init/bootstrap orchestrator (PR-1).

Tests the unified init flow: runtime layout, config, skills, terminals,
hooks, database init, and intelligence import. Validates both main-repo
and worktree path handling.
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

from vnx_init import (
    PASS, SKIP, FAIL,
    StepResult,
    ensure_runtime_layout,
    write_profiles,
    write_config,
    bootstrap_skills,
    bootstrap_terminals,
    init_db,
    intelligence_import,
    run_init,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vnx_env(tmp_path):
    """Create a minimal VNX project structure with paths dict."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    vnx_home = tmp_path / "vnx-system"
    vnx_home.mkdir()
    data_dir = project_root / ".vnx-data"

    # Create VNX_HOME structure
    (vnx_home / "skills").mkdir()
    (vnx_home / "skills" / "skills.yaml").write_text("skills: []\n")
    (vnx_home / "skills" / "test-skill.md").write_text("# Test skill\n")
    (vnx_home / "templates" / "terminals").mkdir(parents=True)
    for tid in ["T0", "T1", "T2", "T3"]:
        (vnx_home / "templates" / "terminals" / f"{tid}.md").write_text(f"# {tid}\n")
    (vnx_home / "hooks").mkdir()
    (vnx_home / "hooks" / "sessionstart.sh").write_text("#!/bin/bash\necho ok\n")
    (vnx_home / "schemas").mkdir()
    (vnx_home / "schemas" / "quality_intelligence.sql").write_text(
        "CREATE TABLE IF NOT EXISTS test_table (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE IF NOT EXISTS t2 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t3 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t4 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t5 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t6 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t7 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t8 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t9 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t10 (id INTEGER);\n"
        "CREATE TABLE IF NOT EXISTS t11 (id INTEGER);\n"
    )

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
        "VNX_CANONICAL_ROOT": str(vnx_home),
        "VNX_SKILLS_DIR": str(project_root / ".claude" / "skills"),
        "VNX_INTELLIGENCE_DIR": str(vnx_home / ".vnx-intelligence"),
    }
    return paths


# ---------------------------------------------------------------------------
# Runtime layout tests
# ---------------------------------------------------------------------------

class TestEnsureRuntimeLayout:
    def test_creates_all_directories(self, vnx_env):
        result = ensure_runtime_layout(vnx_env)
        assert result.status == PASS

        for key in ["VNX_DATA_DIR", "VNX_STATE_DIR", "VNX_LOGS_DIR",
                     "VNX_PIDS_DIR", "VNX_LOCKS_DIR", "VNX_REPORTS_DIR", "VNX_DB_DIR"]:
            assert Path(vnx_env[key]).is_dir(), f"{key} should exist"

        dispatch_dir = Path(vnx_env["VNX_DISPATCH_DIR"])
        for sub in ["pending", "active", "completed", "rejected", "failed"]:
            assert (dispatch_dir / sub).is_dir()

    def test_idempotent(self, vnx_env):
        ensure_runtime_layout(vnx_env)
        result = ensure_runtime_layout(vnx_env)
        assert result.status == SKIP


# ---------------------------------------------------------------------------
# Profiles tests
# ---------------------------------------------------------------------------

class TestWriteProfiles:
    def test_creates_profiles(self, vnx_env):
        ensure_runtime_layout(vnx_env)
        result = write_profiles(vnx_env)
        assert result.status == PASS

        profiles_dir = Path(vnx_env["VNX_DATA_DIR"]) / "profiles"
        assert (profiles_dir / "claude-only.env").exists()
        assert (profiles_dir / "claude-codex.env").exists()
        assert (profiles_dir / "claude-gemini.env").exists()
        assert (profiles_dir / "full-multi.env").exists()

    def test_idempotent(self, vnx_env):
        ensure_runtime_layout(vnx_env)
        write_profiles(vnx_env)
        result = write_profiles(vnx_env)
        assert result.status == SKIP


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestWriteConfig:
    def test_creates_config(self, vnx_env):
        result = write_config(vnx_env)
        assert result.status == PASS
        config = Path(vnx_env["PROJECT_ROOT"]) / ".vnx" / "config.yml"
        assert config.exists()
        content = config.read_text()
        assert vnx_env["PROJECT_ROOT"] in content
        assert vnx_env["VNX_HOME"] in content

    def test_skips_existing(self, vnx_env):
        write_config(vnx_env)
        result = write_config(vnx_env)
        assert result.status == SKIP


# ---------------------------------------------------------------------------
# Skills tests
# ---------------------------------------------------------------------------

class TestBootstrapSkills:
    def test_copies_skills(self, vnx_env):
        result = bootstrap_skills(vnx_env)
        assert result.status == PASS

        skills_dir = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "skills"
        assert skills_dir.is_dir()
        assert (skills_dir / "skills.yaml").exists()
        assert (skills_dir / "test-skill.md").exists()

    def test_skips_existing_dir(self, vnx_env):
        skills_dir = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        result = bootstrap_skills(vnx_env)
        assert result.status == SKIP

    def test_removes_stale_symlink(self, vnx_env):
        skills_dir = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "skills"
        skills_dir.parent.mkdir(parents=True, exist_ok=True)
        skills_dir.symlink_to("/nonexistent")
        result = bootstrap_skills(vnx_env)
        assert result.status == PASS
        assert not skills_dir.is_symlink()

    def test_fails_on_missing_shipped(self, vnx_env):
        import shutil
        shutil.rmtree(str(Path(vnx_env["VNX_HOME"]) / "skills"))
        result = bootstrap_skills(vnx_env)
        assert result.status == FAIL

    def test_worktree_bootstrap_stays_local(self, vnx_env, tmp_path):
        canonical_root = Path(vnx_env["VNX_HOME"])
        worktree_root = tmp_path / "worktree-project"
        worktree_root.mkdir()

        wt_paths = dict(vnx_env)
        wt_paths["PROJECT_ROOT"] = str(worktree_root)
        wt_paths["VNX_DATA_DIR"] = str(worktree_root / ".vnx-data")
        wt_paths["VNX_STATE_DIR"] = str(worktree_root / ".vnx-data" / "state")
        wt_paths["VNX_DISPATCH_DIR"] = str(worktree_root / ".vnx-data" / "dispatches")
        wt_paths["VNX_LOGS_DIR"] = str(worktree_root / ".vnx-data" / "logs")
        wt_paths["VNX_PIDS_DIR"] = str(worktree_root / ".vnx-data" / "pids")
        wt_paths["VNX_LOCKS_DIR"] = str(worktree_root / ".vnx-data" / "locks")
        wt_paths["VNX_REPORTS_DIR"] = str(worktree_root / ".vnx-data" / "unified_reports")
        wt_paths["VNX_DB_DIR"] = str(worktree_root / ".vnx-data" / "database")
        wt_paths["VNX_SKILLS_DIR"] = str(worktree_root / ".claude" / "skills")

        result = bootstrap_skills(wt_paths)

        assert result.status == PASS
        assert (worktree_root / ".claude" / "skills" / "skills.yaml").exists()
        assert not (canonical_root / ".claude" / "skills").exists()


# ---------------------------------------------------------------------------
# Terminals tests
# ---------------------------------------------------------------------------

class TestBootstrapTerminals:
    def test_creates_terminals(self, vnx_env):
        result = bootstrap_terminals(vnx_env)
        assert result.status == PASS

        terminals_dir = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "terminals"
        for tid in ["T0", "T1", "T2", "T3"]:
            assert (terminals_dir / tid / "CLAUDE.md").exists()
            assert (terminals_dir / tid / ".mcp.json").exists()

    def test_skips_existing(self, vnx_env):
        bootstrap_terminals(vnx_env)
        result = bootstrap_terminals(vnx_env)
        assert result.status == SKIP

    def test_force_overwrites(self, vnx_env):
        bootstrap_terminals(vnx_env)
        # Modify a file
        t0_md = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "terminals" / "T0" / "CLAUDE.md"
        t0_md.write_text("modified")

        result = bootstrap_terminals(vnx_env, force=True)
        assert result.status == PASS
        assert t0_md.read_text() == "# T0\n"

    def test_custom_terminal_ids(self, vnx_env):
        result = bootstrap_terminals(vnx_env, terminal_ids=["T0", "T2"])
        assert result.status == PASS
        terminals_dir = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "terminals"
        assert (terminals_dir / "T0" / "CLAUDE.md").exists()
        assert (terminals_dir / "T2" / "CLAUDE.md").exists()
        assert not (terminals_dir / "T1" / "CLAUDE.md").exists()

    def test_mcp_json_created(self, vnx_env):
        bootstrap_terminals(vnx_env)

        mcp_file = Path(vnx_env["PROJECT_ROOT"]) / ".claude" / "terminals" / "T0" / ".mcp.json"
        assert mcp_file.exists()
        mcp_data = json.loads(mcp_file.read_text())
        assert "mcpServers" in mcp_data


# ---------------------------------------------------------------------------
# Init-DB tests
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_db(self, vnx_env):
        ensure_runtime_layout(vnx_env)
        result = init_db(vnx_env)
        assert result.status == PASS

        db_path = Path(vnx_env["VNX_STATE_DIR"]) / "quality_intelligence.db"
        assert db_path.exists()

    def test_skips_missing_schema(self, vnx_env):
        ensure_runtime_layout(vnx_env)
        schema = Path(vnx_env["VNX_HOME"]) / "schemas" / "quality_intelligence.sql"
        schema.unlink()
        result = init_db(vnx_env)
        assert result.status == SKIP


# ---------------------------------------------------------------------------
# Intelligence import tests
# ---------------------------------------------------------------------------

class TestIntelligenceImport:
    def test_skips_when_no_export_dir(self, vnx_env):
        result = intelligence_import(vnx_env)
        assert result.status == SKIP

    def test_skips_when_no_import_script(self, vnx_env):
        intel_dir = Path(vnx_env["VNX_INTELLIGENCE_DIR"]) / "db_export"
        intel_dir.mkdir(parents=True)
        result = intelligence_import(vnx_env)
        assert result.status == SKIP


# ---------------------------------------------------------------------------
# Full init flow tests
# ---------------------------------------------------------------------------

class TestRunInit:
    def test_full_init_flow(self, vnx_env):
        results = run_init(vnx_env, skip_hooks=True)

        # All steps should succeed or skip (not fail)
        for r in results:
            assert r.status in (PASS, SKIP), f"{r.name} failed: {r.message}"

        # Verify key artifacts exist
        project_root = Path(vnx_env["PROJECT_ROOT"])
        assert (project_root / ".vnx" / "config.yml").exists()
        assert (project_root / ".claude" / "skills" / "skills.yaml").exists()
        assert (project_root / ".claude" / "terminals" / "T0" / "CLAUDE.md").exists()
        assert Path(vnx_env["VNX_STATE_DIR"]).is_dir()

    def test_full_init_idempotent(self, vnx_env):
        run_init(vnx_env, skip_hooks=True)
        results = run_init(vnx_env, skip_hooks=True)

        # Second run should mostly skip
        skip_count = sum(1 for r in results if r.status == SKIP)
        assert skip_count >= 4, "Second run should skip most steps"

    def test_returns_failures_without_crashing(self, vnx_env):
        # Remove shipped skills to cause a failure
        import shutil
        shutil.rmtree(str(Path(vnx_env["VNX_HOME"]) / "skills"))

        results = run_init(vnx_env, skip_hooks=True)
        failed = [r for r in results if r.status == FAIL]
        assert len(failed) >= 1
        assert any("skills" in r.name.lower() or "skills" in r.message.lower()
                    for r in failed)


# ---------------------------------------------------------------------------
# Path handling tests (worktree simulation)
# ---------------------------------------------------------------------------

class TestWorktreePaths:
    def test_init_works_with_different_project_root(self, vnx_env, tmp_path):
        """Simulate a worktree by using a different project root."""
        wt_root = tmp_path / "worktree-project"
        wt_root.mkdir()
        wt_data = wt_root / ".vnx-data"

        wt_paths = dict(vnx_env)
        wt_paths["PROJECT_ROOT"] = str(wt_root)
        wt_paths["VNX_DATA_DIR"] = str(wt_data)
        wt_paths["VNX_STATE_DIR"] = str(wt_data / "state")
        wt_paths["VNX_DISPATCH_DIR"] = str(wt_data / "dispatches")
        wt_paths["VNX_LOGS_DIR"] = str(wt_data / "logs")
        wt_paths["VNX_PIDS_DIR"] = str(wt_data / "pids")
        wt_paths["VNX_LOCKS_DIR"] = str(wt_data / "locks")
        wt_paths["VNX_REPORTS_DIR"] = str(wt_data / "unified_reports")
        wt_paths["VNX_DB_DIR"] = str(wt_data / "database")
        wt_paths["VNX_SKILLS_DIR"] = str(wt_root / ".claude" / "skills")

        results = run_init(wt_paths, skip_hooks=True)

        # Key directories should be created under worktree root
        assert wt_data.is_dir()
        assert (wt_data / "state").is_dir()
        assert (wt_root / ".claude" / "terminals" / "T0" / "CLAUDE.md").exists()

        for r in results:
            assert r.status in (PASS, SKIP), f"{r.name} failed: {r.message}"
