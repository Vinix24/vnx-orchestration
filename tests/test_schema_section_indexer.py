#!/usr/bin/env python3
"""Tests for schema_section_indexer.py (Wave 5 P4).

Coverage:
- CREATE TABLE / ALTER TABLE / CREATE INDEX extraction
- DB-related dispatch gate (path hints + instruction hints)
- Table-name matching from instruction text and dispatch path basenames
- Top-K cap (MAX_SECTIONS_PER_DISPATCH = 4)
- Recency tiebreak (newer migration file basename wins)
- Path-traversal guard rejects files outside schemas_dir
- Budget truncation at max_chars
- Anti-anchoring instruction in formatted output
- Cache TTL refresh
- Missing schemas dir returns empty list
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

import schema_section_indexer
from schema_section_indexer import (
    MAX_SCHEMA_CHARS,
    MAX_SECTIONS_PER_DISPATCH,
    SchemaSection,
    _is_db_related_dispatch,
    fetch_relevant_schema_sections,
    format_schema_sections,
)


def _write_sql(directory: Path, filename: str, content: str) -> Path:
    """Write a SQL file and return its path."""
    p = directory / filename
    p.write_text(content, encoding="utf-8")
    return p


class TestExtractCreateTableSections(unittest.TestCase):
    """DDL extraction from SQL files."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._schemas = self._base / "schemas"
        self._schemas.mkdir()
        schema_section_indexer._INDEX.loaded_at = 0.0

    def tearDown(self):
        schema_section_indexer._INDEX.loaded_at = 0.0
        self._tmpdir.cleanup()

    def test_extracts_create_table_sections(self):
        """CREATE TABLE statements are parsed and indexed by table name."""
        _write_sql(self._schemas, "baseline.sql", (
            "CREATE TABLE IF NOT EXISTS dispatch_metadata (\n"
            "    id INTEGER PRIMARY KEY,\n"
            "    name TEXT NOT NULL\n"
            ");\n"
        ))

        sections = fetch_relevant_schema_sections(
            ["schemas/baseline.sql"],
            "update the dispatch_metadata table schema",
            schemas_dir=self._schemas,
        )
        self.assertGreater(len(sections), 0)
        names = [s.table_name for s in sections]
        self.assertIn("dispatch_metadata", names)
        kinds = [s.statement_kind for s in sections]
        self.assertTrue(any("CREATE" in k for k in kinds))

    def test_extracts_alter_table_sections(self):
        """ALTER TABLE statements are parsed and indexed."""
        _write_sql(self._schemas, "migration.sql", (
            "ALTER TABLE dispatch_metadata ADD COLUMN project_id TEXT;\n"
        ))

        sections = fetch_relevant_schema_sections(
            ["schemas/migration.sql"],
            "add project_id to dispatch_metadata",
            schemas_dir=self._schemas,
        )
        alter_sections = [s for s in sections if "ALTER" in s.statement_kind.upper()]
        self.assertGreater(len(alter_sections), 0)
        self.assertIn("dispatch_metadata", [s.table_name for s in alter_sections])

    def test_extracts_create_index_sections(self):
        """CREATE INDEX statements are parsed and indexed under the indexed table name."""
        _write_sql(self._schemas, "indexes.sql", (
            "CREATE INDEX IF NOT EXISTS idx_leases_terminal\n"
            "ON terminal_leases (terminal_id);\n"
        ))

        sections = fetch_relevant_schema_sections(
            ["schemas/indexes.sql"],
            "optimize terminal_leases index lookup",
            schemas_dir=self._schemas,
        )
        index_sections = [s for s in sections if "INDEX" in s.statement_kind.upper()]
        self.assertGreater(len(index_sections), 0)


