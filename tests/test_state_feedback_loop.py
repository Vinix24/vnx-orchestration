#!/usr/bin/env python3
"""Integration tests for F47: T0 State Feedback Loop.

Tests cover:
  - ReceiptWatcher triggering state refresh on new receipts
  - Debounce behaviour (rapid receipts → single refresh)
  - Full dry-run loop: receipt → watcher detects → state refreshed
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

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
# Imports
# ---------------------------------------------------------------------------

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
# Test 2: debounce — 5 rapid receipts → single refresh
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
# Test 3: full dry-run loop — receipt → detect → state refreshed
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
