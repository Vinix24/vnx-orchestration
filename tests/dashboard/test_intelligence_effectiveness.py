#!/usr/bin/env python3
"""Tests for the injection-effectiveness dashboard gauge endpoint
(framework-status-audit-and-cockpit PR-18): GET /api/operator/intelligence/effectiveness.

Covers api_intelligence._intelligence_get_effectiveness_probe(): the point-in-time ignore_rate +
pending_proposals signal read from the same probe (PR-6, injection_effectiveness_probe.py) that
gates the self-learning loop's activation (PR-17). This is a point-in-time gauge, NOT a
time-series -- no ignore_rate history store exists (deepseek finding, PRD PR-18 scope). PR-17
already covers the probe/handler's classification logic in depth
(tests/test_api_intelligence_effectiveness_probe.py); this suite focuses on the PR-18-specific
route wiring and the shape the dashboard gauge depends on.
"""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts" / "lib"))
sys.path.insert(0, str(REPO / "dashboard"))

import serve_dashboard as sd  # noqa: E402
import api_intelligence as api_intel  # noqa: E402


def _make_qi_db(db_path: Path, used: int, ignored: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE pattern_usage (used_count INTEGER, ignored_count INTEGER)")
    conn.execute(
        "INSERT INTO pattern_usage (used_count, ignored_count) VALUES (?, ?)", (used, ignored)
    )
    conn.commit()
    conn.close()


def test_effectiveness_endpoint_returns_expected_shape():
    with patch.object(sd, "DB_PATH", Path("/nonexistent/quality_intelligence.db")):
        result = api_intel._intelligence_get_effectiveness_probe()

    assert set(result.keys()) == {"probe_health", "ignore_rate", "pending_proposals", "signal"}
    assert isinstance(result["signal"], str) and result["signal"]


def test_effectiveness_endpoint_unknown_when_no_data(tmp_path):
    with patch.object(sd, "DB_PATH", tmp_path / "quality_intelligence.db"):
        result = api_intel._intelligence_get_effectiveness_probe()

    assert result["probe_health"] == "unknown"
    assert result["ignore_rate"] is None
    assert result["pending_proposals"] == 0


def test_effectiveness_endpoint_ok_health(tmp_path):
    db_path = tmp_path / "quality_intelligence.db"
    _make_qi_db(db_path, used=90, ignored=10)  # ignore_rate = 0.10 -> ok

    with patch.object(sd, "DB_PATH", db_path):
        result = api_intel._intelligence_get_effectiveness_probe()

    assert result["probe_health"] == "ok"
    assert result["ignore_rate"] == 0.10


def test_effectiveness_endpoint_produces_crap_health(tmp_path):
    db_path = tmp_path / "quality_intelligence.db"
    _make_qi_db(db_path, used=5, ignored=95)  # ignore_rate = 0.95 -> produces_crap

    with patch.object(sd, "DB_PATH", db_path):
        result = api_intel._intelligence_get_effectiveness_probe()

    assert result["probe_health"] == "produces_crap"
    assert result["ignore_rate"] == 0.95


def test_effectiveness_endpoint_counts_pending_proposals(tmp_path):
    db_path = tmp_path / "quality_intelligence.db"
    _make_qi_db(db_path, used=90, ignored=10)
    (tmp_path / "pending_rules.json").write_text(
        '{"pending_rules": [{"status": "pending", "created_at": "2026-07-10T00:00:00Z"}]}',
        encoding="utf-8",
    )

    with patch.object(sd, "DB_PATH", db_path):
        result = api_intel._intelligence_get_effectiveness_probe()

    assert result["pending_proposals"] == 1


def test_route_is_wired_in_serve_dashboard():
    """GET /api/operator/intelligence/effectiveness must be registered as a route
    (PR-18 scope item) on top of PR-17's /api/intelligence/effectiveness-probe gate signal."""
    source = (REPO / "dashboard" / "serve_dashboard.py").read_text(encoding="utf-8")
    assert '"/api/operator/intelligence/effectiveness"' in source


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
