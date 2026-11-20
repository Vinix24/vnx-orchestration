#!/usr/bin/env python3
"""Ensure completion attempt logging still writes queue_event_log entries."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pr_queue_manager import PRQueueManager

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent


def _apply_env(tmp_path: Path, monkeypatch) -> Path:
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VNX_HOME", str(VNX_ROOT))
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("VNX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("VNX_STATE_DIR", str(state_dir))
    monkeypatch.delenv("VNX_PR_QUEUE_DUAL_WRITE_LEGACY", raising=False)
    monkeypatch.delenv("VNX_STATE_DUAL_WRITE_LEGACY", raising=False)
    monkeypatch.delenv("VNX_STATE_SIMPLIFICATION_ROLLBACK", raising=False)

    return state_dir


def test_completion_attempt_event_logged(tmp_path: Path, monkeypatch):
    state_dir = _apply_env(tmp_path, monkeypatch)

    manager = PRQueueManager()
    manager.log_queue_event(
        event="completion_attempt",
        pr_id="PR-77",
        dispatch_id="DISP-77",
        auto_completed=True,
        reason="success",
        extraction_method="receipt_json",
    )

    event_log = state_dir / "queue_event_log.jsonl"
    assert event_log.exists()

    lines = [line for line in event_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["event"] == "completion_attempt"
    assert entry["pr_id"] == "PR-77"
    assert entry["dispatch_id"] == "DISP-77"
    assert entry["auto_completed"] is True
    assert entry["reason"] == "success"
    assert entry["extraction_method"] == "receipt_json"
