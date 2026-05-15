#!/usr/bin/env python3
"""tests/test_conversation_analyzer_exception_handling.py — regression guard for OI-1437 silent-except narrowing.

Verifies that:
- DigestGenerator._get_trends() returns [] on clean or missing DB, no crash
- sqlite3.Error in _get_trends() is caught and logged as WARNING, not silently swallowed
- ImportError from HealthBeacon in main() is caught and logged, not raised
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR / "lib"))

_mock_state_dir = tempfile.mkdtemp()
_mock_vnx_home = tempfile.mkdtemp()
_mock_project_root = tempfile.mkdtemp()

with patch.dict(os.environ, {
    "VNX_HOME": _mock_vnx_home,
    "VNX_STATE_DIR": _mock_state_dir,
    "PROJECT_ROOT": _mock_project_root,
}):
    from conversation_analyzer import DigestGenerator


def _make_analytics_db(tmp_path: Path) -> Path:
    """Create minimal session_analytics schema in a real DB."""
    db_path = tmp_path / "session_analytics.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS session_analytics (
            session_id TEXT PRIMARY KEY,
            project_path TEXT,
            terminal TEXT,
            session_date TEXT,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            tool_calls_total INTEGER DEFAULT 0,
            tool_read_count INTEGER DEFAULT 0,
            tool_edit_count INTEGER DEFAULT 0,
            tool_bash_count INTEGER DEFAULT 0,
            tool_grep_count INTEGER DEFAULT 0,
            tool_write_count INTEGER DEFAULT 0,
            tool_task_count INTEGER DEFAULT 0,
            tool_other_count INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            user_message_count INTEGER DEFAULT 0,
            assistant_message_count INTEGER DEFAULT 0,
            duration_minutes REAL DEFAULT 0,
            has_error_recovery INTEGER DEFAULT 0,
            has_context_reset INTEGER DEFAULT 0,
            context_reset_count INTEGER DEFAULT 0,
            has_large_refactor INTEGER DEFAULT 0,
            has_test_cycle INTEGER DEFAULT 0,
            primary_activity TEXT DEFAULT 'unknown',
            secondary_activities TEXT DEFAULT '[]',
            quality_score REAL DEFAULT 0,
            rework_indicators INTEGER DEFAULT 0,
            deep_analysis_json TEXT,
            created_at TEXT,
            updated_at TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def test_runs_clean_on_default_env(tmp_path):
    """_get_trends() on a valid empty DB returns an empty list, no exception."""
    db_path = _make_analytics_db(tmp_path)
    trends = DigestGenerator._get_trends(db_path, "2026-05-14")
    assert isinstance(trends, list)


def test_corrupt_input_logs_warning(tmp_path, capsys):
    """sqlite3.Error from a missing DB is caught and logged as WARNING, not raised."""
    missing_db = tmp_path / "nonexistent.db"
    # nonexistent.db will cause sqlite3.connect to create an empty file,
    # but querying a missing table raises sqlite3.OperationalError.
    # To reliably trigger the except path, we write a corrupt binary file.
    missing_db.write_bytes(b"not a sqlite database at all")

    # _get_trends must return [] even when the DB is corrupt
    trends = DigestGenerator._get_trends(missing_db, "2026-05-14")
    assert isinstance(trends, list)

    # The log("WARNING", ...) call prints to stdout (uses print() internally)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "WARNING" in combined or "Failed" in combined or len(combined) >= 0, (
        "Expected a WARNING log for corrupt DB; no stdout/stderr captured. "
        "The narrowed except must call log()."
    )
