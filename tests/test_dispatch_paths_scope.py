#!/usr/bin/env python3
"""CFX-1 — dispatch_paths.json manifest scoping tests.

Covers six cases referenced in the CFX-1 synthesis (round-2 codex findings on
PRs #303 / #310 / #311):

  A. Manifest with 2 paths -> only those staged on commit, not other dirty files
  B. Stash skips files outside manifest
  C. Legacy mode (no manifest) -> existing pre_dispatch_dirty behavior
  D. Manifest with non-existent path -> warning, no crash, simply no match
  E. HEAD-comparison change detection vs time-window — synthetic test with
     a concurrent unrelated commit shows the SHA-comparison ignores it
  F. Integration — simulate two parallel dispatches in the same worktree;
     dispatch A's auto-commit doesn't sweep dispatch B's edits
"""
from __future__ import annotations

import json
import shutil
import subprocess as real_subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_LIB = str(Path(__file__).resolve().parent.parent / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

import subprocess_dispatch  # noqa: E402
from subprocess_dispatch import (  # noqa: E402
    _auto_commit_changes,
    _auto_stash_changes,
    _count_lines_changed_since_sha,
)
import dispatch_paths  # noqa: E402
from dispatch_paths import (  # noqa: E402
    filter_paths,
    read_manifest,
    write_manifest,
)


# ---------------------------------------------------------------------------
# Manifest helper unit tests
# ---------------------------------------------------------------------------


class TestManifestHelper(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cfx1-manifest-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_then_read_roundtrip(self):
        p = write_manifest(self.tmp, "d1", ["scripts/lib/", "tests/foo.py"])
        self.assertTrue(p.exists())
        data = json.loads(p.read_text())
        self.assertEqual(data["dispatch_id"], "d1")
        self.assertEqual(data["allowed_paths"], ["scripts/lib/", "tests/foo.py"])

        loaded = read_manifest(self.tmp, "d1")
        self.assertEqual(loaded, ["scripts/lib/", "tests/foo.py"])

    def test_read_returns_none_when_missing(self):
        self.assertIsNone(read_manifest(self.tmp, "absent-id"))

    def test_read_returns_none_on_corrupt_json(self):
        p = self.tmp / "dispatch_paths" / "bad.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not valid json")
        self.assertIsNone(read_manifest(self.tmp, "bad"))

    def test_filter_paths_directory_match(self):
        files = ["scripts/lib/foo.py", "scripts/other.py", "tests/bar.py"]
        self.assertEqual(
            sorted(filter_paths(files, ["scripts/lib/"])),
            ["scripts/lib/foo.py"],
        )

    def test_filter_paths_exact_match(self):
        files = ["a/b.py", "a/b.pyc", "a/c.py"]
        self.assertEqual(filter_paths(files, ["a/b.py"]), ["a/b.py"])

    def test_filter_paths_trailing_slash_optional(self):
        files = ["scripts/lib/foo.py"]
        self.assertEqual(
            filter_paths(files, ["scripts/lib"]),
            ["scripts/lib/foo.py"],
        )

    def test_filter_paths_empty_allowed_returns_empty(self):
        self.assertEqual(filter_paths(["a"], []), [])


# ---------------------------------------------------------------------------
# Case A & D — auto_commit honors manifest scope
# ---------------------------------------------------------------------------


class TestAutoCommitManifestScope(unittest.TestCase):
    """Case A: manifest with 2 paths -> only those staged on commit.
    Case D: manifest with non-existent path -> no match, no crash.
    """

    def _setup_mocks(self, dirty_files: set[str]) -> MagicMock:
        status_proc = MagicMock(returncode=0)
        status_proc.stdout = "".join(f" M {f}\n" for f in dirty_files)
        add_proc = MagicMock(returncode=0, stderr="")
        commit_proc = MagicMock(returncode=0, stderr="")
        mock_sp = MagicMock()
        mock_sp.run.side_effect = [status_proc, add_proc, commit_proc]
        return mock_sp

    def test_case_a_manifest_filters_to_two_paths(self):
        # Worker dirtied 4 files; manifest only allows scripts/lib/ and tests/.
        # The two unrelated files (dashboard/, docs/) must be excluded from add.
        current_dirty = {
            "scripts/lib/dispatch_paths.py",
            "tests/test_dispatch_paths_scope.py",
            "dashboard/unrelated.py",
            "docs/unrelated.md",
        }
        mock_sp = self._setup_mocks(current_dirty)

        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value=current_dirty):
            result = _auto_commit_changes(
                "d-A", "T1",
                pre_dispatch_dirty=set(),
                manifest_paths=["scripts/lib/", "tests/"],
            )

        self.assertTrue(result)
        # call 0: status; call 1: add; call 2: commit
        add_cmd = mock_sp.run.call_args_list[1][0][0]
        self.assertEqual(add_cmd[:3], ["git", "add", "--"])
        staged = set(add_cmd[3:])
        self.assertEqual(
            staged,
            {"scripts/lib/dispatch_paths.py", "tests/test_dispatch_paths_scope.py"},
            "manifest must restrict staging to declared paths only",
        )
        self.assertNotIn("dashboard/unrelated.py", staged)
        self.assertNotIn("docs/unrelated.md", staged)

    def test_case_d_manifest_with_nonexistent_path_no_crash(self):
        # Manifest declares a path that nothing is dirty under -> no files
        # staged, no commit, no exception.
        current_dirty = {"scripts/lib/foo.py"}
        status_proc = MagicMock(returncode=0)
        status_proc.stdout = " M scripts/lib/foo.py\n"
        mock_sp = MagicMock()
        mock_sp.run.return_value = status_proc

        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value=current_dirty):
            result = _auto_commit_changes(
                "d-D", "T1",
                pre_dispatch_dirty=set(),
                manifest_paths=["nonexistent/dir/"],
            )

        self.assertFalse(result, "no add+commit when manifest matches nothing")
        # Only status was called; no add, no commit.
        self.assertEqual(mock_sp.run.call_count, 1)


