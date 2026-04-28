"""Tests for pr_queue_state.py (Phase 2.1).

Covers:
  1. Schema validation — output matches pr_queue/1.0 schema
  2. Empty state — no open PRs → empty arrays
  3. Integration — build_t0_state output contains pr_queue key
  4. Gates map — gate_passed/gate_failed register events populate correctly
  5. Queued features — dispatches not in terminal state appear
  6. Atomic write — pr_queue_state.json written atomically
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from pr_queue_state import (
    _build_gates_map,
    _build_queued_features,
    build_pr_queue_state,
    write_pr_queue_state,
)
from build_t0_state import build_t0_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dirs(tmp_path: Path):
    state_dir = tmp_path / "state"
    dispatch_dir = tmp_path / "dispatches"
    state_dir.mkdir(parents=True)
    (dispatch_dir / "pending").mkdir(parents=True)
    (dispatch_dir / "active").mkdir(parents=True)
    (dispatch_dir / "conflicts").mkdir(parents=True)
    return state_dir, dispatch_dir


# ---------------------------------------------------------------------------
# 1. Schema validation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_schema_field_present(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            result = build_pr_queue_state(state_dir, register_events=[])
        assert result["schema"] == "pr_queue/1.0"

    def test_required_keys_present(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            result = build_pr_queue_state(state_dir, register_events=[])
        for key in ("schema", "timestamp", "open_prs", "merged_today", "queued_features"):
            assert key in result, f"Missing key: {key}"

    def test_open_prs_is_list(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            result = build_pr_queue_state(state_dir, register_events=[])
        assert isinstance(result["open_prs"], list)

    def test_open_pr_fields_complete(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[
            {"number": 42, "title": "Test PR", "branch": "feat/test",
             "state": "active", "ci_status": "pass"},
        ]), patch("pr_queue_state._get_merged_today", return_value=[]):
            result = build_pr_queue_state(state_dir, register_events=[])
        assert len(result["open_prs"]) == 1
        pr = result["open_prs"][0]
        for field in ("number", "title", "branch", "state", "ci_status",
                      "gates_passed", "blocked_on"):
            assert field in pr, f"Missing pr field: {field}"

    def test_is_json_serializable(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            result = build_pr_queue_state(state_dir, register_events=[])
        parsed = json.loads(json.dumps(result))
        assert parsed["schema"] == "pr_queue/1.0"


# ---------------------------------------------------------------------------
# 2. Empty state
# ---------------------------------------------------------------------------

class TestEmptyState:
    def test_empty_register_empty_prs(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            result = build_pr_queue_state(state_dir, register_events=[])
        assert result["open_prs"] == []
        assert result["merged_today"] == []
        assert result["queued_features"] == []

    def test_no_register_file_empty_arrays(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        # No dispatch_register.ndjson present, gh also returns nothing
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            result = build_pr_queue_state(state_dir)
        assert result["open_prs"] == []
        assert result["merged_today"] == []
        assert result["queued_features"] == []

    def test_gh_failure_produces_empty_prs(self, tmp_path):
        state_dir, _ = _make_dirs(tmp_path)
        # _get_open_prs already returns [] on gh failure; verify contract
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            result = build_pr_queue_state(state_dir, register_events=[])
        assert result["open_prs"] == []


# ---------------------------------------------------------------------------
# 3. Integration: hooked into build_t0_state output
# ---------------------------------------------------------------------------

class TestBuildT0StateIntegration:
    def test_pr_queue_key_in_state(self, tmp_path):
        state_dir, dispatch_dir = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        assert "pr_queue" in state, "pr_queue key missing from build_t0_state output"

    def test_pr_queue_has_correct_schema(self, tmp_path):
        state_dir, dispatch_dir = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        pr_queue = state.get("pr_queue", {})
        assert pr_queue.get("schema") == "pr_queue/1.0"

    def test_pr_queue_is_dict(self, tmp_path):
        state_dir, dispatch_dir = _make_dirs(tmp_path)
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            state = build_t0_state(state_dir=state_dir, dispatch_dir=dispatch_dir)
        assert isinstance(state.get("pr_queue"), dict)


# ---------------------------------------------------------------------------
# 4. Gates map
# ---------------------------------------------------------------------------

class TestGatesMap:
    def test_gate_passed_recorded(self):
        events = [
            {"event": "gate_passed", "pr_number": 10, "gate": "codex",
             "timestamp": "2026-04-28T00:00:00Z"},
        ]
        result = _build_gates_map(events)
        assert result[10]["gates_passed"] == ["codex"]
        assert result[10]["blocked_on"] == []

    def test_gate_failed_recorded(self):
        events = [
            {"event": "gate_failed", "pr_number": 10, "gate": "gemini",
             "timestamp": "2026-04-28T00:00:00Z"},
        ]
        result = _build_gates_map(events)
        assert result[10]["blocked_on"] == ["gemini"]

    def test_gate_failed_removes_from_passed(self):
        events = [
            {"event": "gate_passed", "pr_number": 10, "gate": "codex",
             "timestamp": "2026-04-28T00:00:00Z"},
            {"event": "gate_failed", "pr_number": 10, "gate": "codex",
             "timestamp": "2026-04-28T00:00:01Z"},
        ]
        result = _build_gates_map(events)
        assert "codex" not in result[10]["gates_passed"]
        assert "codex" in result[10]["blocked_on"]

    def test_multiple_gates_accumulated(self):
        events = [
            {"event": "gate_passed", "pr_number": 5, "gate": "codex",
             "timestamp": "2026-04-28T00:00:00Z"},
            {"event": "gate_passed", "pr_number": 5, "gate": "gemini",
             "timestamp": "2026-04-28T00:00:01Z"},
        ]
        result = _build_gates_map(events)
        assert set(result[5]["gates_passed"]) == {"codex", "gemini"}

    def test_no_pr_number_skipped(self):
        events = [
            {"event": "gate_passed", "gate": "codex",
             "timestamp": "2026-04-28T00:00:00Z"},
        ]
        result = _build_gates_map(events)
        assert result == {}

    def test_empty_events(self):
        assert _build_gates_map([]) == {}


# ---------------------------------------------------------------------------
# 5. Queued features
# ---------------------------------------------------------------------------

class TestQueuedFeatures:
    def test_created_dispatch_appears(self):
        events = [
            {"dispatch_id": "d-001", "event": "dispatch_created",
             "timestamp": "2026-04-28T00:00:00Z"},
        ]
        result = _build_queued_features(events)
        assert any(e["dispatch_id"] == "d-001" for e in result)

    def test_completed_dispatch_excluded(self):
        events = [
            {"dispatch_id": "d-002", "event": "dispatch_created",
             "timestamp": "2026-04-28T00:00:00Z"},
            {"dispatch_id": "d-002", "event": "dispatch_completed",
             "timestamp": "2026-04-28T00:00:01Z"},
        ]
        result = _build_queued_features(events)
        assert not any(e["dispatch_id"] == "d-002" for e in result)

    def test_failed_dispatch_excluded(self):
        events = [
            {"dispatch_id": "d-003", "event": "dispatch_created",
             "timestamp": "2026-04-28T00:00:00Z"},
            {"dispatch_id": "d-003", "event": "dispatch_failed",
             "timestamp": "2026-04-28T00:00:01Z"},
        ]
        result = _build_queued_features(events)
        assert not any(e["dispatch_id"] == "d-003" for e in result)

    def test_merged_pr_excluded(self):
        events = [
            {"dispatch_id": "d-004", "event": "dispatch_created",
             "timestamp": "2026-04-28T00:00:00Z"},
            {"dispatch_id": "d-004", "event": "pr_merged",
             "timestamp": "2026-04-28T00:00:01Z"},
        ]
        result = _build_queued_features(events)
        assert not any(e["dispatch_id"] == "d-004" for e in result)

    def test_feature_id_propagated(self):
        events = [
            {"dispatch_id": "d-005", "event": "dispatch_created",
             "feature_id": "F99", "timestamp": "2026-04-28T00:00:00Z"},
        ]
        result = _build_queued_features(events)
        match = next(e for e in result if e["dispatch_id"] == "d-005")
        assert match["feature_id"] == "F99"

    def test_empty_events_empty_features(self):
        assert _build_queued_features([]) == []


# ---------------------------------------------------------------------------
# 6. Atomic write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_file_written(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            out = write_pr_queue_state(state_dir, register_events=[])
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["schema"] == "pr_queue/1.0"

    def test_no_tmp_files_left(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            write_pr_queue_state(state_dir, register_events=[])
        tmp_files = list(state_dir.glob("*.tmp.*"))
        assert tmp_files == [], f"Temp files left: {tmp_files}"

    def test_output_is_valid_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            out = write_pr_queue_state(state_dir, register_events=[])
        parsed = json.loads(out.read_text())
        assert isinstance(parsed, dict)

    def test_creates_state_dir_if_missing(self, tmp_path):
        state_dir = tmp_path / "state" / "nested"
        with patch("pr_queue_state._get_open_prs", return_value=[]), \
             patch("pr_queue_state._get_merged_today", return_value=[]):
            out = write_pr_queue_state(state_dir, register_events=[])
        assert out.exists()
