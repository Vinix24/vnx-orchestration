#!/usr/bin/env python3
"""tests/test_learning_loop_tz.py — regression guard for timezone-naive/aware compare.

Verifies that learning_loop.py:
  - accepts naive start_time without TypeError
  - accepts aware start_time without TypeError
  - mixes both in the same call without TypeError
  - defaults to UTC-aware datetime when start_time is None
  - archive_unused_patterns runs without TypeError regardless of last_used tzinfo
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))


def _make_loop_with_db(db_path: Path):
    """Construct a LearningLoop backed by an in-memory sqlite DB at db_path."""
    import learning_loop as ll

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            used_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            last_offered TIMESTAMP,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT,
            rule_type TEXT,
            description TEXT,
            recommendation TEXT,
            confidence REAL DEFAULT 0.5,
            created_at TEXT,
            triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT,
            source TEXT,
            source_dispatch_id TEXT,
            valid_from TEXT,
            valid_until TEXT
        );
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT,
            category TEXT,
            title TEXT,
            description TEXT,
            pattern_data TEXT,
            confidence_score REAL DEFAULT 0.5,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT,
            first_seen TEXT,
            last_used TEXT,
            valid_from TEXT,
            valid_until TEXT
        );
        CREATE TABLE IF NOT EXISTS confidence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            terminal TEXT,
            outcome TEXT NOT NULL,
            patterns_boosted INTEGER DEFAULT 0,
            patterns_decayed INTEGER DEFAULT 0,
            confidence_change REAL NOT NULL,
            occurred_at TEXT NOT NULL
        );
    """)

    # Insert a pattern with a timezone-aware last_used value
    conn.execute(
        """INSERT INTO pattern_usage
           (pattern_id, pattern_title, pattern_hash, used_count, confidence, last_used)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("aware_pat", "Aware Pattern", "hash_aware", 1, 0.8,
         "2026-04-15T13:36:04.278937+00:00"),
    )
    # Insert a pattern with a naive last_used value
    conn.execute(
        """INSERT INTO pattern_usage
           (pattern_id, pattern_title, pattern_hash, used_count, confidence, last_used)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("naive_pat", "Naive Pattern", "hash_naive", 0, 0.2,
         "2025-12-01T00:00:00"),
    )
    # Insert a pattern with NULL last_used
    conn.execute(
        """INSERT INTO pattern_usage
           (pattern_id, pattern_title, pattern_hash, used_count, confidence)
           VALUES (?, ?, ?, ?, ?)""",
        ("null_pat", "Null Pattern", "hash_null", 0, 0.15),
    )
    conn.commit()
    conn.close()


class _FakePaths:
    """Returns fake path env so LearningLoop doesn't touch real state."""
    def __init__(self, state_dir: str, vnx_home: str):
        self._d = {"VNX_STATE_DIR": state_dir, "VNX_HOME": vnx_home}

    def __getitem__(self, key):
        return self._d[key]


def _build_loop(tmp_path: Path):
    """Build a LearningLoop instance pointing at the test DB."""
    import learning_loop as ll

    db_path = tmp_path / "quality_intelligence.db"
    vnx_home = tmp_path / "vnx"
    vnx_home.mkdir()
    (vnx_home / "terminals" / "file_bus" / "receipts").mkdir(parents=True)
    archive_path = tmp_path / "archive" / "patterns"
    archive_path.mkdir(parents=True)

    _make_loop_with_db(db_path)

    fake_paths = _FakePaths(str(tmp_path), str(vnx_home))
    with patch.object(ll, "ensure_env", return_value=fake_paths):
        loop = ll.LearningLoop.__new__(ll.LearningLoop)
        loop.vnx_path = vnx_home
        loop.db_path = db_path
        loop.receipts_path = vnx_home / "terminals" / "file_bus" / "receipts"
        loop.archive_path = archive_path
        loop.conn = sqlite3.connect(str(db_path))
        loop.conn.row_factory = sqlite3.Row
        loop.pattern_metrics = {}
        loop.learning_stats = {
            "patterns_tracked": 0,
            "patterns_used": 0,
            "patterns_ignored": 0,
            "patterns_archived": 0,
            "confidence_adjustments": 0,
            "new_patterns_learned": 0,
        }
        loop.load_pattern_metrics()
    return loop


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExtractUsedPatternsAcceptsNaiveStartTime:
    def test_naive_start_time_no_typeerror(self, tmp_path):
        loop = _build_loop(tmp_path)
        naive = datetime(2026, 4, 1)
        result = loop.extract_used_patterns(naive)
        assert isinstance(result, dict)

    def test_aware_start_time_no_typeerror(self, tmp_path):
        loop = _build_loop(tmp_path)
        aware = datetime(2026, 4, 1, tzinfo=timezone.utc)
        result = loop.extract_used_patterns(aware)
        assert isinstance(result, dict)

    def test_none_start_time_uses_default_window(self, tmp_path):
        loop = _build_loop(tmp_path)
        result = loop.extract_used_patterns(None)
        assert isinstance(result, dict)


