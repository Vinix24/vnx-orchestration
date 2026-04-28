"""Tests for build_t0_state.py — dispatch_register_events exposure (PR-4b).

Coverage:
  1. _build_register_events reads events from dispatch_register.ndjson
  2. build_t0_state return dict contains dispatch_register_events key
  3. dispatch_lifecycle.sh uses Python helper (not inline bash throttle)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import build_t0_state as bts
import dispatch_register


# ---------------------------------------------------------------------------
# Test 1: _build_register_events reads last N events
# ---------------------------------------------------------------------------


def test_build_register_events_reads_ndjson(tmp_path: Path) -> None:
    register_file = tmp_path / "dispatch_register.ndjson"
    events = [
        {"timestamp": "2026-04-28T10:00:00Z", "event": "gate_passed", "dispatch_id": "D-001"},
        {"timestamp": "2026-04-28T10:01:00Z", "event": "gate_failed", "dispatch_id": "D-002"},
        {"timestamp": "2026-04-28T10:02:00Z", "event": "dispatch_completed", "dispatch_id": "D-003"},
    ]
    register_file.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    with mock.patch.object(dispatch_register, "_register_path", return_value=register_file):
        result = bts._build_register_events(limit=50)

    assert len(result) == 3
    assert result[0]["event"] == "gate_passed"
    assert result[2]["event"] == "dispatch_completed"


def test_build_register_events_respects_limit(tmp_path: Path) -> None:
    register_file = tmp_path / "dispatch_register.ndjson"
    events = [
        {"timestamp": f"2026-04-28T10:0{i}:00Z", "event": "gate_passed", "dispatch_id": f"D-{i:03d}"}
        for i in range(10)
    ]
    register_file.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )

    with mock.patch.object(dispatch_register, "_register_path", return_value=register_file):
        result = bts._build_register_events(limit=3)

    assert len(result) == 3
    assert result[0]["dispatch_id"] == "D-007"
    assert result[2]["dispatch_id"] == "D-009"


def test_build_register_events_missing_file_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_file.ndjson"
    with mock.patch.object(dispatch_register, "_register_path", return_value=missing):
        result = bts._build_register_events(limit=50)
    assert result == []


# ---------------------------------------------------------------------------
# Test 2: build_t0_state return dict contains dispatch_register_events
# ---------------------------------------------------------------------------


def test_build_t0_state_exposes_register_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    dispatch_dir = tmp_path / "dispatches"
    state_dir.mkdir(parents=True)
    dispatch_dir.mkdir(parents=True)

    register_file = state_dir / "dispatch_register.ndjson"
    register_file.write_text(
        json.dumps({"timestamp": "2026-04-28T10:00:00Z", "event": "gate_passed", "dispatch_id": "D-TEST"}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR_EXPLICIT", "1")

    with mock.patch.object(dispatch_register, "_register_path", return_value=register_file):
        state = bts.build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)

    assert "dispatch_register_events" in state, "t0_state must include dispatch_register_events key"
    events = state["dispatch_register_events"]
    assert isinstance(events, list)
    assert any(e.get("event") == "gate_passed" for e in events)


# ---------------------------------------------------------------------------
# Test 3: dispatch_lifecycle.sh uses Python helper (not inline bash throttle)
# ---------------------------------------------------------------------------


def test_dispatch_lifecycle_sh_uses_python_helper() -> None:
    lifecycle_sh = SCRIPTS_DIR / "lib" / "dispatch_lifecycle.sh"
    content = lifecycle_sh.read_text(encoding="utf-8")

    assert "maybe_trigger_state_rebuild" in content, \
        "dispatch_lifecycle.sh must call maybe_trigger_state_rebuild"
    assert "from state_rebuild_trigger import maybe_trigger_state_rebuild" in content, \
        "dispatch_lifecycle.sh must import from state_rebuild_trigger"

    # Must NOT contain the old inline throttle reimplementation
    assert "_fdd_throttle_file" not in content, \
        "dispatch_lifecycle.sh must not reimplement the throttle inline"
    assert "nohup python3" not in content, \
        "dispatch_lifecycle.sh must not call nohup directly (use Python helper instead)"
