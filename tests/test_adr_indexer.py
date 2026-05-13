#!/usr/bin/env python3
"""Tests for adr_indexer.py (Wave 5 P1 + CFX-W5-2 IndexCache refactor).

Covers:
  - Index building from real and fixture ADR directories
  - File-path reference extraction
  - Lookup by dispatch_paths overlap
  - Budget truncation
  - Anti-anchoring instruction presence
  - Cache behavior via IndexCache (_ADR_CACHE)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from adr_indexer import (
    CACHE_TTL_SEC,
    AdrEntry,
    AdrIndex,
    _parse_referenced_files,
    _scan_adrs,
    format_adrs_section,
    fetch_relevant_adrs,
    _ADR_CACHE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REAL_ADR_DIR = Path(__file__).resolve().parent.parent / "docs" / "governance" / "decisions"

_ADR_TEMPLATE = """\
# ADR-{num} — {title}

**Status:** Accepted
**Date:** 2026-05-10

## Context

This ADR governs work on {context_files}.

## Decision

**{decision_text}**

## See also

{see_also}
"""


def _write_adr(adr_dir: Path, num: str, title: str, context_files: str,
               decision_text: str, see_also: str) -> Path:
    filename = f"ADR-{num}-test.md"
    content = _ADR_TEMPLATE.format(
        num=num,
        title=title,
        context_files=context_files,
        decision_text=decision_text,
        see_also=see_also,
    )
    p = adr_dir / filename
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Tests: _scan_adrs (replaces old AdrIndex.load)
# ---------------------------------------------------------------------------

class TestAdrIndexLoad(unittest.TestCase):

    def test_load_index_from_adr_dir(self):
        """Loads all 14 ADRs from the real docs/governance/decisions/ directory."""
        if not _REAL_ADR_DIR.is_dir():
            self.skipTest("real ADR dir not available")

        index, mtimes = _scan_adrs(_REAL_ADR_DIR)

        self.assertEqual(len(index.entries), 14, (
            f"Expected 14 ADRs, got {len(index.entries)}: {sorted(index.entries.keys())}"
        ))
        self.assertEqual(len(mtimes), 14)

    def test_load_index_empty_dir(self):
        """Loading from an empty directory yields no entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            index, mtimes = _scan_adrs(Path(tmpdir))
            self.assertEqual(len(index.entries), 0)
            self.assertEqual(len(index.file_to_adrs), 0)
            self.assertEqual(mtimes, {})

    def test_scan_populates_mtimes_for_each_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adr_dir = Path(tmpdir)
            _write_adr(adr_dir, "001", "Test", "scripts/lib/foo.py",
                       "Do the thing.", "- `scripts/lib/foo.py`")
            index, mtimes = _scan_adrs(adr_dir)
            self.assertEqual(len(index.entries), 1)
            self.assertEqual(len(mtimes), 1)
            for path_str, mtime in mtimes.items():
                self.assertIsInstance(mtime, float)
                self.assertGreater(mtime, 0.0)

    def test_scan_none_dir_returns_empty(self):
        """_scan_adrs(None) returns empty AdrIndex and empty mtimes."""
        index, mtimes = _scan_adrs(None)
        self.assertEqual(len(index.entries), 0)
        self.assertEqual(mtimes, {})


# ---------------------------------------------------------------------------
# Tests: file-path reference extraction
# ---------------------------------------------------------------------------

