"""Tests for _state_to_brief() and --format brief output path (PR-4b2 fixup 3).

Covers:
  BLOCKING 1 — pr_progress.blocked is present in brief when source has blocked PRs
  BLOCKING 2 — --format brief routes output to t0_brief.json, not t0_state.json
  ADVISORY   — blockers derived from open_items top_blockers; next_gates from active_work
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_t0_state import _state_to_brief, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_state(**overrides: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "schema_version": "2.0",
        "generated_at": "2026-04-28T00:00:00+00:00",
        "terminals": {},
        "queues": {
            "pending_count": 0,
            "active_count": 0,
            "completed_last_hour": 0,
            "conflict_count": 0,
        },
        "tracks": {},
        "pr_progress": {
            "feature_name": "test-feature",
            "total": 3,
            "completed": 1,
            "in_progress": ["PR-2"],
            "completion_pct": 33,
            "has_blocking_drift": False,
            "blocked": [],
        },
        "open_items": {"open_count": 0, "blocker_count": 0, "top_blockers": []},
        "active_work": [],
        "recent_receipts": [],
        "system_health": {"status": "healthy", "db_initialized": True, "uptime_seconds": 0},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# BLOCKING 1: pr_progress.blocked propagation into brief
# ---------------------------------------------------------------------------

class TestBriefPrProgressBlocked:
    def test_blocked_field_present_in_brief(self):
        state = _minimal_state()
        state["pr_progress"]["blocked"] = ["PR-3", "PR-4"]
        brief = _state_to_brief(state)
        assert "blocked" in brief["pr_progress"]

    def test_blocked_values_propagated(self):
        state = _minimal_state()
        state["pr_progress"]["blocked"] = ["PR-3", "PR-4"]
        brief = _state_to_brief(state)
        assert brief["pr_progress"]["blocked"] == ["PR-3", "PR-4"]

    def test_blocked_empty_when_source_empty(self):
        state = _minimal_state()
        state["pr_progress"]["blocked"] = []
        brief = _state_to_brief(state)
        assert brief["pr_progress"]["blocked"] == []

    def test_blocked_defaults_to_empty_when_key_absent(self):
        state = _minimal_state()
        state["pr_progress"].pop("blocked", None)
        brief = _state_to_brief(state)
        assert brief["pr_progress"]["blocked"] == []


# ---------------------------------------------------------------------------
# BLOCKING 2: --format brief writes to t0_brief.json, not t0_state.json
# ---------------------------------------------------------------------------

class TestFormatBriefOutputPath:
    def test_brief_format_writes_to_t0_brief_json(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)
        (dispatch_dir / "conflicts").mkdir(parents=True)

        monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
        monkeypatch.setenv("VNX_DISPATCH_DIR", str(dispatch_dir))
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))

        import build_t0_state as _bts
        monkeypatch.setattr(_bts, "_STATE_DIR", state_dir)
        monkeypatch.setattr(_bts, "_DISPATCH_DIR", dispatch_dir)
        monkeypatch.setattr(_bts, "_DATA_DIR", tmp_path / "data")

        with patch("sys.argv", ["build_t0_state.py", "--format", "brief"]):
            rc = main()

        assert rc == 0
        brief_path = state_dir / "t0_brief.json"
        state_path = state_dir / "t0_state.json"
        assert brief_path.exists(), "t0_brief.json must be written when --format brief"
        if state_path.exists():
            data = json.loads(state_path.read_text())
            assert data.get("schema_version") != "1.0", (
                "t0_state.json must not contain brief schema when --format brief"
            )

    def test_brief_output_has_version_1_0(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)
        (dispatch_dir / "conflicts").mkdir(parents=True)

        import build_t0_state as _bts
        monkeypatch.setattr(_bts, "_STATE_DIR", state_dir)
        monkeypatch.setattr(_bts, "_DISPATCH_DIR", dispatch_dir)
        monkeypatch.setattr(_bts, "_DATA_DIR", tmp_path / "data")

        with patch("sys.argv", ["build_t0_state.py", "--format", "brief"]):
            rc = main()

        assert rc == 0
        data = json.loads((state_dir / "t0_brief.json").read_text())
        assert data.get("version") == "1.0"

    def test_state_format_writes_to_t0_state_json(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "state"
        dispatch_dir = tmp_path / "dispatches"
        state_dir.mkdir(parents=True)
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir(parents=True)
        (dispatch_dir / "conflicts").mkdir(parents=True)

        import build_t0_state as _bts
        monkeypatch.setattr(_bts, "_STATE_DIR", state_dir)
        monkeypatch.setattr(_bts, "_DISPATCH_DIR", dispatch_dir)
        monkeypatch.setattr(_bts, "_DATA_DIR", tmp_path / "data")

        with patch("sys.argv", ["build_t0_state.py", "--format", "state"]):
            rc = main()

        assert rc == 0
        state_path = state_dir / "t0_state.json"
        assert state_path.exists(), "t0_state.json must be written when --format state"
        data = json.loads(state_path.read_text())
        assert data.get("schema_version") == "2.0"


# ---------------------------------------------------------------------------
# ADVISORY: blockers derived from open_items; next_gates from active_work
# ---------------------------------------------------------------------------

class TestBriefBlockers:
    def test_blockers_empty_when_no_open_items(self):
        state = _minimal_state()
        brief = _state_to_brief(state)
        assert brief["blockers"] == []

    def test_blockers_derived_from_top_blockers(self):
        blocker_item = {"id": "OI-1", "title": "Auth broken", "severity": "blocker"}
        state = _minimal_state()
        state["open_items"]["top_blockers"] = [blocker_item]
        brief = _state_to_brief(state)
        assert len(brief["blockers"]) == 1
        assert brief["blockers"][0]["id"] == "OI-1"

    def test_blockers_capped_at_3(self):
        state = _minimal_state()
        state["open_items"]["top_blockers"] = [
            {"id": f"OI-{i}", "title": f"Blocker {i}"} for i in range(5)
        ]
        brief = _state_to_brief(state)
        assert len(brief["blockers"]) == 3


class TestBriefNextGates:
    def test_next_gates_empty_when_no_active_work(self):
        state = _minimal_state()
        brief = _state_to_brief(state)
        assert brief["next_gates"] == []

    def test_next_gates_derived_from_active_work(self):
        state = _minimal_state()
        state["active_work"] = [
            {"dispatch_id": "d-001", "track": "T1", "gate": "codex", "started_at": "2026-04-28T00:00:00Z"},
            {"dispatch_id": "d-002", "track": "T2", "gate": "ci", "started_at": "2026-04-28T00:01:00Z"},
        ]
        brief = _state_to_brief(state)
        assert set(brief["next_gates"]) == {"codex", "ci"}

    def test_next_gates_skips_none_gate_entries(self):
        state = _minimal_state()
        state["active_work"] = [
            {"dispatch_id": "d-001", "track": "T1", "gate": None, "started_at": "2026-04-28T00:00:00Z"},
            {"dispatch_id": "d-002", "track": "T2", "gate": "review", "started_at": "2026-04-28T00:01:00Z"},
        ]
        brief = _state_to_brief(state)
        assert brief["next_gates"] == ["review"]

    def test_brief_has_next_gates_key(self):
        state = _minimal_state()
        brief = _state_to_brief(state)
        assert "next_gates" in brief
