#!/usr/bin/env python3
"""Tests for vnx_db_maintenance — opt-in DB prune + VACUUM."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

from vnx_db_maintenance import (
    PROTECTED_TABLES,
    PRUNABLE_TABLES,
    DEFAULT_RETENTION_DAYS,
    apply,
    dry_run,
)


def _make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))

    # Minimal schema for prunable tables.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS session_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project_path TEXT NOT NULL DEFAULT '/test',
            terminal TEXT DEFAULT 'T1',
            session_date DATE NOT NULL,
            analyzed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS code_snippets USING fts5(
            title,
            description,
            code,
            file_path,
            last_updated,
            tokenize = 'porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS snippet_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snippet_rowid INTEGER NOT NULL,
            file_path TEXT NOT NULL DEFAULT '/test',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL UNIQUE,
            project_id TEXT NOT NULL DEFAULT 'test',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    return conn


def _insert_sessions(conn: sqlite3.Connection, dates: list) -> None:
    for i, date in enumerate(dates):
        conn.execute(
            "INSERT OR IGNORE INTO session_analytics (session_id, project_path, session_date) VALUES (?, ?, ?)",
            (f"sess-{i:04d}", "/test", date),
        )
    conn.commit()


def _insert_snippets(conn: sqlite3.Connection, dates: list) -> None:
    for i, date in enumerate(dates):
        conn.execute(
            "INSERT INTO code_snippets (title, description, code, file_path, last_updated) VALUES (?, ?, ?, ?, ?)",
            (f"title-{i}", f"desc-{i}", "# code", "/test.py", date),
        )
        rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO snippet_metadata (snippet_rowid, file_path, created_at) VALUES (?, ?, ?)",
            (rowid, "/test.py", date),
        )
    conn.commit()


def _insert_dispatch(conn: sqlite3.Connection, dispatch_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO dispatch_metadata (dispatch_id, project_id) VALUES (?, ?)",
        (dispatch_id, "proj-1"),
    )
    conn.commit()


class TestProtectedTables:
    def test_dispatch_metadata_in_protected_set(self):
        assert "dispatch_metadata" in PROTECTED_TABLES

    def test_dispatch_metadata_not_in_prunable(self):
        prunable_names = {spec["table"] for spec in PRUNABLE_TABLES}
        assert "dispatch_metadata" not in prunable_names


class TestDryRun:
    def test_dry_run_reports_without_deleting(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_sessions(conn, ["2020-01-01", "2020-06-01", "2030-01-01"])
        conn.close()

        result = dry_run(db_path=str(db), retention_days=30)

        assert result["dry_run"] is True
        assert result["total_would_prune"] >= 2

        # Verify nothing was actually deleted.
        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM session_analytics").fetchone()[0]
        conn.close()
        assert count == 3

    def test_dry_run_zero_when_all_recent(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_sessions(conn, ["2099-01-01", "2099-06-01"])
        conn.close()

        result = dry_run(db_path=str(db), retention_days=30)
        assert result["total_would_prune"] == 0

    def test_dry_run_missing_db(self, tmp_path):
        result = dry_run(db_path=str(tmp_path / "missing.db"))
        assert "error" in result

    def test_dry_run_includes_table_breakdown(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_sessions(conn, ["2020-01-01"])
        conn.close()

        result = dry_run(db_path=str(db), retention_days=30)
        assert isinstance(result["tables"], list)
        table_names = [t["table"] for t in result["tables"]]
        assert "session_analytics" in table_names

    def test_dry_run_reports_reclaimable_bytes(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_sessions(conn, ["2020-01-01"] * 20)
        conn.close()

        result = dry_run(db_path=str(db), retention_days=30)
        assert isinstance(result["estimated_reclaimable_bytes"], int)
        assert result["estimated_reclaimable_bytes"] >= 0

    def test_dry_run_does_not_write_audit_ledger(self, tmp_path):
        """dry_run() must NOT write any audit ledger (it does not mutate state)."""
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_sessions(conn, ["2020-01-01"] * 5)
        conn.close()

        dry_run(db_path=str(db), retention_days=30)

        audit_path = db.parent / "db_maintenance_audit.ndjson"
        assert not audit_path.exists(), "dry_run() must not create audit ledger file"


class TestApply:
    def test_apply_prunes_old_sessions(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_sessions(conn, ["2020-01-01", "2020-06-01", "2099-01-01"])
        conn.close()

        result = apply(db_path=str(db), retention_days=30)

        assert result["applied"] is True
        conn = sqlite3.connect(str(db))
        remaining = conn.execute("SELECT COUNT(*) FROM session_analytics").fetchone()[0]
        conn.close()
        assert remaining == 1

    def test_apply_prunes_old_snippets(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_snippets(conn, ["2020-01-01", "2099-01-01"])
        conn.close()

        result = apply(db_path=str(db), retention_days=30)

        conn = sqlite3.connect(str(db))
        remaining = conn.execute("SELECT COUNT(*) FROM snippet_metadata").fetchone()[0]
        conn.close()
        assert remaining <= 1

    def test_apply_never_prunes_dispatch_metadata(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_dispatch(conn, "DISP-KEEP-ME")
        conn.close()

        apply(db_path=str(db), retention_days=1)

        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM dispatch_metadata").fetchone()[0]
        conn.close()
        assert count == 1

    def test_apply_retention_window_respected(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        # Only very old entries should be pruned with 90-day window.
        _insert_sessions(conn, ["2020-01-01", "2020-01-02"])
        _insert_sessions(conn, ["2099-12-31"])
        conn.close()

        result = apply(db_path=str(db), retention_days=90)

        assert result["pruned"]["session_analytics"] == 2

    def test_apply_vacuums_db(self, tmp_path):
        """After apply, the DB must be openable and have consistent page state."""
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_sessions(conn, ["2020-01-01"] * 50)
        conn.close()

        result = apply(db_path=str(db), retention_days=30)

        # VACUUM ran if result has size_after_bytes (and DB is still openable).
        assert "size_after_bytes" in result
        conn = sqlite3.connect(str(db))
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        assert check == "ok"

    def test_apply_missing_db(self, tmp_path):
        result = apply(db_path=str(tmp_path / "missing.db"))
        assert "error" in result

    def test_apply_reports_reclaimed_bytes(self, tmp_path):
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        # Insert enough rows to see meaningful size change.
        _insert_sessions(conn, ["2020-01-01"] * 100)
        conn.close()

        result = apply(db_path=str(db), retention_days=30)

        assert isinstance(result["reclaimed_bytes"], int)
        assert result["reclaimed_bytes"] >= 0
        assert "reclaimed_mb" in result

    def test_apply_snippet_metadata_pruned_with_code_snippets(self, tmp_path):
        """snippet_metadata rows with old code_snippets are pruned together."""
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_snippets(conn, ["2020-01-01", "2020-02-01"])
        _insert_snippets(conn, ["2099-01-01"])
        conn.close()

        result = apply(db_path=str(db), retention_days=30)

        conn = sqlite3.connect(str(db))
        sm_count = conn.execute("SELECT COUNT(*) FROM snippet_metadata").fetchone()[0]
        cs_count = conn.execute("SELECT COUNT(*) FROM code_snippets").fetchone()[0]
        conn.close()
        assert cs_count == 1
        assert sm_count <= 1
        assert result["pruned"].get("snippet_metadata", 0) > 0, (
            "snippet_metadata count must be > 0 when metadata rows were pruned"
        )
        assert result["pruned"].get("code_snippets", 0) > 0

    def test_apply_writes_audit_ledger(self, tmp_path):
        """apply() must write exactly one NDJSON line to db_maintenance_audit.ndjson."""
        db = tmp_path / "quality_intelligence.db"
        conn = _make_db(db)
        _insert_sessions(conn, ["2020-01-01"] * 5)
        conn.close()

        result = apply(db_path=str(db), retention_days=30)

        audit_path = db.parent / "db_maintenance_audit.ndjson"
        assert audit_path.exists(), "Audit ledger file must exist after apply()"

        lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1, f"Expected exactly 1 audit line, got {len(lines)}"

        record = json.loads(lines[0])
        assert record["op"] == "db_maintenance"
        assert isinstance(record["pruned"], dict)
        assert "session_analytics" in record["pruned"]
        assert isinstance(record["bytes_reclaimed"], (int, float))
        assert record["vacuumed"] is True
        assert record["db_path"] == str(db)
        assert record["retention_days"] == 30
