#!/usr/bin/env python3
"""
Tests for fine-grained task_class sub-classification and scope_tags activation.

Covers:
  - infer_task_subclass path/instruction routing
  - resolve_task_class with dispatch_paths
  - _scope_matches strict mode (VNX_INTEL_STRICT_SCOPE)
  - _expand_scope_tags subclass keyword expansion
  - intelligence_backfill.py keyword-to-tag mapping
  - IntelligenceSelector scope filtering (sql scope excludes ui-tagged items)
  - Backwards-compat: VNX_INTEL_STRICT_SCOPE=0 preserves old behaviour
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_sources._common import (
    VALID_TASK_CLASSES,
    _expand_scope_tags,
    _scope_matches,
    infer_task_subclass,
    resolve_task_class,
)
from intelligence_backfill import _infer_tag, backfill_table, run_backfill


# ---------------------------------------------------------------------------
# infer_task_subclass
# ---------------------------------------------------------------------------

class TestInferTaskSubclass(unittest.TestCase):

    def test_sql_extension_path(self):
        result = infer_task_subclass(None, ["schemas/migrations/0025_add_col.sql"], None)
        self.assertEqual(result, "coding_sql")

    def test_migrate_in_path(self):
        result = infer_task_subclass(None, ["scripts/lib/migration_helper.py"], None)
        self.assertEqual(result, "coding_sql")

    def test_schemas_directory(self):
        result = infer_task_subclass(None, ["schemas/quality_intelligence.sql"], None)
        self.assertEqual(result, "coding_sql")

    def test_sql_keyword_in_instruction(self):
        result = infer_task_subclass(None, [], "Add a new table for user sessions")
        self.assertEqual(result, "coding_sql")

    def test_schema_keyword_in_instruction(self):
        result = infer_task_subclass(None, [], "Update the schema to add tenant_id")
        self.assertEqual(result, "coding_sql")

    def test_ui_html_path(self):
        result = infer_task_subclass(None, ["dashboard/index.html"], None)
        self.assertEqual(result, "coding_ui")

    def test_ui_tsx_path(self):
        result = infer_task_subclass(None, ["dashboard/components/Table.tsx"], None)
        self.assertEqual(result, "coding_ui")

    def test_ui_dashboard_directory(self):
        result = infer_task_subclass(None, ["dashboard/static/app.js"], None)
        self.assertEqual(result, "coding_ui")

    def test_runtime_path(self):
        result = infer_task_subclass(None, ["scripts/lib/runtime_coordination.py"], None)
        self.assertEqual(result, "coding_runtime")

    def test_dispatch_path(self):
        result = infer_task_subclass(None, ["scripts/lib/dispatch_tracker.py"], None)
        self.assertEqual(result, "coding_runtime")

    def test_receipt_path(self):
        result = infer_task_subclass(None, ["scripts/lib/receipt_processor.py"], None)
        self.assertEqual(result, "coding_runtime")

    def test_intelligence_path(self):
        result = infer_task_subclass(None, ["scripts/lib/intelligence_selector.py"], None)
        self.assertEqual(result, "coding_intelligence")

    def test_tests_directory(self):
        result = infer_task_subclass(None, ["tests/test_foo.py"], None)
        self.assertEqual(result, "coding_test")

    def test_tests_subdir(self):
        result = infer_task_subclass(None, ["scripts/tests/integration.py"], None)
        self.assertEqual(result, "coding_test")

    def test_default_fallback(self):
        result = infer_task_subclass(None, ["scripts/lib/some_helper.py"], None)
        self.assertEqual(result, "coding_interactive")

    def test_empty_inputs_fallback(self):
        result = infer_task_subclass(None, [], None)
        self.assertEqual(result, "coding_interactive")

    def test_sql_takes_priority_over_tests(self):
        # SQL path alongside test path: SQL wins (higher priority)
        result = infer_task_subclass(None, ["tests/test_migration.sql"], None)
        self.assertEqual(result, "coding_sql")

    def test_all_subclasses_in_valid_task_classes(self):
        for subclass in ("coding_sql", "coding_runtime", "coding_intelligence", "coding_test", "coding_ui"):
            self.assertIn(subclass, VALID_TASK_CLASSES)


# ---------------------------------------------------------------------------
# resolve_task_class with dispatch_paths
# ---------------------------------------------------------------------------

class TestResolveTaskClassWithPaths(unittest.TestCase):

    def test_explicit_class_wins(self):
        result = resolve_task_class("research_structured", None, ["schema.sql"], None)
        self.assertEqual(result, "research_structured")

    def test_infers_sql_from_paths(self):
        result = resolve_task_class(None, "backend-developer", ["schemas/0025.sql"], None)
        self.assertEqual(result, "coding_sql")

    def test_infers_ui_from_paths(self):
        result = resolve_task_class(None, "backend-developer", ["dashboard/index.html"], None)
        self.assertEqual(result, "coding_ui")

    def test_no_paths_returns_base_class(self):
        result = resolve_task_class(None, "backend-developer", None, None)
        self.assertEqual(result, "coding_interactive")

    def test_research_skill_not_subclassed(self):
        # architect skill → research_structured (not coding_interactive), so subclassing skips
        result = resolve_task_class(None, "architect", ["schemas/0025.sql"], None)
        self.assertEqual(result, "research_structured")


# ---------------------------------------------------------------------------
# _expand_scope_tags
# ---------------------------------------------------------------------------

class TestExpandScopeTags(unittest.TestCase):

    def test_coding_sql_expands(self):
        expanded = _expand_scope_tags(["coding_sql"])
        self.assertIn("sql", expanded)
        self.assertIn("schema", expanded)
        self.assertIn("migration", expanded)
        self.assertIn("coding_sql", expanded)

    def test_coding_ui_expands(self):
        expanded = _expand_scope_tags(["coding_ui"])
        self.assertIn("ui", expanded)
        self.assertIn("dashboard", expanded)
        self.assertIn("coding_ui", expanded)

    def test_non_subclass_passthrough(self):
        expanded = _expand_scope_tags(["backend-developer", "Track-T1"])
        self.assertIn("backend-developer", expanded)
        self.assertIn("Track-T1", expanded)
        self.assertNotIn("sql", expanded)

    def test_empty_input(self):
        self.assertEqual(_expand_scope_tags([]), frozenset())


# ---------------------------------------------------------------------------
# _scope_matches strict mode
# ---------------------------------------------------------------------------

class TestScopeMatchesStrictMode(unittest.TestCase):

    def setUp(self):
        os.environ.pop("VNX_INTEL_STRICT_SCOPE", None)

    def tearDown(self):
        os.environ.pop("VNX_INTEL_STRICT_SCOPE", None)

    def test_empty_query_always_matches(self):
        self.assertTrue(_scope_matches([], []))
        self.assertTrue(_scope_matches(["sql"], []))

    def test_default_empty_item_matches_nonempty_query(self):
        # VNX_INTEL_STRICT_SCOPE not set → old behaviour, empty item = matches all
        self.assertTrue(_scope_matches([], ["sql"]))

    def test_strict_off_empty_item_matches_nonempty_query(self):
        os.environ["VNX_INTEL_STRICT_SCOPE"] = "0"
        self.assertTrue(_scope_matches([], ["sql"]))

    def test_strict_on_empty_item_does_not_match_nonempty_query(self):
        os.environ["VNX_INTEL_STRICT_SCOPE"] = "1"
        self.assertFalse(_scope_matches([], ["sql"]))

    def test_strict_on_empty_query_still_matches(self):
        os.environ["VNX_INTEL_STRICT_SCOPE"] = "1"
        self.assertTrue(_scope_matches([], []))

    def test_sql_item_matches_sql_query(self):
        self.assertTrue(_scope_matches(["sql"], ["sql"]))

    def test_sql_item_does_not_match_ui_query(self):
        self.assertFalse(_scope_matches(["sql"], ["ui"]))

    def test_ui_item_does_not_match_sql_scope(self):
        self.assertFalse(_scope_matches(["ui"], ["sql"]))

    def test_coding_sql_scope_matches_sql_item(self):
        # coding_sql expands to include 'sql', so items tagged 'sql' match
        self.assertTrue(_scope_matches(["sql"], ["coding_sql"]))

    def test_coding_sql_scope_does_not_match_ui_item(self):
        self.assertFalse(_scope_matches(["ui"], ["coding_sql"]))


# ---------------------------------------------------------------------------
# intelligence_backfill keyword inference
# ---------------------------------------------------------------------------

class TestBackfillInferTag(unittest.TestCase):

    def test_sql_keyword(self):
        self.assertEqual(_infer_tag("Add table for sessions", ""), "sql")

    def test_schema_keyword(self):
        self.assertEqual(_infer_tag("Schema update", "Adds new column"), "sql")

    def test_migration_keyword(self):
        self.assertEqual(_infer_tag("DB migration", ""), "sql")

    def test_async_keyword(self):
        self.assertEqual(_infer_tag("Async task runner", "Uses asyncio"), "async")

    def test_security_keyword(self):
        self.assertEqual(_infer_tag("Auth token", "security check"), "security")

    def test_ui_keyword(self):
        self.assertEqual(_infer_tag("Dashboard component", "html rendering"), "ui")

    def test_runtime_keyword(self):
        self.assertEqual(_infer_tag("Runtime coordination", "dispatch handling"), "runtime")

    def test_intelligence_keyword(self):
        self.assertEqual(_infer_tag("Intelligence injection", "pattern extraction"), "intelligence")

    def test_no_match_returns_none(self):
        self.assertIsNone(_infer_tag("Generic cleanup", "some util refactor"))

    def test_sql_takes_priority_over_async(self):
        # 'table' matches sql rule before 'async' rule
        self.assertEqual(_infer_tag("Async table migration", ""), "sql")


class TestBackfillTable(unittest.TestCase):

    def _make_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE success_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                description TEXT,
                category TEXT
            );
            CREATE TABLE antipatterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                description TEXT,
                category TEXT
            );
            INSERT INTO success_patterns (title, description, category)
                VALUES ('SQL table migration', 'Adds new table', '');
            INSERT INTO success_patterns (title, description, category)
                VALUES ('UI dashboard fix', 'html rendering fix', NULL);
            INSERT INTO success_patterns (title, description, category)
                VALUES ('Already tagged', 'code pattern', 'code');
            INSERT INTO antipatterns (title, description, category)
                VALUES ('Runtime dispatch error', 'receipt processing bug', '');
        """)
        return conn

    def test_backfill_updates_empty_category(self):
        conn = self._make_db()
        checked, updated = backfill_table(conn, "success_patterns")
        self.assertEqual(checked, 2)  # two rows with empty category
        self.assertEqual(updated, 2)
        row = conn.execute("SELECT category FROM success_patterns WHERE title = 'SQL table migration'").fetchone()
        self.assertEqual(row[0], "sql")
        row = conn.execute("SELECT category FROM success_patterns WHERE title = 'UI dashboard fix'").fetchone()
        self.assertEqual(row[0], "ui")
        conn.close()

    def test_backfill_does_not_touch_existing_category(self):
        conn = self._make_db()
        backfill_table(conn, "success_patterns")
        row = conn.execute("SELECT category FROM success_patterns WHERE title = 'Already tagged'").fetchone()
        self.assertEqual(row[0], "code")
        conn.close()

    def test_dry_run_makes_no_changes(self):
        conn = self._make_db()
        checked, updated = backfill_table(conn, "success_patterns", dry_run=True)
        self.assertEqual(updated, 2)  # would-be updates
        row = conn.execute("SELECT category FROM success_patterns WHERE title = 'SQL table migration'").fetchone()
        self.assertEqual(row[0] or "", "")  # unchanged
        conn.close()

    def test_backfill_antipatterns(self):
        conn = self._make_db()
        checked, updated = backfill_table(conn, "antipatterns")
        self.assertEqual(checked, 1)
        self.assertEqual(updated, 1)
        row = conn.execute("SELECT category FROM antipatterns WHERE id = 1").fetchone()
        self.assertEqual(row[0], "runtime")
        conn.close()


class TestRunBackfill(unittest.TestCase):

    def test_run_backfill_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            run_backfill(Path("/nonexistent/path/quality_intelligence.db"))

    def test_run_backfill_with_real_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = Path(f.name)
        try:
            conn = sqlite3.connect(str(db_path))
            conn.executescript("""
                CREATE TABLE success_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, description TEXT, category TEXT
                );
                CREATE TABLE antipatterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT, description TEXT, category TEXT
                );
                INSERT INTO success_patterns (title, description, category)
                    VALUES ('SQL schema migration', 'table changes', '');
            """)
            conn.close()
            results = run_backfill(db_path)
            self.assertEqual(results["success_patterns"]["checked"], 1)
            self.assertEqual(results["success_patterns"]["updated"], 1)
        finally:
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
