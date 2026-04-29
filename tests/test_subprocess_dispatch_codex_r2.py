#!/usr/bin/env python3
"""Regression tests for codex round-2 finding on PR #315.

Round-2 finding: ``_auto_commit_changes`` and ``_auto_stash_changes`` operate on
the entire repository (effectively ``git add -A`` / ``git stash save``) instead
of the files touched by the *current dispatch*.  In a shared or already-dirty
worktree, a successful worker exit can commit unrelated user/agent edits, and a
failed exit can hide unrelated tracked changes.

Round-1 fixed half the problem by scoping to ``current_dirty - pre_dispatch_dirty``.
Round-2 closes the rest of the gap by adding a second filter — files this
dispatch's worker explicitly wrote via structured tool calls (Write / Edit /
MultiEdit / NotebookEdit).  The intersection of the two sets is the only file
set safely attributable to this worker.

This module verifies:
1. ``_extract_touched_paths_from_event`` picks up Write/Edit/MultiEdit/NotebookEdit
   events and ignores everything else.
2. ``_normalize_repo_path`` resolves to repo-relative POSIX paths and discards
   paths outside the repo root.
3. ``_auto_commit_changes`` and ``_auto_stash_changes`` refuse when
   ``dispatch_touched_files`` is None (fail-safe, mirrors pre_dispatch_dirty
   behaviour).
4. The intersection logic excludes "concurrent edit" files — files that became
   dirty during the dispatch but were NOT written by this worker — even when
   the dispatch did write at least one other file.
5. ``deliver_via_subprocess`` accumulates a ``touched_files`` set from its
   stream events and exposes it via ``_SubprocessResult.touched_files``.
6. ``deliver_with_recovery`` forwards ``sub_result.touched_files`` to
   ``_auto_commit_changes`` and ``_auto_stash_changes``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import subprocess_dispatch  # noqa: E402
from subprocess_dispatch import (  # noqa: E402
    _SubprocessResult,
    _auto_commit_changes,
    _auto_stash_changes,
    _extract_touched_paths_from_event,
    _normalize_repo_path,
)


# ---------------------------------------------------------------------------
# Tool-event extraction
# ---------------------------------------------------------------------------


class _FakeEvent:
    """Minimal stand-in for ``subprocess_adapter.StreamEvent``.

    Only ``type`` and ``data`` are read by ``_extract_touched_paths_from_event``,
    so we avoid importing the real dataclass to keep the test independent of
    the adapter's optional dependencies."""

    def __init__(self, type_: str, data: dict):
        self.type = type_
        self.data = data


class TestExtractTouchedPaths(unittest.TestCase):
    """``_extract_touched_paths_from_event`` returns raw file_path strings only
    for tool_use events whose tool actually writes files."""

    def test_write_event_yields_file_path(self):
        ev = _FakeEvent("tool_use", {
            "name": "Write",
            "input": {"file_path": "/tmp/repo/scripts/foo.py", "content": "x"},
        })
        self.assertEqual(_extract_touched_paths_from_event(ev),
                         ["/tmp/repo/scripts/foo.py"])

    def test_edit_event_yields_file_path(self):
        ev = _FakeEvent("tool_use", {
            "name": "Edit",
            "input": {"file_path": "/tmp/repo/a.py", "old_string": "x", "new_string": "y"},
        })
        self.assertEqual(_extract_touched_paths_from_event(ev), ["/tmp/repo/a.py"])

    def test_multiedit_event_yields_single_file_path(self):
        ev = _FakeEvent("tool_use", {
            "name": "MultiEdit",
            "input": {"file_path": "/tmp/repo/b.py", "edits": []},
        })
        self.assertEqual(_extract_touched_paths_from_event(ev), ["/tmp/repo/b.py"])

    def test_notebook_edit_yields_notebook_path(self):
        ev = _FakeEvent("tool_use", {
            "name": "NotebookEdit",
            "input": {"notebook_path": "/tmp/repo/nb.ipynb"},
        })
        self.assertEqual(_extract_touched_paths_from_event(ev), ["/tmp/repo/nb.ipynb"])

    def test_read_tool_yields_nothing(self):
        ev = _FakeEvent("tool_use", {
            "name": "Read",
            "input": {"file_path": "/tmp/repo/foo.py"},
        })
        self.assertEqual(_extract_touched_paths_from_event(ev), [])

    def test_bash_tool_yields_nothing_even_with_filename_argument(self):
        """Bash file modifications are intentionally not tracked — the
        codex-fix design accepts the trade-off that workers using Bash to
        modify files must commit those changes manually."""
        ev = _FakeEvent("tool_use", {
            "name": "Bash",
            "input": {"command": "echo hi > /tmp/repo/foo.py"},
        })
        self.assertEqual(_extract_touched_paths_from_event(ev), [])

    def test_text_event_yields_nothing(self):
        ev = _FakeEvent("text", {"text": "hello"})
        self.assertEqual(_extract_touched_paths_from_event(ev), [])

    def test_thinking_event_yields_nothing(self):
        ev = _FakeEvent("thinking", {"thinking": "..."})
        self.assertEqual(_extract_touched_paths_from_event(ev), [])

    def test_malformed_event_does_not_raise(self):
        ev = _FakeEvent("tool_use", {"name": "Edit", "input": None})
        self.assertEqual(_extract_touched_paths_from_event(ev), [])

    def test_non_string_file_path_ignored(self):
        ev = _FakeEvent("tool_use", {
            "name": "Edit",
            "input": {"file_path": 12345},
        })
        self.assertEqual(_extract_touched_paths_from_event(ev), [])


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------


