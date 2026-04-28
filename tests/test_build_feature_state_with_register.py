"""Tests for build_t0_state._build_feature_state register integration."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

sys.path.insert(0, str(LIB_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_build_t0_state(tmp_path: Path):
    """Load build_t0_state with VNX env vars pointed at tmp_path."""
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    dispatch_dir = data_dir / "dispatches"
    state_dir.mkdir(parents=True, exist_ok=True)
    (dispatch_dir / "pending").mkdir(parents=True, exist_ok=True)
    (dispatch_dir / "active").mkdir(parents=True, exist_ok=True)

    env_patch = {
        "VNX_DATA_DIR": str(data_dir),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_DISPATCH_DIR": str(dispatch_dir),
        "PROJECT_ROOT": str(tmp_path),
        "VNX_HOME": str(VNX_ROOT),
    }
    return env_patch, state_dir, dispatch_dir


def _write_register(state_dir: Path, events: list) -> None:
    reg = state_dir / "dispatch_register.ndjson"
    with reg.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_empty_register_falls_back_to_feature_plan(tmp_path: Path):
    env_patch, state_dir, dispatch_dir = _load_build_t0_state(tmp_path)

    with mock.patch.dict(os.environ, env_patch):
        # Re-import to pick up env
        if "build_t0_state" in sys.modules:
            del sys.modules["build_t0_state"]
        spec = importlib.util.spec_from_file_location(
            "build_t0_state", SCRIPTS_DIR / "build_t0_state.py"
        )
        mod = importlib.util.module_from_spec(spec)

        # patch feature_state_machine import to avoid file dependency
        fake_fsm = mock.MagicMock()
        fake_state = mock.MagicMock()
        fake_state.as_dict.return_value = {
            "feature_name": "TestFeature",
            "current_pr": "pr-1",
            "next_task": None,
            "assigned_track": "A",
            "assigned_role": "backend-developer",
            "completion_pct": 50,
            "total_prs": 2,
            "completed_prs": 1,
            "status": "in_progress",
        }
        fake_fsm.parse_feature_plan.return_value = fake_state

        # Create a minimal FEATURE_PLAN.md
        (tmp_path / "FEATURE_PLAN.md").write_text("# Feature Plan\n")

        with mock.patch.dict(sys.modules, {"feature_state_machine": fake_fsm}):
            spec.loader.exec_module(mod)
            result = mod._build_feature_state()

    assert result["feature_name"] == "TestFeature"
    assert result["completion_pct"] == 50
    # No register_features key when register is empty/missing
    assert "register_features" not in result


def test_register_events_override_and_populate_register_features(tmp_path: Path):
    env_patch, state_dir, dispatch_dir = _load_build_t0_state(tmp_path)

    events = [
        {
            "timestamp": "2026-04-28T10:00:00Z",
            "event": "dispatch_promoted",
            "dispatch_id": "d001",
            "pr_number": 10,
            "terminal": "T1",
        },
        {
            "timestamp": "2026-04-28T10:05:00Z",
            "event": "dispatch_completed",
            "dispatch_id": "d001",
            "pr_number": 10,
        },
        {
            "timestamp": "2026-04-28T10:10:00Z",
            "event": "dispatch_started",
            "dispatch_id": "d002",
            "pr_number": 11,
            "terminal": "T2",
        },
    ]
    _write_register(state_dir, events)

    with mock.patch.dict(os.environ, env_patch):
        if "build_t0_state" in sys.modules:
            del sys.modules["build_t0_state"]
        spec = importlib.util.spec_from_file_location(
            "build_t0_state", SCRIPTS_DIR / "build_t0_state.py"
        )
        mod = importlib.util.module_from_spec(spec)

        fake_fsm = mock.MagicMock()
        fake_state = mock.MagicMock()
        fake_state.as_dict.return_value = {
            "feature_name": "F46",
            "current_pr": None,
            "next_task": None,
            "assigned_track": None,
            "assigned_role": None,
            "completion_pct": 0,
            "total_prs": 0,
            "completed_prs": 0,
            "status": "planned",
        }
        fake_fsm.parse_feature_plan.return_value = fake_state
        (tmp_path / "FEATURE_PLAN.md").write_text("# F\n")

        with mock.patch.dict(sys.modules, {"feature_state_machine": fake_fsm}):
            spec.loader.exec_module(mod)
            result = mod._build_feature_state()

    assert "register_features" in result
    rf = result["register_features"]

    # pr-10 should be completed
    assert "pr-10" in rf
    assert rf["pr-10"]["status"] == "completed"
    assert 10 in rf["pr-10"]["prs"]

    # pr-11 should be active
    assert "pr-11" in rf
    assert rf["pr-11"]["status"] == "active"


def test_aggregation_by_pr_number_and_feature_id(tmp_path: Path):
    env_patch, state_dir, dispatch_dir = _load_build_t0_state(tmp_path)

    events = [
        {
            "timestamp": "2026-04-28T09:00:00Z",
            "event": "dispatch_created",
            "dispatch_id": "d100",
            "feature_id": "F99",
        },
        {
            "timestamp": "2026-04-28T09:10:00Z",
            "event": "dispatch_promoted",
            "dispatch_id": "d100",
            "feature_id": "F99",
            "terminal": "T1",
        },
        {
            "timestamp": "2026-04-28T09:20:00Z",
            "event": "dispatch_failed",
            "dispatch_id": "d100",
            "feature_id": "F99",
        },
    ]
    _write_register(state_dir, events)

    with mock.patch.dict(os.environ, env_patch):
        if "build_t0_state" in sys.modules:
            del sys.modules["build_t0_state"]
        spec = importlib.util.spec_from_file_location(
            "build_t0_state", SCRIPTS_DIR / "build_t0_state.py"
        )
        mod = importlib.util.module_from_spec(spec)
        fake_fsm = mock.MagicMock()
        fake_state = mock.MagicMock()
        fake_state.as_dict.return_value = {
            "feature_name": None, "current_pr": None, "next_task": None,
            "assigned_track": None, "assigned_role": None,
            "completion_pct": 0, "total_prs": 0, "completed_prs": 0,
            "status": "planned",
        }
        fake_fsm.parse_feature_plan.return_value = fake_state
        (tmp_path / "FEATURE_PLAN.md").write_text("# F\n")

        with mock.patch.dict(sys.modules, {"feature_state_machine": fake_fsm}):
            spec.loader.exec_module(mod)
            result = mod._build_feature_state()

    rf = result["register_features"]
    assert "F99" in rf
    assert rf["F99"]["status"] == "failed"
    assert rf["F99"]["last_event"] == "dispatch_failed"
