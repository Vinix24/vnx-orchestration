#!/usr/bin/env python3
"""
Tests for F50-PR3: feedback loop wiring.

Covers:
- update_confidence_from_outcome: success boosts / failure decays pattern confidence
- generate_prevention_rule_suggestions: antipatterns with count >= 3 produce suggestions
- GET /api/intelligence/learning-summary: endpoint returns valid metrics shape
"""

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
DASHBOARD_DIR = REPO_ROOT / "dashboard"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from intelligence_persist import update_confidence_from_outcome

_UTC = timezone.utc

# ---------------------------------------------------------------------------
# DB fixture helpers
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    pattern_data TEXT NOT NULL,
    confidence_score REAL DEFAULT 0.0,
    usage_count INTEGER DEFAULT 0,
    source_dispatch_ids TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used DATETIME
);

CREATE TABLE IF NOT EXISTS antipatterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    pattern_data TEXT NOT NULL,
    why_problematic TEXT NOT NULL,
    severity TEXT DEFAULT 'medium',
    occurrence_count INTEGER DEFAULT 0,
    source_dispatch_ids TEXT,
    first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_seen DATETIME
);

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
"""


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal quality_intelligence.db for testing."""
    db = tmp_path / "quality_intelligence.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()
    return db


def _insert_pattern(db: Path, dispatch_id: str, confidence: float = 0.6) -> int:
    """Insert a success_pattern linked to a dispatch and return its id."""
    conn = sqlite3.connect(str(db))
    source_ids = json.dumps([dispatch_id])
    cur = conn.execute(
        "INSERT INTO success_patterns "
        "(pattern_type, category, title, description, pattern_data, "
        " confidence_score, usage_count, source_dispatch_ids) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("approach", "governance", "Test pattern", "desc",
         "{}", confidence, 1, source_ids),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def _insert_antipattern(db: Path, title: str, count: int) -> int:
    conn = sqlite3.connect(str(db))
    cur = conn.execute(
        "INSERT INTO antipatterns "
        "(pattern_type, category, title, description, pattern_data, "
        " why_problematic, severity, occurrence_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("approach", "governance", title, "desc", "{}", "problematic", "medium", count),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def _get_confidence(db: Path, pattern_id: int) -> float:
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT confidence_score FROM success_patterns WHERE id = ?", (pattern_id,)
    ).fetchone()
    conn.close()
    return float(row[0])


def _get_usage_count(db: Path, pattern_id: int) -> int:
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT usage_count FROM success_patterns WHERE id = ?", (pattern_id,)
    ).fetchone()
    conn.close()
    return int(row[0])


# ---------------------------------------------------------------------------
# 1. test_success_boosts_confidence
# ---------------------------------------------------------------------------

def test_success_boosts_confidence(tmp_path):
    db = _make_db(tmp_path)
    dispatch_id = "dispatch-abc-001"
    pattern_id = _insert_pattern(db, dispatch_id, confidence=0.6)

    result = update_confidence_from_outcome(db, dispatch_id, "T1", "success")

    assert result["boosted"] == 1
    assert result["decayed"] == 0
    # Beta(success+1, failure+1)/(total+2): first success → (1+1)/(1+0+2) = 2/3
    new_conf = _get_confidence(db, pattern_id)
    assert abs(new_conf - 2 / 3) < 1e-4, f"Expected 0.667, got {new_conf}"
    # usage_count should increment on success
    assert _get_usage_count(db, pattern_id) == 2


def test_success_boost_caps_at_1(tmp_path):
    """Beta posterior never reaches 1.0 — Laplace smoothing keeps the score
    strictly below 1 even after a long success streak.
    """
    db = _make_db(tmp_path)
    dispatch_id = "dispatch-cap-test"
    pattern_id = _insert_pattern(db, dispatch_id, confidence=0.98)

    # Run many successes in a row.
    for _ in range(50):
        update_confidence_from_outcome(db, dispatch_id, "T1", "success")

    new_conf = _get_confidence(db, pattern_id)
    assert new_conf < 1.0
    # 50 successes / 0 failures → 51/52 ≈ 0.981
    assert abs(new_conf - 51 / 52) < 1e-4


def test_success_no_matching_patterns_returns_zero(tmp_path):
    db = _make_db(tmp_path)
    result = update_confidence_from_outcome(db, "nonexistent-dispatch", "T1", "success")
    assert result["boosted"] == 0
    assert result["decayed"] == 0


# ---------------------------------------------------------------------------
# 2. test_failure_decays_confidence
# ---------------------------------------------------------------------------

def test_failure_decays_confidence(tmp_path):
    db = _make_db(tmp_path)
    dispatch_id = "dispatch-xyz-002"
    pattern_id = _insert_pattern(db, dispatch_id, confidence=0.7)

    result = update_confidence_from_outcome(db, dispatch_id, "T1", "failure")

    assert result["decayed"] == 1
    assert result["boosted"] == 0
    # Beta first failure → (0+1)/(0+1+2) = 1/3
    new_conf = _get_confidence(db, pattern_id)
    assert abs(new_conf - 1 / 3) < 1e-4, f"Expected 0.333, got {new_conf}"


def test_failure_decay_floors_at_0(tmp_path):
    """Beta posterior never reaches 0.0 — Laplace smoothing keeps the score
    strictly above 0 even after a long failure streak.
    """
    db = _make_db(tmp_path)
    dispatch_id = "dispatch-floor-test"
    pattern_id = _insert_pattern(db, dispatch_id, confidence=0.05)

    for _ in range(50):
        update_confidence_from_outcome(db, dispatch_id, "T1", "failure")

    new_conf = _get_confidence(db, pattern_id)
    assert new_conf > 0.0
    # 0 successes / 50 failures → 1/52 ≈ 0.019
    assert abs(new_conf - 1 / 52) < 1e-4


