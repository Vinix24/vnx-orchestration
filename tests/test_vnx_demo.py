#!/usr/bin/env python3
"""Tests for VNX Demo Mode — temp-backed demo orchestrator (PR-2).

Validates demo environment creation, evidence seeding, scenario listing,
and cleanup. Does not test actual replay script execution (those are
bash scripts tested by running them).
"""

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_demo import (
    DemoEnvironment,
    AVAILABLE_SCENARIOS,
    DEFAULT_SCENARIO,
    list_scenarios,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vnx_env(tmp_path):
    """Set up VNX environment with demo directory structure."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    vnx_home = tmp_path / "vnx-system"
    vnx_home.mkdir()

    # Create demo directory structure matching real repo
    demo_dir = vnx_home / "demo"
    for scenario_name, info in AVAILABLE_SCENARIOS.items():
        subdir = demo_dir / info["subdir"]
        evidence_dir = subdir / "evidence"
        evidence_dir.mkdir(parents=True)
        (subdir / info["script"]).write_text("#!/bin/bash\necho replay\n")
        # Create sample evidence
        (evidence_dir / "receipts.ndjson").write_text(
            '{"dispatch_id":"d1","status":"success"}\n'
        )
        (evidence_dir / "pr_queue_state.json").write_text('{"prs":[]}')
        (evidence_dir / "open_items_digest.json").write_text('{"items":[]}')
        (evidence_dir / "dispatch_audit.jsonl").write_text(
            '{"event":"promote","dispatch_id":"d1"}\n'
        )

    env_vars = {
        "PROJECT_ROOT": str(project_root),
        "VNX_HOME": str(vnx_home),
        "VNX_DATA_DIR": str(project_root / ".vnx-data"),
        "VNX_STATE_DIR": str(project_root / ".vnx-data" / "state"),
        "VNX_DISPATCH_DIR": str(project_root / ".vnx-data" / "dispatches"),
        "VNX_LOGS_DIR": str(project_root / ".vnx-data" / "logs"),
        "VNX_PIDS_DIR": str(project_root / ".vnx-data" / "pids"),
        "VNX_LOCKS_DIR": str(project_root / ".vnx-data" / "locks"),
        "VNX_REPORTS_DIR": str(project_root / ".vnx-data" / "unified_reports"),
        "VNX_DB_DIR": str(project_root / ".vnx-data" / "database"),
        "VNX_SKILLS_DIR": str(vnx_home / "skills"),
    }

    old_env = {}
    for k, v in env_vars.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    yield {"project_root": project_root, "vnx_home": vnx_home}

    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Demo environment
# ---------------------------------------------------------------------------

class TestDemoEnvironment:
    def test_create_makes_temp_dir(self, vnx_env):
        env = DemoEnvironment.create()
        try:
            assert env.demo_dir.exists()
            assert env.data_dir.exists()
            assert env.state_dir.exists()
        finally:
            env.cleanup()

    def test_create_writes_mode_json(self, vnx_env):
        env = DemoEnvironment.create()
        try:
            mode_file = env.data_dir / "mode.json"
            assert mode_file.exists()
            data = json.loads(mode_file.read_text())
            assert data["mode"] == "demo"
            assert "temp_dir" in data
        finally:
            env.cleanup()

    def test_create_dispatch_dirs(self, vnx_env):
        env = DemoEnvironment.create()
        try:
            assert (env.data_dir / "dispatches" / "pending").is_dir()
            assert (env.data_dir / "dispatches" / "active").is_dir()
            assert (env.data_dir / "dispatches" / "completed").is_dir()
            assert (env.data_dir / "receipts").is_dir()
        finally:
            env.cleanup()

    def test_cleanup_removes_temp(self, vnx_env):
        env = DemoEnvironment.create()
        demo_dir = env.demo_dir
        assert demo_dir.exists()
        env.cleanup()
        assert not demo_dir.exists()

    def test_respects_feature_flag(self, vnx_env):
        os.environ["VNX_DEMO_MODE_ENABLED"] = "0"
        with pytest.raises(RuntimeError, match="disabled"):
            DemoEnvironment.create()
        os.environ.pop("VNX_DEMO_MODE_ENABLED")


# ---------------------------------------------------------------------------
# Evidence seeding
# ---------------------------------------------------------------------------

class TestEvidenceSeeding:
    def test_seed_governance_pipeline(self, vnx_env):
        env = DemoEnvironment.create()
        try:
            result = env.seed_evidence("governance-pipeline")
            assert result is True
            # Receipts copied
            assert (env.data_dir / "receipts" / "receipts.ndjson").exists()
            # State files copied
            assert (env.state_dir / "pr_queue_state.json").exists()
            assert (env.state_dir / "open_items_digest.json").exists()
            assert (env.state_dir / "dispatch_audit.jsonl").exists()
        finally:
            env.cleanup()

    def test_seed_context_rotation(self, vnx_env):
        env = DemoEnvironment.create()
        try:
            result = env.seed_evidence("context-rotation")
            assert result is True
            assert (env.data_dir / "receipts" / "receipts.ndjson").exists()
        finally:
            env.cleanup()

    def test_seed_unknown_scenario_returns_false(self, vnx_env):
        env = DemoEnvironment.create()
        try:
            result = env.seed_evidence("nonexistent-scenario")
            assert result is False
        finally:
            env.cleanup()

    def test_seeded_state_does_not_touch_project(self, vnx_env):
        """Demo evidence goes to temp dir, not project .vnx-data."""
        env = DemoEnvironment.create()
        try:
            env.seed_evidence("governance-pipeline")
            project_data = vnx_env["project_root"] / ".vnx-data"
            # Project data dir should not exist (demo uses temp)
            assert not project_data.exists()
        finally:
            env.cleanup()


# ---------------------------------------------------------------------------
# Scenario listing
# ---------------------------------------------------------------------------

class TestScenarioListing:
    def test_list_scenarios_returns_all(self, vnx_env):
        scenarios = list_scenarios()
        names = {s["name"] for s in scenarios}
        assert "governance-pipeline" in names
        assert "context-rotation" in names

    def test_list_scenarios_checks_availability(self, vnx_env):
        scenarios = list_scenarios()
        for s in scenarios:
            assert "available" in s
            assert isinstance(s["available"], bool)

    def test_default_scenario_exists(self):
        assert DEFAULT_SCENARIO in AVAILABLE_SCENARIOS
