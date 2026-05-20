#!/usr/bin/env python3
"""Test: _effective_db_mtime returns WAL-file mtime when it is more recent.

Dispatch-ID: 20260520-1450-dashboard-phase-a
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"
SCRIPTS_LIB = PROJECT_ROOT / "scripts" / "lib"

if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))


def _import_helper():
    """Import _effective_db_mtime from api_operator (or serve_dashboard as re-export)."""
    try:
        from api_operator import _effective_db_mtime  # noqa: PLC0415
        return _effective_db_mtime
    except ImportError:
        from serve_dashboard import _effective_db_mtime  # noqa: PLC0415
        return _effective_db_mtime


class TestEffectiveDbMtime(unittest.TestCase):
    """_effective_db_mtime picks the most recent sidecar file mtime."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _touch(self, path: Path, offset_seconds: float = 0) -> float:
        """Write an empty file and set its mtime. Returns the set mtime."""
        path.write_bytes(b"")
        target = time.time() + offset_seconds
        os.utime(str(path), (target, target))
        return target

    def test_returns_db_mtime_when_no_wal(self):
        fn = _import_helper()
        db = self.tmpdir / "test.db"
        self._touch(db, offset_seconds=0)
        result = fn(db)
        self.assertAlmostEqual(result, db.stat().st_mtime, places=1)

    def test_returns_wal_mtime_when_wal_is_newer(self):
        fn = _import_helper()
        db = self.tmpdir / "coord.db"
        wal = self.tmpdir / "coord.db-wal"
        self._touch(db, offset_seconds=-10)  # 10 s older
        newer_ts = self._touch(wal, offset_seconds=0)

        result = fn(db)
        self.assertAlmostEqual(result, newer_ts, delta=1.0,
                               msg="Should return WAL mtime when it is more recent than .db")

    def test_returns_shm_mtime_when_shm_is_newest(self):
        fn = _import_helper()
        db = self.tmpdir / "qi.db"
        wal = self.tmpdir / "qi.db-wal"
        shm = self.tmpdir / "qi.db-shm"
        self._touch(db, offset_seconds=-20)
        self._touch(wal, offset_seconds=-10)
        newest_ts = self._touch(shm, offset_seconds=0)

        result = fn(db)
        self.assertAlmostEqual(result, newest_ts, delta=1.0,
                               msg="Should return shm mtime when it is newest")

    def test_returns_zero_for_nonexistent_db(self):
        fn = _import_helper()
        result = fn(self.tmpdir / "nonexistent.db")
        self.assertEqual(result, 0.0)

    def test_db_mtime_wins_when_wal_is_older(self):
        fn = _import_helper()
        db = self.tmpdir / "recent.db"
        wal = self.tmpdir / "recent.db-wal"
        newest_ts = self._touch(db, offset_seconds=0)
        self._touch(wal, offset_seconds=-30)

        result = fn(db)
        self.assertAlmostEqual(result, newest_ts, delta=1.0,
                               msg="Should return .db mtime when it is newer than WAL")


if __name__ == "__main__":
    unittest.main()
