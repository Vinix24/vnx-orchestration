#!/usr/bin/env python3
"""Tests for intelligence_sources/code_anchor.py"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_sources.code_anchor import (
    build_code_anchor_item,
    build_operator_memory_item,
    build_prior_round_item,
)
from intelligence_sources._common import PATTERN_CATEGORY_CODE


class _MockFinding:
    def __init__(self, gate, summary):
        self.gate = gate
        self.summary = summary


class _MockAnchor:
    def __init__(self, file_path, line_start, line_end, snippet):
        self.file_path = file_path
        self.line_start = line_start
        self.line_end = line_end
        self.snippet = snippet


class _MockMemory:
    def __init__(self, name, content):
        self.name = name
        self.content = content


class TestBuildPriorRoundItem(unittest.TestCase):
    def test_returns_none_when_no_pr_id(self):
        item = build_prior_round_item("", ["scripts/lib/foo.py"], "2026-01-01T00:00:00Z")
        self.assertIsNone(item)

    def test_returns_none_when_injector_unavailable(self):
        import intelligence_sources.code_anchor as _mod
        original = _mod._prior_round_injector
        _mod._prior_round_injector = None
        try:
            item = build_prior_round_item("pr-42", ["scripts/lib/foo.py"], "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._prior_round_injector = original

    def test_returns_none_when_no_findings(self):
        mock_injector = MagicMock()
        mock_injector.fetch_prior_findings.return_value = []
        import intelligence_sources.code_anchor as _mod
        original = _mod._prior_round_injector
        _mod._prior_round_injector = mock_injector
        try:
            item = build_prior_round_item("pr-42", [], "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._prior_round_injector = original

    def test_returns_item_when_findings_exist(self):
        finding = _MockFinding("codex_gate", "Missing null guard")
        mock_injector = MagicMock()
        mock_injector.fetch_prior_findings.return_value = [finding]
        mock_injector.format_findings_section.return_value = "## Finding\nMissing null guard"
        import intelligence_sources.code_anchor as _mod
        original = _mod._prior_round_injector
        _mod._prior_round_injector = mock_injector
        try:
            item = build_prior_round_item("pr-42", [], "2026-01-01T00:00:00Z")
            self.assertIsNotNone(item)
            self.assertEqual(item.item_class, "prior_round_finding")
        finally:
            _mod._prior_round_injector = original

    def test_item_confidence_is_one(self):
        finding = _MockFinding("codex_gate", "F1")
        mock_injector = MagicMock()
        mock_injector.fetch_prior_findings.return_value = [finding]
        mock_injector.format_findings_section.return_value = "content"
        import intelligence_sources.code_anchor as _mod
        original = _mod._prior_round_injector
        _mod._prior_round_injector = mock_injector
        try:
            item = build_prior_round_item("pr-42", [], "2026-01-01T00:00:00Z")
            self.assertEqual(item.confidence, 1.0)
        finally:
            _mod._prior_round_injector = original

    def test_item_id_includes_pr_id(self):
        finding = _MockFinding("g", "f")
        mock_injector = MagicMock()
        mock_injector.fetch_prior_findings.return_value = [finding]
        mock_injector.format_findings_section.return_value = "c"
        import intelligence_sources.code_anchor as _mod
        original = _mod._prior_round_injector
        _mod._prior_round_injector = mock_injector
        try:
            item = build_prior_round_item("pr-99", [], "2026-01-01T00:00:00Z")
            self.assertIn("pr-99", item.item_id)
        finally:
            _mod._prior_round_injector = original


class TestBuildCodeAnchorItem(unittest.TestCase):
    def test_returns_none_when_no_paths(self):
        item = build_code_anchor_item("d-001", [], "fix null", "2026-01-01T00:00:00Z")
        self.assertIsNone(item)

    def test_returns_none_when_no_instruction(self):
        item = build_code_anchor_item("d-001", ["scripts/lib/foo.py"], "", "2026-01-01T00:00:00Z")
        self.assertIsNone(item)

    def test_returns_none_when_finder_unavailable(self):
        import intelligence_sources.code_anchor as _mod
        original = _mod._code_anchor_finder
        _mod._code_anchor_finder = None
        try:
            item = build_code_anchor_item("d-001", ["scripts/lib/foo.py"], "fix null guard", "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._code_anchor_finder = original

    def test_returns_none_when_no_anchors_match(self):
        mock_finder = MagicMock()
        mock_finder.fetch_code_anchors.return_value = []
        import intelligence_sources.code_anchor as _mod
        original = _mod._code_anchor_finder
        _mod._code_anchor_finder = mock_finder
        try:
            item = build_code_anchor_item("d-001", ["scripts/lib/foo.py"], "instruction", "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._code_anchor_finder = original

    def test_returns_item_when_anchors_match(self):
        anchor = _MockAnchor("scripts/lib/foo.py", 10, 20, "def foo(): pass")
        mock_finder = MagicMock()
        mock_finder.fetch_code_anchors.return_value = [anchor]
        mock_finder.format_code_anchors_section.return_value = "## scripts/lib/foo.py:10-20\ndef foo(): pass"
        import intelligence_sources.code_anchor as _mod
        original = _mod._code_anchor_finder
        _mod._code_anchor_finder = mock_finder
        try:
            item = build_code_anchor_item("d-001", ["scripts/lib/foo.py"], "fix foo", "2026-01-01T00:00:00Z")
            self.assertIsNotNone(item)
            self.assertEqual(item.item_class, "code_anchor")
        finally:
            _mod._code_anchor_finder = original

    def test_item_confidence_is_one(self):
        anchor = _MockAnchor("f.py", 1, 5, "code")
        mock_finder = MagicMock()
        mock_finder.fetch_code_anchors.return_value = [anchor]
        mock_finder.format_code_anchors_section.return_value = "content"
        import intelligence_sources.code_anchor as _mod
        original = _mod._code_anchor_finder
        _mod._code_anchor_finder = mock_finder
        try:
            item = build_code_anchor_item("d-001", ["f.py"], "fix", "2026-01-01T00:00:00Z")
            self.assertEqual(item.confidence, 1.0)
        finally:
            _mod._code_anchor_finder = original

    def test_source_refs_contain_line_ranges(self):
        anchor = _MockAnchor("foo.py", 10, 20, "code")
        mock_finder = MagicMock()
        mock_finder.fetch_code_anchors.return_value = [anchor]
        mock_finder.format_code_anchors_section.return_value = "content"
        import intelligence_sources.code_anchor as _mod
        original = _mod._code_anchor_finder
        _mod._code_anchor_finder = mock_finder
        try:
            item = build_code_anchor_item("d-001", ["foo.py"], "fix", "2026-01-01T00:00:00Z")
            self.assertIn("foo.py:10-20", item.source_refs)
        finally:
            _mod._code_anchor_finder = original


class TestBuildOperatorMemoryItem(unittest.TestCase):
    def test_returns_none_when_no_inputs(self):
        item = build_operator_memory_item("d-001", None, [], "", "2026-01-01T00:00:00Z")
        self.assertIsNone(item)

    def test_returns_none_when_indexer_unavailable(self):
        import intelligence_sources.code_anchor as _mod
        original = _mod._operator_memory_indexer
        _mod._operator_memory_indexer = None
        try:
            item = build_operator_memory_item("d-001", "architect", ["f.py"], "instruction", "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._operator_memory_indexer = original

    def test_returns_none_when_no_memories_match(self):
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_memories.return_value = []
        import intelligence_sources.code_anchor as _mod
        original = _mod._operator_memory_indexer
        _mod._operator_memory_indexer = mock_indexer
        try:
            item = build_operator_memory_item("d-001", "architect", [], "instruction", "2026-01-01T00:00:00Z")
            self.assertIsNone(item)
        finally:
            _mod._operator_memory_indexer = original

    def test_returns_item_when_memories_match(self):
        memory = _MockMemory("feedback_atomic_writes", "Always use atomic writes")
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_memories.return_value = [memory]
        mock_indexer.format_memories_section.return_value = "## Operator memories\nAlways use atomic writes"
        import intelligence_sources.code_anchor as _mod
        original = _mod._operator_memory_indexer
        _mod._operator_memory_indexer = mock_indexer
        try:
            item = build_operator_memory_item("d-001", "architect", [], "fix atomic", "2026-01-01T00:00:00Z")
            self.assertIsNotNone(item)
            self.assertEqual(item.item_class, "operator_memory")
        finally:
            _mod._operator_memory_indexer = original

    def test_source_refs_contain_memory_names(self):
        memories = [_MockMemory("mem_a", "A"), _MockMemory("mem_b", "B")]
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_memories.return_value = memories
        mock_indexer.format_memories_section.return_value = "content"
        import intelligence_sources.code_anchor as _mod
        original = _mod._operator_memory_indexer
        _mod._operator_memory_indexer = mock_indexer
        try:
            item = build_operator_memory_item("d-001", "architect", [], "fix", "2026-01-01T00:00:00Z")
            self.assertIn("mem_a", item.source_refs)
            self.assertIn("mem_b", item.source_refs)
        finally:
            _mod._operator_memory_indexer = original

    def test_item_confidence_is_one(self):
        memory = _MockMemory("m", "content")
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_memories.return_value = [memory]
        mock_indexer.format_memories_section.return_value = "content"
        import intelligence_sources.code_anchor as _mod
        original = _mod._operator_memory_indexer
        _mod._operator_memory_indexer = mock_indexer
        try:
            item = build_operator_memory_item("d-001", "architect", [], "fix", "2026-01-01T00:00:00Z")
            self.assertEqual(item.confidence, 1.0)
        finally:
            _mod._operator_memory_indexer = original

    def test_skill_name_only_triggers_lookup(self):
        mock_indexer = MagicMock()
        mock_indexer.fetch_relevant_memories.return_value = []
        import intelligence_sources.code_anchor as _mod
        original = _mod._operator_memory_indexer
        _mod._operator_memory_indexer = mock_indexer
        try:
            build_operator_memory_item("d-001", "architect", [], "", "2026-01-01T00:00:00Z")
            mock_indexer.fetch_relevant_memories.assert_called_once()
        finally:
            _mod._operator_memory_indexer = original
