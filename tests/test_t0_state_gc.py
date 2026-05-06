"""Unit tests for _gc_t0_detail() in build_t0_state.py (W-UX-4).

Covers:
  1. Old file (mtime > retention window) is removed
  2. Fresh file (mtime within window) is retained
  3. VNX_T0_DETAIL_RETENTION_DAYS=0 disables GC entirely
  4. GC is idempotent across two consecutive runs
  5. Non-existent detail_dir returns 0 without error
  6. Non-JSON files are not touched
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

from build_t0_state import _gc_t0_detail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_old_file(detail_dir: Path, name: str = "feature_state.json", days_old: int = 20) -> Path:
    """Create a JSON file with mtime set `days_old` days in the past."""
    detail_dir.mkdir(parents=True, exist_ok=True)
    p = detail_dir / name
    p.write_text('{"old": true}', encoding="utf-8")
    old_mtime = time.time() - days_old * 86400
    os.utime(p, (old_mtime, old_mtime))
    return p


def _make_fresh_file(detail_dir: Path, name: str = "open_items.json", days_old: int = 1) -> Path:
    """Create a JSON file with mtime set `days_old` days in the past (within window)."""
    detail_dir.mkdir(parents=True, exist_ok=True)
    p = detail_dir / name
    p.write_text('{"fresh": true}', encoding="utf-8")
    fresh_mtime = time.time() - days_old * 86400
    os.utime(p, (fresh_mtime, fresh_mtime))
    return p


# ---------------------------------------------------------------------------
# 1. Old file is removed
# ---------------------------------------------------------------------------

class TestOldFileRemoved:
    def test_file_older_than_retention_is_deleted(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        old = _make_old_file(detail_dir, days_old=20)

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            deleted = _gc_t0_detail(detail_dir)

        assert not old.exists(), "Old file should have been removed"
        assert deleted == 1

    def test_returns_count_of_deleted_files(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        _make_old_file(detail_dir, name="a.json", days_old=30)
        _make_old_file(detail_dir, name="b.json", days_old=15)

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            deleted = _gc_t0_detail(detail_dir)

        assert deleted == 2
        assert not (detail_dir / "a.json").exists()
        assert not (detail_dir / "b.json").exists()


# ---------------------------------------------------------------------------
# 2. Fresh file is retained
# ---------------------------------------------------------------------------

class TestFreshFileRetained:
    def test_file_within_window_is_not_deleted(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        fresh = _make_fresh_file(detail_dir, days_old=1)

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            deleted = _gc_t0_detail(detail_dir)

        assert fresh.exists(), "Fresh file should be retained"
        assert deleted == 0

    def test_mixed_old_and_fresh_files(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        old = _make_old_file(detail_dir, name="stale.json", days_old=20)
        fresh = _make_fresh_file(detail_dir, name="recent.json", days_old=5)

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            deleted = _gc_t0_detail(detail_dir)

        assert not old.exists(), "Stale file should be removed"
        assert fresh.exists(), "Recent file should be retained"
        assert deleted == 1

    def test_file_13_days_old_is_retained(self, tmp_path):
        """File 13 days old (within the 14-day window) is never deleted."""
        detail_dir = tmp_path / "t0_detail"
        detail_dir.mkdir(parents=True, exist_ok=True)
        p = detail_dir / "recent.json"
        p.write_text('{}', encoding="utf-8")
        mtime = time.time() - 13 * 86400
        os.utime(p, (mtime, mtime))

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            deleted = _gc_t0_detail(detail_dir)

        assert p.exists(), "13-day-old file should be within the 14-day window"
        assert deleted == 0


# ---------------------------------------------------------------------------
# 3. VNX_T0_DETAIL_RETENTION_DAYS=0 disables GC
# ---------------------------------------------------------------------------

class TestGcDisabledWithZero:
    def test_zero_disables_gc_entirely(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        old = _make_old_file(detail_dir, days_old=365)

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "0"}):
            deleted = _gc_t0_detail(detail_dir)

        assert old.exists(), "GC disabled: old file must not be deleted"
        assert deleted == 0

    def test_zero_string_disables_gc(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        old = _make_old_file(detail_dir, days_old=100)

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "0"}):
            result = _gc_t0_detail(detail_dir)

        assert old.exists()
        assert result == 0


# ---------------------------------------------------------------------------
# 4. GC is idempotent
# ---------------------------------------------------------------------------

class TestGcIdempotent:
    def test_two_consecutive_runs_make_no_further_changes(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        _make_old_file(detail_dir, name="stale.json", days_old=20)
        _make_fresh_file(detail_dir, name="fresh.json", days_old=3)

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            deleted_first = _gc_t0_detail(detail_dir)
            deleted_second = _gc_t0_detail(detail_dir)

        assert deleted_first == 1, "First run should delete the stale file"
        assert deleted_second == 0, "Second run should delete nothing more"
        assert (detail_dir / "fresh.json").exists()

    def test_idempotent_with_no_files(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        detail_dir.mkdir()

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            first = _gc_t0_detail(detail_dir)
            second = _gc_t0_detail(detail_dir)

        assert first == 0
        assert second == 0


# ---------------------------------------------------------------------------
# 5. Non-existent directory returns 0 without error
# ---------------------------------------------------------------------------

class TestMissingDirectory:
    def test_missing_dir_returns_zero(self, tmp_path):
        missing = tmp_path / "does_not_exist" / "t0_detail"

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            result = _gc_t0_detail(missing)

        assert result == 0

    def test_missing_dir_with_gc_disabled_returns_zero(self, tmp_path):
        missing = tmp_path / "nowhere"

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "0"}):
            result = _gc_t0_detail(missing)

        assert result == 0


# ---------------------------------------------------------------------------
# 6. Non-JSON files are not touched
# ---------------------------------------------------------------------------

class TestNonJsonFilesUntouched:
    def test_non_json_files_are_not_deleted(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        detail_dir.mkdir()
        txt_file = detail_dir / "notes.txt"
        txt_file.write_text("keep me")
        old_mtime = time.time() - 30 * 86400
        os.utime(txt_file, (old_mtime, old_mtime))

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "14"}):
            deleted = _gc_t0_detail(detail_dir)

        assert txt_file.exists(), "Non-JSON files must not be touched"
        assert deleted == 0


# ---------------------------------------------------------------------------
# 7. Default retention (no env-var set)
# ---------------------------------------------------------------------------

class TestDefaultRetention:
    def test_default_14_days_removes_older_files(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        old = _make_old_file(detail_dir, days_old=15)
        fresh = _make_fresh_file(detail_dir, name="fresh.json", days_old=7)

        env = {k: v for k, v in os.environ.items() if k != "VNX_T0_DETAIL_RETENTION_DAYS"}
        with patch.dict(os.environ, env, clear=True):
            deleted = _gc_t0_detail(detail_dir)

        assert not old.exists()
        assert fresh.exists()
        assert deleted == 1

    def test_invalid_env_var_falls_back_to_14(self, tmp_path):
        detail_dir = tmp_path / "t0_detail"
        old = _make_old_file(detail_dir, days_old=20)

        with patch.dict(os.environ, {"VNX_T0_DETAIL_RETENTION_DAYS": "notanumber"}):
            deleted = _gc_t0_detail(detail_dir)

        # Invalid value → falls back to 14 days → 20-day-old file deleted
        assert not old.exists()
        assert deleted == 1
