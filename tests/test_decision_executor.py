#!/usr/bin/env python3
"""Tests for decision_parser and decision_executor.

Covers:
- parse_decision: DISPATCH, WAIT, malformed input
- execute_decision: DISPATCH writes pending dir, duplicate blocking, max-cycle guard
- Other decision types: WAIT, COMPLETE, REJECT, ESCALATE
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Add scripts/lib to path
_LIB_DIR = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(_LIB_DIR))

from decision_parser import parse_decision, extract_json, collect_text_from_stream  # noqa: E402
import decision_executor  # noqa: E402
from decision_executor import (  # noqa: E402
    execute_decision,
    reset_cycle_counter,
    MAX_DISPATCHES_PER_CYCLE,
    _task_hash,
    _save_recent_hashes,
    _now_utc,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stream_result(decision_json: dict) -> str:
    """Build a minimal stream-json NDJSON string with a result event."""
    result_line = json.dumps({"type": "result", "result": json.dumps(decision_json)})
    return result_line


def _dispatch_decision(target: str = "T1", task: str = "Implement feature X") -> dict:
    return {
        "decision": "DISPATCH",
        "dispatch_target": target,
        "dispatch_task": task,
        "role": "backend-developer",
    }


# ---------------------------------------------------------------------------
# parse_decision tests
# ---------------------------------------------------------------------------

class TestParseDispatchDecision:
    def test_valid_dispatch_json_is_parsed(self):
        raw = _make_stream_result({"decision": "DISPATCH", "dispatch_target": "T1", "dispatch_task": "do work"})
        decision_type, parsed = parse_decision(raw)
        assert decision_type == "DISPATCH"
        assert parsed is not None
        assert parsed["dispatch_target"] == "T1"

    def test_dispatch_decision_key_case_insensitive(self):
        raw = _make_stream_result({"decision": "dispatch", "dispatch_target": "T2", "dispatch_task": "test stuff"})
        decision_type, parsed = parse_decision(raw)
        assert decision_type == "DISPATCH"

    def test_fallback_from_text_blocks(self):
        # No result event — only text block content
        text_block = json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": json.dumps({"decision": "DISPATCH", "dispatch_target": "T3", "dispatch_task": "x"})},
        })
        decision_type, parsed = parse_decision(text_block)
        assert decision_type == "DISPATCH"


class TestParseWaitDecision:
    def test_wait_decision_returns_correct_type(self):
        raw = _make_stream_result({"decision": "WAIT", "reason": "terminal busy"})
        decision_type, parsed = parse_decision(raw)
        assert decision_type == "WAIT"
        assert parsed is not None
        assert parsed["reason"] == "terminal busy"


class TestParseMalformed:
    def test_garbage_input_returns_unknown(self):
        decision_type, parsed = parse_decision("not json at all just garbage text")
        assert decision_type == "UNKNOWN"
        assert parsed is None

    def test_empty_string_returns_unknown(self):
        decision_type, parsed = parse_decision("")
        assert decision_type == "UNKNOWN"
        assert parsed is None

    def test_unknown_decision_type_normalized(self):
        raw = _make_stream_result({"decision": "FROBULATE", "reason": "weird"})
        decision_type, parsed = parse_decision(raw)
        assert decision_type == "UNKNOWN"

    def test_missing_decision_key_returns_unknown(self):
        raw = _make_stream_result({"reason": "no decision key"})
        decision_type, parsed = parse_decision(raw)
        assert decision_type == "UNKNOWN"


# ---------------------------------------------------------------------------
# execute_decision — DISPATCH tests
# ---------------------------------------------------------------------------

class TestExecuteDispatchWritesPending:
    def test_dispatch_creates_file_in_pending_dir(self, tmp_path, monkeypatch):
        """DISPATCH creates a dispatch.json in the pending/ directory."""
        dispatches_dir = tmp_path / "dispatches"
        pending_dir = dispatches_dir / "pending"
        pending_dir.mkdir(parents=True)

        # Create a skills dir with the role subdir so role validation passes
        skills_dir = tmp_path / "skills"
        (skills_dir / "backend-developer").mkdir(parents=True)

        # Patch headless_dispatch_writer to use tmp_path
        import headless_dispatch_writer

        monkeypatch.setattr(headless_dispatch_writer, "_dispatch_dir", lambda: dispatches_dir)
        monkeypatch.setattr(headless_dispatch_writer, "_skills_dir", lambda: skills_dir)

        reset_cycle_counter()

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        decision = _dispatch_decision(target="T1", task="Build the auth module")
        status = execute_decision(decision, "test_trigger", state_dir=state_dir)

        assert status == "dispatched"
        # Find the created pending dir
        created = list(pending_dir.iterdir())
        assert len(created) == 1
        dispatch_json = created[0] / "dispatch.json"
        assert dispatch_json.exists()
        payload = json.loads(dispatch_json.read_text())
        assert payload["terminal"] == "T1"
        assert payload["track"] == "A"
        assert "Build the auth module" in payload["instruction"]


class TestDuplicateDispatchBlocked:
    def test_same_dispatch_task_within_window_is_refused(self, tmp_path, monkeypatch):
        """Same dispatch_task within 30 min is blocked."""
        import headless_dispatch_writer

        dispatches_dir = tmp_path / "dispatches"
        (dispatches_dir / "pending").mkdir(parents=True)
        skills_dir = tmp_path / "skills"
        (skills_dir / "backend-developer").mkdir(parents=True)

        monkeypatch.setattr(headless_dispatch_writer, "_dispatch_dir", lambda: dispatches_dir)
        monkeypatch.setattr(headless_dispatch_writer, "_skills_dir", lambda: skills_dir)

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        task_text = "Implement duplicate detection"
        decision = _dispatch_decision(task=task_text)

        # Seed the hash file with the same task hash
        h = _task_hash(task_text)
        _save_recent_hashes(state_dir, {h: _now_utc()})

        reset_cycle_counter()
        status = execute_decision(decision, "trigger", state_dir=state_dir)

        assert status.startswith("duplicate:")


class TestMaxDispatchesEnforced:
    def test_exceeding_max_dispatches_returns_error(self, tmp_path, monkeypatch):
        """More than MAX_DISPATCHES_PER_CYCLE dispatches are refused."""
        import headless_dispatch_writer

        dispatches_dir = tmp_path / "dispatches"
        (dispatches_dir / "pending").mkdir(parents=True)
        skills_dir = tmp_path / "skills"
        (skills_dir / "backend-developer").mkdir(parents=True)

        monkeypatch.setattr(headless_dispatch_writer, "_dispatch_dir", lambda: dispatches_dir)
        monkeypatch.setattr(headless_dispatch_writer, "_skills_dir", lambda: skills_dir)

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        reset_cycle_counter()

        statuses = []
        for i in range(MAX_DISPATCHES_PER_CYCLE + 1):
            # Each task must be unique to avoid duplicate detection
            decision = _dispatch_decision(task=f"Unique task number {i} at {time.monotonic()}")
            status = execute_decision(decision, "trigger", state_dir=state_dir)
            statuses.append(status)

        # First MAX_DISPATCHES_PER_CYCLE should succeed
        assert all(s == "dispatched" for s in statuses[:MAX_DISPATCHES_PER_CYCLE])
        # The next one should be refused
        assert statuses[MAX_DISPATCHES_PER_CYCLE].startswith("error:")


# ---------------------------------------------------------------------------
# Other decision types
# ---------------------------------------------------------------------------

class TestOtherDecisionTypes:
    def test_wait_decision_returns_waited(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        status = execute_decision({"decision": "WAIT", "reason": "nothing to do"}, "trigger", state_dir=state_dir)
        assert status == "waited"

    def test_complete_decision_returns_completed(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        status = execute_decision({"decision": "COMPLETE", "reason": "all done"}, "trigger", state_dir=state_dir)
        assert status == "completed"

    def test_reject_decision_returns_rejected(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        status = execute_decision({"decision": "REJECT", "reason": "missing tests"}, "trigger", state_dir=state_dir)
        assert status == "rejected"

    def test_escalate_decision_writes_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        status = execute_decision(
            {"decision": "ESCALATE", "reason": "architectural blocker"},
            "trigger",
            state_dir=state_dir,
        )
        assert status == "escalated"
        # Check escalation file was created
        esc_files = list((state_dir / "escalations").glob("escalation_*.json"))
        assert len(esc_files) == 1
        payload = json.loads(esc_files[0].read_text())
        assert "architectural blocker" in payload["reason"]

    def test_dry_run_dispatch_skips_file_write(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        dispatches_dir = tmp_path / "dispatches"
        (dispatches_dir / "pending").mkdir(parents=True)

        reset_cycle_counter()
        decision = _dispatch_decision(task="dry run task")
        status = execute_decision(decision, "trigger", state_dir=state_dir, dry_run=True)

        assert status == "dry-run dispatch"
        # No files written
        assert list((dispatches_dir / "pending").iterdir()) == []
