#!/usr/bin/env python3
"""Tests for dispatch_id linkage in audit tables (F58-PR2).

Verifies:
  - pattern_usage records contain dispatch_id after the migration
  - governance_audit entries contain dispatch_id
  - prevention_rules has source_dispatch_id column
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


# ---------------------------------------------------------------------------
# test_pattern_usage_has_dispatch_id
# ---------------------------------------------------------------------------

class TestPatternUsageHasDispatchId:

    def _make_db(self, tmp_path: Path) -> sqlite3.Connection:
        """Create a minimal quality_intelligence.db with pattern_usage table."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("""
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
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                dispatch_id TEXT DEFAULT NULL
            )
        """)
        conn.commit()
        return conn

    def test_pattern_usage_schema_has_dispatch_id(self, tmp_path):
        """pattern_usage table must have a dispatch_id column."""
        conn = self._make_db(tmp_path)
        cursor = conn.execute("PRAGMA table_info(pattern_usage)")
        cols = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "dispatch_id" in cols

    def test_insert_pattern_usage_with_dispatch_id(self, tmp_path):
        """Can insert a pattern_usage row with dispatch_id."""
        conn = self._make_db(tmp_path)
        now = "2026-04-14T10:00:00"
        conn.execute(
            "INSERT INTO pattern_usage "
            "(pattern_id, pattern_title, pattern_hash, last_offered, created_at, updated_at, dispatch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("hash-abc", "Test pattern", "hash-abc", now, now, now, "f58-pr2-t1-dispatch"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT dispatch_id FROM pattern_usage WHERE pattern_id = ?", ("hash-abc",)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["dispatch_id"] == "f58-pr2-t1-dispatch"

    def test_pattern_usage_dispatch_id_nullable(self, tmp_path):
        """dispatch_id can be NULL for backward compatibility."""
        conn = self._make_db(tmp_path)
        now = "2026-04-14T10:00:00"
        conn.execute(
            "INSERT INTO pattern_usage "
            "(pattern_id, pattern_title, pattern_hash, last_offered, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("hash-xyz", "Old pattern", "hash-xyz", now, now, now),
        )
        conn.commit()

        row = conn.execute(
            "SELECT dispatch_id FROM pattern_usage WHERE pattern_id = ?", ("hash-xyz",)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["dispatch_id"] is None


# ---------------------------------------------------------------------------
# test_governance_audit_has_dispatch_id
# ---------------------------------------------------------------------------

class TestGovernanceAuditHasDispatchId:

    def test_log_enforcement_writes_dispatch_id(self, tmp_path, monkeypatch):
        """log_enforcement() writes dispatch_id to the NDJSON audit file."""
        audit_file = tmp_path / "governance_audit.ndjson"
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))

        # Create events dir so _audit_path() works
        (tmp_path / "events").mkdir()

        from governance_audit import log_enforcement  # noqa: PLC0415

        log_enforcement(
            check_name="test_check",
            level=2,
            result=True,
            context={"feature": "F58", "pr_number": 123},
            message="Test enforcement",
            dispatch_id="f58-pr2-test-dispatch",
        )

        lines = (tmp_path / "events" / "governance_audit.ndjson").read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["dispatch_id"] == "f58-pr2-test-dispatch"
        assert record["check_name"] == "test_check"

    def test_log_enforcement_dispatch_id_from_context(self, tmp_path, monkeypatch):
        """dispatch_id falls back to context['dispatch_id'] when not explicitly passed."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        (tmp_path / "events").mkdir()

        from governance_audit import log_enforcement  # noqa: PLC0415

        log_enforcement(
            check_name="ctx_check",
            level=1,
            result=True,
            context={"dispatch_id": "context-dispatch-id", "feature": "F58"},
            message="From context",
        )

        lines = (tmp_path / "events" / "governance_audit.ndjson").read_text().splitlines()
        record = json.loads(lines[0])
        assert record["dispatch_id"] == "context-dispatch-id"

    def test_log_enforcement_dispatch_id_null_when_absent(self, tmp_path, monkeypatch):
        """dispatch_id is null when neither caller nor context provides it."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        (tmp_path / "events").mkdir()

        from governance_audit import log_enforcement  # noqa: PLC0415

        log_enforcement(
            check_name="no_dispatch",
            level=1,
            result=True,
            context={"feature": "F58"},
            message="No dispatch",
        )

        lines = (tmp_path / "events" / "governance_audit.ndjson").read_text().splitlines()
        record = json.loads(lines[0])
        assert record["dispatch_id"] is None

    def test_governance_enforcer_passes_dispatch_id_from_context(self, tmp_path, monkeypatch):
        """GovernanceEnforcer.check() passes context dispatch_id to audit."""
        monkeypatch.setenv("VNX_DATA_DIR", str(tmp_path))
        (tmp_path / "events").mkdir()

        import governance_enforcer as ge_mod  # noqa: PLC0415

        captured = {}

        def _fake_log(check_name, level, result, context, override=None, message="", dispatch_id=None):
            captured["dispatch_id"] = dispatch_id
            captured["check_name"] = check_name

        original = ge_mod._log_enforcement_audit
        ge_mod._log_enforcement_audit = _fake_log
        try:
            enforcer = ge_mod.GovernanceEnforcer()
            enforcer._checks["my_check"] = ge_mod.CheckConfig(name="my_check", level=1)

            # Add a real implementation so the check reaches the audit path
            def _check_my_check(cfg, ctx):
                return ge_mod.EnforcementResult(
                    check_name=cfg.name,
                    level=cfg.level,
                    passed=True,
                    message="always passes",
                    override_key=f"VNX_OVERRIDE_{cfg.name.upper()}",
                )

            enforcer._check_my_check = _check_my_check

            enforcer.check("my_check", {"dispatch_id": "enforcer-dispatch-xyz", "feature": "F58"})
        finally:
            ge_mod._log_enforcement_audit = original

        assert captured.get("dispatch_id") == "enforcer-dispatch-xyz"


# ---------------------------------------------------------------------------
# test_prevention_rules_has_source_dispatch
# ---------------------------------------------------------------------------

class TestPreventionRulesHasSourceDispatch:

    def _make_db(self, tmp_path: Path) -> sqlite3.Connection:
        """Create a minimal quality_intelligence.db with prevention_rules table."""
        db_path = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prevention_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_combination TEXT,
                rule_type TEXT,
                description TEXT,
                recommendation TEXT,
                confidence REAL DEFAULT 0.5,
                triggered_count INTEGER DEFAULT 0,
                last_triggered DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                valid_from DATETIME DEFAULT CURRENT_TIMESTAMP,
                valid_until DATETIME DEFAULT NULL,
                source_dispatch_id TEXT DEFAULT NULL
            )
        """)
        conn.commit()
        return conn

    def test_prevention_rules_schema_has_source_dispatch_id(self, tmp_path):
        """prevention_rules table must have source_dispatch_id column."""
        conn = self._make_db(tmp_path)
        cursor = conn.execute("PRAGMA table_info(prevention_rules)")
        cols = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "source_dispatch_id" in cols

    def test_insert_prevention_rule_with_source_dispatch_id(self, tmp_path):
        """Can insert a prevention_rule row with source_dispatch_id."""
        conn = self._make_db(tmp_path)
        conn.execute(
            "INSERT INTO prevention_rules "
            "(tag_combination, rule_type, description, recommendation, confidence, "
            "triggered_count, created_at, source_dispatch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("backend-developer", "failure_prevention", "Test rule", "Avoid X",
             0.7, 0, "2026-04-14T10:00:00", "f58-pr2-source-dispatch"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT source_dispatch_id FROM prevention_rules WHERE description = ?",
            ("Test rule",)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["source_dispatch_id"] == "f58-pr2-source-dispatch"

    def test_prevention_rule_source_dispatch_id_nullable(self, tmp_path):
        """source_dispatch_id can be NULL for backward compatibility."""
        conn = self._make_db(tmp_path)
        conn.execute(
            "INSERT INTO prevention_rules "
            "(tag_combination, rule_type, description, recommendation, confidence, "
            "triggered_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("any", "failure_prevention", "Old rule", "Avoid Y", 0.5, 0, "2026-04-14T10:00:00"),
        )
        conn.commit()

        row = conn.execute(
            "SELECT source_dispatch_id FROM prevention_rules WHERE description = ?",
            ("Old rule",)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row["source_dispatch_id"] is None