class TestNormalizeRepoPath(unittest.TestCase):
    """``_normalize_repo_path`` returns repo-relative POSIX strings or None."""

    def setUp(self):
        # Use a real tmpdir so resolve() on symlinks behaves correctly.
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "foo.py").write_text("")

    def tearDown(self):
        self._tmp.cleanup()

    def test_absolute_path_inside_repo(self):
        norm = _normalize_repo_path(str(self.repo / "scripts" / "foo.py"), self.repo)
        self.assertEqual(norm, "scripts/foo.py")

    def test_relative_path(self):
        norm = _normalize_repo_path("scripts/foo.py", self.repo)
        self.assertEqual(norm, "scripts/foo.py")

    def test_path_outside_repo_returns_none(self):
        # /tmp is not inside the temp repo (well, on macOS /tmp may symlink to
        # /private/tmp; either way, parent dirs of self.repo are outside it).
        outside = self.repo.parent / "definitely_outside.py"
        self.assertIsNone(_normalize_repo_path(str(outside), self.repo))

    def test_empty_path_returns_none(self):
        self.assertIsNone(_normalize_repo_path("", self.repo))

    def test_dotdot_within_repo_collapses(self):
        norm = _normalize_repo_path("scripts/../scripts/foo.py", self.repo)
        self.assertEqual(norm, "scripts/foo.py")


# ---------------------------------------------------------------------------
# Auto-commit / auto-stash with dispatch_touched_files filter
# ---------------------------------------------------------------------------


def _mock_subprocess_for_commit(status_lines: list[str]):
    """Build a mock of ``subprocess`` whose run() returns: status, add, commit."""
    mock_sp = MagicMock()
    status_proc = MagicMock(stdout="\n".join(status_lines), returncode=0)
    add_proc = MagicMock(returncode=0, stderr="")
    commit_proc = MagicMock(returncode=0, stderr="")
    mock_sp.run.side_effect = [status_proc, add_proc, commit_proc]
    return mock_sp


def _mock_subprocess_for_stash(status_lines: list[str]):
    """Build a mock of ``subprocess`` whose run() returns: status, stash."""
    mock_sp = MagicMock()
    status_proc = MagicMock(stdout="\n".join(status_lines), returncode=0)
    stash_proc = MagicMock(returncode=0, stderr="")
    mock_sp.run.side_effect = [status_proc, stash_proc]
    return mock_sp


