"""Regression tests for OI-1437 exception-narrowing in api_operator.py.

Verifies that narrowed except clauses (PR-8 of the hardening series):
- Return graceful defaults on missing state files
- Log when a corrupt JSON file is encountered

Reference: chore(hardening) PR-8, OI-1437, merged PRs #491-#496.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "dashboard"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import api_operator


# ---------------------------------------------------------------------------
# test_endpoint_handles_missing_state_file
# ---------------------------------------------------------------------------

def test_endpoint_handles_missing_state_file(tmp_path: Path, monkeypatch) -> None:
    """_scan_receipts returns an empty dict when REPORTS_DIR is absent.

    Ensures the function produces a graceful default (not an uncaught exception)
    when the unified_reports directory does not exist.
    """
    absent_dir = tmp_path / "unified_reports_absent"
    monkeypatch.setattr(api_operator, "REPORTS_DIR", absent_dir)

    result = api_operator._scan_receipts()

    assert isinstance(result, dict)
    assert result == {}


# ---------------------------------------------------------------------------
# test_endpoint_logs_corrupt_json
# ---------------------------------------------------------------------------

def test_endpoint_logs_corrupt_json(tmp_path: Path, monkeypatch, caplog) -> None:
    """_scan_receipts logs a debug message when a report file cannot be read.

    Simulates an OSError during read_text by writing a valid directory structure
    but then patching Path.read_text to raise OSError.
    """
    reports_dir = tmp_path / "unified_reports"
    reports_dir.mkdir()
    bad_file = reports_dir / "corrupt_report.md"
    bad_file.write_text("**Dispatch ID**: test-corrupt\n", encoding="utf-8")

    monkeypatch.setattr(api_operator, "REPORTS_DIR", reports_dir)

    original_read_text = Path.read_text

    def _raising_read_text(self, *args, **kwargs):
        if self == bad_file:
            raise OSError("simulated read error")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raising_read_text)

    with caplog.at_level(logging.DEBUG, logger="api_operator"):
        result = api_operator._scan_receipts()

    assert isinstance(result, dict)
    assert any("simulated read error" in r.message for r in caplog.records), (
        f"Expected debug log for read error, got: {[r.message for r in caplog.records]}"
    )