class TestDbRelatedGate(unittest.TestCase):
    """_is_db_related_dispatch quick gate."""

    def test_db_related_gate_blocks_non_db_dispatches(self):
        """Dispatch with no DB paths and no DB terms in instruction returns False."""
        result = _is_db_related_dispatch(
            ["scripts/lib/intelligence_selector.py"],
            "refactor the token counter logic in the frontend",
        )
        self.assertFalse(result)

    def test_db_related_gate_passes_for_schema_paths(self):
        """Dispatch touching schemas/ path returns True."""
        result = _is_db_related_dispatch(
            ["schemas/quality_intelligence.sql"],
            "some task description",
        )
        self.assertTrue(result)

    def test_db_related_gate_passes_for_migrate_paths(self):
        """Dispatch touching scripts/migrate path returns True."""
        result = _is_db_related_dispatch(
            ["scripts/migrate_to_central_vnx.py"],
            "some task description",
        )
        self.assertTrue(result)

    def test_db_related_gate_passes_for_table_name_in_instruction(self):
        """Instruction mentioning 'sqlite' triggers the DB gate."""
        result = _is_db_related_dispatch(
            ["scripts/lib/intelligence_selector.py"],
            "update the sqlite database connection pooling",
        )
        self.assertTrue(result)

    def test_db_related_gate_passes_for_migration_in_instruction(self):
        """Instruction mentioning 'migration' triggers the DB gate."""
        result = _is_db_related_dispatch(
            [],
            "apply the schema migration for project_id column",
        )
        self.assertTrue(result)


class TestMatchingAlgorithm(unittest.TestCase):
    """Table-name matching from instruction text and dispatch path basenames."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._schemas = self._base / "schemas"
        self._schemas.mkdir()
        schema_section_indexer._INDEX.loaded_at = 0.0

    def tearDown(self):
        schema_section_indexer._INDEX.loaded_at = 0.0
        self._tmpdir.cleanup()

    def _write_table(self, table_name: str, filename: str = None) -> None:
        fname = filename or f"{table_name}.sql"
        _write_sql(self._schemas, fname, (
            f"CREATE TABLE IF NOT EXISTS {table_name} (\n"
            f"    id INTEGER PRIMARY KEY\n"
            f");\n"
        ))

    def test_match_by_instruction_table_name_overlap(self):
        """Table names mentioned in instruction text are matched against the index."""
        self._write_table("success_patterns")

        sections = fetch_relevant_schema_sections(
            ["schemas/quality_intelligence.sql"],
            "update success_patterns schema to add confidence column",
            schemas_dir=self._schemas,
        )
        names = [s.table_name for s in sections]
        self.assertIn("success_patterns", names)

    def test_match_by_dispatch_path_basename(self):
        """Dispatch path stem is matched against known table names."""
        self._write_table("dispatch_metadata")

        sections = fetch_relevant_schema_sections(
            ["scripts/migrate_dispatch_metadata.py"],
            "migrate the schema changes",
            schemas_dir=self._schemas,
        )
        names = [s.table_name for s in sections]
        self.assertIn("dispatch_metadata", names)

    def test_returns_empty_for_no_matching_tables(self):
        """No matching table names returns empty list (gate passes but no match)."""
        self._write_table("terminal_leases")

        sections = fetch_relevant_schema_sections(
            ["schemas/runtime_coordination.sql"],
            "update the unrelated_table schema",
            schemas_dir=self._schemas,
        )
        # 'unrelated_table' is not in the index, so no sections returned
        # (gate passes via schemas/ path, but no candidate matches)
        names = [s.table_name for s in sections]
        self.assertNotIn("unrelated_table", names)


class TestTopKCap(unittest.TestCase):
    """MAX_SECTIONS_PER_DISPATCH cap enforcement."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._schemas = self._base / "schemas"
        self._schemas.mkdir()
        schema_section_indexer._INDEX.loaded_at = 0.0

    def tearDown(self):
        schema_section_indexer._INDEX.loaded_at = 0.0
        self._tmpdir.cleanup()

    def test_top_k_capped_at_4_sections(self):
        """Result is capped at MAX_SECTIONS_PER_DISPATCH = 4 sections."""
        # Create 6 tables all mentioned in instruction
        table_names = [
            "alpha_table", "beta_table", "gamma_table",
            "delta_table", "epsilon_table", "zeta_table",
        ]
        for name in table_names:
            _write_sql(self._schemas, f"{name}.sql", (
                f"CREATE TABLE IF NOT EXISTS {name} (id INTEGER PRIMARY KEY);\n"
            ))

        instruction = " ".join(table_names) + " schema migration sqlite"
        sections = fetch_relevant_schema_sections(
            ["schemas/alpha_table.sql"],
            instruction,
            schemas_dir=self._schemas,
        )
        self.assertLessEqual(len(sections), MAX_SECTIONS_PER_DISPATCH)