class TestAutoCommitTouchedFilesFilter(unittest.TestCase):
    """``_auto_commit_changes`` must intersect with ``dispatch_touched_files``."""

    def test_refuses_to_commit_when_touched_files_is_none(self):
        """``dispatch_touched_files=None`` is fail-safe — no git work runs."""
        mock_sp = MagicMock()
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_commit_changes(
                "d-r2-a", "T1",
                pre_dispatch_dirty=set(),
                dispatch_touched_files=None,
            )
        self.assertFalse(result)
        self.assertFalse(mock_sp.run.called)

    def test_concurrent_edit_file_excluded_when_only_other_files_touched(self):
        """Shared-worktree scenario: this worker wrote ``mine.py`` while a
        concurrent terminal touched ``their_concurrent.py`` during the same
        window.  Only the file in dispatch_touched_files is staged."""
        pre = set()
        # Both files appear in the dispatch window (not in pre).
        status_lines = [" M mine.py", " M their_concurrent.py"]
        mock_sp = _mock_subprocess_for_commit(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files",
                   return_value={"mine.py", "their_concurrent.py"}):
            result = _auto_commit_changes(
                "d-r2-b", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"mine.py"}),
            )

        self.assertTrue(result)
        add_cmd = mock_sp.run.call_args_list[1][0][0]
        self.assertIn("mine.py", add_cmd)
        self.assertNotIn("their_concurrent.py", add_cmd)

    def test_no_commit_when_only_concurrent_edits_present(self):
        """If every dispatch-window dirty file came from another terminal
        (none in dispatch_touched_files), refuse to commit."""
        pre = set()
        status_lines = [" M their_concurrent.py", "?? other_terminal_new.py"]
        mock_sp = _mock_subprocess_for_commit(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files",
                   return_value={"their_concurrent.py", "other_terminal_new.py"}):
            result = _auto_commit_changes(
                "d-r2-c", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"unrelated_touched.py"}),
            )

        self.assertFalse(result)
        # status was checked, but add and commit must not have run.
        called_cmds = [c[0][0] for c in mock_sp.run.call_args_list]
        for cmd in called_cmds:
            self.assertNotEqual(cmd[:2], ["git", "add"])
            self.assertNotEqual(cmd[:2], ["git", "commit"])

    def test_touched_file_already_dirty_pre_dispatch_excluded(self):
        """A file the worker explicitly touched but which was *already* dirty
        before the dispatch is excluded — operator must commit pre-existing
        dirty files manually."""
        pre = {"shared.py"}
        status_lines = [" M shared.py"]
        mock_sp = _mock_subprocess_for_commit(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"shared.py"}):
            result = _auto_commit_changes(
                "d-r2-d", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"shared.py"}),
            )

        self.assertFalse(result)
        called_cmds = [c[0][0] for c in mock_sp.run.call_args_list]
        for cmd in called_cmds:
            self.assertNotEqual(cmd[:2], ["git", "add"])

    def test_touched_file_not_currently_dirty_excluded(self):
        """A file the worker touched but later reverted (no longer dirty) is
        not staged — there is nothing to commit."""
        pre = set()
        status_lines = []  # clean tree
        mock_sp = MagicMock()
        mock_sp.run.return_value = MagicMock(stdout="", returncode=0)
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_commit_changes(
                "d-r2-e", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"reverted.py"}),
            )
        self.assertFalse(result)


class TestAutoStashTouchedFilesFilter(unittest.TestCase):
    """``_auto_stash_changes`` must intersect with ``dispatch_touched_files``."""

    def test_refuses_to_stash_when_touched_files_is_none(self):
        mock_sp = MagicMock()
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_stash_changes(
                "d-r2-s1", "T1",
                pre_dispatch_dirty=set(),
                dispatch_touched_files=None,
            )
        self.assertFalse(result)
        self.assertFalse(mock_sp.run.called)

    def test_concurrent_edit_excluded_from_stash(self):
        pre = set()
        status_lines = [" M mine.py", "?? their_new.py"]
        mock_sp = _mock_subprocess_for_stash(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files",
                   return_value={"mine.py", "their_new.py"}):
            result = _auto_stash_changes(
                "d-r2-s2", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"mine.py"}),
            )

        self.assertTrue(result)
        stash_cmd = mock_sp.run.call_args_list[1][0][0]
        self.assertEqual(stash_cmd[:3], ["git", "stash", "push"])
        self.assertIn("mine.py", stash_cmd)
        self.assertNotIn("their_new.py", stash_cmd)

    def test_no_stash_when_only_concurrent_files_dirty(self):
        pre = set()
        status_lines = [" M their_concurrent.py"]
        mock_sp = _mock_subprocess_for_stash(status_lines)
        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files",
                   return_value={"their_concurrent.py"}):
            result = _auto_stash_changes(
                "d-r2-s3", "T1",
                pre_dispatch_dirty=pre,
                dispatch_touched_files=frozenset({"unrelated.py"}),
            )
        self.assertFalse(result)
        called_cmds = [c[0][0] for c in mock_sp.run.call_args_list]
        for cmd in called_cmds:
            self.assertNotEqual(cmd[:2], ["git", "stash"])


# ---------------------------------------------------------------------------
# deliver_via_subprocess: touched_files plumbed into _SubprocessResult
# ---------------------------------------------------------------------------