def test_confidence_event_written(tmp_path):
    db = _make_db(tmp_path)
    dispatch_id = "dispatch-event-audit"
    _insert_pattern(db, dispatch_id, confidence=0.5)

    update_confidence_from_outcome(db, dispatch_id, "T2", "success")

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT outcome, patterns_boosted, confidence_change FROM confidence_events "
        "WHERE dispatch_id = ?",
        (dispatch_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "success"
    assert row[1] == 1
    assert float(row[2]) > 0


# ---------------------------------------------------------------------------
# 3. test_prevention_rule_generated
# ---------------------------------------------------------------------------

def test_prevention_rule_generated(tmp_path):
    """Antipattern with occurrence_count >= 3 must produce a prevention_rules suggestion."""
    db = _make_db(tmp_path)
    _insert_antipattern(db, "Missing gate check", 5)
    _insert_antipattern(db, "Low occurrence", 2)  # should NOT qualify

    sys.path.insert(0, str(SCRIPTS_DIR))
    import importlib
    import types

    with patch.dict(os.environ, {
        "VNX_HOME": str(tmp_path),
        "VNX_STATE_DIR": str(tmp_path),
        "PROJECT_ROOT": str(tmp_path),
    }):
        # Import with patched ensure_env
        with patch("vnx_paths.ensure_env", return_value={
            "VNX_HOME": str(tmp_path),
            "VNX_STATE_DIR": str(tmp_path),
            "PROJECT_ROOT": str(tmp_path),
            "VNX_SKILLS_DIR": str(tmp_path),
        }):
            import generate_suggested_edits as gse
            importlib.reload(gse)

    conn = sqlite3.connect(str(db))
    suggestions = gse.generate_prevention_rule_suggestions(conn)
    conn.close()

    # Only the antipattern with count >= 3 qualifies
    titles = [s["proposed_change"] for s in suggestions]
    assert any("Missing gate check" in t for t in titles), f"Expected suggestion for 'Missing gate check', got {titles}"
    assert all("Low occurrence" not in t for t in titles)
    # confidence = min(5 * 0.1, 0.9) = 0.5
    matching = [s for s in suggestions if "Missing gate check" in s["proposed_change"]]
    assert abs(matching[0]["confidence"] - 0.5) < 1e-6


def test_prevention_rule_below_threshold_skipped(tmp_path):
    db = _make_db(tmp_path)
    _insert_antipattern(db, "Rare issue", 1)
    _insert_antipattern(db, "Occasional issue", 2)

    import generate_suggested_edits as gse
    conn = sqlite3.connect(str(db))
    suggestions = gse.generate_prevention_rule_suggestions(conn)
    conn.close()

    assert suggestions == []


# ---------------------------------------------------------------------------
# 4. test_learning_summary_endpoint
# ---------------------------------------------------------------------------

def test_learning_summary_endpoint_returns_valid_metrics(tmp_path):
    """The /api/intelligence/learning-summary handler returns the expected keys."""
    db = _make_db(tmp_path)

    # Insert confidence events
    now = datetime.now(_UTC).isoformat()
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO confidence_events (dispatch_id, terminal, outcome, patterns_boosted, "
        "patterns_decayed, confidence_change, occurred_at) VALUES (?,?,?,?,?,?,?)",
        ("d1", "T1", "success", 2, 0, 0.10, now),
    )
    conn.execute(
        "INSERT INTO confidence_events (dispatch_id, terminal, outcome, patterns_boosted, "
        "patterns_decayed, confidence_change, occurred_at) VALUES (?,?,?,?,?,?,?)",
        ("d2", "T1", "failure", 0, 1, -0.10, now),
    )
    conn.commit()
    conn.close()
    # Insert qualifying antipattern (after closing the previous connection)
    _insert_antipattern(db, "Bad practice", 4)

    sys.path.insert(0, str(DASHBOARD_DIR))
    import api_intelligence

    mock_sd = MagicMock()
    mock_sd.DB_PATH = db

    with patch.object(api_intelligence, "_sd", return_value=mock_sd):
        payload, status = api_intelligence._intelligence_get_learning_summary()

    assert status == 200
    assert "boosts" in payload
    assert "decays" in payload
    assert "net_confidence_drift" in payload
    assert "prevention_suggestions" in payload
    assert payload["boosts"] == 1
    assert payload["decays"] == 1
    assert abs(payload["net_confidence_drift"] - 0.0) < 1e-4
    assert payload["prevention_suggestions"] == 1


def test_learning_summary_empty_db(tmp_path):
    """Returns zeros when confidence_events is empty."""
    db = _make_db(tmp_path)

    sys.path.insert(0, str(DASHBOARD_DIR))
    import api_intelligence

    mock_sd = MagicMock()
    mock_sd.DB_PATH = db

    with patch.object(api_intelligence, "_sd", return_value=mock_sd):
        payload, status = api_intelligence._intelligence_get_learning_summary()

    assert status == 200
    assert payload["boosts"] == 0
    assert payload["decays"] == 0
    assert payload["net_confidence_drift"] == 0.0
    assert payload["prevention_suggestions"] == 0


def test_learning_summary_missing_db(tmp_path):
    """Returns zeros gracefully when DB does not exist."""
    sys.path.insert(0, str(DASHBOARD_DIR))
    import api_intelligence

    mock_sd = MagicMock()
    mock_sd.DB_PATH = tmp_path / "nonexistent.db"

    with patch.object(api_intelligence, "_sd", return_value=mock_sd):
        payload, status = api_intelligence._intelligence_get_learning_summary()

    assert status == 200
    assert payload["boosts"] == 0