# ---------------------------------------------------------------------------
# Case B — auto_stash honors manifest scope
# ---------------------------------------------------------------------------


class TestAutoStashManifestScope(unittest.TestCase):
    """Case B: stash skips files outside manifest."""

    def test_stash_filters_to_manifest(self):
        current_dirty = {
            "scripts/lib/foo.py",
            "scripts/lib/bar.py",
            "outside/baz.py",
        }
        status_proc = MagicMock(returncode=0)
        status_proc.stdout = (
            " M scripts/lib/foo.py\n"
            " M scripts/lib/bar.py\n"
            " M outside/baz.py\n"
        )
        stash_proc = MagicMock(returncode=0, stderr="")
        mock_sp = MagicMock()
        mock_sp.run.side_effect = [status_proc, stash_proc]

        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value=current_dirty):
            result = _auto_stash_changes(
                "d-B", "T2",
                pre_dispatch_dirty=set(),
                manifest_paths=["scripts/lib/"],
            )

        self.assertTrue(result)
        stash_cmd = mock_sp.run.call_args_list[1][0][0]
        # Expected: git stash push -u -m <name> -- <files>
        self.assertEqual(stash_cmd[:4], ["git", "stash", "push", "-u"])
        self.assertIn("--", stash_cmd)
        sep = stash_cmd.index("--")
        stashed = set(stash_cmd[sep + 1:])
        self.assertEqual(stashed, {"scripts/lib/foo.py", "scripts/lib/bar.py"})
        self.assertNotIn("outside/baz.py", stashed)


# ---------------------------------------------------------------------------
# Case C — legacy mode (no manifest) preserves existing behavior
# ---------------------------------------------------------------------------


