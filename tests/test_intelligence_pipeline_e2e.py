#!/usr/bin/env python3
"""End-to-end tests for the intelligence extraction pipeline.

Verifies that learning_loop.py persists patterns to the DB tables
that intelligence_selector.py reads, closing the extraction gap.
"""

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import sys
import os

# Add scripts/ and scripts/lib/ to path
scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))
sys.path.insert(0, str(scripts_dir / "lib"))


def _create_test_db(db_path: Path) -> sqlite3.Connection:
    """Create a minimal quality_intelligence.db for testing."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            pattern_data TEXT NOT NULL,
            code_example TEXT,
            prerequisites TEXT,
            outcomes TEXT,
            success_rate REAL DEFAULT 0.0,
            usage_count INTEGER DEFAULT 0,
            avg_completion_time INTEGER,
            confidence_score REAL DEFAULT 0.0,
            source_dispatch_ids TEXT,
            source_receipts TEXT,
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
            problem_example TEXT,
            why_problematic TEXT NOT NULL,
            better_alternative TEXT,
            occurrence_count INTEGER DEFAULT 0,
            avg_resolution_time INTEGER,
            severity TEXT DEFAULT 'medium',
            source_dispatch_ids TEXT,
            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_seen DATETIME
        );

        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT NOT NULL,
            rule_type TEXT NOT NULL,
            description TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            confidence REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT
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

        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            dispatch_id TEXT PRIMARY KEY,
            terminal TEXT,
            track TEXT,
            role TEXT,
            skill_name TEXT,
            gate TEXT,
            outcome_status TEXT,
            dispatched_at TEXT,
            completed_at TEXT,
            pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    return conn


@pytest.fixture
def test_env(tmp_path):
    """Set up test environment with temp dirs and DB."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "quality_intelligence.db"
    conn = _create_test_db(db_path)

    # Create receipts dir (learning_loop expects it)
    receipts_dir = tmp_path / "terminals" / "file_bus" / "receipts"
    receipts_dir.mkdir(parents=True)

    env = {
        "VNX_HOME": str(tmp_path),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_DATA_DIR": str(tmp_path / "data"),
    }

    conn.close()
    return env, db_path, state_dir, receipts_dir


class _LoopContext:
    """Holds a LearningLoop with ensure_env permanently mocked."""
    def __init__(self, env):
        self._patcher = patch("learning_loop.ensure_env", return_value=env)
        self._patcher.start()
        from learning_loop import LearningLoop
        self.loop = LearningLoop()

    def close(self):
        self._patcher.stop()


def _make_loop(env):
    """Create a LearningLoop instance with mocked paths (mock stays active)."""
    ctx = _LoopContext(env)
    return ctx.loop


class TestLearningLoopPopulatesDB:
    """Test that persist_to_intelligence_db() writes to success_patterns."""

    def test_high_confidence_patterns_written(self, test_env):
        env, db_path, state_dir, _ = test_env

        # Seed pattern_usage with a high-confidence used pattern
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO pattern_usage "
            "(pattern_id, pattern_title, pattern_hash, used_count, confidence, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test-pattern-1", "Test Driven Development", "abc123",
             5, 0.85, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        loop = _make_loop(env)
        loop.persist_to_intelligence_db()

        # Verify success_patterns was populated
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM success_patterns WHERE pattern_data LIKE '%learning_loop%'"
        ).fetchall()
        conn.close()

        assert len(rows) >= 1
        row = dict(rows[0])
        assert row["title"] == "Test Driven Development"
        assert row["confidence_score"] == 0.85
        assert row["usage_count"] == 5

    def test_low_confidence_patterns_skipped(self, test_env):
        env, db_path, state_dir, _ = test_env

        # Seed with low-confidence pattern (below 0.6 threshold)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO pattern_usage "
            "(pattern_id, pattern_title, pattern_hash, used_count, confidence, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("low-conf", "Low Confidence Pattern", "def456",
             1, 0.3, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        loop = _make_loop(env)
        loop.persist_to_intelligence_db()

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT * FROM success_patterns WHERE pattern_data LIKE '%learning_loop%'"
        ).fetchall()
        conn.close()

        assert len(rows) == 0

    def test_unused_patterns_skipped(self, test_env):
        env, db_path, state_dir, _ = test_env

        # Seed with unused pattern (used_count = 0)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO pattern_usage "
            "(pattern_id, pattern_title, pattern_hash, used_count, confidence, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("unused", "Unused Pattern", "ghi789",
             0, 0.9, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        loop = _make_loop(env)
        loop.persist_to_intelligence_db()

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT * FROM success_patterns WHERE pattern_data LIKE '%learning_loop%'"
        ).fetchall()
        conn.close()

        assert len(rows) == 0


