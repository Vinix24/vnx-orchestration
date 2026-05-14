"""Regression tests for OI-1437 exception-narrowing in api_intelligence.py.

Verifies that narrowed except clauses (PR-5 of the hardening series):
- Return graceful defaults on missing state files (not silent crashes)
- Log a warning when a DB file is corrupt (sqlite3.Error path, line ~349)

Reference: chore(hardening) PR-5, merged PRs #491-#493.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "dashboard"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import api_intelligence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sd(tmp_path: Path, db_path: Path | None = None) -> types.SimpleNamespace:
    sd = types.SimpleNamespace()
    sd.DB_PATH = db_path if db_path is not None else (tmp_path / "missing.db")
    sd.REPORTS_DIR = tmp_path / "unified_reports"
    sd.RECEIPTS_PATH = tmp_path / "t0_receipts.ndjson"
    sd.DISPATCHES_DIR = tmp_path / "dispatches"
    sd.VNX_DATA_DIR = tmp_path
    return sd


# ---------------------------------------------------------------------------
# test_endpoint_handles_missing_state_file
# ---------------------------------------------------------------------------

def test_endpoint_handles_missing_state_file(tmp_path: Path) -> None:
    """_intelligence_get_injections returns an empty list when DB_PATH is absent.

    Ensures the endpoint produces a graceful default (not a 500 or an uncaught
    exception) when the state file does not exist yet.
    """
    sd = _make_sd(tmp_path)  # db_path defaults to non-existent path
    assert not sd.DB_PATH.exists(), "precondition: DB must not exist"

    with patch.object(api_intelligence, "_sd", return_value=sd):
        result = api_intelligence._intelligence_get_injections({})

    assert result == {"injections": []}, (
        f"expected graceful empty response, got {result!r}"
    )


# ---------------------------------------------------------------------------
# test_endpoint_logs_corrupt_json (DB-level corruption → sqlite3.Error warning)
# ---------------------------------------------------------------------------

def test_endpoint_logs_corrupt_json(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """_intelligence_get_injections logs a warning when DB file is corrupt.

    A file filled with non-SQLite bytes triggers sqlite3.DatabaseError (a
    subclass of sqlite3.Error). The narrowed except clause at line ~349 must
    log at WARNING level and return an empty list rather than swallowing
    silently.
    """
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"not a sqlite3 database -- garbage content\xff\xfe")

    sd = _make_sd(tmp_path, db_path=corrupt_db)

    with caplog.at_level(logging.WARNING, logger="api_intelligence"):
        with patch.object(api_intelligence, "_sd", return_value=sd):
            result = api_intelligence._intelligence_get_injections({})

    assert result == {"injections": []}, (
        f"expected graceful empty response on corrupt DB, got {result!r}"
    )
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("coordination_events" in m for m in warning_msgs), (
        f"expected a warning mentioning 'coordination_events', got: {warning_msgs!r}"
    )
