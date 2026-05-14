#!/usr/bin/env python3
"""tests/test_learning_loop_exception_handling.py — regression guard for OI-1437 silent-except narrowing.

Verifies that:
- LearningLoop constructs and runs its main entry without unhandled exceptions
- Corrupt DB state causes a logged debug message, not a silent pass or unhandled exception
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))


def _make_loop_with_db(db_path: Path):
    """Return a LearningLoop backed by a real on-disk SQLite DB."""
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
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT,
            category TEXT,
            title TEXT,
            description TEXT,
            pattern_data TEXT,
            confidence_score REAL DEFAULT 1.0,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT DEFAULT '[]',
            first_seen TEXT,
            last_used TEXT,
            valid_from TEXT,
            valid_until TEXT
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT,
            category TEXT,
            title TEXT,
            description TEXT,
            pattern_data TEXT,
            why_problematic TEXT,
            severity TEXT,
            occurrence_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT DEFAULT '[]',
            first_seen TEXT,
            last_seen TEXT,
            valid_from TEXT,
            valid_until TEXT
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
            source_dispatch_id TEXT,
            valid_until TEXT,
            valid_from TEXT
        );
    """)
    conn.commit()
    conn.close()

    mock_paths = {
        "VNX_HOME": str(_REPO_ROOT),
        "VNX_STATE_DIR": str(db_path.parent),
        "VNX_DATA_DIR": str(db_path.parent),
    }

    with patch("learning_loop.ensure_env", return_value=mock_paths):
        loop = ll.LearningLoop.__new__(ll.LearningLoop)
        loop.vnx_path = Path(mock_paths["VNX_HOME"])
        loop.db_path = db_path
        loop.receipts_path = Path(mock_paths["VNX_HOME"]) / "terminals" / "file_bus" / "receipts"
        loop.archive_path = db_path.parent / "archive" / "patterns"
        loop.archive_path.mkdir(parents=True, exist_ok=True)
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

    return loop, mock_paths


def test_runs_clean_on_default_env(tmp_path):
    """LearningLoop main methods complete without unhandled exceptions on a clean DB."""
    db_path = tmp_path / "quality_intelligence.db"
    loop, mock_paths = _make_loop_with_db(db_path)

    with patch("learning_loop.ensure_env", return_value=mock_paths):
        used = loop.extract_used_patterns()
        ignored = loop.extract_ignored_patterns()
        loop.update_confidence_scores(used, ignored)
        loop.persist_to_intelligence_db()
        loop.archive_unused_patterns(threshold_days=30)
        loop.save_pattern_metrics()

    # No assertion needed beyond no exception being raised


def test_corrupt_state_logs_warning(tmp_path, caplog):
    """Broken DB raises OperationalError which is logged at debug level, not silently swallowed."""
    db_path = tmp_path / "quality_intelligence.db"
    loop, mock_paths = _make_loop_with_db(db_path)

    # Close and corrupt the connection so subsequent queries raise OperationalError
    loop.conn.close()
    loop.conn = sqlite3.connect(":memory:")  # empty — no tables

    with caplog.at_level(logging.DEBUG, logger="learning_loop"):
        with patch("learning_loop.ensure_env", return_value=mock_paths):
            # extract_ignored_patterns fallback path hits the narrowed except
            result = loop.extract_ignored_patterns()

    assert isinstance(result, dict)
    # At least one debug message logged from the OperationalError catch
    debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("failed" in m.lower() or "query" in m.lower() for m in debug_msgs), (
        f"Expected a debug log from OperationalError, got: {debug_msgs}"
    )