class TestDeliverViaSubprocessTouchedFiles(unittest.TestCase):
    """``deliver_via_subprocess`` must accumulate touched_files from streamed
    tool_use events and surface it via the returned ``_SubprocessResult``."""

    def _patch_chain(self):
        return [
            patch("subprocess_dispatch._inject_skill_context", return_value="instr"),
            patch("subprocess_dispatch._inject_permission_profile", return_value="instr"),
            patch("subprocess_dispatch._resolve_agent_cwd", return_value=None),
            patch("subprocess_dispatch._write_manifest", return_value="/tmp/m.json"),
            patch("subprocess_dispatch._promote_manifest", return_value="/tmp/done.json"),
        ]

    def test_touched_files_accumulated_from_tool_use_events(self):
        repo_root = subprocess_dispatch.Path(
            subprocess_dispatch.__file__
        ).resolve().parents[2]

        # Two writes inside the repo, one Read (ignored), one path outside repo
        # (must be filtered out by _normalize_repo_path).
        events = iter([
            _FakeEvent("tool_use", {
                "name": "Edit",
                "input": {"file_path": str(repo_root / "scripts" / "lib" / "subprocess_dispatch.py")},
            }),
            _FakeEvent("tool_use", {
                "name": "Write",
                "input": {"file_path": str(repo_root / "tests" / "test_new.py")},
            }),
            _FakeEvent("tool_use", {
                "name": "Read",
                "input": {"file_path": str(repo_root / "scripts" / "lib" / "subprocess_dispatch.py")},
            }),
            _FakeEvent("tool_use", {
                "name": "Write",
                "input": {"file_path": "/etc/passwd"},  # outside repo — drop
            }),
        ])

        adapter = MagicMock()
        adapter.deliver.return_value = MagicMock(success=True)
        adapter.read_events_with_timeout.return_value = events
        obs = MagicMock()
        obs.transport_state = {"returncode": 0}
        adapter.observe.return_value = obs
        adapter.was_timed_out.return_value = False
        adapter.get_session_id.return_value = "sess-touched"
        adapter._get_event_store.return_value = None
        adapter.trigger_report_pipeline.return_value = None

        cms = self._patch_chain() + [
            patch("subprocess_dispatch.SubprocessAdapter", return_value=adapter),
        ]
        for cm in cms:
            cm.start()
        try:
            result = subprocess_dispatch.deliver_via_subprocess(
                "T1", "do work", "sonnet", "d-r2-touched",
            )
        finally:
            for cm in reversed(cms):
                cm.stop()

        self.assertTrue(result.success)
        self.assertIsInstance(result, _SubprocessResult)
        # Repo-internal Edit + Write captured; Read and out-of-repo Write dropped.
        self.assertEqual(
            result.touched_files,
            frozenset({
                "scripts/lib/subprocess_dispatch.py",
                "tests/test_new.py",
            }),
        )

    def test_touched_files_empty_set_when_no_writes(self):
        adapter = MagicMock()
        adapter.deliver.return_value = MagicMock(success=True)
        adapter.read_events_with_timeout.return_value = iter([])
        obs = MagicMock()
        obs.transport_state = {"returncode": 0}
        adapter.observe.return_value = obs
        adapter.was_timed_out.return_value = False
        adapter.get_session_id.return_value = "sess-empty"
        adapter._get_event_store.return_value = None
        adapter.trigger_report_pipeline.return_value = None

        cms = self._patch_chain() + [
            patch("subprocess_dispatch.SubprocessAdapter", return_value=adapter),
        ]
        for cm in cms:
            cm.start()
        try:
            result = subprocess_dispatch.deliver_via_subprocess(
                "T1", "do work", "sonnet", "d-r2-empty",
            )
        finally:
            for cm in reversed(cms):
                cm.stop()
        self.assertTrue(result.success)
        self.assertEqual(result.touched_files, frozenset())

    def test_touched_files_returned_on_failure_paths_too(self):
        """Even on non-zero exit, accumulated touched_files must be returned so
        the failure-path stash can scope correctly."""
        repo_root = subprocess_dispatch.Path(
            subprocess_dispatch.__file__
        ).resolve().parents[2]
        events = iter([
            _FakeEvent("tool_use", {
                "name": "Write",
                "input": {"file_path": str(repo_root / "scripts" / "lib" / "halfwritten.py")},
            }),
        ])

        adapter = MagicMock()
        adapter.deliver.return_value = MagicMock(success=True)
        adapter.read_events_with_timeout.return_value = events
        obs = MagicMock()
        obs.transport_state = {"returncode": 2}  # non-zero → fail-closed
        adapter.observe.return_value = obs
        adapter.was_timed_out.return_value = False
        adapter.get_session_id.return_value = "sess-failed"
        adapter._get_event_store.return_value = None
        adapter.trigger_report_pipeline.return_value = None

        cms = self._patch_chain() + [
            patch("subprocess_dispatch.SubprocessAdapter", return_value=adapter),
        ]
        for cm in cms:
            cm.start()
        try:
            result = subprocess_dispatch.deliver_via_subprocess(
                "T1", "do work", "sonnet", "d-r2-failed",
            )
        finally:
            for cm in reversed(cms):
                cm.stop()

        self.assertFalse(result.success)
        self.assertEqual(
            result.touched_files,
            frozenset({"scripts/lib/halfwritten.py"}),
        )