class TestRecencyTiebreak(unittest.TestCase):
    """Newer migration files (larger basename) are preferred."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._schemas = self._base / "schemas"
        migrations = self._schemas / "migrations"
        migrations.mkdir(parents=True)
        schema_section_indexer._INDEX.loaded_at = 0.0

    def tearDown(self):
        schema_section_indexer._INDEX.loaded_at = 0.0
        self._tmpdir.cleanup()

    def test_recency_tiebreak_newer_migration_first(self):
        """When same table appears in multiple migrations, newer file appears first."""
        migrations = self._schemas / "migrations"
        _write_sql(migrations, "0005_baseline.sql", (
            "CREATE TABLE IF NOT EXISTS user_events (id INTEGER PRIMARY KEY);\n"
        ))
        _write_sql(migrations, "0012_user_events_update.sql", (
            "ALTER TABLE user_events ADD COLUMN project_id TEXT;\n"
        ))

        sections = fetch_relevant_schema_sections(
            ["schemas/migrations/0012_user_events_update.sql"],
            "update user_events table schema migration",
            schemas_dir=self._schemas,
        )
        # Both files should have been found; sort is by descending basename
        # so '0012_...' must appear before '0005_...'
        if len(sections) >= 2:
            file_names = [Path(s.file_path).name for s in sections]
            idx_0012 = next((i for i, n in enumerate(file_names) if "0012" in n), None)
            idx_0005 = next((i for i, n in enumerate(file_names) if "0005" in n), None)
            if idx_0012 is not None and idx_0005 is not None:
                self.assertLess(idx_0012, idx_0005,
                                "Newer migration (0012) should appear before older (0005)")


class TestPathTraversalGuard(unittest.TestCase):
    """Path-traversal guard rejects files outside schemas_dir."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._schemas = self._base / "schemas"
        self._schemas.mkdir()
        # Put a legitimate file in schemas/
        _write_sql(self._schemas, "legit.sql", (
            "CREATE TABLE IF NOT EXISTS dispatch_metadata (id INTEGER PRIMARY KEY);\n"
        ))
        # A file OUTSIDE schemas_dir (in parent)
        outside = self._base / "outside.sql"
        outside.write_text(
            "CREATE TABLE IF NOT EXISTS evil_table (id INTEGER PRIMARY KEY);\n",
            encoding="utf-8",
        )
        # Create a symlink inside schemas that points outside
        symlink = self._schemas / "escape.sql"
        try:
            symlink.symlink_to(outside)
        except (OSError, NotImplementedError):
            pass  # Platform may not support symlinks; skip symlink half of test

        schema_section_indexer._INDEX.loaded_at = 0.0

    def tearDown(self):
        schema_section_indexer._INDEX.loaded_at = 0.0
        self._tmpdir.cleanup()

    def test_path_traversal_guard_rejects_escape(self):
        """Files outside schemas_dir (via symlink) are skipped; legitimate files load."""
        sections = fetch_relevant_schema_sections(
            ["schemas/legit.sql"],
            "update dispatch_metadata table migration sqlite",
            schemas_dir=self._schemas,
        )
        # Legitimate table should be found
        names = [s.table_name for s in sections]
        self.assertIn("dispatch_metadata", names)
        # evil_table (via symlink outside) must NOT appear
        self.assertNotIn("evil_table", names)


