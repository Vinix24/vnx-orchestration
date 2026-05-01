#!/usr/bin/env python3
"""W4F regression tests — worker attribution + manifest convenience helper.

Covers the dispatch 20260501-w4f-subprocess-git-scope work items:

  OI-1198 (attribution): subprocess auto-commit messages must include
    ``Worker-Terminal:`` and ``Worker-Model:`` trailers in addition to the
    existing ``Dispatch-ID:`` trailer so that downstream auditing can
    distinguish operator-authored commits from machine-authored ones, even
    though both share the same git identity.

  OI-1196 (manifest convenience): ``dispatch_paths.paths_for_dispatch`` is
    a single-arg wrapper around ``read_manifest`` that resolves the VNX
    state directory automatically.

The git scope semantics (OI-1196 add-scope, OI-1197 ``stash push -u``) are
covered in ``test_auto_commit_stash_isolation.py`` and
``test_dispatch_paths_scope.py``; we only add coverage for the missing
attribution + helper here.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import dispatch_paths  # noqa: E402
import subprocess_dispatch  # noqa: E402
from subprocess_dispatch import _auto_commit_changes  # noqa: E402
from subprocess_dispatch_internals.receipt_writer import (  # noqa: E402
    _build_commit_message,
)


# ---------------------------------------------------------------------------
# OI-1198 — worker attribution trailers
# ---------------------------------------------------------------------------


class TestBuildCommitMessage(unittest.TestCase):
    """Pure function — easy to test the trailer layout in isolation."""

    def test_includes_dispatch_id_trailer(self):
        msg = _build_commit_message("w4f", "20260501-attr-1", "T1", "opus")
        self.assertIn("Dispatch-ID: 20260501-attr-1", msg)

    def test_includes_worker_terminal_trailer(self):
        msg = _build_commit_message("w4f", "20260501-attr-2", "T2", "sonnet")
        self.assertIn("Worker-Terminal: T2", msg)

    def test_includes_worker_model_trailer(self):
        msg = _build_commit_message("w4f", "20260501-attr-3", "T3", "haiku")
        self.assertIn("Worker-Model: haiku", msg)

    def test_omits_worker_model_when_unknown(self):
        """Older callers without model context must not produce a
        ``Worker-Model:`` line — empty/missing model would be misleading."""
        msg = _build_commit_message("w4f", "20260501-attr-4", "T1", None)
        self.assertNotIn("Worker-Model:", msg)
        # But terminal + dispatch-id remain.
        self.assertIn("Worker-Terminal: T1", msg)
        self.assertIn("Dispatch-ID: 20260501-attr-4", msg)

    def test_subject_line_format_preserved(self):
        """The conventional-commit subject line is unchanged so existing
        tooling that greps the first line still works."""
        msg = _build_commit_message("w4f", "20260501-attr-5", "T1", "opus")
        first_line = msg.splitlines()[0]
        self.assertEqual(
            first_line, "feat(w4f): auto-commit from headless worker T1"
        )

    def test_trailers_separated_from_subject_by_blank_line(self):
        """Git trailer parsing requires a blank line between subject and
        the trailer block."""
        msg = _build_commit_message("w4f", "20260501-attr-6", "T1", "opus")
        lines = msg.splitlines()
        self.assertEqual(lines[1], "", "second line must be blank")


class TestAutoCommitWiresModelIntoMessage(unittest.TestCase):
    """End-to-end: ``_auto_commit_changes(... model=...)`` flows into the
    git commit invocation."""

    def _mock_subprocess(self, status_lines):
        mock_sp = MagicMock()
        status_proc = MagicMock()
        status_proc.stdout = "\n".join(status_lines)
        status_proc.returncode = 0
        add_proc = MagicMock()
        add_proc.returncode = 0
        add_proc.stderr = ""
        commit_proc = MagicMock()
        commit_proc.returncode = 0
        commit_proc.stderr = ""
        mock_sp.run.side_effect = [status_proc, add_proc, commit_proc]
        return mock_sp

    def test_model_appears_in_commit_message(self):
        mock_sp = self._mock_subprocess([" M scripts/lib/foo.py"])
        with patch("subprocess_dispatch.subprocess", mock_sp), patch(
            "subprocess_dispatch._get_dirty_files",
            return_value={"scripts/lib/foo.py"},
        ):
            committed = _auto_commit_changes(
                "20260501-attr-e2e",
                "T1",
                pre_dispatch_dirty=set(),
                dispatch_touched_files=frozenset({"scripts/lib/foo.py"}),
                model="opus",
            )
        self.assertTrue(committed)
        commit_call = mock_sp.run.call_args_list[2]
        cmd = commit_call[0][0]
        msg = cmd[cmd.index("-m") + 1]
        self.assertIn("Worker-Terminal: T1", msg)
        self.assertIn("Worker-Model: opus", msg)
        self.assertIn("Dispatch-ID: 20260501-attr-e2e", msg)

    def test_legacy_callers_without_model_kwarg_still_work(self):
        """Backward compat: pre-W4F call sites that do not pass ``model``
        must still produce a valid commit, just without the model trailer."""
        mock_sp = self._mock_subprocess([" M scripts/lib/bar.py"])
        with patch("subprocess_dispatch.subprocess", mock_sp), patch(
            "subprocess_dispatch._get_dirty_files",
            return_value={"scripts/lib/bar.py"},
        ):
            committed = _auto_commit_changes(
                "20260501-attr-legacy",
                "T2",
                pre_dispatch_dirty=set(),
                dispatch_touched_files=frozenset({"scripts/lib/bar.py"}),
            )
        self.assertTrue(committed)
        commit_call = mock_sp.run.call_args_list[2]
        cmd = commit_call[0][0]
        msg = cmd[cmd.index("-m") + 1]
        self.assertIn("Worker-Terminal: T2", msg)
        self.assertNotIn("Worker-Model:", msg)


# ---------------------------------------------------------------------------
# OI-1196 — paths_for_dispatch convenience helper
# ---------------------------------------------------------------------------


class TestPathsForDispatch(unittest.TestCase):
    """``paths_for_dispatch`` should resolve the default VNX state dir
    automatically and forward to ``read_manifest``."""

    def test_returns_none_when_no_manifest(self):
        with patch(
            "subprocess_dispatch_internals.state_paths._default_state_dir",
            return_value=Path("/nonexistent/state-dir-paths-helper"),
        ):
            result = dispatch_paths.paths_for_dispatch(
                "20260501-pfd-missing"
            )
        self.assertIsNone(result)

    def test_round_trip_with_real_state_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dispatch_paths.write_manifest(
                tmp_path,
                "20260501-pfd-rt",
                ["scripts/foo.py", "tests/bar.py"],
            )
            with patch(
                "subprocess_dispatch_internals.state_paths._default_state_dir",
                return_value=tmp_path,
            ):
                result = dispatch_paths.paths_for_dispatch(
                    "20260501-pfd-rt"
                )
        self.assertEqual(
            result, ["scripts/foo.py", "tests/bar.py"]
        )

    def test_empty_manifest_returns_empty_list(self):
        """Distinct from None: caller must be able to tell an empty
        manifest (declared zero scope -> fail-safe) from a missing one
        (legacy dispatch -> fall back to pre/touched scoping)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dispatch_paths.write_manifest(
                tmp_path, "20260501-pfd-empty", []
            )
            with patch(
                "subprocess_dispatch_internals.state_paths._default_state_dir",
                return_value=tmp_path,
            ):
                result = dispatch_paths.paths_for_dispatch(
                    "20260501-pfd-empty"
                )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
