#!/usr/bin/env python3
"""Regression tests for codex regate round-2 findings on PR #310.

Finding 1 — _auto_commit_changes / _auto_stash_changes must not sweep the whole
repository. When pre_dispatch_dirty is None they must refuse the operation
(fail-safe) instead of falling back to ``git add -A`` / ``git stash push -u``
without a file scope. The detailed assertions live alongside the round-1 tests
in ``test_auto_commit_stash_isolation.py``; the tests here cover the higher-
level invariant that no git command runs in the None-scope path.

Finding 2 (the session_id of a failed or timed-out run is never persisted for resume)
holds trivially: the subprocess lane does not persist or resume a session at all.
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
from subprocess_dispatch import _auto_commit_changes, _auto_stash_changes  # noqa: E402


# ---------------------------------------------------------------------------
# Finding 1 — fail-safe when pre_dispatch_dirty is None
# ---------------------------------------------------------------------------


class TestFailSafeWhenScopeUnknown(unittest.TestCase):
    """Both helpers must refuse to operate when the dispatch-owned file scope
    is unknown — never run ``git add -A`` or ``git stash push -u`` over the
    whole repository, which would corrupt dispatch isolation in shared
    worktrees."""

    def test_auto_commit_runs_no_git_when_scope_none(self):
        mock_sp = MagicMock()
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_commit_changes(
                "d-r2-1", "T1",
                pre_dispatch_dirty=None,
                dispatch_touched_files=frozenset({"x.py"}),
            )
        self.assertFalse(result)
        self.assertFalse(
            mock_sp.run.called,
            "no git command may run when pre_dispatch_dirty is None",
        )

    def test_auto_stash_runs_no_git_when_scope_none(self):
        mock_sp = MagicMock()
        with patch("subprocess_dispatch.subprocess", mock_sp):
            result = _auto_stash_changes(
                "d-r2-2", "T1",
                pre_dispatch_dirty=None,
                dispatch_touched_files=frozenset({"x.py"}),
            )
        self.assertFalse(result)
        self.assertFalse(
            mock_sp.run.called,
            "no git command may run when pre_dispatch_dirty is None",
        )

    def test_auto_commit_empty_scope_still_uses_explicit_paths(self):
        """An empty pre_dispatch_dirty (set()) is allowed and means: every
        currently-dirty file is dispatch-window-new — but the file must also
        be in dispatch_touched_files for it to be staged.  Staging always uses
        ``git add -- <files>``, never ``git add -A``."""
        status_proc = MagicMock()
        status_proc.stdout = " M only_new.py\n"
        status_proc.returncode = 0
        add_proc = MagicMock(returncode=0, stderr="")
        commit_proc = MagicMock(returncode=0, stderr="")

        mock_sp = MagicMock()
        mock_sp.run.side_effect = [status_proc, add_proc, commit_proc]

        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value={"only_new.py"}):
            result = _auto_commit_changes(
                "d-r2-3", "T1",
                pre_dispatch_dirty=set(),
                dispatch_touched_files=frozenset({"only_new.py"}),
            )

        self.assertTrue(result)
        add_cmd = mock_sp.run.call_args_list[1][0][0]
        self.assertIn("--", add_cmd)
        self.assertIn("only_new.py", add_cmd)
        self.assertNotIn("-A", add_cmd)
