#!/usr/bin/env python3
"""Tests for VNX Setup — one-command project setup orchestrator (PR-4).

Tests the setup flow: prereq check → init → doctor → register → next-steps.
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

from vnx_setup import (
    PASS, SKIP, FAIL,
    SetupStep,
    step_prereq_check,
    step_write_mode,
    get_next_steps,
    run_setup,
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
    data_dir.mkdir()
    state_dir = data_dir / "state"
    state_dir.mkdir()

    paths = {
        "PROJECT_ROOT": str(project_root),
        "VNX_HOME": str(vnx_home),
        "VNX_DATA_DIR": str(data_dir),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_DISPATCH_DIR": str(data_dir / "dispatches"),
        "VNX_LOGS_DIR": str(data_dir / "logs"),
        "VNX_PIDS_DIR": str(data_dir / "pids"),
        "VNX_LOCKS_DIR": str(data_dir / "locks"),
        "VNX_REPORTS_DIR": str(data_dir / "unified_reports"),
        "VNX_DB_DIR": str(data_dir / "database"),
    }

    # Set env vars for vnx_mode to work
    for k, v in paths.items():
        os.environ[k] = v

    yield paths

    # Cleanup env
    for k in paths:
        os.environ.pop(k, None)


# ---------------------------------------------------------------------------
# SetupStep model tests
# ---------------------------------------------------------------------------

class TestSetupStep:
    def test_to_dict(self):
        step = SetupStep("test", PASS, "all good", ["detail"])
        d = step.to_dict()
        assert d["name"] == "test"
        assert d["status"] == "pass"
        assert d["message"] == "all good"
        assert d["details"] == ["detail"]


# ---------------------------------------------------------------------------
# Prereq check tests
# ---------------------------------------------------------------------------

class TestStepPrereqCheck:
    def test_prereq_check_with_real_validator(self, vnx_env):
        """Prereq check calls vnx_install.py from SCRIPT_DIR (not VNX_HOME).
        The result depends on real system tools, so just check it returns a step."""
        result = step_prereq_check(vnx_env)
        assert result.name == "prereq-check"
        assert result.status in (PASS, SKIP, FAIL)
        # It should NOT skip (validator exists in scripts/)
        assert result.status != SKIP

    @patch("vnx_setup.subprocess.run")
    def test_prereq_check_handles_timeout(self, mock_run, vnx_env):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=30)
        result = step_prereq_check(vnx_env)
        assert result.status == FAIL


# ---------------------------------------------------------------------------
# Mode write tests
# ---------------------------------------------------------------------------

class TestStepWriteMode:
    def test_write_starter_mode(self, vnx_env):
        result = step_write_mode(vnx_env, "starter")
        assert result.status == PASS
        assert "starter" in result.message

        mode_file = Path(vnx_env["VNX_DATA_DIR"]) / "mode.json"
        assert mode_file.exists()
        data = json.loads(mode_file.read_text())
        assert data["mode"] == "starter"

    def test_write_operator_mode(self, vnx_env):
        result = step_write_mode(vnx_env, "operator")
        assert result.status == PASS
        assert "operator" in result.message

    def test_write_invalid_mode(self, vnx_env):
        result = step_write_mode(vnx_env, "nonexistent")
        assert result.status == FAIL


# ---------------------------------------------------------------------------
# Next steps tests
# ---------------------------------------------------------------------------

class TestGetNextSteps:
    def test_starter_next_steps(self, vnx_env):
        steps = get_next_steps("starter", vnx_env)
        combined = " ".join(steps)
        assert "doctor" in combined
        assert "operator" in combined

    def test_operator_next_steps(self, vnx_env):
        steps = get_next_steps("operator", vnx_env)
        combined = " ".join(steps)
        assert "doctor" in combined
        assert "start" in combined


# ---------------------------------------------------------------------------
# Run setup integration tests
# ---------------------------------------------------------------------------

class TestRunSetup:
    @patch("vnx_setup.step_init")
    @patch("vnx_setup.step_prereq_check")
    def test_stops_on_prereq_failure(self, mock_prereq, mock_init, vnx_env):
        mock_prereq.return_value = SetupStep("prereq-check", FAIL, "python3 missing")
        results = run_setup(mode="starter")
        # Should stop after prereq failure — init should NOT be called
        mock_init.assert_not_called()
        assert results[-1].status == FAIL

    @patch("vnx_setup.step_register")
    @patch("vnx_setup.step_doctor")
    @patch("vnx_setup.step_init")
    @patch("vnx_setup.step_prereq_check")
    def test_full_flow_success(self, mock_prereq, mock_init, mock_doctor,
                               mock_register, vnx_env):
        mock_prereq.return_value = SetupStep("prereq-check", PASS, "OK")
        mock_init.return_value = SetupStep("init", PASS, "Initialized")
        mock_doctor.return_value = SetupStep("doctor", PASS, "Healthy")
        mock_register.return_value = SetupStep("register", PASS, "Registered")

        results = run_setup(mode="starter")
        statuses = [r.status for r in results]
        assert FAIL not in statuses

    @patch("vnx_setup.step_init")
    @patch("vnx_setup.step_prereq_check")
    def test_stops_on_init_failure(self, mock_prereq, mock_init, vnx_env):
        mock_prereq.return_value = SetupStep("prereq-check", PASS, "OK")
        mock_init.return_value = SetupStep("init", FAIL, "Init failed")
        results = run_setup(mode="starter")
        assert results[-1].status == FAIL

    @patch("vnx_setup.step_init")
    @patch("vnx_setup.step_prereq_check")
    def test_skip_doctor(self, mock_prereq, mock_init, vnx_env):
        mock_prereq.return_value = SetupStep("prereq-check", PASS, "OK")
        mock_init.return_value = SetupStep("init", PASS, "OK")

        results = run_setup(mode="starter", skip_doctor=True)
        names = [r.name for r in results]
        assert "doctor" not in names

    @patch("vnx_setup.step_init")
    @patch("vnx_setup.step_prereq_check")
    def test_skip_register(self, mock_prereq, mock_init, vnx_env):
        mock_prereq.return_value = SetupStep("prereq-check", PASS, "OK")
        mock_init.return_value = SetupStep("init", PASS, "OK")

        results = run_setup(mode="starter", skip_register=True)
        names = [r.name for r in results]
        assert "register" not in names
