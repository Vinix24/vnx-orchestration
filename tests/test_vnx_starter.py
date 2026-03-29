#!/usr/bin/env python3
"""Tests for VNX Starter Mode — single-terminal runtime (PR-2).

Validates starter init, dispatch lifecycle, receipt generation,
and status reporting.
"""

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_mode import VNXMode, read_mode
from vnx_starter import (
    StarterConfig,
    init_starter,
    create_starter_dispatch,
    promote_starter_dispatch,
    complete_starter_dispatch,
    write_starter_receipt,
    get_starter_status,
    STARTER_TERMINAL_ID,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vnx_env(tmp_path):
    """Set up VNX environment pointing at a temp directory."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    vnx_home = tmp_path / "vnx-system"
    vnx_home.mkdir()
    data_dir = project_root / ".vnx-data"

    env_vars = {
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
        "VNX_SKILLS_DIR": str(vnx_home / "skills"),
    }

    old_env = {}
    for k, v in env_vars.items():
        old_env[k] = os.environ.get(k)
        os.environ[k] = v

    yield {"project_root": project_root, "vnx_home": vnx_home, "data_dir": data_dir}

    for k, v in old_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Starter init
# ---------------------------------------------------------------------------

class TestStarterInit:
    def test_init_creates_mode_json(self, vnx_env):
        config = init_starter()
        mode = read_mode(config.data_dir)
        assert mode == VNXMode.STARTER

    def test_init_creates_directories(self, vnx_env):
        config = init_starter()
        assert Path(config.state_dir).is_dir()
        assert Path(config.dispatch_dir).is_dir()
        assert Path(config.receipts_dir).is_dir()
        assert (Path(config.dispatch_dir) / "pending").is_dir()
        assert (Path(config.dispatch_dir) / "active").is_dir()
        assert (Path(config.dispatch_dir) / "completed").is_dir()

    def test_init_writes_terminal_state(self, vnx_env):
        config = init_starter()
        state_file = Path(config.state_dir) / "terminal_state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["mode"] == "starter"
        assert STARTER_TERMINAL_ID in state["terminals"]
        assert state["terminals"][STARTER_TERMINAL_ID]["status"] == "idle"

    def test_init_writes_panes_json(self, vnx_env):
        config = init_starter()
        panes_file = Path(config.state_dir) / "panes.json"
        assert panes_file.exists()
        panes = json.loads(panes_file.read_text())
        assert panes["session"] is None  # No tmux
        assert panes["mode"] == "starter"

    def test_init_is_idempotent(self, vnx_env):
        config1 = init_starter()
        config2 = init_starter()
        assert config1.data_dir == config2.data_dir

    def test_init_respects_feature_flag(self, vnx_env):
        os.environ["VNX_STARTER_MODE_ENABLED"] = "0"
        with pytest.raises(RuntimeError, match="disabled"):
            init_starter()
        os.environ.pop("VNX_STARTER_MODE_ENABLED")


# ---------------------------------------------------------------------------
# Dispatch lifecycle
# ---------------------------------------------------------------------------

class TestDispatchLifecycle:
    def test_create_dispatch(self, vnx_env):
        config = init_starter()
        dispatch_id = "20260329-120000-test-dispatch-A"
        bundle_dir = create_starter_dispatch(
            config, dispatch_id, "Test dispatch", pr="PR-2"
        )
        assert bundle_dir.exists()
        bundle = json.loads((bundle_dir / "bundle.json").read_text())
        assert bundle["dispatch_id"] == dispatch_id
        assert bundle["mode"] == "starter"
        assert bundle["status"] == "pending"

    def test_create_dispatch_with_prompt(self, vnx_env):
        config = init_starter()
        dispatch_id = "20260329-120000-test-prompt-A"
        bundle_dir = create_starter_dispatch(
            config, dispatch_id, "Test", prompt="Do the thing"
        )
        assert (bundle_dir / "prompt.txt").read_text() == "Do the thing"

    def test_promote_dispatch(self, vnx_env):
        config = init_starter()
        dispatch_id = "20260329-120000-test-promote-A"
        create_starter_dispatch(config, dispatch_id, "Test")
        active_dir = promote_starter_dispatch(config, dispatch_id)
        assert active_dir.exists()
        assert "active" in str(active_dir)
        bundle = json.loads((active_dir / "bundle.json").read_text())
        assert bundle["status"] == "active"
        assert "promoted_at" in bundle

    def test_promote_missing_dispatch_raises(self, vnx_env):
        config = init_starter()
        with pytest.raises(FileNotFoundError):
            promote_starter_dispatch(config, "nonexistent")

    def test_complete_dispatch(self, vnx_env):
        config = init_starter()
        dispatch_id = "20260329-120000-test-complete-A"
        create_starter_dispatch(config, dispatch_id, "Test")
        promote_starter_dispatch(config, dispatch_id)
        completed_dir = complete_starter_dispatch(config, dispatch_id)
        assert completed_dir.exists()
        assert "completed" in str(completed_dir)
        bundle = json.loads((completed_dir / "bundle.json").read_text())
        assert bundle["status"] == "success"

    def test_complete_dispatch_with_error(self, vnx_env):
        config = init_starter()
        dispatch_id = "20260329-120000-test-error-A"
        create_starter_dispatch(config, dispatch_id, "Test")
        promote_starter_dispatch(config, dispatch_id)
        complete_starter_dispatch(config, dispatch_id, status="error", error="Something broke")
        completed_dir = Path(config.dispatch_dir) / "completed" / dispatch_id
        bundle = json.loads((completed_dir / "bundle.json").read_text())
        assert bundle["status"] == "error"
        assert bundle["error"] == "Something broke"


# ---------------------------------------------------------------------------
# Receipt generation
# ---------------------------------------------------------------------------

class TestReceiptGeneration:
    def test_write_receipt_creates_ndjson(self, vnx_env):
        config = init_starter()
        dispatch_id = "20260329-120000-test-receipt-A"
        receipt_file = write_starter_receipt(
            config, dispatch_id, "success",
            work_type="coding",
            files_changed=["src/foo.py"],
            duration_seconds=42.5,
        )
        assert receipt_file.exists()
        lines = receipt_file.read_text().strip().split("\n")
        assert len(lines) == 1
        receipt = json.loads(lines[0])
        assert receipt["dispatch_id"] == dispatch_id
        assert receipt["status"] == "success"
        assert receipt["mode"] == "starter"
        assert receipt["terminal_id"] == STARTER_TERMINAL_ID
        assert receipt["provenance"] == "CLEAN"
        assert receipt["files_changed"] == ["src/foo.py"]

    def test_receipt_appends(self, vnx_env):
        config = init_starter()
        write_starter_receipt(config, "d1", "success")
        write_starter_receipt(config, "d2", "success")
        receipt_file = Path(config.receipts_dir) / "t0_receipts.ndjson"
        lines = receipt_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_receipt_per_dispatch_file(self, vnx_env):
        config = init_starter()
        dispatch_id = "20260329-120000-per-dispatch-A"
        write_starter_receipt(config, dispatch_id, "success")
        per_dispatch = Path(config.dispatch_dir) / "completed" / f"{dispatch_id}.receipt.json"
        assert per_dispatch.exists()
        receipt = json.loads(per_dispatch.read_text())
        assert receipt["dispatch_id"] == dispatch_id

    def test_receipt_includes_trace_token(self, vnx_env):
        config = init_starter()
        dispatch_id = "20260329-120000-trace-A"
        write_starter_receipt(config, dispatch_id, "success")
        receipt_file = Path(config.receipts_dir) / "t0_receipts.ndjson"
        receipt = json.loads(receipt_file.read_text().strip())
        assert receipt["trace_token"] == f"Dispatch-ID: {dispatch_id}"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStarterStatus:
    def test_status_empty(self, vnx_env):
        config = init_starter()
        status = get_starter_status(config)
        assert status["mode"] == "starter"
        assert status["provider"] == "claude_code"
        assert status["dispatches"]["pending"] == 0
        assert status["receipts"] == 0

    def test_status_counts_dispatches(self, vnx_env):
        config = init_starter()
        create_starter_dispatch(config, "d1", "First")
        create_starter_dispatch(config, "d2", "Second")
        promote_starter_dispatch(config, "d1")
        status = get_starter_status(config)
        assert status["dispatches"]["pending"] == 1
        assert status["dispatches"]["active"] == 1

    def test_status_counts_receipts(self, vnx_env):
        config = init_starter()
        write_starter_receipt(config, "d1", "success")
        write_starter_receipt(config, "d2", "error")
        status = get_starter_status(config)
        assert status["receipts"] == 2
