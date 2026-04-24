#!/usr/bin/env python3
"""tests/test_report_findings_schema.py — verify report_findings schema migration.

Certifies that:
  1. report_findings table exists after quality_db_init.py migration
  2. report_findings has the expected columns
  3. link_sessions_dispatches.py Phase 3 runs without OperationalError when table exists
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "lib"))

EXPECTED_COLUMNS = {
    "id", "report_path", "report_date", "terminal", "task_type",
    "patterns_found", "antipatterns_found", "prevention_rules_found",
    "tags_found", "summary", "age_category", "extracted_at", "dispatch_id",
}


def _minimal_db(path: Path) -> None:
    """Create a minimal quality_intelligence.db with just the tables needed for migration."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snippet_metadata (
            id INTEGER PRIMARY KEY,
            pattern_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS session_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project_path TEXT NOT NULL,
            terminal TEXT,
            session_date DATE NOT NULL,
            dispatch_id TEXT,
            session_model TEXT DEFAULT 'unknown',
            context_reset_count INTEGER DEFAULT 0,
            analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            analyzer_version TEXT DEFAULT '1.0.0'
        );
        CREATE TABLE IF NOT EXISTS improvement_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            category TEXT NOT NULL,
            current_behavior TEXT NOT NULL,
            suggested_improvement TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nightly_digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_date DATE NOT NULL UNIQUE,
            digest_markdown TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            session_id TEXT,
            outcome_status TEXT,
            outcome_report_path TEXT,
            completed_at TEXT,
            cqs REAL,
            normalized_status TEXT,
            cqs_components TEXT,
            target_open_items TEXT,
            open_items_created INTEGER DEFAULT 0,
            open_items_resolved INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            dispatch_id TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT,
            source_dispatch_id TEXT DEFAULT NULL,
            valid_from DATETIME DEFAULT NULL,
            valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT,
            valid_from DATETIME DEFAULT NULL,
            valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_hash TEXT,
            valid_from DATETIME DEFAULT NULL,
            valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS governance_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_start DATE NOT NULL,
            period_end DATE NOT NULL,
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            sample_size INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS spc_control_limits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_name TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS spc_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            scope_type TEXT NOT NULL,
            scope_value TEXT NOT NULL,
            observed_value REAL NOT NULL,
            detected_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS confidence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            confidence_change REAL NOT NULL,
            occurred_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


class TestReportFindingsSchemaCreation:

    def test_report_findings_table_created_by_migration(self, tmp_path):
        """quality_db_init migration must create report_findings when it's absent."""
        import quality_db_init as qdi

        db_path = tmp_path / "quality_intelligence.db"
        vnx_home = tmp_path / "vnx"
        vnx_home.mkdir()
        schemas_dir = vnx_home / "schemas"
        schemas_dir.mkdir()
        (schemas_dir / "quality_intelligence.sql").write_text("-- placeholder\n")

        _minimal_db(db_path)

        with (
            patch.object(qdi, "DB_PATH", db_path),
            patch.object(qdi, "SCHEMAS_DIR", schemas_dir),
            patch.object(qdi, "STATE_DIR", tmp_path),
            patch.object(qdi, "SCHEMA_FILE", schemas_dir / "quality_intelligence.sql"),
        ):
            qdi.initialize_database()

        conn = sqlite3.connect(str(db_path))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "report_findings" in tables, "report_findings must be created by migration"

    def test_report_findings_columns_match_schema(self, tmp_path):
        """report_findings must have all expected columns after migration."""
        import quality_db_init as qdi

        db_path = tmp_path / "quality_intelligence.db"
        vnx_home = tmp_path / "vnx"
        vnx_home.mkdir()
        schemas_dir = vnx_home / "schemas"
        schemas_dir.mkdir()
        (schemas_dir / "quality_intelligence.sql").write_text("-- placeholder\n")

        _minimal_db(db_path)

        with (
            patch.object(qdi, "DB_PATH", db_path),
            patch.object(qdi, "SCHEMAS_DIR", schemas_dir),
            patch.object(qdi, "STATE_DIR", tmp_path),
            patch.object(qdi, "SCHEMA_FILE", schemas_dir / "quality_intelligence.sql"),
        ):
            qdi.initialize_database()

        conn = sqlite3.connect(str(db_path))
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(report_findings)").fetchall()
        }
        conn.close()
        missing = EXPECTED_COLUMNS - cols
        assert not missing, f"report_findings missing columns: {missing}"

    def test_migration_is_idempotent(self, tmp_path):
        """Running initialize_database twice must not raise."""
        import quality_db_init as qdi

        db_path = tmp_path / "quality_intelligence.db"
        vnx_home = tmp_path / "vnx"
        vnx_home.mkdir()
        schemas_dir = vnx_home / "schemas"
        schemas_dir.mkdir()
        (schemas_dir / "quality_intelligence.sql").write_text("-- placeholder\n")

        _minimal_db(db_path)

        with (
            patch.object(qdi, "DB_PATH", db_path),
            patch.object(qdi, "SCHEMAS_DIR", schemas_dir),
            patch.object(qdi, "STATE_DIR", tmp_path),
            patch.object(qdi, "SCHEMA_FILE", schemas_dir / "quality_intelligence.sql"),
        ):
            qdi.initialize_database()
            qdi.initialize_database()  # second run must be safe


class TestPhase3RunsWithoutSchemaError:

    def test_link_sessions_dispatches_no_operational_error(self, tmp_path):
        """Phase 3 must not raise OperationalError when report_findings exists."""
        import link_sessions_dispatches as lsd

        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                session_id TEXT,
                outcome_status TEXT,
                outcome_report_path TEXT,
                completed_at TEXT
            );
            CREATE TABLE session_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                dispatch_id TEXT
            );
            CREATE TABLE report_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_path TEXT NOT NULL,
                dispatch_id TEXT
            );
        """)
        conn.commit()
        conn.close()

        fake_paths = {"VNX_STATE_DIR": str(tmp_path)}

        with (
            patch.object(lsd, "DB_PATH", db_path),
            patch.object(lsd, "RECEIPTS_FILE", tmp_path / "t0_receipts.ndjson"),
        ):
            db_conn = sqlite3.connect(str(db_path))
            # Must not raise OperationalError
            count = lsd.link_reports_to_dispatches(db_conn)
            db_conn.close()

        assert count == 0  # no reports to link in empty DB

    def test_link_sessions_dispatches_full_main(self, tmp_path):
        """Phase 3 main() must return 0 when report_findings table exists."""
        import link_sessions_dispatches as lsd

        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                session_id TEXT,
                outcome_status TEXT,
                outcome_report_path TEXT,
                completed_at TEXT
            );
            CREATE TABLE session_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL UNIQUE,
                dispatch_id TEXT
            );
            CREATE TABLE report_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_path TEXT NOT NULL,
                dispatch_id TEXT
            );
        """)
        conn.commit()
        conn.close()

        with (
            patch.object(lsd, "DB_PATH", db_path),
            patch.object(lsd, "RECEIPTS_FILE", tmp_path / "t0_receipts.ndjson"),
        ):
            result = lsd.main()

        assert result == 0
