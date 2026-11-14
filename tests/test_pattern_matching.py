#!/usr/bin/env python3
"""
Test Pattern Matching Engine (PR #2)
Validates that patterns are queryable and quality_context is populated
"""

import sys
import os
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))  # cli_output lives here
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))
from scripts.gather_intelligence import T0IntelligenceGatherer


_SNIPPETS = [
    ("browser_cleanup", "release browser memory", "def cleanup(): pass",
     "src/browser.py", "1-5", "crawler", "python"),
    ("memory_leak_fix", "fix memory leak in cleanup", "def fix(): pass",
     "src/memory.py", "10-20", "performance", "python"),
    ("optimize_query", "optimize database performance", "def opt(): pass",
     "src/db.py", "3-9", "storage", "python"),
    ("validate_coverage", "validate test coverage", "def cov(): pass",
     "src/cov.py", "7-12", "testing", "python"),
]


class TestPatternMatching(unittest.TestCase):
    """Test pattern matching functionality"""

    @classmethod
    def setUpClass(cls):
        """Build a throwaway quality DB (the live one is not hermetic), then init the gatherer."""
        cls._tmp = tempfile.mkdtemp(prefix="pattern-matching-")
        schema = Path(__file__).parent.parent / "schemas" / "quality_intelligence.sql"
        conn = sqlite3.connect(Path(cls._tmp) / "quality_intelligence.db")
        conn.executescript(schema.read_text())
        for title, desc, code, path, rng, tags, lang in _SNIPPETS:
            conn.execute(
                "INSERT INTO code_snippets (title, description, code, file_path, line_range,"
                " tags, language, framework, dependencies, quality_score, usage_count, last_updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '', '', 90, 0, '2026-11-13')",
                (title, desc, code, path, rng, tags, lang),
            )
        conn.commit()
        conn.close()
        cls._saved_state_dir = os.environ.get("VNX_STATE_DIR")
        os.environ["VNX_STATE_DIR"] = cls._tmp
        cls.gatherer = T0IntelligenceGatherer()

    @classmethod
    def tearDownClass(cls):
        if cls._saved_state_dir is None:
            os.environ.pop("VNX_STATE_DIR", None)
        else:
            os.environ["VNX_STATE_DIR"] = cls._saved_state_dir
        if cls.gatherer.quality_db is not None:
            cls.gatherer.quality_db.close()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_database_connectivity(self):
        """Test that quality database is accessible"""
        self.assertIsNotNone(self.gatherer.quality_db, "Quality database should be connected")

        # Check pattern count
        cursor = self.gatherer.quality_db.execute("SELECT COUNT(*) FROM code_snippets")
        count = cursor.fetchone()[0]
        self.assertEqual(count, len(_SNIPPETS))

    def test_keyword_extraction(self):
        """Test keyword extraction from task descriptions"""
        test_cases = [
            ("implement browser cleanup memory management",
             ["browser", "cleanup", "memory", "management"]),
            ("fix authentication bug in user-login system",
             ["fix", "authentication", "bug", "user-login", "system"]),
            ("optimize database_query performance",
             ["optimize", "database_query", "performance"])
        ]

        for task, expected_keywords in test_cases:
            keywords = self.gatherer.extract_keywords(task)
            for keyword in expected_keywords:
                self.assertIn(keyword, keywords,
                             f"Keyword '{keyword}' not found in extracted keywords for '{task}'")

        # 'implement' is a stopword: it must be dropped, not extracted
        self.assertNotIn("implement", self.gatherer.extract_keywords("implement browser cleanup"))

    def test_query_relevant_patterns(self):
        """Test pattern query returns relevant results"""
        test_tasks = [
            "implement browser cleanup",
            "fix memory leak",
            "optimize performance",
            "validate test coverage"
        ]

        for task in test_tasks:
            patterns = self.gatherer.query_relevant_patterns(task, limit=5)

            # Should return up to 5 patterns
            self.assertLessEqual(len(patterns), 5, f"Should return max 5 patterns for '{task}'")

            # Each pattern should have required fields
            for pattern in patterns:
                self.assertIn('title', pattern)
                self.assertIn('relevance_score', pattern)
                self.assertIn('quality_score', pattern)
                self.assertIn('file_path', pattern)

    def test_query_finds_seeded_pattern(self):
        """The throwaway DB must actually be searched: a seeded title comes back."""
        titles = [p["title"] for p in self.gatherer.query_relevant_patterns("browser cleanup", limit=5)]
        self.assertIn("browser_cleanup", titles)

    def test_quality_context_population(self):
        """Test that quality_context is properly populated"""
        result = self.gatherer.gather_for_dispatch(
            "fix memory leak in browser cleanup",
            "T1",
            "performance-profiler"
        )

        # Check quality_context is populated
        self.assertIn('quality_context', result)
        quality_context = result['quality_context']

        self.assertTrue(quality_context['agent_validated'])
        self.assertIn('patterns_available', quality_context)
        self.assertIn('pattern_count', quality_context)
        self.assertIn('offered_pattern_hashes', quality_context)

        # Pattern count should match
        self.assertEqual(quality_context['pattern_count'], len(result['suggested_patterns']))


if __name__ == "__main__":
    unittest.main(verbosity=2)
