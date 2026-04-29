#!/usr/bin/env python3
"""Regression tests for codex findings on _auto_commit_changes and _auto_stash_changes.

Finding 1: _auto_commit_changes must NOT use git add -A when pre_dispatch_dirty is
           provided — only stage files that became dirty during the dispatch.

Finding 2: _auto_stash_changes must use git stash push -u -- <files> (not git stash
           save without -u) so that: (a) untracked dispatch files are included, and
           (b) pre-existing dirty files from other terminals are excluded.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import subprocess_dispatch
from subprocess_dispatch import (
    _auto_commit_changes,
    _auto_stash_changes,
    _get_dirty_files,
)


# ---------------------------------------------------------------------------
# _get_dirty_files
# ---------------------------------------------------------------------------

class TestGetDirtyFiles(unittest.TestCase):
    def _run(self, stdout: str) -> set:
        with patch("subprocess_dispatch.subprocess") as mock_sp:
            proc = MagicMock()
            proc.stdout = stdout
            mock_sp.run.return_value = proc
            from pathlib import Path as _Path
            return _get_dirty_files(_Path("/fake/repo"))

    def test_empty_output_returns_empty_set(self):
        result = self._run("")
        self.assertEqual(result, set())

    def test_single_modified_file(self):
        result = self._run(" M scripts/lib/foo.py\n")
        self.assertIn("scripts/lib/foo.py", result)

    def test_untracked_file(self):
        result = self._run("?? scripts/lib/new_file.py\n")
        self.assertIn("scripts/lib/new_file.py", result)

    def test_renamed_file_captures_destination(self):
        result = self._run("R  old_name.py -> new_name.py\n")
        self.assertIn("new_name.py", result)
        self.assertNotIn("old_name.py", result)

    def test_multiple_files(self):
        stdout = " M a.py\n?? b.py\n M c.py\n"
        result = self._run(stdout)
        self.assertEqual(result, {"a.py", "b.py", "c.py"})

    def test_returns_empty_set_on_subprocess_exception(self):
        with patch("subprocess_dispatch.subprocess") as mock_sp:
            mock_sp.run.side_effect = OSError("git not found")
            result = _get_dirty_files(Path("/fake/repo"))
        self.assertEqual(result, set())


# ---------------------------------------------------------------------------
# _auto_commit_changes — Finding 1
# ---------------------------------------------------------------------------

class TestAutoCommitIsolation(unittest.TestCase):
    """_auto_commit_changes must scope staging to dispatch-specific files."""

    def _mock_subprocess(self, status_lines: list[str], add_rc: int = 0, commit_rc: int = 0):
        """Return a mock subprocess module. Calls: status -> add -> commit."""
        mock_sp = MagicMock()
        calls = []

        status_proc = MagicMock()
        status_proc.stdout = "\n".join(status_lines)
        status_proc.returncode = 0

        add_proc = MagicMock()
        add_proc.returncode = add_rc
        add_proc.stderr = ""

        commit_proc = MagicMock()
        commit_proc.returncode = commit_rc
        commit_proc.stderr = ""

        mock_sp.run.side_effect = [status_proc, add_proc, commit_proc]
        return mock_sp

    def test_scoped_add_excludes_pre_existing_files(self):
        """Only the file NOT in pre_dispatch_dirty is staged."""
        pre = {"old_file.py"}
        status_lines = [" M old_file.py", " M new_file.py"]

        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"old_file.py", "new_file.py"}):
            result = _auto_commit_changes("d-001", "T1", pre_dispatch_dirty=pre)

        self.assertTrue(result)
        add_call = mock_sp.run.call_args_list[1]
        cmd = add_call[0][0]
        self.assertIn("--", cmd)
        self.assertIn("new_file.py", cmd)
        self.assertNotIn("old_file.py", cmd)
        # Must NOT be git add -A
        self.assertNotIn("-A", cmd)

    def test_no_stage_when_all_files_pre_existing(self):
        """If all dirty files were dirty before the dispatch, nothing is staged."""
        pre = {"already_dirty.py"}
        status_lines = [" M already_dirty.py"]

        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"already_dirty.py"}):
            result = _auto_commit_changes("d-002", "T1", pre_dispatch_dirty=pre)

        self.assertFalse(result)
        # git add must NOT have been called
        for c in mock_sp.run.call_args_list:
            cmd = c[0][0]
            self.assertNotEqual(cmd[0:2], ["git", "add"])

    def test_fallback_to_add_all_when_pre_dispatch_dirty_is_none(self):
        """Without pre_dispatch_dirty, falls back to git add -A (backward compat)."""
        status_lines = [" M some_file.py"]
        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_commit_changes("d-003", "T1", pre_dispatch_dirty=None)

        self.assertTrue(result)
        add_call = mock_sp.run.call_args_list[1]
        cmd = add_call[0][0]
        self.assertIn("-A", cmd)

    def test_returns_false_on_clean_tree(self):
        """Returns False without calling add/commit when tree is clean."""
        mock_sp = self._mock_subprocess([])
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_commit_changes("d-004", "T1", pre_dispatch_dirty=set())

        self.assertFalse(result)
        self.assertEqual(mock_sp.run.call_count, 1)  # only git status

    def test_commit_message_contains_dispatch_id(self):
        """Commit message includes the dispatch ID."""
        pre = set()
        status_lines = [" M foo.py"]
        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"foo.py"}):
            _auto_commit_changes("dispatch-xyz-123", "T2", pre_dispatch_dirty=pre)

        commit_call = mock_sp.run.call_args_list[2]
        cmd = commit_call[0][0]
        msg_idx = cmd.index("-m") + 1
        self.assertIn("dispatch-xyz-123", cmd[msg_idx])


# ---------------------------------------------------------------------------
# _auto_stash_changes — Finding 2
# ---------------------------------------------------------------------------

class TestAutoStashIsolation(unittest.TestCase):
    """_auto_stash_changes must use git stash push -u -- <files> scoped to dispatch."""

    def _mock_subprocess(self, status_lines: list[str], stash_rc: int = 0):
        """Return a mock subprocess module. Calls: status -> stash."""
        mock_sp = MagicMock()

        status_proc = MagicMock()
        status_proc.stdout = "\n".join(status_lines)
        status_proc.returncode = 0

        stash_proc = MagicMock()
        stash_proc.returncode = stash_rc
        stash_proc.stderr = ""

        mock_sp.run.side_effect = [status_proc, stash_proc]
        return mock_sp

    def test_scoped_stash_excludes_pre_existing_files(self):
        """Only dispatch-specific files are passed to git stash push."""
        pre = {"pre_existing.py"}
        status_lines = [" M pre_existing.py", "?? new_untracked.py"]

        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files",
                   return_value={"pre_existing.py", "new_untracked.py"}):
            result = _auto_stash_changes("d-010", "T1", pre_dispatch_dirty=pre)

        self.assertTrue(result)
        stash_call = mock_sp.run.call_args_list[1]
        cmd = stash_call[0][0]
        # Must use git stash push
        self.assertEqual(cmd[:3], ["git", "stash", "push"])
        # Must include -u flag
        self.assertIn("-u", cmd)
        # Must include the file separator
        self.assertIn("--", cmd)
        # Must include the new file
        self.assertIn("new_untracked.py", cmd)
        # Must NOT include the pre-existing file
        self.assertNotIn("pre_existing.py", cmd)

    def test_no_stash_when_all_files_pre_existing(self):
        """If all dirty files pre-existed the dispatch, nothing is stashed."""
        pre = {"already_there.py"}
        status_lines = [" M already_there.py"]

        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"already_there.py"}):
            result = _auto_stash_changes("d-011", "T1", pre_dispatch_dirty=pre)

        self.assertFalse(result)
        # stash must NOT have been called
        for c in mock_sp.run.call_args_list:
            cmd = c[0][0]
            self.assertNotEqual(cmd[:2], ["git", "stash"])

    def test_fallback_uses_stash_push_with_u_flag(self):
        """Without pre_dispatch_dirty, fallback also uses -u to capture untracked files."""
        status_lines = [" M some.py"]
        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_stash_changes("d-012", "T1", pre_dispatch_dirty=None)

        self.assertTrue(result)
        stash_call = mock_sp.run.call_args_list[1]
        cmd = stash_call[0][0]
        self.assertEqual(cmd[:3], ["git", "stash", "push"])
        self.assertIn("-u", cmd)
        # Fallback must NOT use the old 'git stash save' form
        self.assertNotIn("save", cmd)

    def test_returns_false_on_clean_tree(self):
        """Returns False without calling stash when tree is clean."""
        mock_sp = self._mock_subprocess([])
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_stash_changes("d-013", "T1", pre_dispatch_dirty=set())

        self.assertFalse(result)
        self.assertEqual(mock_sp.run.call_count, 1)  # only git status

    def test_stash_name_contains_dispatch_id(self):
        """Stash message / name contains the dispatch ID."""
        pre = set()
        status_lines = [" M bar.py"]
        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"bar.py"}):
            _auto_stash_changes("dispatch-abc-789", "T3", pre_dispatch_dirty=pre)

        stash_call = mock_sp.run.call_args_list[1]
        cmd_str = " ".join(stash_call[0][0])
        self.assertIn("dispatch-abc-789", cmd_str)


# ---------------------------------------------------------------------------
# deliver_with_recovery: pre_dispatch_dirty captured and forwarded
# ---------------------------------------------------------------------------

class TestDeliverWithRecoveryPreDispatchCapture(unittest.TestCase):
    """deliver_with_recovery must capture pre_dispatch_dirty and forward to auto_commit/stash."""

    def _make_mock_adapter(self, success: bool = True, returncode: int = 0):
        adapter = MagicMock()
        adapter.deliver.return_value = MagicMock(success=success)
        adapter.read_events_with_timeout.return_value = iter([])
        obs = MagicMock()
        obs.transport_state = {"returncode": returncode}
        adapter.observe.return_value = obs
        adapter.was_timed_out.return_value = False
        adapter._get_event_store.return_value = None
        adapter.get_session_id.return_value = "sess-abc"
        adapter.trigger_report_pipeline.return_value = None
        return adapter

    def test_pre_dispatch_dirty_forwarded_to_auto_commit_on_success(self):
        """On success path, _auto_commit_changes receives the pre-dispatch dirty set."""
        fake_pre_dirty = {"existing_file.py"}

        with patch("subprocess_dispatch.SubprocessAdapter") as mock_cls, \
             patch("subprocess_dispatch._write_receipt") as mock_receipt, \
             patch("subprocess_dispatch._check_commit_since", return_value=True), \
             patch("subprocess_dispatch._get_commit_hash", return_value="abc123"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("subprocess_dispatch._update_pattern_confidence", return_value=0), \
             patch("subprocess_dispatch._get_dirty_files", return_value=fake_pre_dirty) as mock_gdf, \
             patch("subprocess_dispatch._auto_commit_changes", return_value=True) as mock_ac, \
             patch("subprocess_dispatch.WorkerHealthMonitor") as mock_monitor_cls:

            mock_cls.return_value = self._make_mock_adapter(success=True)
            monitor = MagicMock()
            monitor.stuck_count = 0
            monitor.mark_completed = MagicMock()
            mock_monitor_cls.return_value = monitor
            mock_receipt.return_value = Path("/tmp/r.ndjson")

            subprocess_dispatch.deliver_with_recovery(
                "T1", "do work", "sonnet", "dispatch-fwd-01",
                max_retries=0, auto_commit=True,
            )

        mock_ac.assert_called_once()
        _, kwargs = mock_ac.call_args
        self.assertEqual(
            kwargs.get("pre_dispatch_dirty"), fake_pre_dirty,
            "deliver_with_recovery must forward pre_dispatch_dirty to _auto_commit_changes",
        )

    def test_pre_dispatch_dirty_forwarded_to_auto_stash_on_failure(self):
        """On failure path, _auto_stash_changes receives the pre-dispatch dirty set."""
        fake_pre_dirty = {"other_terminal_work.py"}

        with patch("subprocess_dispatch.SubprocessAdapter") as mock_cls, \
             patch("subprocess_dispatch._write_receipt") as mock_receipt, \
             patch("subprocess_dispatch._check_commit_since", return_value=False), \
             patch("subprocess_dispatch._get_commit_hash", return_value="abc123"), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("subprocess_dispatch._update_pattern_confidence", return_value=0), \
             patch("subprocess_dispatch._get_dirty_files", return_value=fake_pre_dirty), \
             patch("subprocess_dispatch._auto_stash_changes", return_value=False) as mock_stash, \
             patch("subprocess_dispatch.WorkerHealthMonitor") as mock_monitor_cls:

            failed_adapter = MagicMock()
            failed_adapter.deliver.return_value = MagicMock(success=False)
            failed_adapter._get_event_store.return_value = None
            failed_adapter.trigger_report_pipeline.return_value = None
            mock_cls.return_value = failed_adapter

            monitor = MagicMock()
            monitor.stuck_count = 0
            monitor.mark_completed = MagicMock()
            mock_monitor_cls.return_value = monitor
            mock_receipt.return_value = Path("/tmp/r.ndjson")

            subprocess_dispatch.deliver_with_recovery(
                "T1", "do work", "sonnet", "dispatch-fwd-02",
                max_retries=0, auto_commit=True,
            )

        mock_stash.assert_called_once()
        _, kwargs = mock_stash.call_args
        self.assertEqual(
            kwargs.get("pre_dispatch_dirty"), fake_pre_dirty,
            "deliver_with_recovery must forward pre_dispatch_dirty to _auto_stash_changes",
        )


if __name__ == "__main__":
    unittest.main()
