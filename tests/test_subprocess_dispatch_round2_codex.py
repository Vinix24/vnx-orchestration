#!/usr/bin/env python3
"""Regression tests for codex regate round-2 findings on PR #310.

Finding 1 — _auto_commit_changes / _auto_stash_changes must not sweep the whole
repository. When pre_dispatch_dirty is None they must refuse the operation
(fail-safe) instead of falling back to ``git add -A`` / ``git stash push -u``
without a file scope. The detailed assertions live alongside the round-1 tests
in ``test_auto_commit_stash_isolation.py``; the tests here cover the higher-
level invariant that no git command runs in the None-scope path.

Finding 2 — deliver_via_subprocess must NOT persist the session_id for resume
when the subprocess failed (non-zero returncode) or was timeout-killed. With
``VNX_SESSION_RESUME=1`` the next dispatch would otherwise resume a poisoned
conversation state.
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


# ---------------------------------------------------------------------------
# Finding 2 — session_id must not be saved on failure / timeout
# ---------------------------------------------------------------------------


def _make_adapter(returncode: int, was_timed_out: bool = False,
                  session_id: str | None = "post-failure-session"):
    """Build a SubprocessAdapter mock for deliver_via_subprocess unit tests."""
    deliver_result = MagicMock()
    deliver_result.success = True

    obs_result = MagicMock()
    obs_result.transport_state = {"returncode": returncode}

    adapter = MagicMock()
    adapter.deliver.return_value = deliver_result
    adapter.read_events_with_timeout.return_value = iter([])
    adapter.get_session_id.return_value = session_id
    adapter.observe.return_value = obs_result
    adapter.was_timed_out.return_value = was_timed_out
    adapter._get_event_store.return_value = None
    adapter.trigger_report_pipeline.return_value = None
    return adapter


def _common_patches():
    """Patch the helpers that aren't relevant to session-save ordering."""
    return [
        patch("subprocess_dispatch._inject_skill_context", return_value="instr"),
        patch("subprocess_dispatch._inject_permission_profile", return_value="instr"),
        patch("subprocess_dispatch._resolve_agent_cwd", return_value=None),
        patch("subprocess_dispatch._write_manifest", return_value="/tmp/m.json"),
        patch("subprocess_dispatch._promote_manifest", return_value="/tmp/done.json"),
        patch("subprocess_dispatch._capture_dispatch_parameters"),
        patch("subprocess_dispatch._capture_dispatch_outcome"),
    ]


class TestSessionIdNotSavedOnFailureOrTimeout(unittest.TestCase):
    """deliver_via_subprocess must save session_id only after all fail-closed
    checks have passed.  Otherwise a subsequent VNX_SESSION_RESUME=1 dispatch
    will resume a failed/timed-out conversation."""

    def test_session_id_not_saved_when_returncode_nonzero(self):
        store = MagicMock()
        adapter = _make_adapter(returncode=1)

        cms = _common_patches() + [
            patch("subprocess_dispatch.SubprocessAdapter", return_value=adapter),
            patch("session_store.SessionStore", return_value=store),
            patch.dict("os.environ", {"VNX_SESSION_RESUME": "1"}, clear=False),
        ]
        for cm in cms:
            cm.start()
        try:
            result = subprocess_dispatch.deliver_via_subprocess(
                "T1", "do work", "sonnet", "d-r2-fail",
            )
        finally:
            for cm in reversed(cms):
                cm.stop()

        self.assertFalse(result.success)
        # Session_id is captured in the result for diagnostics, but must NOT be
        # persisted to the SessionStore for resume.
        self.assertFalse(
            store.save.called,
            "session must not be saved when subprocess exited non-zero",
        )

    def test_session_id_not_saved_when_timed_out(self):
        store = MagicMock()
        # Timeout path: returncode is None because stop() removed the process
        # from the adapter; was_timed_out() is the authoritative signal.
        adapter = _make_adapter(returncode=None, was_timed_out=True)

        cms = _common_patches() + [
            patch("subprocess_dispatch.SubprocessAdapter", return_value=adapter),
            patch("session_store.SessionStore", return_value=store),
            patch.dict("os.environ", {"VNX_SESSION_RESUME": "1"}, clear=False),
        ]
        for cm in cms:
            cm.start()
        try:
            result = subprocess_dispatch.deliver_via_subprocess(
                "T1", "do work", "sonnet", "d-r2-timeout",
            )
        finally:
            for cm in reversed(cms):
                cm.stop()

        self.assertFalse(result.success)
        self.assertFalse(
            store.save.called,
            "session must not be saved when dispatch was killed by timeout",
        )

    def test_session_id_saved_only_on_clean_success(self):
        """Sanity: the success path still persists the session_id when
        VNX_SESSION_RESUME=1, so we know the move did not break the feature."""
        store = MagicMock()
        adapter = _make_adapter(returncode=0, was_timed_out=False,
                                session_id="fresh-success-session")

        cms = _common_patches() + [
            patch("subprocess_dispatch.SubprocessAdapter", return_value=adapter),
            patch("session_store.SessionStore", return_value=store),
            patch.dict("os.environ", {"VNX_SESSION_RESUME": "1"}, clear=False),
        ]
        for cm in cms:
            cm.start()
        try:
            result = subprocess_dispatch.deliver_via_subprocess(
                "T1", "do work", "sonnet", "d-r2-ok",
            )
        finally:
            for cm in reversed(cms):
                cm.stop()

        self.assertTrue(result.success)
        store.save.assert_called_once()
        args, kwargs = store.save.call_args
        # Save signature: save(terminal_id, session_id, dispatch_id=...)
        self.assertEqual(args[0], "T1")
        self.assertEqual(args[1], "fresh-success-session")


if __name__ == "__main__":
    unittest.main()
