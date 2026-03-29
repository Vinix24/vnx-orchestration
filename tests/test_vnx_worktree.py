#!/usr/bin/env python3
"""Tests for VNX Worktree — Python-led worktree detection, snapshot, lifecycle (PR-3).

Tests worktree context detection, intelligence snapshot, worktree start/stop/
refresh/status operations. Validates path resolution determinism (A-R4).
"""

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from vnx_worktree import (
    WorktreeContext,
    WorktreeResult,
    WorktreeInfo,
    DB_NAMES,
    detect_worktree_context,
    snapshot_intelligence,
    worktree_start,
    worktree_stop,
    worktree_refresh,
    worktree_status,
    _ensure_worktree_layout,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_git_main(tmp_path):
    """Simulate a main repo (not a worktree)."""
    main = tmp_path / "main-project"
    main.mkdir()
    data = main / ".vnx-data"
    data.mkdir()
    (data / "database").mkdir()
    (data / "state").mkdir()
    (data / "startup_presets").mkdir()
    return main


@pytest.fixture
def mock_git_worktree(tmp_path, mock_git_main):
    """Simulate a worktree directory."""
    wt = tmp_path / "worktree-feature"
    wt.mkdir()
    return wt


# ---------------------------------------------------------------------------
# Worktree detection
# ---------------------------------------------------------------------------

class TestDetectWorktreeContext:
    def test_returns_dataclass(self):
        ctx = detect_worktree_context("/nonexistent")
        assert isinstance(ctx, WorktreeContext)

    def test_not_worktree_on_error(self):
        ctx = detect_worktree_context("/tmp/definitely-not-a-git-repo-xyz")
        assert ctx.is_worktree is False

    def test_current_dir_detection(self):
        """Should work with default project_root."""
        ctx = detect_worktree_context()
        assert isinstance(ctx.is_worktree, bool)


# ---------------------------------------------------------------------------
# Intelligence snapshot
# ---------------------------------------------------------------------------

class TestSnapshotIntelligence:
    def test_copies_databases(self, mock_git_main, tmp_path):
        wt_data = tmp_path / "wt-data"
        wt_data.mkdir()
        (wt_data / "database").mkdir()
        (wt_data / "state").mkdir()

        # Create source databases
        main_db = mock_git_main / ".vnx-data" / "database"
        for db in DB_NAMES[:2]:
            (main_db / db).write_text("db content")

        copied = snapshot_intelligence(
            str(mock_git_main / ".vnx-data"),
            str(wt_data),
        )
        assert copied >= 2
        assert (wt_data / "database" / DB_NAMES[0]).exists()
        assert (wt_data / "database" / DB_NAMES[1]).exists()

    def test_copies_receipts(self, mock_git_main, tmp_path):
        wt_data = tmp_path / "wt-data"
        wt_data.mkdir()
        (wt_data / "state").mkdir()

        # Create receipt file
        (mock_git_main / ".vnx-data" / "state" / "t0_receipts.ndjson").write_text('{"r":1}\n')

        copied = snapshot_intelligence(
            str(mock_git_main / ".vnx-data"),
            str(wt_data),
        )
        assert (wt_data / "state" / "t0_receipts.ndjson").exists()

    def test_copies_presets(self, mock_git_main, tmp_path):
        wt_data = tmp_path / "wt-data"
        wt_data.mkdir()

        # Create preset
        presets = mock_git_main / ".vnx-data" / "startup_presets"
        (presets / "fast.env").write_text("VNX_T1_PROVIDER=codex\n")

        copied = snapshot_intelligence(
            str(mock_git_main / ".vnx-data"),
            str(wt_data),
        )
        assert (wt_data / "startup_presets" / "fast.env").exists()

    def test_writes_snapshot_meta(self, mock_git_main, tmp_path):
        wt_data = tmp_path / "wt-data"
        wt_data.mkdir()

        snapshot_intelligence(
            str(mock_git_main / ".vnx-data"),
            str(wt_data),
        )
        meta = wt_data / ".snapshot_meta"
        assert meta.exists()
        content = meta.read_text()
        assert "snapshot_date=" in content
        assert "source_dir=" in content

    def test_empty_source(self, tmp_path):
        """No databases to copy should still write metadata."""
        main_data = tmp_path / "main"
        main_data.mkdir()
        wt_data = tmp_path / "wt"
        wt_data.mkdir()

        copied = snapshot_intelligence(str(main_data), str(wt_data))
        assert copied == 0
        assert (wt_data / ".snapshot_meta").exists()


# ---------------------------------------------------------------------------
# Worktree layout
# ---------------------------------------------------------------------------

class TestEnsureWorktreeLayout:
    def test_creates_all_subdirs(self, tmp_path):
        data_dir = tmp_path / "vnx-data"
        _ensure_worktree_layout(str(data_dir))

        assert (data_dir / "state").is_dir()
        assert (data_dir / "logs").is_dir()
        assert (data_dir / "pids").is_dir()
        assert (data_dir / "locks").is_dir()
        assert (data_dir / "database").is_dir()
        assert (data_dir / "unified_reports").is_dir()
        assert (data_dir / "dispatches" / "pending").is_dir()
        assert (data_dir / "dispatches" / "active").is_dir()
        assert (data_dir / "dispatches" / "completed").is_dir()
        assert (data_dir / "dispatches" / "failed").is_dir()

    def test_idempotent(self, tmp_path):
        data_dir = tmp_path / "vnx-data"
        _ensure_worktree_layout(str(data_dir))
        _ensure_worktree_layout(str(data_dir))  # Should not raise
        assert (data_dir / "state").is_dir()


# ---------------------------------------------------------------------------
# Worktree start
# ---------------------------------------------------------------------------

class TestWorktreeStart:
    def test_fails_when_not_worktree(self):
        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(False, error="not a worktree")
            result = worktree_start("/some/path")
            assert result.success is False
            assert "Not in a git worktree" in result.message

    def test_fails_when_no_main_data(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        wt = tmp_path / "worktree"
        wt.mkdir()

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(main))
            result = worktree_start(str(wt))
            assert result.success is False
            assert "no .vnx-data" in result.message

    def test_already_initialized(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        (main / ".vnx-data").mkdir()
        wt = tmp_path / "worktree"
        wt.mkdir()
        wt_data = wt / ".vnx-data"
        wt_data.mkdir()
        (wt_data / ".snapshot_meta").write_text("snapshot_date=2026-01-01\n")

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(main))
            result = worktree_start(str(wt))
            assert result.success is True
            assert "already initialized" in result.message

    def test_creates_isolated_data(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        main_data = main / ".vnx-data"
        main_data.mkdir()
        (main_data / "database").mkdir()

        wt = tmp_path / "worktree"
        wt.mkdir()

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(main))
            result = worktree_start(str(wt))
            assert result.success is True

            wt_data = wt / ".vnx-data"
            assert wt_data.is_dir()
            assert (wt_data / ".snapshot_meta").exists()
            assert (wt_data / ".env_override").exists()

            env_content = (wt_data / ".env_override").read_text()
            assert "VNX_DATA_DIR=" in env_content

    def test_removes_old_symlink(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        (main / ".vnx-data").mkdir()

        wt = tmp_path / "worktree"
        wt.mkdir()
        # Create old-style symlink
        old_target = tmp_path / "old-data"
        old_target.mkdir()
        (wt / ".vnx-data").symlink_to(old_target)

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(main))
            result = worktree_start(str(wt))
            assert result.success is True
            assert not (wt / ".vnx-data").is_symlink()


# ---------------------------------------------------------------------------
# Worktree stop
# ---------------------------------------------------------------------------

class TestWorktreeStop:
    def test_fails_when_not_worktree(self):
        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(False)
            result = worktree_stop("/some/path")
            assert result.success is False

    def test_fails_when_no_data(self, tmp_path):
        wt = tmp_path / "worktree"
        wt.mkdir()

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(tmp_path / "main"))
            result = worktree_stop(str(wt))
            assert result.success is False

    def test_merge_only_preserves_data(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        (main / ".vnx-data" / "inbox").mkdir(parents=True)

        wt = tmp_path / "worktree"
        wt.mkdir()
        wt_data = wt / ".vnx-data"
        wt_data.mkdir()

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(main))
            result = worktree_stop(str(wt), merge_only=True, skip_merge=True)
            assert result.success is True
            assert wt_data.exists()  # Not deleted

    def test_full_stop_cleans_data(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        (main / ".vnx-data" / "inbox").mkdir(parents=True)

        wt = tmp_path / "worktree"
        wt.mkdir()
        wt_data = wt / ".vnx-data"
        wt_data.mkdir()

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(main))
            result = worktree_stop(str(wt), skip_merge=True)
            assert result.success is True
            assert not wt_data.exists()  # Deleted


