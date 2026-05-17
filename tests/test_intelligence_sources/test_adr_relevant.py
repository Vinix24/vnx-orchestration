#!/usr/bin/env python3
"""Tests for intelligence_sources/adr_relevant.py"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_sources.adr_relevant import build_adr_item, build_schema_section_item
from intelligence_sources._common import PATTERN_CATEGORY_CODE


class _MockADR:
    def __init__(self, adr_id, title, content):
        self.adr_id = adr_id
        self.title = title
        self.content = content


class _MockSchemaSection:
    def __init__(self, table_name, file_path, sql):
        self.table_name = table_name
        self.file_path = file_path
        self.sql = sql


class TestBuildAdrItem(unittest.TestCase):
    def test_returns_none_when_no_paths(self):
        item = build_adr_item("d-001", [], "2026-01-01T00:00:00Z")
        self.assertIsNone(item)

    def test_returns_none_when_indexer_unavailable(self):
        import intelligence_sources.adr_relevant as _mod
        original = _mod._adr_indexer
        _mod._adr_indexer = None
        try:
            item = build_adr_item("d-001", ["scripts/lib/foo.py"], "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._adr_indexer = original

    def test_returns_none_when_no_matching_adrs(self):
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_adrs.return_value = []
        import intelligence_sources.adr_relevant as _mod
        original = _mod._adr_indexer
        _mod._adr_indexer = mock_indexer
        try:
            item = build_adr_item("d-001", ["scripts/lib/foo.py"], "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._adr_indexer = original

    def test_returns_item_when_adrs_match(self):
        adr = _MockADR("ADR-001", "Use SQLite", "Use SQLite for persistence.")
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_adrs.return_value = [adr]
        mock_indexer.format_adrs_section.return_value = "## ADR-001\nUse SQLite"
        import intelligence_sources.adr_relevant as _mod
        original = _mod._adr_indexer
        _mod._adr_indexer = mock_indexer
        try:
            item = build_adr_item("d-001", ["scripts/lib/foo.py"], "2026-01-01T00:00:00Z")
            self.assertIsNotNone(item)
            self.assertEqual(item.item_class, "adr_relevant")
        finally:
            _mod._adr_indexer = original

    def test_item_has_high_confidence(self):
        adr = _MockADR("ADR-002", "ADR title", "ADR content.")
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_adrs.return_value = [adr]
        mock_indexer.format_adrs_section.return_value = "## ADR-002"
        import intelligence_sources.adr_relevant as _mod
        original = _mod._adr_indexer
        _mod._adr_indexer = mock_indexer
        try:
            item = build_adr_item("d-001", ["scripts/lib/foo.py"], "2026-01-01T00:00:00Z")
            self.assertEqual(item.confidence, 1.0)
        finally:
            _mod._adr_indexer = original

    def test_source_refs_contain_adr_ids(self):
        adrs = [_MockADR("ADR-001", "T1", "C1"), _MockADR("ADR-002", "T2", "C2")]
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_adrs.return_value = adrs
        mock_indexer.format_adrs_section.return_value = "ADR content"
        import intelligence_sources.adr_relevant as _mod
        original = _mod._adr_indexer
        _mod._adr_indexer = mock_indexer
        try:
            item = build_adr_item("d-001", ["scripts/lib/foo.py"], "2026-01-01T00:00:00Z")
            self.assertIn("ADR-001", item.source_refs)
            self.assertIn("ADR-002", item.source_refs)
        finally:
            _mod._adr_indexer = original

    def test_evidence_count_equals_adr_count(self):
        adrs = [_MockADR(f"ADR-{i}", f"T{i}", f"C{i}") for i in range(3)]
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_adrs.return_value = adrs
        mock_indexer.format_adrs_section.return_value = "ADR content"
        import intelligence_sources.adr_relevant as _mod
        original = _mod._adr_indexer
        _mod._adr_indexer = mock_indexer
        try:
            item = build_adr_item("d-001", ["f.py"], "2026-01-01T00:00:00Z")
            self.assertEqual(item.evidence_count, 3)
        finally:
            _mod._adr_indexer = original

    def test_pattern_category_is_code(self):
        adr = _MockADR("ADR-001", "T", "C")
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_adrs.return_value = [adr]
        mock_indexer.format_adrs_section.return_value = "C"
        import intelligence_sources.adr_relevant as _mod
        original = _mod._adr_indexer
        _mod._adr_indexer = mock_indexer
        try:
            item = build_adr_item("d-001", ["f.py"], "2026-01-01T00:00:00Z")
            self.assertEqual(item.pattern_category, PATTERN_CATEGORY_CODE)
        finally:
            _mod._adr_indexer = original


class TestBuildSchemaSectionItem(unittest.TestCase):
    def test_returns_none_when_no_paths_and_no_text(self):
        item = build_schema_section_item("d-001", [], "", "2026-01-01T00:00:00Z")
        self.assertIsNone(item)

    def test_returns_none_when_indexer_unavailable(self):
        import intelligence_sources.adr_relevant as _mod
        original = _mod._schema_section_indexer
        _mod._schema_section_indexer = None
        try:
            item = build_schema_section_item("d-001", ["schemas/foo.sql"], "CREATE TABLE", "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._schema_section_indexer = original

    def test_returns_none_when_no_sections_match(self):
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_schema_sections.return_value = []
        import intelligence_sources.adr_relevant as _mod
        original = _mod._schema_section_indexer
        _mod._schema_section_indexer = mock_indexer
        try:
            item = build_schema_section_item("d-001", ["schemas/foo.sql"], "ALTER TABLE", "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._schema_section_indexer = original

    def test_returns_item_when_sections_match(self):
        section = _MockSchemaSection("users", "schemas/users.sql", "CREATE TABLE users (id INT)")
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_schema_sections.return_value = [section]
        mock_indexer.format_schema_sections.return_value = "## users\nCREATE TABLE users (id INT)"
        import intelligence_sources.adr_relevant as _mod
        original = _mod._schema_section_indexer
        _mod._schema_section_indexer = mock_indexer
        try:
            item = build_schema_section_item("d-001", ["schemas/users.sql"], "users", "2026-01-01T00:00:00Z")
            self.assertIsNotNone(item)
            self.assertEqual(item.item_class, "schema_section")
        finally:
            _mod._schema_section_indexer = original

    def test_evidence_count_equals_section_count(self):
        sections = [
            _MockSchemaSection("t1", "f.sql", "CREATE TABLE t1 ()"),
            _MockSchemaSection("t2", "f.sql", "CREATE TABLE t2 ()"),
        ]
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_schema_sections.return_value = sections
        mock_indexer.format_schema_sections.return_value = "content"
        import intelligence_sources.adr_relevant as _mod
        original = _mod._schema_section_indexer
        _mod._schema_section_indexer = mock_indexer
        try:
            item = build_schema_section_item("d-001", ["f.sql"], "t1 t2", "2026-01-01T00:00:00Z")
            self.assertEqual(item.evidence_count, 2)
        finally:
            _mod._schema_section_indexer = original

    def test_item_has_full_confidence(self):
        section = _MockSchemaSection("t", "f.sql", "CREATE TABLE t ()")
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_schema_sections.return_value = [section]
        mock_indexer.format_schema_sections.return_value = "content"
        import intelligence_sources.adr_relevant as _mod
        original = _mod._schema_section_indexer
        _mod._schema_section_indexer = mock_indexer
        try:
            item = build_schema_section_item("d-001", ["f.sql"], "t", "2026-01-01T00:00:00Z")
            self.assertEqual(item.confidence, 1.0)
        finally:
            _mod._schema_section_indexer = original