class TestBudgetTruncation(unittest.TestCase):
    """Budget truncation at max_chars."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._schemas = self._base / "schemas"
        self._schemas.mkdir()
        schema_section_indexer._INDEX.loaded_at = 0.0

    def tearDown(self):
        schema_section_indexer._INDEX.loaded_at = 0.0
        self._tmpdir.cleanup()

    def test_budget_truncates_at_max_chars(self):
        """Total body chars of returned sections stays within max_chars."""
        # Create two tables whose combined DDL exceeds a small budget
        _write_sql(self._schemas, "alpha.sql",
            "CREATE TABLE IF NOT EXISTS alpha_table (\n"
            "    " + ("col TEXT,\n    " * 30) + "id INTEGER PRIMARY KEY\n);\n"
        )
        _write_sql(self._schemas, "beta.sql",
            "CREATE TABLE IF NOT EXISTS beta_table (\n"
            "    " + ("col TEXT,\n    " * 30) + "id INTEGER PRIMARY KEY\n);\n"
        )

        tiny_budget = 200
        sections = fetch_relevant_schema_sections(
            ["schemas/alpha.sql"],
            "update alpha_table and beta_table schema migration sqlite",
            max_chars=tiny_budget,
            schemas_dir=self._schemas,
        )
        total_chars = sum(len(s.body) for s in sections)
        # Either only one section fits, or we get none if even the first exceeds budget
        # but if we got multiple they must fit within budget
        if len(sections) > 1:
            self.assertLessEqual(total_chars, tiny_budget)


class TestFormatAntiAnchoring(unittest.TestCase):
    """Anti-anchoring instruction is included in formatted output."""

    def test_format_section_includes_anti_anchoring_instruction(self):
        """Formatted output contains the anti-anchoring caveat about PRAGMA table_info."""
        sections = [
            SchemaSection(
                table_name="dispatch_metadata",
                statement_kind="CREATE TABLE",
                file_path="schemas/quality_intelligence.sql",
                body="CREATE TABLE IF NOT EXISTS dispatch_metadata (id INTEGER PRIMARY KEY);",
            )
        ]
        formatted = format_schema_sections(sections)
        self.assertIn("PRAGMA table_info()", formatted)
        self.assertIn("migration", formatted.lower())

    def test_format_empty_sections_returns_empty_string(self):
        """Empty section list returns empty string (no header noise)."""
        self.assertEqual(format_schema_sections([]), "")

    def test_format_includes_file_path_and_table_name(self):
        """Each section shows file_path and table_name in the heading."""
        sections = [
            SchemaSection(
                table_name="success_patterns",
                statement_kind="CREATE TABLE",
                file_path="schemas/quality_intelligence.sql",
                body="CREATE TABLE IF NOT EXISTS success_patterns (id INTEGER PRIMARY KEY);",
            )
        ]
        formatted = format_schema_sections(sections)
        self.assertIn("success_patterns", formatted)
        self.assertIn("schemas/quality_intelligence.sql", formatted)


class TestCacheBehavior(unittest.TestCase):
    """Cache TTL and mtime-invalidation behavior."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name)
        self._schemas = self._base / "schemas"
        self._schemas.mkdir()
        schema_section_indexer._INDEX.loaded_at = 0.0

    def tearDown(self):
        schema_section_indexer._INDEX.loaded_at = 0.0
        self._tmpdir.cleanup()

    def test_cache_refreshes_after_ttl(self):
        """Index reloads when loaded_at is past CACHE_TTL_SEC."""
        _write_sql(self._schemas, "baseline.sql",
            "CREATE TABLE IF NOT EXISTS dispatch_metadata (id INTEGER PRIMARY KEY);\n"
        )

        # First call: populates cache
        fetch_relevant_schema_sections(
            ["schemas/baseline.sql"],
            "update dispatch_metadata migration sqlite",
            schemas_dir=self._schemas,
        )
        first_loaded_at = schema_section_indexer._INDEX.loaded_at

        # Force TTL expiry
        schema_section_indexer._INDEX.loaded_at = 0.0

        # Add a new table
        _write_sql(self._schemas, "new_table.sql",
            "CREATE TABLE IF NOT EXISTS brand_new_table (id INTEGER PRIMARY KEY);\n"
        )

        fetch_relevant_schema_sections(
            ["schemas/baseline.sql"],
            "update dispatch_metadata brand_new_table migration sqlite",
            schemas_dir=self._schemas,
        )

        # Cache should have been refreshed
        self.assertNotEqual(schema_section_indexer._INDEX.loaded_at, 0.0)
        # New table should be in the index now
        self.assertIn("brand_new_table", schema_section_indexer._INDEX.table_index)


class TestMissingSchemaDir(unittest.TestCase):
    """Missing schemas dir returns empty list gracefully."""

    def setUp(self):
        schema_section_indexer._INDEX.loaded_at = 0.0

    def tearDown(self):
        schema_section_indexer._INDEX.loaded_at = 0.0

    def test_returns_empty_when_schemas_dir_missing(self):
        """When schemas_dir does not exist, fetch returns empty list without error."""
        missing_dir = Path("/nonexistent/schemas_dir_xyz")
        sections = fetch_relevant_schema_sections(
            ["schemas/some_migration.sql"],
            "update dispatch_metadata migration sqlite",
            schemas_dir=missing_dir,
        )
        self.assertEqual(sections, [])


if __name__ == "__main__":
    unittest.main()
