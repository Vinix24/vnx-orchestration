"""Tests for build_t0_state._build_feature_state register integration."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _clear_cached_vnx_paths():
    """Clear vnx_paths from sys.modules between tests.

    test_dispatch_register_hooks.py stubs vnx_paths as a MagicMock whose
    ensure_env() returns only 3 keys. If that stub lingers in sys.modules when
    build_t0_state.py is loaded here, it causes a KeyError on VNX_DISPATCH_DIR.
    """
    sys.modules.pop("vnx_paths", None)
    yield
    sys.modules.pop("vnx_paths", None)

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


def _load_mod_with_fake_fsm(tmp_path: Path, env_patch: dict, state_dir: Path):
    """Load build_t0_state with a minimal fake feature_state_machine."""
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
    return result


def test_dispatch_id_primary_key_grouping(tmp_path: Path):
    """dispatch_promoted (no pr_number) + task_complete (pr_number set) for the same
    dispatch_id must aggregate to ONE entry keyed 'pr-{n}', status 'completed'."""
    env_patch, state_dir, _ = _load_build_t0_state(tmp_path)

    events = [
        {
            "timestamp": "2026-04-28T11:00:00Z",
            "event": "dispatch_promoted",
            "dispatch_id": "d-split-test",
            "terminal": "T1",
            # No pr_number on the early event — this was the root cause.
        },
        {
            "timestamp": "2026-04-28T11:30:00Z",
            "event": "dispatch_completed",
            "dispatch_id": "d-split-test",
            "pr_number": 99,
        },
    ]
    _write_register(state_dir, events)

    with mock.patch.dict(os.environ, env_patch):
        result = _load_mod_with_fake_fsm(tmp_path, env_patch, state_dir)

    rf = result["register_features"]

    # Must be exactly one entry (keyed by derived pr_number, not by dispatch_id).
    assert len(rf) == 1, f"Expected 1 entry, got {len(rf)}: {list(rf.keys())}"
    assert "pr-99" in rf, f"Expected key 'pr-99', got {list(rf.keys())}"
    entry = rf["pr-99"]
    assert entry["status"] == "completed"
    assert 99 in entry["prs"]
    # No stale 'd-split-test' entry under its own key.
    assert "d-split-test" not in rf


def test_dispatch_id_primary_key_no_stale_active(tmp_path: Path):
    """A dispatch that starts without pr_number and later gets one must not leave
    a stale 'active' entry alongside the completed one."""
    env_patch, state_dir, _ = _load_build_t0_state(tmp_path)

    events = [
        {
            "timestamp": "2026-04-28T12:00:00Z",
            "event": "dispatch_promoted",
            "dispatch_id": "d-stale-active",
            "terminal": "T2",
        },
        {
            "timestamp": "2026-04-28T12:10:00Z",
            "event": "dispatch_started",
            "dispatch_id": "d-stale-active",
            "pr_number": 200,
            "terminal": "T2",
        },
        {
            "timestamp": "2026-04-28T12:45:00Z",
            "event": "dispatch_completed",
            "dispatch_id": "d-stale-active",
            "pr_number": 200,
        },
    ]
    _write_register(state_dir, events)

    with mock.patch.dict(os.environ, env_patch):
        result = _load_mod_with_fake_fsm(tmp_path, env_patch, state_dir)

    rf = result["register_features"]
    assert "pr-200" in rf
    assert rf["pr-200"]["status"] == "completed"
    # No leftover entry for the key that would have been used without the fix.
    assert "d-stale-active" not in rf


def test_retry_dispatch_after_completed_yields_active(tmp_path: Path):
    """A completed dispatch followed by a newer promoted dispatch on the same PR
    must yield PR status 'active', not 'completed'.

    Regression for: build_t0_state used max-rank aggregation, so a completed
    dispatch (rank 3) prevented a later retry dispatch (active, rank 2) from
    moving the PR status back to 'active'.
    """
    env_patch, state_dir, _ = _load_build_t0_state(tmp_path)

    events = [
        {
            "timestamp": "2026-04-28T09:00:00Z",
            "event": "dispatch_promoted",
            "dispatch_id": "d-first",
            "pr_number": 50,
            "terminal": "T1",
        },
        {
            "timestamp": "2026-04-28T09:30:00Z",
            "event": "dispatch_completed",
            "dispatch_id": "d-first",
            "pr_number": 50,
        },
        # Retry dispatch: promoted after the first one completed.
        {
            "timestamp": "2026-04-28T10:00:00Z",
            "event": "dispatch_promoted",
            "dispatch_id": "d-retry",
            "pr_number": 50,
            "terminal": "T1",
        },
    ]
    _write_register(state_dir, events)

    with mock.patch.dict(os.environ, env_patch):
        result = _load_mod_with_fake_fsm(tmp_path, env_patch, state_dir)

    rf = result["register_features"]
    assert "pr-50" in rf, f"Expected 'pr-50' in {list(rf.keys())}"
    assert rf["pr-50"]["status"] == "active", (
        f"Expected 'active' (retry dispatch is most recent), got {rf['pr-50']['status']!r}"
    )
