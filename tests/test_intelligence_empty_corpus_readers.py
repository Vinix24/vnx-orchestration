"""Q7: corpus readers work on an empty store built from the production DDL."""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from intelligence_daemon_monitor import (
    _collect_pattern_metrics,
    validate_monitor_schema_compatibility,
)
from intelligence_selector import build_doc_relevant_item
from intelligence_sources.doc_relevant import fetch_relevant_doc_sections
from quality_dashboard_integration import QualityDashboardIntegrator


@pytest.fixture
def empty_corpus(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "quality_intelligence.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.executescript((ROOT / "schemas/quality_intelligence.sql").read_text())
        assert conn.execute("SELECT COUNT(*) FROM code_snippets").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM snippet_metadata").fetchone()[0] == 0
    return db_path


def test_monitor_accepts_empty_corpus(empty_corpus):
    with closing(sqlite3.connect(empty_corpus)) as conn:
        validate_monitor_schema_compatibility(conn)
        assert _collect_pattern_metrics(conn) == (0, None)


def test_dashboard_returns_zero_snippet_metrics(empty_corpus):
    dashboard = QualityDashboardIntegrator(empty_corpus)
    dashboard.connect()
    try:
        assert dashboard.get_snippet_summary() == {
            "total_snippets": 0,
            "avg_quality": 0,
            "high_quality_count": 0,
            "frequently_used": 0,
            "source_files": 0,
        }
    finally:
        dashboard.close()


def test_doc_relevant_has_no_item_for_empty_corpus(empty_corpus):
    paths = ["scripts/lib/intelligence_hygiene.py"]
    instruction = "Verify intelligence corpus quarantine"
    with closing(sqlite3.connect(empty_corpus)) as conn:
        conn.row_factory = sqlite3.Row
        assert fetch_relevant_doc_sections(conn, paths, instruction) == []
        assert build_doc_relevant_item(
            conn, "dlv-649b59a2a2fb", paths, instruction, "2026-11-09T00:00:00Z"
        ) is None


def test_gather_cli_reports_zero_patterns(empty_corpus):
    env = os.environ.copy()
    env.update({
        "VNX_STATE_DIR": str(empty_corpus.parent),
        "VNX_HOME": str(ROOT),
        "VNX_PROJECT_ROOT": str(ROOT),
        "VNX_CANONICAL_ROOT": str(ROOT),
        "VNX_STATE_SIMPLIFICATION_ROLLBACK": "0",
    })
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/gather_intelligence.py"),
         "gather", "corpus quarantine", "T0"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["pattern_count"] == 0
    assert payload["suggested_patterns"] == []