# ---------------------------------------------------------------------------
# Worktree refresh
# ---------------------------------------------------------------------------

class TestWorktreeRefresh:
    def test_fails_when_not_worktree(self):
        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(False)
            result = worktree_refresh("/path")
            assert result.success is False

    def test_refreshes_snapshot(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        main_data = main / ".vnx-data"
        main_data.mkdir()
        (main_data / "database").mkdir()
        (main_data / "database" / "intelligence.db").write_text("updated")

        wt = tmp_path / "worktree"
        wt.mkdir()
        wt_data = wt / ".vnx-data"
        wt_data.mkdir()
        (wt_data / "database").mkdir()

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(main))
            result = worktree_refresh(str(wt))
            assert result.success is True
            assert (wt_data / "database" / "intelligence.db").exists()


# ---------------------------------------------------------------------------
# Worktree status
# ---------------------------------------------------------------------------

class TestWorktreeStatus:
    def test_returns_context_and_list(self):
        with patch("vnx_worktree.detect_worktree_context") as mock_detect, \
             patch("vnx_worktree.subprocess") as mock_sub:
            mock_detect.return_value = WorktreeContext(False, "/main", "/main")
            mock_sub.check_output.return_value = (
                "/main abc123 [main]\n"
                "/wt def456 [feature]\n"
            )

            ctx, wts = worktree_status("/main")
            assert isinstance(ctx, WorktreeContext)
            assert len(wts) == 2

    def test_handles_git_failure(self):
        import subprocess as sp
        with patch("vnx_worktree.detect_worktree_context") as mock_detect, \
             patch("vnx_worktree.subprocess") as mock_sub:
            mock_detect.return_value = WorktreeContext(False, "/main", "/main")
            mock_sub.check_output.side_effect = sp.CalledProcessError(1, "git")
            mock_sub.CalledProcessError = sp.CalledProcessError

            ctx, wts = worktree_status("/main")
            assert wts == []


# ---------------------------------------------------------------------------
# Path resolution determinism (A-R4)
# ---------------------------------------------------------------------------

class TestPathDeterminism:
    def test_snapshot_uses_absolute_paths(self, tmp_path):
        main_data = tmp_path / "main" / ".vnx-data"
        main_data.mkdir(parents=True)
        (main_data / "database").mkdir()
        (main_data / "database" / "intelligence.db").write_text("data")

        wt_data = tmp_path / "wt" / ".vnx-data"
        wt_data.mkdir(parents=True)
        (wt_data / "database").mkdir()

        snapshot_intelligence(str(main_data), str(wt_data))

        meta = (wt_data / ".snapshot_meta").read_text()
        assert str(main_data) in meta

    def test_env_override_uses_absolute_path(self, tmp_path):
        main = tmp_path / "main"
        main.mkdir()
        (main / ".vnx-data").mkdir()

        wt = tmp_path / "worktree"
        wt.mkdir()

        with patch("vnx_worktree.detect_worktree_context") as mock:
            mock.return_value = WorktreeContext(True, str(wt), str(main))
            worktree_start(str(wt))

            env_content = (wt / ".vnx-data" / ".env_override").read_text()
            # Should contain absolute path
            assert str(wt / ".vnx-data") in env_content
