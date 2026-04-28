#!/usr/bin/env python3
"""Regression tests: update_confidence_from_outcome called exactly once per dispatch.

After VNX-R4, the canonical confidence-update path is:
  worker appends task_complete/task_failed receipt
  → append_receipt_payload → _update_confidence_from_receipt → update_confidence_from_outcome

The duplicate call that previously lived in deliver_with_recovery() has been removed.
These tests confirm:
  A) task_complete dispatch → exactly one confidence update call (via append_receipt path)
  B) task_failed dispatch → exactly one confidence update call (via append_receipt path)
  C) dispatch_metadata outcome capture is unaffected
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
SCRIPTS_LIB = SCRIPTS_DIR / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_receipt(event_type: str, dispatch_id: str, terminal: str, status: str) -> dict:
    return {
        "event_type": event_type,
        "dispatch_id": dispatch_id,
        "terminal": terminal,
        "status": status,
        "timestamp": "2026-04-28T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Case A: task_complete receipt triggers exactly ONE confidence update
# ---------------------------------------------------------------------------

class TestTaskCompleteDedup:
    """update_confidence_from_outcome called once on task_complete receipt."""

    def test_single_call_on_task_complete(self, tmp_path):
        """Simulate append_receipt_payload receiving a task_complete receipt.

        Asserts update_confidence_from_outcome is called exactly once — not twice.
        """
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Create minimal DB so the call doesn't short-circuit on exists() check
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS success_patterns "
            "(id INTEGER PRIMARY KEY, confidence_score REAL, usage_count INTEGER, "
            " source_dispatch_ids TEXT, last_used TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS confidence_events "
            "(dispatch_id TEXT, terminal TEXT, outcome TEXT, patterns_boosted INTEGER, "
            " patterns_decayed INTEGER, confidence_change REAL, occurred_at TEXT)"
        )
        conn.commit()
        conn.close()

        receipt = _make_receipt("task_complete", "dispatch-A1", "T1", "success")
        call_count = []

        real_update = None
        try:
            from intelligence_persist import update_confidence_from_outcome as _real
            real_update = _real
        except ImportError:
            pass

        def counting_upcf(db, dispatch_id, terminal, outcome):
            call_count.append((dispatch_id, terminal, outcome))
            if real_update:
                real_update(db, dispatch_id, terminal, outcome)

        with patch("append_receipt.resolve_state_dir", return_value=db_path.parent), \
             patch("append_receipt._register_quality_open_items"), \
             patch("append_receipt._emit_dispatch_register"), \
             patch("append_receipt._maybe_trigger_state_rebuild"), \
             patch("append_receipt._enrich_completion_receipt", side_effect=lambda r: r), \
             patch("append_receipt._count_quality_violations", return_value=0):
            import append_receipt
            # Patch update_confidence_from_outcome inside the module that imports it
            with patch.object(
                sys.modules.get("intelligence_persist", MagicMock()),
                "update_confidence_from_outcome",
                side_effect=counting_upcf,
            ):
                # Manually call _update_confidence_from_receipt directly
                append_receipt._update_confidence_from_receipt(receipt)

        assert len(call_count) == 1, (
            f"Expected exactly 1 call to update_confidence_from_outcome, got {len(call_count)}: {call_count}"
        )
        assert call_count[0][2] == "success"

    def test_no_call_for_subprocess_completion_receipt(self, tmp_path):
        """subprocess_completion event_type must NOT trigger a confidence update.

        This was the type written by _write_receipt in subprocess_dispatch.py.
        Confidence updates only happen when workers write task_complete/task_failed.
        """
        receipt = _make_receipt("subprocess_completion", "dispatch-A2", "T1", "done")
        call_count = []

        def counting_upcf(*args, **kwargs):
            call_count.append(args)

        import append_receipt
        with patch.object(
            sys.modules.get("intelligence_persist", MagicMock()),
            "update_confidence_from_outcome",
            side_effect=counting_upcf,
        ):
            append_receipt._update_confidence_from_receipt(receipt)

        assert len(call_count) == 0, (
            "subprocess_completion receipt must not trigger confidence update"
        )


# ---------------------------------------------------------------------------
# Case B: task_failed receipt triggers exactly ONE confidence update
# ---------------------------------------------------------------------------

class TestTaskFailedDedup:
    """update_confidence_from_outcome called once on task_failed receipt."""

    def test_single_call_on_task_failed(self, tmp_path):
        db_path = tmp_path / "state" / "quality_intelligence.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS success_patterns "
            "(id INTEGER PRIMARY KEY, confidence_score REAL, usage_count INTEGER, "
            " source_dispatch_ids TEXT, last_used TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS confidence_events "
            "(dispatch_id TEXT, terminal TEXT, outcome TEXT, patterns_boosted INTEGER, "
            " patterns_decayed INTEGER, confidence_change REAL, occurred_at TEXT)"
        )
        conn.commit()
        conn.close()

        receipt = _make_receipt("task_failed", "dispatch-B1", "T2", "failure")
        call_count = []

        def counting_upcf(db, dispatch_id, terminal, outcome):
            call_count.append((dispatch_id, terminal, outcome))

        import append_receipt
        with patch("append_receipt.resolve_state_dir", return_value=db_path.parent):
            with patch.object(
                sys.modules.get("intelligence_persist", MagicMock()),
                "update_confidence_from_outcome",
                side_effect=counting_upcf,
            ):
                append_receipt._update_confidence_from_receipt(receipt)

        assert len(call_count) == 1, (
            f"Expected exactly 1 confidence update for task_failed, got {len(call_count)}"
        )
        assert call_count[0][2] == "failure"


# ---------------------------------------------------------------------------
# Case C: subprocess_dispatch.py no longer imports intelligence_persist
# ---------------------------------------------------------------------------

class TestNoDirectImport:
    """deliver_with_recovery path no longer calls update_confidence_from_outcome."""

    def test_intelligence_persist_not_called_in_subprocess_dispatch(self):
        """Parse subprocess_dispatch source and confirm no import of intelligence_persist.

        This is a static check — it ensures the source-level dedup fix is present
        regardless of test environment mocking.
        """
        source_path = SCRIPTS_LIB / "subprocess_dispatch.py"
        assert source_path.exists(), f"subprocess_dispatch.py not found at {source_path}"

        source = source_path.read_text(encoding="utf-8")

        # The inline import that caused the duplicate should be gone
        assert "from intelligence_persist import update_confidence_from_outcome" not in source, (
            "Found removed duplicate import 'from intelligence_persist import "
            "update_confidence_from_outcome' — dedup fix was reverted"
        )

    def test_pattern_confidence_calls_still_present(self):
        """_update_pattern_confidence calls must remain — they update pattern_usage table,
        a different linkage than update_confidence_from_outcome (source_dispatch_ids)."""
        source_path = SCRIPTS_LIB / "subprocess_dispatch.py"
        source = source_path.read_text(encoding="utf-8")

        assert "_update_pattern_confidence" in source, (
            "_update_pattern_confidence call missing — this is NOT the duplicate; it should stay"
        )


# ---------------------------------------------------------------------------
# Case C extended: dispatch_metadata outcome capture unaffected
# ---------------------------------------------------------------------------

class TestOutcomeCaptureUnchanged:
    """_capture_dispatch_outcome still fires — only the duplicate confidence call was removed."""

    def test_capture_dispatch_outcome_still_in_source(self):
        source_path = SCRIPTS_LIB / "subprocess_dispatch.py"
        source = source_path.read_text(encoding="utf-8")
        assert "_capture_dispatch_outcome" in source, (
            "_capture_dispatch_outcome missing — outcome recording regressed"
        )

    def test_capture_dispatch_outcome_called_after_receipt(self):
        """In the success path, _capture_dispatch_outcome comes after _write_receipt.

        Verifies ordering hasn't changed during dedup edit.
        """
        source_path = SCRIPTS_LIB / "subprocess_dispatch.py"
        source = source_path.read_text(encoding="utf-8")

        write_pos = source.find("_write_receipt(")
        capture_pos = source.find("_capture_dispatch_outcome(")

        assert write_pos != -1, "_write_receipt not found"
        assert capture_pos != -1, "_capture_dispatch_outcome not found"
        assert capture_pos > write_pos, (
            "_capture_dispatch_outcome must appear after _write_receipt in source"
        )
