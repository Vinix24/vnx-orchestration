#!/usr/bin/env python3
"""Tests for the subprocess lane's VNX_SHARED_GOVERN wiring onto dispatch_govern.govern().

dispatch_govern.py's own module docstring names tmux AND subprocess as its intended
callers, but only the tmux lane called govern() — the subprocess lane still used the
legacy stub-writer (_ensure_unified_report) on success and emitted no report at all on
a budget-exhausted failure. This closes that gap behind VNX_SHARED_GOVERN (default off).

Verifies that:
  - VNX_SHARED_GOVERN unset/0 (default): behavior is unchanged — _ensure_unified_report
    runs on success, dispatch_govern.govern() is never called, and no report is emitted
    on final failure (matching pre-existing behavior).
  - VNX_SHARED_GOVERN=1: govern() runs instead of _ensure_unified_report on success, and
    also runs on final failure (new coverage) — both with lane="subprocess".
  - _write_receipt is called exactly once in every case, unaffected by the flag.
"""

import os
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


class TestHandleSuccessSharedGovernOff(_HandleSuccessTestBase):
    def test_default_off_uses_legacy_stub_writer(self):
        os.environ.pop("VNX_SHARED_GOVERN", None)
        mock_receipt, mock_stub, mock_dg_govern = self._run_handle_success()

        mock_stub.assert_called_once_with("dispatch-govern-success", "T1", "done")
        mock_dg_govern.assert_not_called()
        mock_receipt.assert_called_once()

    def test_explicit_off_uses_legacy_stub_writer(self):
        os.environ["VNX_SHARED_GOVERN"] = "0"
        try:
            mock_receipt, mock_stub, mock_dg_govern = self._run_handle_success()
        finally:
            os.environ.pop("VNX_SHARED_GOVERN", None)

        mock_stub.assert_called_once()
        mock_dg_govern.assert_not_called()
        mock_receipt.assert_called_once()


class TestHandleSuccessSharedGovernOn(_HandleSuccessTestBase):
    def test_on_calls_govern_instead_of_stub(self):
        os.environ["VNX_SHARED_GOVERN"] = "1"
        try:
            mock_receipt, mock_stub, mock_dg_govern = self._run_handle_success()
        finally:
            os.environ.pop("VNX_SHARED_GOVERN", None)

        mock_stub.assert_not_called()
        mock_dg_govern.assert_called_once()
        _, kwargs = mock_dg_govern.call_args
        # govern(spec, raw, lane=...) — inspect positional + kwarg lane.
        args = mock_dg_govern.call_args.args
        spec = args[0]
        raw = args[1]
        lane = mock_dg_govern.call_args.kwargs.get("lane") or args[2]

        self.assertEqual(lane, "subprocess")
        self.assertEqual(spec.dispatch_id, "dispatch-govern-success")
        self.assertEqual(spec.terminal_id, "T1")
        self.assertEqual(spec.instruction, "Do the thing.")
        self.assertEqual(spec.base_sha, "abc123")
        self.assertEqual(spec.model, "sonnet")
        self.assertEqual(spec.role, "backend-developer")
        self.assertEqual(raw.receipt.get("status"), "done")
        # Receipt writing is untouched by the flag.
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


class TestHandleFinalFailureSharedGovern(_HandleFinalFailureTestBase):
    def test_default_off_emits_no_report_at_all(self):
        """Pre-existing behavior: on final failure, no report emission — only the receipt."""
        os.environ.pop("VNX_SHARED_GOVERN", None)
        mock_receipt, mock_dg_govern = self._run_handle_final_failure()

        mock_dg_govern.assert_not_called()
        mock_receipt.assert_called_once()

    def test_on_calls_govern_with_failed_status(self):
        """New coverage: VNX_SHARED_GOVERN=1 gives failed subprocess dispatches an
        honest governed report, matching the tmux lane's always-govern behavior."""
        os.environ["VNX_SHARED_GOVERN"] = "1"
        try:
            mock_receipt, mock_dg_govern = self._run_handle_final_failure()
        finally:
            os.environ.pop("VNX_SHARED_GOVERN", None)

        mock_dg_govern.assert_called_once()
        args = mock_dg_govern.call_args.args
        spec, raw = args[0], args[1]
        lane = mock_dg_govern.call_args.kwargs.get("lane") or args[2]

        self.assertEqual(lane, "subprocess")
        self.assertEqual(spec.dispatch_id, "dispatch-govern-failure")
        self.assertEqual(raw.receipt.get("status"), "failed")
        # Receipt writing is untouched by the flag.
        mock_receipt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
