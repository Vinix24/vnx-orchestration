#!/usr/bin/env python3
"""Tests for F52-PR3: post-dispatch commit enforcement.

Gate: f52-pr3
Covers:
  - auto_commit_on_success: uncommitted changes committed after successful dispatch
  - auto_stash_on_failure: uncommitted changes stashed after failed dispatch
  - no_commit_when_clean: no commit attempt when working tree is clean
  - no_auto_commit_flag: --no-auto-commit disables both commit and stash
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)


def _make_run_result(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestAutoCommitOnSuccess(unittest.TestCase):
    """_auto_commit_changes commits dirty changes and returns True."""

    @patch("subprocess_dispatch.subprocess.run")
    def test_auto_commit_on_success(self, mock_run):
        from subprocess_dispatch import _auto_commit_changes

        # git status --porcelain returns two dirty files
        mock_run.side_effect = [
            _make_run_result(stdout="M  scripts/lib/foo.py\nA  tests/test_foo.py\n"),  # status
            _make_run_result(returncode=0),   # git add -A
            _make_run_result(returncode=0),   # git commit
        ]

        result = _auto_commit_changes("dispatch-abc", "T1", gate="f52-pr3")
        self.assertTrue(result)

        # Verify commit message includes gate tag and terminal
        commit_call = mock_run.call_args_list[2]
        commit_cmd = commit_call[0][0]
        self.assertIn("git", commit_cmd)
        self.assertIn("commit", commit_cmd)
        msg = commit_cmd[commit_cmd.index("-m") + 1]
        self.assertIn("f52-pr3", msg)
        self.assertIn("T1", msg)

    @patch("subprocess_dispatch.subprocess.run")
    def test_auto_commit_uses_dispatch_id_when_no_gate(self, mock_run):
        from subprocess_dispatch import _auto_commit_changes

        mock_run.side_effect = [
            _make_run_result(stdout="M  foo.py\n"),
            _make_run_result(returncode=0),
            _make_run_result(returncode=0),
        ]

        result = _auto_commit_changes("dispatch-xyz1234567890", "T2")
        self.assertTrue(result)
        commit_cmd = mock_run.call_args_list[2][0][0]
        msg = commit_cmd[commit_cmd.index("-m") + 1]
        # Should use first 12 chars of dispatch_id when no gate
        # "dispatch-xyz1234567890"[:12] == "dispatch-xyz"
        self.assertIn("dispatch-xyz", msg)


class TestAutoStashOnFailure(unittest.TestCase):
    """_auto_stash_changes stashes dirty changes and returns True."""

    @patch("subprocess_dispatch.subprocess.run")
    def test_auto_stash_on_failure(self, mock_run):
        from subprocess_dispatch import _auto_stash_changes

        mock_run.side_effect = [
            _make_run_result(stdout="M  scripts/lib/worker_health_monitor.py\n"),  # status
            _make_run_result(returncode=0),   # git stash save
        ]

        result = _auto_stash_changes("dispatch-fail-001", "T1")
        self.assertTrue(result)

        stash_call = mock_run.call_args_list[1]
        stash_cmd = stash_call[0][0]
        self.assertIn("stash", stash_cmd)
        self.assertIn("save", stash_cmd)
        stash_name = stash_cmd[-1]
        self.assertIn("dispatch-fail-001", stash_name)

    @patch("subprocess_dispatch.subprocess.run")
    def test_auto_stash_git_failure_returns_false(self, mock_run):
        from subprocess_dispatch import _auto_stash_changes

        mock_run.side_effect = [
            _make_run_result(stdout="M  foo.py\n"),
            _make_run_result(returncode=1, stderr="stash failed"),
        ]

        result = _auto_stash_changes("dispatch-fail-002", "T1")
        self.assertFalse(result)


class TestNoCommitWhenClean(unittest.TestCase):
    """_auto_commit_changes returns False without calling git add/commit when tree is clean."""

    @patch("subprocess_dispatch.subprocess.run")
    def test_no_commit_when_clean(self, mock_run):
        from subprocess_dispatch import _auto_commit_changes

        mock_run.return_value = _make_run_result(stdout="")  # clean working tree

        result = _auto_commit_changes("dispatch-clean-001", "T1")
        self.assertFalse(result)

        # Only git status should be called — no add or commit
        self.assertEqual(mock_run.call_count, 1)
        cmd = mock_run.call_args[0][0]
        self.assertIn("status", cmd)
        self.assertIn("--porcelain", cmd)

    @patch("subprocess_dispatch.subprocess.run")
    def test_no_stash_when_clean(self, mock_run):
        from subprocess_dispatch import _auto_stash_changes

        mock_run.return_value = _make_run_result(stdout="")

        result = _auto_stash_changes("dispatch-clean-002", "T1")
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, 1)


class TestNoAutoCommitFlag(unittest.TestCase):
    """deliver_with_recovery respects auto_commit=False — no commit or stash."""

    @patch("subprocess_dispatch._write_receipt")
    @patch("subprocess_dispatch._check_commit_since")
    @patch("subprocess_dispatch._auto_stash_changes")
    @patch("subprocess_dispatch._auto_commit_changes")
    @patch("subprocess_dispatch.deliver_via_subprocess")
    @patch("subprocess_dispatch.WorkerHealthMonitor")
    def test_no_auto_commit_flag_on_success(
        self,
        mock_monitor_cls,
        mock_deliver,
        mock_commit,
        mock_stash,
        mock_check,
        mock_write_receipt,
    ):
        from subprocess_dispatch import deliver_with_recovery

        mock_monitor_cls.return_value = MagicMock()
        mock_deliver.return_value = True
        mock_check.return_value = True  # commit_missing=True, but auto_commit is off

        deliver_with_recovery(
            "T1", "do work", "sonnet", "dispatch-nac-001",
            max_retries=0, auto_commit=False,
        )

        mock_commit.assert_not_called()
        mock_stash.assert_not_called()

    @patch("subprocess_dispatch._write_receipt")
    @patch("subprocess_dispatch._auto_stash_changes")
    @patch("subprocess_dispatch._auto_commit_changes")
    @patch("subprocess_dispatch.deliver_via_subprocess")
    @patch("subprocess_dispatch.WorkerHealthMonitor")
    def test_no_auto_commit_flag_on_failure(
        self,
        mock_monitor_cls,
        mock_deliver,
        mock_commit,
        mock_stash,
        mock_write_receipt,
    ):
        from subprocess_dispatch import deliver_with_recovery

        mock_monitor_cls.return_value = MagicMock()
        mock_deliver.return_value = False  # dispatch fails

        deliver_with_recovery(
            "T1", "do work", "sonnet", "dispatch-nac-002",
            max_retries=0, auto_commit=False,
        )

        mock_commit.assert_not_called()
        mock_stash.assert_not_called()

    @patch("subprocess_dispatch._write_receipt")
    @patch("subprocess_dispatch._check_commit_since")
    @patch("subprocess_dispatch._auto_commit_changes")
    @patch("subprocess_dispatch.deliver_via_subprocess")
    @patch("subprocess_dispatch.WorkerHealthMonitor")
    def test_committed_flag_in_receipt(
        self,
        mock_monitor_cls,
        mock_deliver,
        mock_commit,
        mock_check,
        mock_write_receipt,
    ):
        """Receipt should include committed=True when auto-commit succeeds."""
        from subprocess_dispatch import deliver_with_recovery

        mock_monitor_cls.return_value = MagicMock()
        mock_deliver.return_value = True
        mock_check.return_value = True   # commit_missing
        mock_commit.return_value = True  # auto-commit succeeded

        deliver_with_recovery(
            "T1", "do work", "sonnet", "dispatch-committed-001",
            max_retries=0, auto_commit=True,
        )

        mock_write_receipt.assert_called_once()
        kwargs = mock_write_receipt.call_args[1]
        self.assertTrue(kwargs.get("committed"))
        self.assertFalse(kwargs.get("commit_missing", False))


if __name__ == "__main__":
    unittest.main()
