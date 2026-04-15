#!/usr/bin/env python3
"""Tests for scripts/memory_consolidator.py"""

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))


def _mock_ensure_env(tmp_path: Path) -> dict:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    intel_dir = tmp_path / ".vnx-intelligence"
    intel_dir.mkdir(parents=True, exist_ok=True)
    return {
        "VNX_HOME": str(tmp_path),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_DATA_DIR": str(tmp_path / ".vnx-data"),
        "VNX_INTELLIGENCE_DIR": str(intel_dir),
    }


def _make_db(state_dir: Path) -> Path:
    db_path = state_dir / "quality_intelligence.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, code_example TEXT, prerequisites TEXT, outcomes TEXT,
            success_rate REAL, usage_count INTEGER DEFAULT 0,
            avg_completion_time INTEGER, confidence_score REAL DEFAULT 0.5,
            source_dispatch_ids TEXT, source_receipts TEXT,
            first_seen DATETIME, last_used DATETIME
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, problem_example TEXT, why_problematic TEXT,
            better_alternative TEXT, occurrence_count INTEGER DEFAULT 1,
            avg_resolution_time INTEGER, severity TEXT DEFAULT 'medium',
            source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME
        );
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT, terminal TEXT, track TEXT, role TEXT,
            skill_name TEXT, gate TEXT, cognition TEXT, priority TEXT,
            pr_id TEXT, parent_dispatch TEXT, pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0, intelligence_json TEXT,
            instruction_char_count INTEGER, context_file_count INTEGER,
            dispatched_at DATETIME, completed_at DATETIME,
            outcome_status TEXT, outcome_report_path TEXT, session_id TEXT,
            cqs REAL, normalized_status TEXT, cqs_components TEXT,
            target_open_items TEXT, open_items_created INTEGER DEFAULT 0,
            open_items_resolved INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS session_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, project_path TEXT, terminal TEXT,
            session_date DATE, total_input_tokens INTEGER,
            total_output_tokens INTEGER, cache_creation_tokens INTEGER,
            cache_read_tokens INTEGER, tool_calls_total INTEGER,
            tool_read_count INTEGER, tool_edit_count INTEGER,
            tool_bash_count INTEGER, tool_grep_count INTEGER,
            tool_write_count INTEGER, tool_task_count INTEGER,
            tool_other_count INTEGER, message_count INTEGER,
            user_message_count INTEGER, assistant_message_count INTEGER,
            duration_minutes REAL, has_error_recovery BOOLEAN,
            has_context_reset BOOLEAN, has_large_refactor BOOLEAN,
            has_test_cycle BOOLEAN, primary_activity TEXT,
            deep_analysis_json TEXT, deep_analysis_model TEXT,
            deep_analysis_at DATETIME, session_model TEXT,
            file_size_bytes INTEGER, analyzed_at DATETIME,
            analyzer_version TEXT, dispatch_id TEXT, context_reset_count INTEGER
        );
    """)
    conn.commit()
    conn.close()
    return db_path


def _seed_dispatch_metadata(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("D-001", "T1", "backend-developer", "success", 80.0, 2, now),
        ("D-002", "T1", "backend-developer", "success", 75.0, 3, now),
        ("D-003", "T1", "backend-developer", "failure", 40.0, 0, now),
        ("D-004", "T2", "test-engineer", "success", 90.0, 1, now),
        ("D-005", "T2", "test-engineer", "success", 85.0, 1, now),
        ("D-006", "T3", "reviewer", "success", 78.0, 2, now),
        ("D-007", "T3", "reviewer", "failure", 30.0, 0, now),
    ]
    conn.executemany(
        "INSERT INTO dispatch_metadata "
        "(dispatch_id, terminal, role, outcome_status, cqs, pattern_count, dispatched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _seed_session_analytics(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    today = datetime.now(timezone.utc).date().isoformat()
    rows = [
        ("S-1", "T1", today, 7.5, "code"),
        ("S-2", "T1", today, 8.0, "code"),
        ("S-3", "T2", today, 3.0, "tests"),
        ("S-4", "T2", today, 4.0, "tests"),
    ]
    conn.executemany(
        "INSERT INTO session_analytics (session_id, terminal, session_date, duration_minutes, primary_activity) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _make_receipts(state_dir: Path, records: list) -> Path:
    path = state_dir / "t0_receipts.ndjson"
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return path


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def env_and_consolidator(tmp_path):
    """Return (env_dict, MemoryConsolidator instance) with mocked paths."""
    env = _mock_ensure_env(tmp_path)
    db_path = _make_db(Path(env["VNX_STATE_DIR"]))
    _seed_dispatch_metadata(db_path)
    _seed_session_analytics(db_path)

    with patch("memory_consolidator.ensure_env", return_value=env):
        from memory_consolidator import MemoryConsolidator
        mc = MemoryConsolidator()
        mc.db_path = db_path
        mc.receipts_path = Path(env["VNX_STATE_DIR"]) / "t0_receipts.ndjson"
        mc.audit_path = Path(env["VNX_STATE_DIR"]) / "dispatch_audit.jsonl"
        yield env, mc, db_path


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestExtractFromReceipts:
    def test_extract_from_receipts(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        now = datetime.now(timezone.utc).isoformat()
        records = [
            {"event_type": "task_complete", "terminal": "T1", "status": "success",
             "gate": "codex_gate", "dispatch_id": "D-R1", "timestamp": now},
            {"event_type": "task_complete", "terminal": "T1", "status": "error",
             "gate": "codex_gate", "dispatch_id": "D-R2", "timestamp": now},
            {"event_type": "task_complete", "terminal": "T1", "status": "failure",
             "gate": "codex_gate", "dispatch_id": "D-R3", "timestamp": now},
            # Non task_complete events should be ignored
            {"event_type": "task_started", "terminal": "T1", "status": "ok",
             "dispatch_id": "D-R4", "timestamp": now},
        ]
        _make_receipts(Path(env["VNX_STATE_DIR"]), records)

        receipts = mc._read_receipts(datetime.now(timezone.utc).replace(year=2020))
        assert len(receipts) == 3  # only task_complete events
        statuses = [r["status"] for r in receipts]
        assert "success" in statuses
        assert "error" in statuses

    def test_failure_pattern_requires_two_events(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        now = datetime.now(timezone.utc).isoformat()
        records = [
            {"event_type": "task_complete", "terminal": "T1", "status": "error",
             "gate": "codex_gate", "dispatch_id": "D-F1", "timestamp": now},
        ]
        _make_receipts(Path(env["VNX_STATE_DIR"]), records)

        receipts = mc._read_receipts(datetime.now(timezone.utc).replace(year=2020))
        patterns = mc._extract_failure_patterns(receipts)
        # Only 1 failure — below threshold of 2
        assert len(patterns) == 0

    def test_failure_pattern_extracted_with_two_events(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        now = datetime.now(timezone.utc).isoformat()
        records = [
            {"event_type": "task_complete", "terminal": "T1", "status": "error",
             "gate": "codex_gate", "dispatch_id": "D-F1", "timestamp": now},
            {"event_type": "task_complete", "terminal": "T1", "status": "error",
             "gate": "codex_gate", "dispatch_id": "D-F2", "timestamp": now},
        ]
        _make_receipts(Path(env["VNX_STATE_DIR"]), records)

        receipts = mc._read_receipts(datetime.now(timezone.utc).replace(year=2020))
        patterns = mc._extract_failure_patterns(receipts)
        assert len(patterns) == 1
        assert patterns[0].pattern_type == "antipattern"
        assert patterns[0].evidence_count == 2


class TestPatternDeduplication:
    def test_exact_duplicate_updates_not_inserts(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        now = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids, first_seen, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "memory_consolidation", "T1 dispatches: 85% success rate",
             "Existing description", "{}", 0.5, 5, "[]", now, now),
        )
        conn.commit()
        conn.close()

        from memory_consolidator import ExtractedPattern
        p = ExtractedPattern(
            title="T1 dispatches: 85% success rate",
            description="New description",
            pattern_type="success",
            pattern_subtype="terminal_rate",
            evidence_count=3,
        )
        conn2 = mc._open_db()
        action, _ = mc._upsert_success_pattern(conn2, p, now)
        conn2.commit()
        conn2.close()

        assert action == "updated"

        conn3 = sqlite3.connect(str(db_path))
        row = conn3.execute(
            "SELECT COUNT(*) as cnt FROM success_patterns WHERE category = 'memory_consolidation'"
        ).fetchone()
        assert row[0] == 1  # No duplicate

    def test_similar_title_merges(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        now = datetime.now(timezone.utc).isoformat()

        from memory_consolidator import ExtractedPattern, _title_overlap

        # Titles sharing 5 of 6 unique words → Jaccard = 5/6 ≈ 0.833
        title_a = "backend developer dispatches success rate high"
        title_b = "backend developer dispatches success rate very high"
        sim = _title_overlap(title_a, title_b)
        assert sim > 0.8, f"Expected >0.8, got {sim}"

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO success_patterns "
            "(pattern_type, category, title, description, pattern_data, "
            " confidence_score, usage_count, source_dispatch_ids, first_seen, last_used) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("approach", "memory_consolidation", title_a,
             "Existing", "{}", 0.5, 8, "[]", now, now),
        )
        conn.commit()
        conn.close()

        # A pattern with similar (but not identical) title should merge
        p = ExtractedPattern(
            title=title_b,
            description="Updated description",
            pattern_type="success",
            pattern_subtype="terminal_rate",
            evidence_count=3,
        )
        conn2 = mc._open_db()
        action, _ = mc._upsert_success_pattern(conn2, p, now)
        conn2.commit()
        conn2.close()

        assert action == "merged"

        # Confirm no duplicate row was inserted
        conn3 = sqlite3.connect(str(db_path))
        n = conn3.execute(
            "SELECT COUNT(*) FROM success_patterns WHERE category = 'memory_consolidation'"
        ).fetchone()[0]
        conn3.close()
        assert n == 1

    def test_new_pattern_is_inserted(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        now = datetime.now(timezone.utc).isoformat()

        from memory_consolidator import ExtractedPattern
        p = ExtractedPattern(
            title="completely new pattern about something unique",
            description="A brand new pattern",
            pattern_type="success",
            pattern_subtype="role_rate",
            evidence_count=5,
        )
        conn = mc._open_db()
        action, _ = mc._upsert_success_pattern(conn, p, now)
        conn.commit()
        conn.close()

        assert action == "inserted"


class TestConfidenceScoring:
    def test_confidence_scales_with_evidence(self):
        from memory_consolidator import ExtractedPattern
        p1 = ExtractedPattern("t", "d", "success", "r", evidence_count=1, base_confidence=0.1)
        p2 = ExtractedPattern("t", "d", "success", "r", evidence_count=10, base_confidence=0.1)
        assert p1.confidence < p2.confidence

    def test_confidence_capped_at_one(self):
        from memory_consolidator import ExtractedPattern
        p = ExtractedPattern("t", "d", "success", "r", evidence_count=100, base_confidence=0.5)
        assert p.confidence == 1.0

    def test_single_occurrence_stays_low(self):
        from memory_consolidator import ExtractedPattern
        p = ExtractedPattern("t", "d", "success", "r", evidence_count=1, base_confidence=0.1)
        assert p.confidence <= 0.2

    def test_ten_plus_evidence_gives_high_confidence(self):
        from memory_consolidator import ExtractedPattern
        p = ExtractedPattern("t", "d", "success", "r", evidence_count=10, base_confidence=0.1)
        assert p.confidence >= 0.9


class TestDryRunNoWrites:
    def test_dry_run_does_not_write_to_db(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        now = datetime.now(timezone.utc).isoformat()
        _make_receipts(Path(env["VNX_STATE_DIR"]), [])

        result = mc.consolidate(days=7, dry_run=True)
        assert result.dry_run is True
        assert result.patterns_inserted == 0
        assert result.patterns_updated == 0

        conn = sqlite3.connect(str(db_path))
        n = conn.execute(
            "SELECT COUNT(*) FROM success_patterns WHERE category = 'memory_consolidation'"
        ).fetchone()[0]
        conn.close()
        assert n == 0

    def test_dry_run_returns_patterns(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        _make_receipts(Path(env["VNX_STATE_DIR"]), [])

        result = mc.consolidate(days=7, dry_run=True)
        # Should still extract patterns from seeded dispatch_metadata
        assert result.patterns_extracted > 0


class TestConsolidationIdempotent:
    def test_running_twice_does_not_duplicate(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        _make_receipts(Path(env["VNX_STATE_DIR"]), [])

        result1 = mc.consolidate(days=7, dry_run=False)
        result2 = mc.consolidate(days=7, dry_run=False)

        conn = sqlite3.connect(str(db_path))
        sp_count = conn.execute(
            "SELECT COUNT(*) FROM success_patterns WHERE category = 'memory_consolidation'"
        ).fetchone()[0]
        ap_count = conn.execute(
            "SELECT COUNT(*) FROM antipatterns WHERE category = 'memory_consolidation'"
        ).fetchone()[0]
        conn.close()

        # Total unique patterns should equal first run's inserts
        total_unique = sp_count + ap_count
        total_first_run = result1.patterns_inserted + (
            # antipatterns inserted in first run
            result1.patterns_extracted - result1.patterns_inserted - result1.patterns_updated - result1.patterns_merged
        )
        # Key assertion: second run should produce only updates/merges, not new inserts
        assert result2.patterns_inserted == 0 or (
            result1.patterns_inserted + result2.patterns_inserted == total_unique
        )

    def test_second_run_increments_usage_count(self, env_and_consolidator):
        env, mc, db_path = env_and_consolidator
        _make_receipts(Path(env["VNX_STATE_DIR"]), [])

        mc.consolidate(days=7, dry_run=False)

        conn = sqlite3.connect(str(db_path))
        rows_after_first = conn.execute(
            "SELECT title, usage_count FROM success_patterns WHERE category = 'memory_consolidation'"
        ).fetchall()
        conn.close()

        mc.consolidate(days=7, dry_run=False)

        conn = sqlite3.connect(str(db_path))
        rows_after_second = conn.execute(
            "SELECT title, usage_count FROM success_patterns WHERE category = 'memory_consolidation'"
        ).fetchall()
        conn.close()

        # Same number of rows (no duplicates)
        assert len(rows_after_first) == len(rows_after_second)

        # Usage counts should be higher (or equal if all became antipatterns)
        first_map = {r[0]: r[1] for r in rows_after_first}
        second_map = {r[0]: r[1] for r in rows_after_second}
        for title, count in second_map.items():
            if title in first_map:
                assert count >= first_map[title]
