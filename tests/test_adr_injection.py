#!/usr/bin/env python3
"""Tests for Wave-5 ADR context injection (PR-INT-2).

Covers: trigger detection, DB query, format, opt-out flag, Superseded guard, top-3 cap.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import intelligence_injection as ii


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _create_adrs_db(path: Path) -> None:
    """Create a minimal adrs + adrs_fts schema and seed 3 ADRs + 1 Superseded."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS adrs (
            adr_id              TEXT NOT NULL,
            project_id          TEXT NOT NULL DEFAULT 'vnx-dev',
            status              TEXT NOT NULL,
            title               TEXT NOT NULL,
            decision_summary    TEXT NOT NULL,
            binding_rules       TEXT NOT NULL DEFAULT '[]',
            applies_to_tables   TEXT NOT NULL DEFAULT '[]',
            applies_to_skills   TEXT NOT NULL DEFAULT '[]',
            triggers            TEXT NOT NULL DEFAULT '[]',
            file_path           TEXT NOT NULL,
            indexed_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            source_hash         TEXT NOT NULL,
            PRIMARY KEY (adr_id, project_id)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS adrs_fts USING fts5(
            adr_id UNINDEXED,
            title,
            decision_summary,
            binding_rules,
            content='adrs',
            content_rowid='rowid'
        );
    """)

    adrs = [
        (
            "ADR-005", "vnx-dev", "Accepted",
            "Append-Only NDJSON Audit Ledger",
            "All dispatch lifecycle events MUST write to NDJSON before any SQLite write.",
            json.dumps(["Write NDJSON first", "SQLite is downstream"]),
            json.dumps(["dispatch_metadata"]),
            json.dumps(["database-engineer", "intelligence-engineer"]),
            json.dumps(["scripts/lib/coordination_db.py", "scripts/lib/quality_db.py"]),
            "docs/governance/decisions/ADR-005.md",
            "2026-05-09T00:00:00.000Z",
            "abc123",
        ),
        (
            "ADR-007", "vnx-dev", "Accepted",
            "Multi-tenant project_id Stamping Pattern",
            "All multi-tenant tables carry project_id TEXT NOT NULL, composite UNIQUE.",
            json.dumps(["Every table needs project_id", "UNIQUE constraints must be composite"]),
            json.dumps(["adrs", "dispatch_metadata"]),
            json.dumps(["database-engineer", "architect"]),
            json.dumps(["schemas/migrations/"]),
            "docs/governance/decisions/ADR-007.md",
            "2026-05-09T00:00:00.000Z",
            "def456",
        ),
        (
            "ADR-010", "vnx-dev", "Accepted",
            "Frontend Component Isolation",
            "React components must be isolated with no shared mutable state.",
            json.dumps(["Use context API", "No global state"]),
            json.dumps([]),
            json.dumps(["frontend-developer"]),
            json.dumps(["src/components/"]),
            "docs/governance/decisions/ADR-010.md",
            "2026-05-09T00:00:00.000Z",
            "ghi789",
        ),
        (
            "ADR-999", "vnx-dev", "Superseded",
            "Old approach that was superseded",
            "This decision was superseded and must not be injected.",
            json.dumps([]),
            json.dumps([]),
            json.dumps(["database-engineer"]),
            json.dumps([]),
            "docs/governance/decisions/ADR-999.md",
            "2026-05-01T00:00:00.000Z",
            "zzz000",
        ),
    ]

    conn.executemany(
        "INSERT OR IGNORE INTO adrs "
        "(adr_id, project_id, status, title, decision_summary, binding_rules, "
        " applies_to_tables, applies_to_skills, triggers, file_path, indexed_at, source_hash) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        adrs,
    )

    # Populate FTS5 content table
    for row in adrs:
        conn.execute(
            "INSERT INTO adrs_fts (adr_id, title, decision_summary, binding_rules) "
            "VALUES (?,?,?,?)",
            (row[0], row[3], row[4], row[5]),
        )

    conn.commit()
    conn.close()


@pytest.fixture
def adr_db_dir(tmp_path):
    """Return a tmp state dir containing a seeded quality_intelligence.db."""
    _create_adrs_db(tmp_path / "quality_intelligence.db")
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: role=database-engineer + schemas/ path → injects ADR-005 + ADR-007
# ---------------------------------------------------------------------------

class TestAdrInjectionTriggerMatch:

    def test_database_engineer_with_schemas_path_injects_adr005_and_adr007(self, adr_db_dir):
        result = ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-001",
            role="database-engineer",
            dispatch_paths=["schemas/migrations/0025_new_table.sql"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        assert "ADR-005" in result
        assert "ADR-007" in result
        # ADR-010 (frontend-developer only) must NOT appear
        assert "ADR-010" not in result
        # The block must carry the binding header
        assert "ADR Context (auto-injected per Wave-5)" in result
        assert "BINDING" in result

    def test_result_contains_decision_summary_and_binding_rules(self, adr_db_dir):
        result = ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-002",
            role="database-engineer",
            dispatch_paths=["schemas/migrations/0026_fix.sql"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        # decision_summary present (truncated at 200 chars)
        assert "All dispatch lifecycle events MUST write to NDJSON" in result
        # binding rules as bullet list
        assert "- Write NDJSON first" in result

    def test_ndjson_audit_event_written_on_injection(self, adr_db_dir):
        ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-003",
            role="database-engineer",
            dispatch_paths=["schemas/migrations/0027_col.sql"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        register = adr_db_dir / "dispatch_register.ndjson"
        assert register.exists()
        lines = register.read_text().splitlines()
        events = [json.loads(line) for line in lines if line.strip()]
        matched = [e for e in events if e.get("event") == "adr_context_injected"
                   and e.get("dispatch_id") == "test-dispatch-003"]
        assert matched, "No adr_context_injected event found in NDJSON ledger"
        assert set(matched[0]["adr_ids"]) >= {"ADR-005", "ADR-007"}


# ---------------------------------------------------------------------------
# Test 2: role=frontend-developer + tests/ path → no injection
# ---------------------------------------------------------------------------

class TestAdrInjectionNoTrigger:

    def test_frontend_developer_with_tests_path_returns_empty(self, adr_db_dir):
        result = ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-010",
            role="frontend-developer",
            dispatch_paths=["tests/test_crawler.py"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        assert result == ""

    def test_no_dispatch_paths_no_trigger_role_returns_empty(self, adr_db_dir):
        result = ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-011",
            role="backend-developer",
            dispatch_paths=None,
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        assert result == ""

    def test_no_ndjson_written_when_no_injection(self, adr_db_dir):
        ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-012",
            role="frontend-developer",
            dispatch_paths=["src/components/Button.tsx"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        register = adr_db_dir / "dispatch_register.ndjson"
        if register.exists():
            lines = register.read_text().splitlines()
            events = [json.loads(l) for l in lines if l.strip()]
            matched = [e for e in events if e.get("dispatch_id") == "test-dispatch-012"]
            assert not matched


# ---------------------------------------------------------------------------
# Test 3: --no-adr-inject flag → no injection regardless of triggers
# ---------------------------------------------------------------------------

class TestAdrInjectionOptOut:

    def test_no_inject_kwarg_disables_injection(self, adr_db_dir):
        result = ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-020",
            role="database-engineer",
            dispatch_paths=["schemas/migrations/0028_tbl.sql"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
            no_inject=True,
        )
        assert result == ""

    def test_env_var_disables_injection(self, adr_db_dir):
        with patch.dict(os.environ, {"VNX_NO_ADR_INJECT": "1"}):
            result = ii.fetch_adr_context_section(
                dispatch_id="test-dispatch-021",
                role="database-engineer",
                dispatch_paths=["schemas/migrations/0029_idx.sql"],
                state_dir=adr_db_dir,
                project_id="vnx-dev",
            )
        assert result == ""

    def test_env_var_zero_does_not_disable_injection(self, adr_db_dir):
        with patch.dict(os.environ, {"VNX_NO_ADR_INJECT": "0"}):
            result = ii.fetch_adr_context_section(
                dispatch_id="test-dispatch-022",
                role="database-engineer",
                dispatch_paths=["schemas/migrations/0030_fix.sql"],
                state_dir=adr_db_dir,
                project_id="vnx-dev",
            )
        assert result != ""


# ---------------------------------------------------------------------------
# Test 4: Superseded ADRs never injected
# ---------------------------------------------------------------------------

class TestAdrInjectionSupersededGuard:

    def test_superseded_adr_not_in_result(self, adr_db_dir):
        result = ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-030",
            role="database-engineer",
            dispatch_paths=["schemas/migrations/0031_tbl.sql"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        assert "ADR-999" not in result
        assert "superseded" not in result.lower()

    def test_superseded_title_not_in_result(self, adr_db_dir):
        result = ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-031",
            role="database-engineer",
            dispatch_paths=["schemas/migrations/0032_col.sql"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        assert "Old approach that was superseded" not in result


# ---------------------------------------------------------------------------
# Test 5: top-3 limit honored (mock 5 matches, assert only 3 in output)
# ---------------------------------------------------------------------------

class TestAdrInjectionTopThreeLimit:

    def _seed_five_adrs(self, db_path: Path) -> None:
        """Add ADR-001 through ADR-004 (on top of existing 005/007 in the fixture)."""
        conn = sqlite3.connect(str(db_path))
        extras = [
            ("ADR-001", "vnx-dev", "Accepted", "Extra ADR One",
             "Summary one.", "[]", "[]", '["database-engineer"]', "[]",
             "docs/ADR-001.md", "2026-01-01T00:00:00.000Z", "h1"),
            ("ADR-002", "vnx-dev", "Accepted", "Extra ADR Two",
             "Summary two.", "[]", "[]", '["database-engineer"]', "[]",
             "docs/ADR-002.md", "2026-01-02T00:00:00.000Z", "h2"),
            ("ADR-003", "vnx-dev", "Accepted", "Extra ADR Three",
             "Summary three.", "[]", "[]", '["database-engineer"]', "[]",
             "docs/ADR-003.md", "2026-01-03T00:00:00.000Z", "h3"),
            ("ADR-004", "vnx-dev", "Accepted", "Extra ADR Four",
             "Summary four.", "[]", "[]", '["database-engineer"]', "[]",
             "docs/ADR-004.md", "2026-01-04T00:00:00.000Z", "h4"),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO adrs "
            "(adr_id, project_id, status, title, decision_summary, binding_rules, "
            " applies_to_tables, applies_to_skills, triggers, file_path, indexed_at, source_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            extras,
        )
        for row in extras:
            conn.execute(
                "INSERT INTO adrs_fts (adr_id, title, decision_summary, binding_rules) "
                "VALUES (?,?,?,?)",
                (row[0], row[3], row[4], row[5]),
            )
        conn.commit()
        conn.close()

    def test_at_most_three_adrs_injected(self, adr_db_dir):
        self._seed_five_adrs(adr_db_dir / "quality_intelligence.db")
        result = ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-040",
            role="database-engineer",
            dispatch_paths=["schemas/migrations/0033_x.sql"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        # Count how many ### ADR-... headers appear
        import re
        adr_headers = re.findall(r"### ADR-\w+", result)
        assert len(adr_headers) <= 3, (
            f"Expected at most 3 ADR headers, got {len(adr_headers)}: {adr_headers}"
        )

    def test_ndjson_event_lists_at_most_three_ids(self, adr_db_dir):
        self._seed_five_adrs(adr_db_dir / "quality_intelligence.db")
        ii.fetch_adr_context_section(
            dispatch_id="test-dispatch-041",
            role="database-engineer",
            dispatch_paths=["schemas/migrations/0034_y.sql"],
            state_dir=adr_db_dir,
            project_id="vnx-dev",
        )
        register = adr_db_dir / "dispatch_register.ndjson"
        lines = register.read_text().splitlines()
        events = [json.loads(l) for l in lines if l.strip()]
        matched = [e for e in events if e.get("dispatch_id") == "test-dispatch-041"]
        assert matched
        assert len(matched[0]["adr_ids"]) <= 3


# ---------------------------------------------------------------------------
# Trigger helper unit tests
# ---------------------------------------------------------------------------

class TestAdrTriggerLogic:

    def test_database_engineer_role_triggers(self):
        assert ii._adr_injection_triggered("database-engineer", None)

    def test_architect_role_triggers(self):
        assert ii._adr_injection_triggered("architect", [])

    def test_intelligence_engineer_role_triggers(self):
        assert ii._adr_injection_triggered("intelligence-engineer", [])

    def test_security_engineer_role_triggers(self):
        assert ii._adr_injection_triggered("security-engineer", [])

    def test_schemas_migrations_path_triggers(self):
        assert ii._adr_injection_triggered(None, ["schemas/migrations/0001_init.sql"])

    def test_schemas_path_triggers(self):
        assert ii._adr_injection_triggered(None, ["schemas/quality_intelligence.sql"])

    def test_coordination_db_path_triggers(self):
        assert ii._adr_injection_triggered(None, ["scripts/lib/coordination_db.py"])

    def test_quality_db_path_triggers(self):
        assert ii._adr_injection_triggered(None, ["scripts/lib/quality_db.py"])

    def test_unrelated_role_and_path_does_not_trigger(self):
        assert not ii._adr_injection_triggered(
            "frontend-developer", ["src/components/Button.tsx"]
        )

    def test_empty_paths_no_trigger_role_does_not_trigger(self):
        assert not ii._adr_injection_triggered("backend-developer", [])


# ---------------------------------------------------------------------------
# NDJSON event raise-on-failure
# ---------------------------------------------------------------------------

class TestAdrInjectionEventWriteRaisesOnFailure:

    def test_osierror_raised_when_state_dir_is_file(self, tmp_path):
        """_emit_adr_injection_event raises OSError when state_dir is a file."""
        # Create state_dir as a FILE so mkdir/open fails
        fake_dir = tmp_path / "not_a_dir"
        fake_dir.write_text("I am a file")
        with pytest.raises(OSError):
            ii._emit_adr_injection_event(
                dispatch_id="test-err-001",
                adr_ids=["ADR-005"],
                state_dir=fake_dir,
            )