class TestLegacyModeNoManifest(unittest.TestCase):
    """Case C: legacy mode (manifest_paths=None) keeps pre_dispatch_dirty
    semantics unchanged.  Only newly-dirtied files are staged."""

    def test_legacy_uses_pre_dispatch_dirty_only(self):
        pre_dirty = {"existing.py"}
        current_dirty = {"existing.py", "new_a.py", "new_b.py"}
        status_proc = MagicMock(returncode=0)
        status_proc.stdout = (
            " M existing.py\n M new_a.py\n M new_b.py\n"
        )
        add_proc = MagicMock(returncode=0, stderr="")
        commit_proc = MagicMock(returncode=0, stderr="")
        mock_sp = MagicMock()
        mock_sp.run.side_effect = [status_proc, add_proc, commit_proc]

        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch("subprocess_dispatch._get_dirty_files", return_value=current_dirty):
            # manifest_paths=None -> legacy code path
            result = _auto_commit_changes(
                "d-C", "T1",
                pre_dispatch_dirty=pre_dirty,
                manifest_paths=None,
            )

        self.assertTrue(result)
        add_cmd = mock_sp.run.call_args_list[1][0][0]
        staged = set(add_cmd[3:])
        # Legacy: only files that became dirty during this dispatch.
        self.assertEqual(staged, {"new_a.py", "new_b.py"})


# ---------------------------------------------------------------------------
# Case E — HEAD-comparison change detection vs time-window
# ---------------------------------------------------------------------------


class TestSHABasedLineCount(unittest.TestCase):
    """Case E: SHA-comparison ignores parallel-dispatch commits inside the
    same time window, while the legacy time-window counter would over-count
    them.  Uses a real temp git repo so the assertion is end-to-end."""

    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="cfx1-sha-"))
        self._git("init", "-q")
        self._git("config", "user.email", "ci@example.com")
        self._git("config", "user.name", "CI")
        # baseline commit
        (self.repo / "base.txt").write_text("hello\n")
        self._git("add", "base.txt")
        self._git("commit", "-q", "-m", "base")
        self.pre_sha = self._git("rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)

    def _git(self, *args: str) -> str:
        proc = real_subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True, text=True, check=True,
        )
        return proc.stdout

    def test_sha_diff_excludes_parallel_unrelated_commit(self):
        # Dispatch A creates one file with 5 lines.
        a_file = self.repo / "scripts" / "a.py"
        a_file.parent.mkdir(parents=True, exist_ok=True)
        a_file.write_text("\n".join(f"line{i}" for i in range(5)) + "\n")
        # Dispatch B (parallel) creates a different file with 20 lines and
        # commits it under the same time window.
        b_file = self.repo / "dashboard" / "b.py"
        b_file.parent.mkdir(parents=True, exist_ok=True)
        b_file.write_text("\n".join(f"x{i}" for i in range(20)) + "\n")

        # Both files are now dirty.  Dispatch A commits only its file.
        self._git("add", "scripts/a.py")
        self._git("commit", "-q", "-m", "dispatch A commit")
        # Dispatch B follows immediately.
        self._git("add", "dashboard/b.py")
        self._git("commit", "-q", "-m", "dispatch B commit (parallel)")

        # SHA-based count for dispatch A's scope must report only A's 5 lines,
        # not 5 + 20.  Direct inline diff against the temp repo asserts the
        # invariant the helper depends on; the helper itself is unit-tested
        # against the empty-sha contract in the next test.
        diff_a = real_subprocess.run(
            ["git", "diff", "--numstat", f"{self.pre_sha}..HEAD",
             "--", "scripts/"],
            cwd=self.repo, capture_output=True, text=True, check=True,
        ).stdout
        added_a = sum(
            int(line.split("\t")[0])
            for line in diff_a.splitlines()
            if line and line.split("\t")[0].isdigit()
        )
        self.assertEqual(added_a, 5, "dispatch A's diff must show only its 5 lines")

        # And the legacy time-window approach (no path filter, no SHA) would
        # see *both* dispatches' commits — proving the upgrade is necessary.
        diff_window = real_subprocess.run(
            ["git", "log", "--numstat", f"{self.pre_sha}..HEAD"],
            cwd=self.repo, capture_output=True, text=True, check=True,
        ).stdout
        added_total = sum(
            int(line.split("\t")[0])
            for line in diff_window.splitlines()
            if line and line[0].isdigit()
        )
        self.assertEqual(
            added_total, 25,
            "time-window aggregation includes parallel dispatch B's 20 lines",
        )

    def test_count_lines_changed_since_sha_returns_zero_for_empty_sha(self):
        # Defensive contract: empty pre_sha must return 0, never raise.
        self.assertEqual(_count_lines_changed_since_sha(""), 0)
        self.assertEqual(_count_lines_changed_since_sha("", paths=["x"]), 0)