class TestApprovedRulesIngest:
    """Test that ingest_approved_rules() writes approved rules to prevention_rules."""

    def test_approved_rules_ingested(self, test_env):
        env, db_path, state_dir, _ = test_env

        # Write pending_rules.json with approved entries
        pending = {
            "pending_rules": [
                {
                    "id": "rule-abc123",
                    "pattern": "Error pattern: timeout on T2",
                    "terminal_constraint": "T2",
                    "prevention": "Increase timeout to 120s for T2 tasks",
                    "confidence": 0.7,
                    "status": "approved",
                },
                {
                    "id": "rule-def456",
                    "pattern": "Error pattern: import failure",
                    "terminal_constraint": "any",
                    "prevention": "Validate imports before dispatch",
                    "confidence": 0.5,
                    "status": "pending",  # Should NOT be ingested
                },
            ]
        }
        pending_path = state_dir / "pending_rules.json"
        pending_path.write_text(json.dumps(pending))

        loop = _make_loop(env)
        loop.ingest_approved_rules()

        # Verify only approved rule was inserted
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM prevention_rules").fetchall()
        conn.close()

        assert len(rows) == 1
        row = dict(rows[0])
        assert row["tag_combination"] == "T2"
        assert row["recommendation"] == "Increase timeout to 120s for T2 tasks"
        assert row["confidence"] == 0.7

        # Verify JSON was updated
        updated = json.loads(pending_path.read_text())
        approved_rule = [r for r in updated["pending_rules"] if r["id"] == "rule-abc123"][0]
        assert approved_rule["status"] == "ingested"
        assert "ingested_at" in approved_rule

    def test_no_pending_file_is_noop(self, test_env):
        env, db_path, state_dir, _ = test_env
        loop = _make_loop(env)
        loop.ingest_approved_rules()  # Should not raise

    def test_duplicate_rules_skipped(self, test_env):
        env, db_path, state_dir, _ = test_env

        # Pre-insert a rule
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO prevention_rules "
            "(tag_combination, rule_type, description, recommendation, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("T1", "failure_prevention", "Error pattern: flaky test",
             "Retry flaky tests", 0.6, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        # Approve the same rule
        pending = {
            "pending_rules": [{
                "id": "rule-dup",
                "pattern": "Error pattern: flaky test",
                "terminal_constraint": "T1",
                "prevention": "Retry flaky tests",
                "confidence": 0.6,
                "status": "approved",
            }]
        }
        (state_dir / "pending_rules.json").write_text(json.dumps(pending))

        loop = _make_loop(env)
        loop.ingest_approved_rules()

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM prevention_rules").fetchone()[0]
        conn.close()
        assert count == 1  # No duplicate


class TestSelectorReadsLearningLoopPatterns:
    """Test that intelligence_selector can read patterns written by learning_loop."""

    def test_selector_finds_learning_loop_patterns(self, test_env):
        env, db_path, state_dir, _ = test_env

        # Seed a high-confidence pattern via learning loop
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO pattern_usage "
            "(pattern_id, pattern_title, pattern_hash, used_count, confidence, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("selector-test", "Proven Backend Pattern", "sel123",
             3, 0.8, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

        # Run persist
        loop = _make_loop(env)
        loop.persist_to_intelligence_db()

        # Now test selector
        from intelligence_selector import IntelligenceSelector
        selector = IntelligenceSelector(quality_db_path=db_path)
        try:
            result = selector.select(
                dispatch_id="test-dispatch-001",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
            # Should have at least one proven_pattern item
            proven = [i for i in result.items if i.item_class == "proven_pattern"]
            assert len(proven) >= 1, (
                f"Expected proven_pattern items but got: "
                f"items={[i.item_class for i in result.items]}, "
                f"suppressed={[s.reason for s in result.suppressed]}"
            )
        finally:
            selector.close()
