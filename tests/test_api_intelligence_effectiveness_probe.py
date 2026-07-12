"""Tests for /api/intelligence/effectiveness-probe (framework-status-audit-and-cockpit PR-17).

Exposes the injection-effectiveness probe (PR-6) signal — the same probe that
gates the learning loop's activation — for dashboard display. Read-only.
"""
from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "dashboard"))
sys.path.insert(0, str(_ROOT / "scripts" / "lib"))

import api_intelligence


def _mock_sd(db_path: Path):
    sd = types.SimpleNamespace()
    sd.DB_PATH = db_path
    return sd


def _make_db_with_usage(tmp_path: Path, used: int, ignored: int) -> Path:
    db_path = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE pattern_usage (pattern_id TEXT PRIMARY KEY, "
        "pattern_title TEXT, pattern_hash TEXT, used_count INTEGER DEFAULT 0, "
        "ignored_count INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash, "
        "used_count, ignored_count) VALUES ('p1', 'P', 'h', ?, ?)",
        (used, ignored),
    )
    conn.commit()
    conn.close()
    return db_path


def test_no_data_reports_unknown(tmp_path):
    db_path = tmp_path / "quality_intelligence.db"  # never created -> no data
    sd = _mock_sd(db_path)

    with patch.object(api_intelligence, "_sd", return_value=sd):
        result = api_intelligence._intelligence_get_effectiveness_probe()

    assert result["probe_health"] == "unknown"
    assert result["ignore_rate"] is None
    assert result["pending_proposals"] == 0
    assert isinstance(result["signal"], str)


def test_healthy_signal_reports_ok_with_ignore_rate(tmp_path):
    db_path = _make_db_with_usage(tmp_path, used=95, ignored=5)
    sd = _mock_sd(db_path)

    with patch.object(api_intelligence, "_sd", return_value=sd):
        result = api_intelligence._intelligence_get_effectiveness_probe()

    assert result["probe_health"] == "ok"
    assert result["ignore_rate"] == 0.05


def test_high_ignore_rate_reports_produces_crap(tmp_path):
    db_path = _make_db_with_usage(tmp_path, used=2, ignored=98)
    sd = _mock_sd(db_path)

    with patch.object(api_intelligence, "_sd", return_value=sd):
        result = api_intelligence._intelligence_get_effectiveness_probe()

    assert result["probe_health"] == "produces_crap"
    assert result["ignore_rate"] == 0.98


def test_probe_import_failure_degrades_to_unknown_not_500(tmp_path):
    """A broken probe import must never crash the dashboard endpoint."""
    db_path = tmp_path / "quality_intelligence.db"
    sd = _mock_sd(db_path)

    with patch.object(api_intelligence, "_sd", return_value=sd), \
         patch.dict(sys.modules, {"injection_effectiveness_probe": None}):
        result = api_intelligence._intelligence_get_effectiveness_probe()

    assert result["probe_health"] == "unknown"
    assert result["pending_proposals"] == 0
