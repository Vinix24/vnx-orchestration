#!/usr/bin/env python3
"""Tests for the subprocess lane's report writing.

The subprocess lane writes its own contract-valid close-out report
(_ensure_unified_report) on success and emits no report at all on a
budget-exhausted failure; it never calls dispatch_govern.govern().

Verifies that:
  - _ensure_unified_report (the contract-valid close-out report writer) runs on
    success and dispatch_govern.govern() is never called.
  - No report is emitted on final failure.
  - _write_receipt is called exactly once in every case.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import subprocess_dispatch  # noqa: E402
from subprocess_dispatch_internals.delivery_runtime import _SubprocessResult  # noqa: E402
from subprocess_dispatch_internals.recovery import (  # noqa: E402
    _handle_final_failure,
    _handle_success,
)


def _success_result(**overrides) -> _SubprocessResult:
    defaults = dict(
        success=True,
        session_id="sess-govern-test",
        event_count=3,
        manifest_path="/active/dispatch/manifest.json",
        touched_files=frozenset(),
    )
    defaults.update(overrides)
    return _SubprocessResult(**defaults)


class _HandleSuccessTestBase(unittest.TestCase):
    """Shared patch scaffolding for _handle_success — mirrors test_subprocess_dispatch_stuck_count.py."""

    def _run_handle_success(self, **kwargs):
        monitor = MagicMock()
        monitor.stuck_count = 0
        monitor.mark_completed = MagicMock()

        with patch.object(subprocess_dispatch, "_get_commit_hash", return_value="abc123"), \
             patch.object(subprocess_dispatch, "_check_commit_since", return_value=False), \
             patch.object(subprocess_dispatch, "_write_receipt") as mock_receipt, \
             patch.object(subprocess_dispatch, "_ensure_unified_report") as mock_stub, \
             patch.object(subprocess_dispatch, "_update_pattern_confidence", return_value=0), \
             patch.object(subprocess_dispatch, "_capture_dispatch_outcome"), \
             patch.object(subprocess_dispatch, "cleanup_worker_exit"), \
             patch("dispatch_govern.govern") as mock_dg_govern:
            _handle_success(
                dispatch_id="dispatch-govern-success",
                terminal_id="T1",
                attempt=0,
                sub_result=_success_result(),
                monitor=monitor,
                auto_commit=False,
                gate="",
                pre_dispatch_dirty=frozenset(),
                manifest_paths=None,
                commit_hash_before="abc123",
                dispatch_start_ts="2026-07-11T00:00:00+00:00",
                pre_sha="abc123",
                lease_generation=None,
                model="sonnet",
                pr_id=None,
                mandate_id=None,
                instruction="Do the thing.",
                role="backend-developer",
                **kwargs,
            )
        return mock_receipt, mock_stub, mock_dg_govern


class TestHandleSuccessCloseoutReport(_HandleSuccessTestBase):
    def test_uses_closeout_report_writer(self):
        mock_receipt, mock_stub, mock_dg_govern = self._run_handle_success()

        # The success path writes a contract-valid close-out report and must pass
        # the model it ran with — without it the receipt converter refuses the report.
        mock_stub.assert_called_once_with(
            "dispatch-govern-success", "T1", "done",
            model="sonnet", provider="claude", changed_files=[],
        )
        mock_dg_govern.assert_not_called()
        mock_receipt.assert_called_once()

class _HandleFinalFailureTestBase(unittest.TestCase):
    def _run_handle_final_failure(self, **kwargs):
        monitor = MagicMock()
        monitor.stuck_count = 0
        monitor.mark_completed = MagicMock()

        failed_result = _SubprocessResult(
            success=False,
            session_id=None,
            event_count=1,
            manifest_path="/active/dispatch/manifest.json",
            touched_files=frozenset(),
        )

        with patch.object(subprocess_dispatch, "_write_receipt") as mock_receipt, \
             patch.object(subprocess_dispatch, "_auto_stash_changes", return_value=False), \
             patch.object(subprocess_dispatch, "_update_pattern_confidence", return_value=0), \
             patch.object(subprocess_dispatch, "_capture_dispatch_outcome"), \
             patch.object(subprocess_dispatch, "_promote_manifest"), \
             patch.object(subprocess_dispatch, "cleanup_worker_exit"), \
             patch("dispatch_govern.govern") as mock_dg_govern:
            _handle_final_failure(
                dispatch_id="dispatch-govern-failure",
                terminal_id="T1",
                attempt=2,
                sub_result=failed_result,
                monitor=monitor,
                auto_commit=False,
                pre_dispatch_dirty=frozenset(),
                manifest_paths=None,
                commit_hash_before="abc123",
                dispatch_start_ts="2026-07-11T00:00:00+00:00",
                pre_sha="abc123",
                max_retries=2,
                lease_generation=None,
                model="sonnet",
                pr_id=None,
                instruction="Do the thing.",
                role="backend-developer",
                **kwargs,
            )
        return mock_receipt, mock_dg_govern


class TestHandleFinalFailureNoReport(_HandleFinalFailureTestBase):
    def test_emits_no_report_at_all(self):
        """Pre-existing behavior: on final failure, no report emission — only the receipt."""
        mock_receipt, mock_dg_govern = self._run_handle_final_failure()

        mock_dg_govern.assert_not_called()
        mock_receipt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