class TestExtractIgnoredPatternsAcceptsNaiveStartTime:
    def test_naive_start_time_no_typeerror(self, tmp_path):
        loop = _build_loop(tmp_path)
        naive = datetime(2026, 4, 1)
        result = loop.extract_ignored_patterns(naive)
        assert isinstance(result, dict)

    def test_aware_start_time_no_typeerror(self, tmp_path):
        loop = _build_loop(tmp_path)
        aware = datetime(2026, 4, 1, tzinfo=timezone.utc)
        result = loop.extract_ignored_patterns(aware)
        assert isinstance(result, dict)

    def test_none_start_time_uses_default_window(self, tmp_path):
        loop = _build_loop(tmp_path)
        result = loop.extract_ignored_patterns(None)
        assert isinstance(result, dict)


class TestMixedNaiveAwareNoTypeError:
    def test_archive_unused_patterns_aware_last_used(self, tmp_path):
        """Regression: must not raise TypeError when last_used is tz-aware."""
        loop = _build_loop(tmp_path)
        # Should not raise
        loop.archive_unused_patterns(threshold_days=30)

    def test_archive_unused_patterns_naive_last_used(self, tmp_path):
        """Regression: must not raise TypeError when last_used is tz-naive."""
        import learning_loop as ll
        loop = _build_loop(tmp_path)
        # Override metrics with naive last_used
        loop.pattern_metrics["naive_test"] = ll.PatternUsageMetric(
            pattern_id="naive_test",
            pattern_title="Naive Test",
            pattern_hash="h1",
            confidence=0.1,
            last_used=datetime(2025, 1, 1),  # naive, old, low-confidence
        )
        loop.archive_unused_patterns(threshold_days=30)

    def test_archive_unused_patterns_null_last_used(self, tmp_path):
        """Regression: must handle None last_used without TypeError."""
        import learning_loop as ll
        loop = _build_loop(tmp_path)
        loop.pattern_metrics["null_test"] = ll.PatternUsageMetric(
            pattern_id="null_test",
            pattern_title="Null Test",
            pattern_hash="h2",
            confidence=0.1,
            last_used=None,
        )
        loop.archive_unused_patterns(threshold_days=30)

    def test_to_aware_utc_naive(self):
        import learning_loop as ll
        naive = datetime(2026, 4, 1, 12, 0, 0)
        result = ll._to_aware_utc(naive)
        assert result is not None
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0

    def test_to_aware_utc_aware(self):
        import learning_loop as ll
        aware = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = ll._to_aware_utc(aware)
        assert result == aware

    def test_to_aware_utc_none(self):
        import learning_loop as ll
        assert ll._to_aware_utc(None) is None


class TestLoadPatternMetricsNormalizesLastUsed:
    def test_aware_last_used_loaded_as_aware(self, tmp_path):
        """Patterns with tz-aware last_used must load as tz-aware datetime."""
        loop = _build_loop(tmp_path)
        m = loop.pattern_metrics.get("aware_pat")
        assert m is not None
        assert m.last_used is not None
        assert m.last_used.tzinfo is not None

    def test_naive_last_used_loaded_as_aware(self, tmp_path):
        """Patterns with tz-naive last_used must be normalized to tz-aware."""
        loop = _build_loop(tmp_path)
        m = loop.pattern_metrics.get("naive_pat")
        assert m is not None
        assert m.last_used is not None
        assert m.last_used.tzinfo is not None

    def test_null_last_used_loaded_as_none(self, tmp_path):
        """Patterns with NULL last_used must load as None."""
        loop = _build_loop(tmp_path)
        m = loop.pattern_metrics.get("null_pat")
        assert m is not None
        assert m.last_used is None