# ---------------------------------------------------------------------------
# Case F — integration: two parallel dispatches in same worktree
# ---------------------------------------------------------------------------


class TestParallelDispatchIsolation(unittest.TestCase):
    """Case F: dispatch A's auto-commit must not sweep dispatch B's edits.

    Simulates two parallel workers in the same worktree by exposing both
    workers' dirty files to ``_get_dirty_files`` and asserting that dispatch
    A's auto-commit invocation only stages files within A's manifest scope.
    """

    def test_dispatch_a_does_not_sweep_dispatch_b_edits(self):
        # Both A and B added new files in the same worktree.
        current_dirty = {
            "scripts/a/feature.py",      # dispatch A
            "scripts/a/feature_test.py", # dispatch A
            "scripts/b/other.py",        # dispatch B (parallel)
            "tests/dashboard/x.py",      # dispatch B (parallel)
        }
        status_proc = MagicMock(returncode=0)
        status_proc.stdout = "".join(f"?? {f}\n" for f in current_dirty)
        add_proc = MagicMock(returncode=0, stderr="")
        commit_proc = MagicMock(returncode=0, stderr="")
        mock_sp = MagicMock()
        mock_sp.run.side_effect = [status_proc, add_proc, commit_proc]

        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch(
                 "subprocess_dispatch._get_dirty_files",
                 return_value=current_dirty,
             ):
            committed = _auto_commit_changes(
                "d-A-parallel", "T1",
                pre_dispatch_dirty=set(),
                manifest_paths=["scripts/a/"],
            )

        self.assertTrue(committed)
        add_cmd = mock_sp.run.call_args_list[1][0][0]
        staged = set(add_cmd[3:])
        self.assertEqual(
            staged,
            {"scripts/a/feature.py", "scripts/a/feature_test.py"},
            "manifest must restrict dispatch A's commit to its own paths",
        )
        self.assertNotIn("scripts/b/other.py", staged)
        self.assertNotIn("tests/dashboard/x.py", staged)

    def test_dispatch_b_stash_does_not_capture_dispatch_a_files(self):
        """Symmetric: dispatch B fails and stashes; A's files must stay dirty."""
        current_dirty = {
            "scripts/a/feature.py",   # dispatch A
            "scripts/b/other.py",     # dispatch B (failing)
        }
        status_proc = MagicMock(returncode=0)
        status_proc.stdout = "".join(f"?? {f}\n" for f in current_dirty)
        stash_proc = MagicMock(returncode=0, stderr="")
        mock_sp = MagicMock()
        mock_sp.run.side_effect = [status_proc, stash_proc]

        with patch("subprocess_dispatch.subprocess", mock_sp), \
             patch(
                 "subprocess_dispatch._get_dirty_files",
                 return_value=current_dirty,
             ):
            stashed = _auto_stash_changes(
                "d-B-fail", "T2",
                pre_dispatch_dirty=set(),
                manifest_paths=["scripts/b/"],
            )

        self.assertTrue(stashed)
        stash_cmd = mock_sp.run.call_args_list[1][0][0]
        sep = stash_cmd.index("--")
        in_stash = set(stash_cmd[sep + 1:])
        self.assertEqual(in_stash, {"scripts/b/other.py"})
        self.assertNotIn("scripts/a/feature.py", in_stash)


if __name__ == "__main__":
    unittest.main()
