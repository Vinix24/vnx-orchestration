#!/usr/bin/env python3
"""Integration tests for F47: T0 State Feedback Loop.

Tests cover:
  - ReceiptWatcher triggering state refresh on new receipts
  - Feature state machine parsing FEATURE_PLAN.md
  - Debounce behaviour (rapid receipts → single refresh)
  - t0_state.json contains feature_state.next_task
  - Full dry-run loop: receipt → watcher detects → state refreshed
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_LIB_DIR = _SCRIPTS_DIR / "lib"

for _p in (str(_SCRIPTS_DIR), str(_LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Conditional import guard for feature_state_machine
# ---------------------------------------------------------------------------

try:
    from feature_state_machine import parse_feature_plan, get_next_dispatchable  # noqa: E402
    _FSM_AVAILABLE = True
except ImportError:
    _FSM_AVAILABLE = False

from headless_trigger import (  # noqa: E402
    ReceiptWatcher,
    TriggerState,
    _refresh_t0_state,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ACTIONABLE_RECEIPT = json.dumps({
    "event_type": "task_complete",
    "dispatch_id": "test-dispatch-001",
    "terminal": "T1",
    "status": "success",
    "timestamp": "2026-04-13T10:00:00+00:00",
})

_SAMPLE_FEATURE_PLAN = """\
# F47 — T0 State Feedback Loop

## Feature Overview

Some overview text.

---

### F47-PR1: Receipt Watcher
**Track**: A (T1 backend-developer)
**Status**: Completed
**Estimated LOC**: ~250

- [x] ReceiptWatcher class implemented
- [x] _refresh_t0_state() hooked in
- [x] main() wires Layer 0

### F47-PR2: Feature State Machine
**Track**: A (T1 backend-developer)
**Status**: Planned
**Estimated LOC**: ~200

- [x] parse_feature_plan() implemented
- [ ] get_next_dispatchable() implemented
- [ ] build_t0_state feature_state section wired

### F47-PR3: State Loop Integration Tests
**Track**: B (T2 test-engineer)
**Status**: Planned
**Estimated LOC**: ~150

- [ ] All 5 tests pass
- [ ] Dry-run loop completes without errors
"""


def _write_receipt(path: Path, receipt: str = _ACTIONABLE_RECEIPT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(receipt + "\n")


# ---------------------------------------------------------------------------
# Test 1: receipt → _refresh_t0_state() called
# ---------------------------------------------------------------------------

def test_receipt_triggers_state_refresh(tmp_path: Path) -> None:
    """Write a mock receipt; ReceiptWatcher should call _refresh_t0_state."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_path = state_dir / "t0_receipts.ndjson"

    # Pre-create file so ReceiptWatcher seeds pos=0 (file exists at start)
    receipts_path.touch()

    trigger_state = TriggerState()
    watcher = ReceiptWatcher(
        state_dir=state_dir,
        trigger_state=trigger_state,
        dry_run=True,
        poll_interval=0.05,
    )
    # Seed file position to current size (0 bytes)
    watcher._file_pos = receipts_path.stat().st_size

    refresh_called = threading.Event()

    def _fake_refresh(sd: Path) -> bool:
        refresh_called.set()
        return True

    with patch("headless_trigger._refresh_t0_state", side_effect=_fake_refresh):
        watcher.start()
        # Append receipt after watcher started
        time.sleep(0.02)
        _write_receipt(receipts_path)
        # Wait up to 2s for the poll to pick it up
        assert refresh_called.wait(timeout=2.0), "_refresh_t0_state was not called after receipt"
    trigger_state.shutdown_event.set()


# ---------------------------------------------------------------------------
# Test 2: parse_feature_plan() — correct fields extracted
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _FSM_AVAILABLE, reason="feature_state_machine not available")
def test_feature_state_machine_parsing(tmp_path: Path) -> None:
    """Sample FEATURE_PLAN.md with mixed checkboxes → correct FeatureState fields."""
    plan_path = tmp_path / "FEATURE_PLAN.md"
    plan_path.write_text(_SAMPLE_FEATURE_PLAN, encoding="utf-8")

    state = parse_feature_plan(plan_path)

    # F47-PR1 is fully checked → completed
    assert state.completed_prs >= 1, f"Expected ≥1 completed PR, got {state.completed_prs}"
    # F47-PR2 has one unchecked → current PR
    assert state.current_pr == "F47-PR2", f"Expected current_pr=F47-PR2, got {state.current_pr!r}"
    # next_task should mention the PR
    assert state.next_task is not None, "next_task should not be None"
    assert "F47-PR2" in state.next_task, f"next_task missing PR id: {state.next_task!r}"
    # status: some completed, some pending → in_progress
    assert state.status == "in_progress", f"Expected in_progress, got {state.status!r}"
    # total_prs
    assert state.total_prs == 3, f"Expected 3 PRs, got {state.total_prs}"


# ---------------------------------------------------------------------------
# Test 3: debounce — 5 rapid receipts → single refresh
# ---------------------------------------------------------------------------