class TestExtractReferences(unittest.TestCase):

    def test_extracts_file_path_references_from_see_also(self):
        """Extracts .py and .md references from a fixture ADR."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adr_dir = Path(tmpdir)
            _write_adr(
                adr_dir, "009", "Schema-First Migrations",
                "scripts/migrate_to_central_vnx.py",
                "Use PRAGMA introspection.",
                "- `scripts/migrate_to_central_vnx.py` — canonical helpers\n"
                "- `tests/test_migrate_dry_run.py` — structural test",
            )
            index, _ = _scan_adrs(adr_dir)
            entry = index.entries.get("ADR-009")
            self.assertIsNotNone(entry)
            self.assertIn("scripts/migrate_to_central_vnx.py", entry.referenced_files)
            self.assertIn("tests/test_migrate_dry_run.py", entry.referenced_files)

    def test_extracts_sql_and_md_references(self):
        """Extracts .sql and .md file references."""
        text = (
            "See `schemas/migrations/0010_add_project_id.sql` and "
            "`claudedocs/2026-05-09-p4-lessons.md` for details."
        )
        refs = _parse_referenced_files(text)
        self.assertIn("schemas/migrations/0010_add_project_id.sql", refs)
        self.assertIn("claudedocs/2026-05-09-p4-lessons.md", refs)

    def test_extracts_yaml_references(self):
        """Extracts .yaml file references."""
        refs = _parse_referenced_files("See `worker_permissions.yaml` for config.")
        self.assertIn("worker_permissions.yaml", refs)

    def test_ignores_unknown_extensions(self):
        """Does not extract extensions not in the allowlist."""
        refs = _parse_referenced_files("See `.vnx-data/events/T1.ndjson` for events.")
        self.assertNotIn(".vnx-data/events/T1.ndjson", refs)


# ---------------------------------------------------------------------------
# Tests: lookup
# ---------------------------------------------------------------------------

class TestAdrIndexLookup(unittest.TestCase):

    def _make_index_with_two_adrs(self) -> tuple:
        tmpdir = tempfile.mkdtemp()
        adr_dir = Path(tmpdir)
        _write_adr(
            adr_dir, "009", "Schema-First Migrations",
            "scripts/migrate_to_central_vnx.py",
            "Use PRAGMA introspection.",
            "- `scripts/migrate_to_central_vnx.py`\n- `tests/test_migrate_dry_run.py`",
        )
        _write_adr(
            adr_dir, "005", "NDJSON Audit Ledger",
            "scripts/receipt_processor.py",
            "Write NDJSON before SQLite.",
            "- `scripts/receipt_processor.py`\n- `scripts/lib/vnx_paths.py`",
        )
        index, _ = _scan_adrs(adr_dir)
        return tmpdir, index

    def test_lookup_by_dispatch_paths_overlap(self):
        """A single dispatch path matches exactly one ADR."""
        tmpdir, index = self._make_index_with_two_adrs()
        try:
            results = index.lookup(["scripts/migrate_to_central_vnx.py"])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].adr_id, "ADR-009")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_lookup_by_dispatch_paths_overlap_multi(self):
        """Multiple dispatch paths match multiple ADRs."""
        tmpdir, index = self._make_index_with_two_adrs()
        try:
            results = index.lookup([
                "scripts/migrate_to_central_vnx.py",
                "scripts/receipt_processor.py",
            ])
            ids = {r.adr_id for r in results}
            self.assertIn("ADR-009", ids)
            self.assertIn("ADR-005", ids)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_lookup_no_match_returns_empty(self):
        """Paths with no ADR overlap return empty list."""
        tmpdir, index = self._make_index_with_two_adrs()
        try:
            results = index.lookup(["scripts/unrelated_tool.py"])
            self.assertEqual(results, [])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_lookup_empty_paths_returns_empty(self):
        """Empty dispatch_paths list returns empty result."""
        tmpdir, index = self._make_index_with_two_adrs()
        try:
            self.assertEqual(index.lookup([]), [])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests: budget truncation
# ---------------------------------------------------------------------------

class TestBudgetTruncation(unittest.TestCase):

    def test_budget_truncates_at_max_chars(self):
        """fetch_relevant_adrs respects max_chars and drops entries that exceed it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adr_dir = Path(tmpdir)
            # Write 5 ADRs all referencing the same file; each entry is ~180 chars
            for i in range(1, 6):
                _write_adr(
                    adr_dir, f"00{i}", f"Decision {i}",
                    "scripts/shared_target.py",
                    "A" * 50,  # moderate decision text
                    "- `scripts/shared_target.py`",
                )
            # Use a budget that fits exactly 1 entry but not all 5.
            # Header alone is ~171 chars; one entry adds ~110 chars → ~281 total for 1.
            # Use 400 to ensure at least 1 fits but not all 5.
            results = fetch_relevant_adrs(
                ["scripts/shared_target.py"],
                max_chars=400,
                adr_dir=adr_dir,
            )
            formatted = format_adrs_section(results)
            self.assertLessEqual(len(formatted), 400)
            # At least one ADR should be present
            self.assertGreaterEqual(len(results), 1)
            # Not all 5 should be present (budget is too small)
            self.assertLess(len(results), 5)

    def test_zero_budget_returns_empty(self):
        """max_chars=0 returns nothing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            adr_dir = Path(tmpdir)
            _write_adr(adr_dir, "001", "Test", "scripts/foo.py",
                       "Decision.", "- `scripts/foo.py`")
            results = fetch_relevant_adrs(
                ["scripts/foo.py"],
                max_chars=0,
                adr_dir=adr_dir,
            )
            self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Tests: format_adrs_section
# ---------------------------------------------------------------------------

class TestFormatAdrsSection(unittest.TestCase):

    def test_anti_anchoring_instruction_in_formatted_section(self):
        """Formatted section includes the anti-anchoring governance notice."""
        entry = AdrEntry(
            adr_id="ADR-009",
            title="Schema-First Migrations",
            file_path=Path("docs/governance/decisions/ADR-009.md"),
            referenced_files=frozenset(["scripts/migrate_to_central_vnx.py"]),
            excerpt="Use PRAGMA introspection instead of hardcoded column lists.",
        )
        section = format_adrs_section([entry])
        self.assertIn("governance constraints", section)
        self.assertIn("not task descriptions", section)
        self.assertIn("ADR-009", section)

    def test_empty_list_returns_empty_string(self):
        self.assertEqual(format_adrs_section([]), "")

    def test_section_header_present(self):
        entry = AdrEntry(
            adr_id="ADR-001",
            title="No Redis",
            file_path=Path("docs/governance/decisions/ADR-001.md"),
            referenced_files=frozenset(),
            excerpt="No external Redis.",
        )
        section = format_adrs_section([entry])
        self.assertIn("RELEVANT ARCHITECTURAL DECISIONS", section)

    def test_excerpt_included_in_output(self):
        entry = AdrEntry(
            adr_id="ADR-003",
            title="OAuth Only",
            file_path=Path("docs/governance/decisions/ADR-003.md"),
            referenced_files=frozenset(),
            excerpt="All Claude routing via OAuth subprocess only.",
        )
        section = format_adrs_section([entry])
        self.assertIn("All Claude routing via OAuth subprocess only.", section)


# ---------------------------------------------------------------------------
# Tests: cache behavior via IndexCache
# ---------------------------------------------------------------------------

class TestCacheBehavior(unittest.TestCase):

    def test_cache_refreshes_after_ttl(self):
        """IndexCache re-scans when TTL has elapsed."""
        from index_cache import IndexCache

        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            return (AdrIndex(), {})

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = IndexCache(ttl_sec=0.01, scanner=scanner, glob_pattern="ADR-*.md")
            cache.get(Path(tmpdir))
            time.sleep(0.05)
            cache.get(Path(tmpdir))
            self.assertEqual(call_count[0], 2)

    def test_cache_fresh_within_ttl(self):
        """IndexCache does not re-scan within TTL."""
        from index_cache import IndexCache

        call_count = [0]

        def scanner(d):
            call_count[0] += 1
            return (AdrIndex(), {})

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="ADR-*.md")
            cache.get(Path(tmpdir))
            cache.get(Path(tmpdir))
            self.assertEqual(call_count[0], 1)

    def test_cache_invalidates_on_adr_file_mtime_change(self):
        """IndexCache re-scans when an ADR file mtime changes."""
        from index_cache import IndexCache

        with tempfile.TemporaryDirectory() as tmpdir:
            adr_dir = Path(tmpdir)
            adr_file = _write_adr(
                adr_dir, "001", "Test ADR", "scripts/foo.py",
                "Do something.", "- `scripts/foo.py`"
            )

            call_count = [0]

            def scanner(d):
                call_count[0] += 1
                if d is None or not d.is_dir():
                    return (AdrIndex(), {})
                index, mtimes = _scan_adrs(d)
                return (index, mtimes)

            cache = IndexCache(ttl_sec=60, scanner=scanner, glob_pattern="ADR-*.md")
            cache.get(adr_dir)
            self.assertEqual(call_count[0], 1)

            # Advance the file's mtime to a future time
            future_mtime = time.time() + 5
            os.utime(str(adr_file), (future_mtime, future_mtime))

            cache.get(adr_dir)
            self.assertEqual(call_count[0], 2)

    def test_fetch_relevant_adrs_loads_on_first_call(self):
        """fetch_relevant_adrs populates index from adr_dir on first use."""
        _ADR_CACHE.invalidate()
        with tempfile.TemporaryDirectory() as tmpdir:
            adr_dir = Path(tmpdir)
            _write_adr(
                adr_dir, "001", "Test", "scripts/foo.py",
                "Do the thing.", "- `scripts/foo.py`"
            )
            results = fetch_relevant_adrs(["scripts/foo.py"], adr_dir=adr_dir)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].adr_id, "ADR-001")

    def test_cache_ttl_constant_positive(self):
        """CACHE_TTL_SEC is a positive number."""
        self.assertGreater(CACHE_TTL_SEC, 0)


if __name__ == "__main__":
    unittest.main()
