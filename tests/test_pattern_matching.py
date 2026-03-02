#!/usr/bin/env python3
"""
Test Pattern Matching Engine (PR #2)
Validates that 1,143 patterns are queryable and quality_context is populated
"""

import sys
import os
import json
import unittest
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts.gather_intelligence import T0IntelligenceGatherer


class TestPatternMatching(unittest.TestCase):
    """Test pattern matching functionality"""

    @classmethod
    def setUpClass(cls):
        """Initialize intelligence gatherer once for all tests"""
        cls.gatherer = T0IntelligenceGatherer()

    def test_database_connectivity(self):
        """Test that quality database is accessible"""
        self.assertIsNotNone(self.gatherer.quality_db, "Quality database should be connected")

        # Check pattern count
        cursor = self.gatherer.quality_db.execute("SELECT COUNT(*) FROM code_snippets")
        count = cursor.fetchone()[0]
        self.assertEqual(count, 1143, f"Expected 1143 patterns, found {count}")

    def test_keyword_extraction(self):
        """Test keyword extraction from task descriptions"""
        test_cases = [
            ("implement browser cleanup memory management",
             ["implement", "browser", "cleanup", "memory", "management"]),
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

    def test_quality_context_population(self):
        """Test that quality_context is properly populated"""
        result = self.gatherer.gather_for_dispatch(
            "fix memory leak in browser cleanup",
            "T1",
            "performance-engineer"
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
