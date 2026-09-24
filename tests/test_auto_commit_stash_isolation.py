#!/usr/bin/env python3
"""Regression tests for codex findings on _auto_commit_changes and _auto_stash_changes.

Finding 1: _auto_commit_changes must NOT use git add -A when pre_dispatch_dirty is
           provided — only stage files that became dirty during the dispatch.

Finding 2: _auto_stash_changes must use git stash push -u -- <files> (not git stash
           save without -u) so that: (a) untracked dispatch files are included, and
           (b) pre-existing dirty files from other terminals are excluded.

Hermetic under pytest AND ``python -m unittest``: the deliver_with_recovery tests
pin the whole VNX store to a tmp dir themselves (tests/conftest.py does that
only under pytest) and replace the worker spawn, so no run can launch a real
``claude -p`` or write the operator's central store.
"""

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import subprocess_dispatch
from subprocess_dispatch import (
    _SubprocessResult,
    _auto_commit_changes,
    _auto_stash_changes,
    _get_dirty_files,
)


# ---------------------------------------------------------------------------
# _get_dirty_files
# ---------------------------------------------------------------------------

# _get_dirty_files lives in subprocess_dispatch_internals.path_utils and calls its
# own ``subprocess`` import. Patching ``subprocess_dispatch.subprocess`` (the
# facade attribute, which the receipt_writer helpers do go through) never reached
# it: the real ``git status`` ran against /fake/repo and four of these tests
# asserted on stdout the patch was meant to supply.
_PATH_UTILS_SUBPROCESS = "subprocess_dispatch_internals.path_utils.subprocess"


class TestGetDirtyFiles(unittest.TestCase):
    def _run(self, stdout: str) -> set:
        with patch(_PATH_UTILS_SUBPROCESS) as mock_sp:
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
        with patch(_PATH_UTILS_SUBPROCESS) as mock_sp:
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
            result = _auto_commit_changes(
                "d-001", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"new_file.py"}),
            )

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
            result = _auto_commit_changes(
                "d-002", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset(),
            )

        self.assertFalse(result)
        # git add must NOT have been called
        for c in mock_sp.run.call_args_list:
            cmd = c[0][0]
            self.assertNotEqual(cmd[0:2], ["git", "add"])

    def test_refuses_to_commit_when_pre_dispatch_dirty_is_none(self):
        """Codex round-2 finding 1: when pre_dispatch_dirty is None, refuse to commit
        rather than fall back to git add -A — that would sweep unrelated user changes
        in a dirty or shared worktree, breaking dispatch isolation."""
        status_lines = [" M some_file.py"]
        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_commit_changes(
                "d-003", "T1",
                pre_dispatch_dirty=None,
                dispatch_touched_files=frozenset({"some_file.py"}),
            )

        self.assertFalse(result)
        # No git command must have been invoked at all — fail-safe before any git work.
        self.assertEqual(mock_sp.run.call_count, 0)

    def test_returns_false_on_clean_tree(self):
        """Returns False without calling add/commit when tree is clean."""
        mock_sp = self._mock_subprocess([])
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_commit_changes(
                "d-004", "T1",
                pre_dispatch_dirty=set(),
                dispatch_touched_files=frozenset(),
            )

        self.assertFalse(result)
        self.assertEqual(mock_sp.run.call_count, 1)  # only git status

    def test_commit_message_contains_dispatch_id(self):
        """Commit message includes the dispatch ID."""
        pre = set()
        status_lines = [" M foo.py"]
        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"foo.py"}):
            _auto_commit_changes(
                "dispatch-xyz-123", "T2",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"foo.py"}),
            )

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
            result = _auto_stash_changes(
                "d-010", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"new_untracked.py"}),
            )

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
            result = _auto_stash_changes(
                "d-011", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset(),
            )

        self.assertFalse(result)
        # stash must NOT have been called
        for c in mock_sp.run.call_args_list:
            cmd = c[0][0]
            self.assertNotEqual(cmd[:2], ["git", "stash"])

    def test_refuses_to_stash_when_pre_dispatch_dirty_is_none(self):
        """Codex round-2 finding 1: when pre_dispatch_dirty is None, refuse to stash
        rather than running ``git stash push -u`` over the whole repo — that would
        hide unrelated edits from other terminals into this dispatch's stash."""
        status_lines = [" M some.py"]
        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_stash_changes(
                "d-012", "T1",
                pre_dispatch_dirty=None,
                dispatch_touched_files=frozenset({"some.py"}),
            )

        self.assertFalse(result)
        # No git command must have been invoked — fail-safe before any git work.
        self.assertEqual(mock_sp.run.call_count, 0)

    def test_returns_false_on_clean_tree(self):
        """Returns False without calling stash when tree is clean."""
        mock_sp = self._mock_subprocess([])
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_stash_changes(
                "d-013", "T1",
                pre_dispatch_dirty=set(),
                dispatch_touched_files=frozenset(),
            )

        self.assertFalse(result)
        self.assertEqual(mock_sp.run.call_count, 1)  # only git status

    def test_stash_name_contains_dispatch_id(self):
        """Stash message / name contains the dispatch ID."""
        pre = set()
        status_lines = [" M bar.py"]
        mock_sp = self._mock_subprocess(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"bar.py"}):
            _auto_stash_changes(
                "dispatch-abc-789", "T3",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"bar.py"}),
            )

        stash_call = mock_sp.run.call_args_list[1]
        cmd_str = " ".join(stash_call[0][0])
        self.assertIn("dispatch-abc-789", cmd_str)