# ---------------------------------------------------------------------------
# deliver_with_recovery: touched_files forwarded to commit/stash helpers
# ---------------------------------------------------------------------------


class TestDeliverWithRecoveryForwardsTouchedFiles(unittest.TestCase):
    """deliver_with_recovery must forward _SubprocessResult.touched_files to
    both auto_commit and auto_stash helpers."""

    def test_touched_files_forwarded_to_auto_commit(self):
        touched = frozenset({"scripts/lib/foo.py"})
        sub_result = _SubprocessResult(
            success=True,
            session_id="s1",
            event_count=2,
            manifest_path="/tmp/m.json",
            touched_files=touched,
        )

        with patch("subprocess_dispatch.deliver_via_subprocess", return_value=sub_result), \
             patch("subprocess_dispatch._auto_commit_changes", return_value=True) as mock_ac, \
             patch("subprocess_dispatch._auto_stash_changes") as mock_as, \
             patch("subprocess_dispatch._write_receipt"), \
             patch("subprocess_dispatch._check_commit_since", return_value=True), \
             patch("subprocess_dispatch._get_commit_hash", return_value="abc"), \
             patch("subprocess_dispatch._get_dirty_files", return_value=set()), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("subprocess_dispatch._update_pattern_confidence", return_value=0), \
             patch("subprocess_dispatch.WorkerHealthMonitor") as mock_monitor:
            mock_monitor.return_value = MagicMock(stuck_count=0)
            subprocess_dispatch.deliver_with_recovery(
                "T1", "do work", "sonnet", "d-r2-fwd-1",
                max_retries=0, auto_commit=True,
            )
            mock_as.assert_not_called()

        mock_ac.assert_called_once()
        kwargs = mock_ac.call_args.kwargs
        self.assertEqual(kwargs.get("dispatch_touched_files"), touched)

    def test_touched_files_forwarded_to_auto_stash_on_failure(self):
        touched = frozenset({"scripts/lib/halfwritten.py"})
        sub_result = _SubprocessResult(
            success=False,
            session_id=None,
            event_count=1,
            manifest_path="/tmp/m.json",
            touched_files=touched,
        )

        with patch("subprocess_dispatch.deliver_via_subprocess", return_value=sub_result), \
             patch("subprocess_dispatch._auto_commit_changes") as mock_ac, \
             patch("subprocess_dispatch._auto_stash_changes", return_value=False) as mock_as, \
             patch("subprocess_dispatch._write_receipt"), \
             patch("subprocess_dispatch._get_commit_hash", return_value="abc"), \
             patch("subprocess_dispatch._get_dirty_files", return_value=set()), \
             patch("subprocess_dispatch._capture_dispatch_parameters"), \
             patch("subprocess_dispatch._capture_dispatch_outcome"), \
             patch("subprocess_dispatch._update_pattern_confidence", return_value=0), \
             patch("subprocess_dispatch.WorkerHealthMonitor") as mock_monitor:
            mock_monitor.return_value = MagicMock(stuck_count=0)
            subprocess_dispatch.deliver_with_recovery(
                "T1", "do work", "sonnet", "d-r2-fwd-2",
                max_retries=0, auto_commit=True,
            )
            mock_ac.assert_not_called()

        mock_as.assert_called_once()
        kwargs = mock_as.call_args.kwargs
        self.assertEqual(kwargs.get("dispatch_touched_files"), touched)


if __name__ == "__main__":
    unittest.main()