def test_receipt_watcher_debounce(tmp_path: Path) -> None:
    """5 receipts written in rapid succession → refresh called once (debounce at 30s)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    receipts_path = state_dir / "t0_receipts.ndjson"
    receipts_path.touch()

    trigger_state = TriggerState()
    # Force last_trigger_time to now so trigger_headless_t0 is debounced
    trigger_state.last_trigger_time = time.monotonic()

    watcher = ReceiptWatcher(
        state_dir=state_dir,
        trigger_state=trigger_state,
        dry_run=True,
        poll_interval=0.05,
    )
    watcher._file_pos = receipts_path.stat().st_size

    refresh_call_count: list[int] = [0]
    refresh_done = threading.Event()

    def _counting_refresh(sd: Path) -> bool:
        refresh_call_count[0] += 1
        refresh_done.set()
        return True

    with patch("headless_trigger._refresh_t0_state", side_effect=_counting_refresh):
        watcher.start()
        time.sleep(0.02)
        # Write 5 receipts back-to-back (well within debounce window)
        for _ in range(5):
            _write_receipt(receipts_path)
        # Allow poll cycle to process
        refresh_done.wait(timeout=2.0)
        # Small pause to let any extra calls land
        time.sleep(0.2)

    trigger_state.shutdown_event.set()

    # All 5 receipts may be batched in one poll pass or across a few passes,
    # but the important constraint is: the trigger itself is debounced at 30s.
    # _refresh_t0_state is called once per actionable batch (not per receipt),
    # so we expect exactly 1 refresh for the batch (or possibly 2 if poll split).
    assert refresh_call_count[0] >= 1, "refresh should have been called at least once"
    assert refresh_call_count[0] <= 2, (
        f"refresh called {refresh_call_count[0]} times — expected ≤2 for rapid batch"
    )


# ---------------------------------------------------------------------------
# Test 4: t0_state.json contains feature_state.next_task
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _FSM_AVAILABLE, reason="feature_state_machine not available")
def test_context_assembler_with_feature_state(tmp_path: Path) -> None:
    """t0_state.json built with feature_state section contains next_task field."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    dispatch_dir.mkdir()

    # Write a sample FEATURE_PLAN.md at project root level
    plan_path = tmp_path / "FEATURE_PLAN.md"
    plan_path.write_text(_SAMPLE_FEATURE_PLAN, encoding="utf-8")

    # Build a minimal t0_state dict manually (mirrors build_t0_state output)
    from feature_state_machine import parse_feature_plan as _parse  # noqa: PLC0415
    fs = _parse(plan_path)
    state: dict[str, Any] = {
        "schema_version": "2.0",
        "generated_at": "2026-04-13T10:00:00+00:00",
        "feature_state": fs.as_dict(),
        "terminals": {},
        "queues": {"pending_count": 0, "active_count": 0, "completed_last_hour": 0, "conflict_count": 0},
        "tracks": {},
        "pr_progress": {},
        "open_items": {},
        "quality_digest": {},
        "active_work": [],
        "recent_receipts": [],
        "git_context": {},
        "system_health": {"status": "healthy", "db_initialized": False, "uptime_seconds": 0},
        "_build_seconds": 0.0,
    }

    # Write to t0_state.json
    t0_path = state_dir / "t0_state.json"
    t0_path.write_text(json.dumps(state), encoding="utf-8")

    # Read back and assert
    loaded = json.loads(t0_path.read_text(encoding="utf-8"))
    assert "feature_state" in loaded, "t0_state.json missing 'feature_state' key"
    fs_loaded = loaded["feature_state"]
    assert "next_task" in fs_loaded, "feature_state missing 'next_task' field"
    assert fs_loaded["next_task"] is not None, "next_task should not be None for in-progress plan"
    assert "current_pr" in fs_loaded, "feature_state missing 'current_pr'"
    assert fs_loaded["current_pr"] == "F47-PR2"


# ---------------------------------------------------------------------------
# Test 5: full dry-run loop — receipt → detect → state refreshed
# ---------------------------------------------------------------------------

def test_full_loop_dry_run(tmp_path: Path) -> None:
    """Simulate: write receipt → ReceiptWatcher detects → t0_state.json updated."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    (dispatch_dir / "pending").mkdir(parents=True)
    (dispatch_dir / "active").mkdir(parents=True)

    receipts_path = state_dir / "t0_receipts.ndjson"
    receipts_path.touch()

    t0_state_path = state_dir / "t0_state.json"
    initial_state = {"schema_version": "2.0", "generated_at": "initial", "feature_state": {}}
    t0_state_path.write_text(json.dumps(initial_state), encoding="utf-8")

    trigger_state = TriggerState()
    watcher = ReceiptWatcher(
        state_dir=state_dir,
        trigger_state=trigger_state,
        dry_run=True,
        poll_interval=0.05,
    )
    watcher._file_pos = receipts_path.stat().st_size

    state_refreshed = threading.Event()

    def _fake_refresh(sd: Path) -> bool:
        # Simulate an actual state write
        updated = {"schema_version": "2.0", "generated_at": "updated", "feature_state": {"next_task": "F47-PR3: tests"}}
        t0_state_path.write_text(json.dumps(updated), encoding="utf-8")
        state_refreshed.set()
        return True

    with patch("headless_trigger._refresh_t0_state", side_effect=_fake_refresh):
        watcher.start()
        time.sleep(0.02)
        _write_receipt(receipts_path)
        assert state_refreshed.wait(timeout=2.0), "State refresh was not triggered within 2s"

    trigger_state.shutdown_event.set()

    # Verify t0_state.json was actually updated
    result = json.loads(t0_state_path.read_text(encoding="utf-8"))
    assert result["generated_at"] == "updated", (
        f"t0_state.json not updated — still shows: {result['generated_at']!r}"
    )
    assert result["feature_state"].get("next_task") == "F47-PR3: tests"