# ---------------------------------------------------------------------------
# deliver_with_recovery: pre_dispatch_dirty captured and forwarded
# ---------------------------------------------------------------------------

class TestDeliverWithRecoveryPreDispatchCapture(unittest.TestCase):
    """deliver_with_recovery must capture pre_dispatch_dirty and forward to auto_commit/stash."""

    def setUp(self):
        # Pin the store to a tmp dir. Under pytest tests/conftest.py already
        # does this; under ``python -m unittest`` nothing does, and the run
        # then resolved to the operator's real central store (a
        # ``cleanup_worker_exit`` beacon for ``dispatch-fwd-01`` sat on ``fail``
        # there). Every ambient VNX_* pin is dropped first: a governed shell
        # exports VNX_STATE_DIR and friends, and those beat VNX_DATA_DIR.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith("VNX_")}
        env.update(
            HOME=str(root / "home"),
            VNX_DATA_DIR=str(root / "data"),
            VNX_DATA_DIR_EXPLICIT="1",
        )
        env_patch = patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.assertTrue(
            str(subprocess_dispatch._default_state_dir()).startswith(str(root)),
            "the test store must resolve under the tmp dir, not the real store",
        )

    def _deliver(self, dispatch_id: str, *, worker_success: bool, pre_dirty: set):
        """Run deliver_with_recovery with every external effect replaced.

        Replaced: the worker spawn (``deliver_via_subprocess``; below it
        ``provider_spawns.claude_spawn`` imports its own ``SubprocessAdapter``,
        so patching ``subprocess_dispatch.SubprocessAdapter`` does not reach
        it and the old version of this test launched a real ``claude -p`` for
        up to ``total_deadline``), the receipt, pattern feedback, outcome
        capture, the git reads and the worker-exit cleanup.
        """
        worker = _SubprocessResult(
            success=worker_success,
            session_id="sess-abc",
            event_count=0,
            manifest_path=None,
            touched_files=frozenset({"touched_by_worker.py"}),
        )
        monitor = MagicMock()
        monitor.stuck_count = 0
        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(patch("subprocess_dispatch.deliver_via_subprocess", return_value=worker))
            enter(patch("subprocess_dispatch._write_receipt"))
            enter(patch("subprocess_dispatch._check_commit_since", return_value=worker_success))
            enter(patch("subprocess_dispatch._get_commit_hash", return_value="abc123"))
            enter(patch("subprocess_dispatch._capture_dispatch_parameters"))
            enter(patch("subprocess_dispatch._capture_dispatch_outcome"))
            enter(patch("subprocess_dispatch._update_pattern_confidence", return_value=0))
            enter(patch("subprocess_dispatch._get_dirty_files", return_value=pre_dirty))
            enter(patch("subprocess_dispatch.WorkerHealthMonitor", return_value=monitor))
            cleanup = enter(patch("subprocess_dispatch.cleanup_worker_exit"))
            auto_commit = enter(patch("subprocess_dispatch._auto_commit_changes", return_value=True))
            auto_stash = enter(patch("subprocess_dispatch._auto_stash_changes", return_value=False))
            subprocess_dispatch.deliver_with_recovery(
                "T1", "do work", "sonnet", dispatch_id,
                max_retries=0, auto_commit=True,
            )
        return SimpleNamespace(auto_commit=auto_commit, auto_stash=auto_stash, cleanup=cleanup)

    def test_pre_dispatch_dirty_forwarded_to_auto_commit_on_success(self):
        """On success path, _auto_commit_changes receives both the pre-dispatch
        dirty set and the dispatch_touched_files captured from tool events."""
        fake_pre_dirty = {"existing_file.py"}

        run = self._deliver("dispatch-fwd-01", worker_success=True, pre_dirty=fake_pre_dirty)

        run.auto_commit.assert_called_once()
        run.auto_stash.assert_not_called()
        self.assertEqual(run.cleanup.call_args.kwargs["exit_status"], "success")
        _, kwargs = run.auto_commit.call_args
        self.assertEqual(
            kwargs.get("pre_dispatch_dirty"), fake_pre_dirty,
            "deliver_with_recovery must forward pre_dispatch_dirty to _auto_commit_changes",
        )
        self.assertEqual(
            kwargs.get("dispatch_touched_files"), frozenset({"touched_by_worker.py"}),
            "deliver_with_recovery must forward dispatch_touched_files to _auto_commit_changes",
        )

    def test_pre_dispatch_dirty_forwarded_to_auto_stash_on_failure(self):
        """On failure path, _auto_stash_changes receives the pre-dispatch dirty set."""
        fake_pre_dirty = {"other_terminal_work.py"}

        run = self._deliver("dispatch-fwd-02", worker_success=False, pre_dirty=fake_pre_dirty)

        run.auto_stash.assert_called_once()
        run.auto_commit.assert_not_called()
        self.assertEqual(run.cleanup.call_args.kwargs["exit_status"], "failure")
        _, kwargs = run.auto_stash.call_args
        self.assertEqual(
            kwargs.get("pre_dispatch_dirty"), fake_pre_dirty,
            "deliver_with_recovery must forward pre_dispatch_dirty to _auto_stash_changes",
        )
        self.assertEqual(
            kwargs.get("dispatch_touched_files"), frozenset({"touched_by_worker.py"}),
            "deliver_with_recovery must forward dispatch_touched_files to _auto_stash_changes",
        )


if __name__ == "__main__":
    unittest.main()
