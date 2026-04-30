#!/usr/bin/env python3
"""Tests for T5-PR4: stuck_event_count forwarding in subprocess_dispatch.

Verifies that:
  - _write_receipt includes stuck_event_count=N when N > 0
  - _write_receipt omits stuck_event_count when 0
  - deliver_with_recovery forwards monitor.stuck_count to _write_receipt
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import subprocess_dispatch
from subprocess_dispatch import _write_receipt


class TestWriteReceiptStuckEventCount(unittest.TestCase):
    """Direct unit tests for _write_receipt stuck_event_count field."""

    def _call_write_receipt_bare(self, stuck_event_count: int) -> dict:
        """Call _write_receipt using the bare-write fallback path and return the parsed receipt."""
        captured = {}

        def fake_open(path, mode="r", **kwargs):
            import io
            buf = io.StringIO()

            class _Ctx:
                def __enter__(self_inner):
                    return buf

                def __exit__(self_inner, *_):
                    captured["raw"] = buf.getvalue()

            return _Ctx()

        # Force the fallback path by making append_receipt_payload raise ImportError
        # _write_receipt lives in dispatch_receipt after the OI-1205/OI-1201 split.
        with patch.dict("sys.modules", {"append_receipt": None}):
            with patch("dispatch_receipt._default_state_dir") as mock_state_dir:
                tmpdir = Path(tempfile.mkdtemp())
                mock_state_dir.return_value = tmpdir
                _write_receipt(
                    "dispatch-rc-test",
                    "T1",
                    "done",
                    stuck_event_count=stuck_event_count,
                )
                receipt_file = tmpdir / "t0_receipts.ndjson"
                if receipt_file.exists():
                    raw = receipt_file.read_text().strip()
                    return json.loads(raw)
        return {}

    def test_stuck_event_count_present_when_nonzero(self):
        receipt = self._call_write_receipt_bare(stuck_event_count=3)
        self.assertIn("stuck_event_count", receipt, "stuck_event_count must appear in receipt when N>0")
        self.assertEqual(receipt["stuck_event_count"], 3)

    def test_stuck_event_count_absent_when_zero(self):
        receipt = self._call_write_receipt_bare(stuck_event_count=0)
        self.assertNotIn(
            "stuck_event_count", receipt,
            "stuck_event_count must NOT appear in receipt when 0",
        )

    def test_stuck_event_count_one(self):
        receipt = self._call_write_receipt_bare(stuck_event_count=1)
        self.assertEqual(receipt.get("stuck_event_count"), 1)

    def test_receipt_has_required_fields(self):
        receipt = self._call_write_receipt_bare(stuck_event_count=2)
        for field in ("dispatch_id", "terminal", "status", "event_count", "source"):
            self.assertIn(field, receipt, f"receipt missing required field: {field}")
        self.assertEqual(receipt["dispatch_id"], "dispatch-rc-test")
        self.assertEqual(receipt["terminal"], "T1")
        self.assertEqual(receipt["status"], "done")


class TestDeliverWithRecoveryStuckCount(unittest.TestCase):
    """Integration tests: deliver_with_recovery forwards stuck_count to _write_receipt."""

    def _make_mock_adapter(self, events=None, returncode=0):
        """Build a fully mocked SubprocessAdapter."""
        adapter = MagicMock()
        adapter.deliver.return_value = MagicMock(success=True)
        adapter.read_events_with_timeout.return_value = iter(events or [])
        obs = MagicMock()
        obs.transport_state = {"returncode": returncode}
        adapter.observe.return_value = obs
        adapter.was_timed_out.return_value = False
        adapter._get_event_store.return_value = None
        adapter.get_session_id.return_value = "sess-x"
        adapter.trigger_report_pipeline.return_value = None
        return adapter

    def test_stuck_count_forwarded_to_write_receipt_on_success(self):
        """On success, _write_receipt receives stuck_event_count from monitor.stuck_count."""
        with patch("subprocess_dispatch.SubprocessAdapter") as mock_cls, \
             patch("subprocess_dispatch._write_receipt") as mock_receipt, \
             patch("subprocess_dispatch._check_commit_since", return_value=False), \
             patch("subprocess_dispatch._get_commit_hash", return_value="abc123"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("subprocess_dispatch._update_pattern_confidence", return_value=0), \
             patch("subprocess_dispatch.WorkerHealthMonitor") as mock_monitor_cls:

            mock_adapter = self._make_mock_adapter(events=[MagicMock(type="text")])
            mock_cls.return_value = mock_adapter

            monitor = MagicMock()
            monitor.stuck_count = 5
            monitor.mark_completed = MagicMock()
            mock_monitor_cls.return_value = monitor

            mock_receipt.return_value = Path("/tmp/fake_receipt.ndjson")

            subprocess_dispatch.deliver_with_recovery(
                "T1", "run task", "sonnet", "dispatch-sc-01",
                max_retries=0,
                auto_commit=False,
            )

            mock_receipt.assert_called_once()
            _, kwargs = mock_receipt.call_args
            self.assertEqual(
                kwargs.get("stuck_event_count"), 5,
                "deliver_with_recovery must forward monitor.stuck_count as stuck_event_count",
            )

    def test_stuck_count_zero_forwarded_on_success(self):
        """Zero stuck_count is correctly forwarded (no STUCK events occurred)."""
        with patch("subprocess_dispatch.SubprocessAdapter") as mock_cls, \
             patch("subprocess_dispatch._write_receipt") as mock_receipt, \
             patch("subprocess_dispatch._check_commit_since", return_value=False), \
             patch("subprocess_dispatch._get_commit_hash", return_value="abc123"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("subprocess_dispatch._update_pattern_confidence", return_value=0), \
             patch("subprocess_dispatch.WorkerHealthMonitor") as mock_monitor_cls:

            mock_adapter = self._make_mock_adapter()
            mock_cls.return_value = mock_adapter

            monitor = MagicMock()
            monitor.stuck_count = 0
            monitor.mark_completed = MagicMock()
            mock_monitor_cls.return_value = monitor

            mock_receipt.return_value = Path("/tmp/fake_receipt.ndjson")

            subprocess_dispatch.deliver_with_recovery(
                "T1", "run task", "sonnet", "dispatch-sc-02",
                max_retries=0,
                auto_commit=False,
            )

            _, kwargs = mock_receipt.call_args
            self.assertEqual(kwargs.get("stuck_event_count"), 0)

    def test_stuck_count_forwarded_on_failure(self):
        """On final failure, stuck_count is still forwarded to the failure receipt."""
        with patch("subprocess_dispatch.SubprocessAdapter") as mock_cls, \
             patch("subprocess_dispatch._write_receipt") as mock_receipt, \
             patch("subprocess_dispatch._check_commit_since", return_value=False), \
             patch("subprocess_dispatch._get_commit_hash", return_value="abc123"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("subprocess_dispatch._update_pattern_confidence", return_value=0), \
             patch("subprocess_dispatch.WorkerHealthMonitor") as mock_monitor_cls, \
             patch("subprocess_dispatch._auto_stash_changes", return_value=False):

            failed_adapter = MagicMock()
            failed_adapter.deliver.return_value = MagicMock(success=False)
            failed_adapter._get_event_store.return_value = None
            failed_adapter.trigger_report_pipeline.return_value = None
            mock_cls.return_value = failed_adapter

            monitor = MagicMock()
            monitor.stuck_count = 2
            monitor.mark_completed = MagicMock()
            mock_monitor_cls.return_value = monitor

            mock_receipt.return_value = Path("/tmp/fake_receipt.ndjson")

            subprocess_dispatch.deliver_with_recovery(
                "T1", "run task", "sonnet", "dispatch-sc-03",
                max_retries=0,
                auto_commit=True,
            )

            mock_receipt.assert_called_once()
            _, kwargs = mock_receipt.call_args
            self.assertEqual(
                kwargs.get("stuck_event_count"), 2,
                "Failed-dispatch receipt must also carry stuck_event_count",
            )


if __name__ == "__main__":
    unittest.main()
