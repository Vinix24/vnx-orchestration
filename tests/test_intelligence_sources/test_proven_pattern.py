#!/usr/bin/env python3
"""Tests for intelligence_sources/proven_pattern.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_sources.proven_pattern import (
    _query_central,
    _query_per_project,
    query_proven_patterns,
)
from intelligence_sources._common import (
    IntelligenceItem,
    PATTERN_CATEGORY_GOVERNANCE,
    PATTERN_CATEGORY_CODE,
)


def _make_db(path=":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            confidence_score REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT, first_seen DATETIME, last_used DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
        );
    """)
    conn.commit()
    return conn


def _make_db_with_extras(path=":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            confidence_score REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT, first_seen DATETIME, last_used DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL,
            content_hash TEXT, project_id TEXT, pattern_category TEXT
        );
    """)
    conn.commit()
    return conn


def _seed(conn, title="Test pattern", description="Description", category="architect",
          confidence=0.8, usage_count=5, valid_until=None):
    cur = conn.execute(
        """INSERT INTO success_patterns (title, description, category, confidence_score,
           usage_count, first_seen)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (title, description, category, confidence, usage_count),
    )
    conn.commit()
    return cur.lastrowid


def _no_reconcile():
    pass


def _no_column(table, col):
    return False


class TestQueryPerProject(unittest.TestCase):
    def test_empty_db_returns_empty(self):
        db = _make_db()
        items = _query_per_project(db, "coding_interactive", [], _no_column, _no_reconcile)
        self.assertEqual(items, [])
        db.close()

    def test_single_pattern_returned(self):
        db = _make_db()
        _seed(db, title="Use structured output", confidence=0.85, usage_count=5)
        items = _query_per_project(db, "coding_interactive", [], _no_column, _no_reconcile)
        self.assertEqual(len(items), 1)
        self.assertIn("structured output", items[0].title)
        db.close()

    def test_item_class_is_proven_pattern(self):
        db = _make_db()
        _seed(db)
        items = _query_per_project(db, "coding_interactive", [], _no_column, _no_reconcile)
        self.assertEqual(items[0].item_class, "proven_pattern")
        db.close()

    def test_confidence_preserved(self):
        db = _make_db()
        _seed(db, confidence=0.73)
        items = _query_per_project(db, "coding_interactive", [], _no_column, _no_reconcile)
        self.assertAlmostEqual(items[0].confidence, 0.73)
        db.close()

    def test_expired_patterns_excluded(self):
        db = _make_db()
        db.execute(
            """INSERT INTO success_patterns (title, description, confidence_score,
               usage_count, valid_until, first_seen)
               VALUES ('Expired', 'x', 0.9, 5, datetime('now', '-1 day'), datetime('now'))"""
        )
        db.commit()
        items = _query_per_project(db, "coding_interactive", [], _no_column, _no_reconcile)
        self.assertEqual(items, [])
        db.close()

    def test_scope_filter_no_match(self):
        db = _make_db()
        _seed(db, category="reviewer", confidence=0.9)
        items = _query_per_project(db, "coding_interactive", ["architect"], _no_column, _no_reconcile)
        self.assertEqual(items, [])
        db.close()

    def test_scope_filter_match(self):
        db = _make_db()
        _seed(db, category="architect", confidence=0.9)
        items = _query_per_project(db, "research_structured", ["architect"], _no_column, _no_reconcile)
        self.assertEqual(len(items), 1)
        db.close()

    def test_sorted_by_confidence_desc(self):
        db = _make_db()
        _seed(db, title="Low", confidence=0.5)
        _seed(db, title="High", confidence=0.9)
        items = _query_per_project(db, "coding_interactive", [], _no_column, _no_reconcile)
        self.assertGreater(items[0].confidence, items[1].confidence)
        db.close()

    def test_reconcile_called(self):
        called = []
        def _reconcile():
            called.append(True)
        db = _make_db()
        _query_per_project(db, "coding_interactive", [], _no_column, _reconcile)
        self.assertEqual(len(called), 1)
        db.close()

    def test_source_refs_parsed_from_json(self):
        db = _make_db()
        import json
        db.execute(
            """INSERT INTO success_patterns (title, description, confidence_score,
               usage_count, source_dispatch_ids, first_seen)
               VALUES ('P', 'D', 0.8, 3, ?, datetime('now'))""",
            (json.dumps(["d-001", "d-002"]),),
        )
        db.commit()
        items = _query_per_project(db, "coding_interactive", [], _no_column, _no_reconcile)
        self.assertIn("d-001", items[0].source_refs)
        db.close()


class TestQueryCentral(unittest.TestCase):
    def test_none_connection_returns_empty(self):
        items = _query_central("coding_interactive", [], lambda: None)
        self.assertEqual(items, [])

    def test_central_item_returned(self):
        db = _make_db_with_extras()
        db.execute(
            """INSERT INTO success_patterns (title, description, category, confidence_score,
               usage_count, first_seen, project_id)
               VALUES ('Central pattern', 'Desc', 'architect', 0.85, 5, datetime('now'), 'proj-1')"""
        )
        db.commit()
        project_id_fn = lambda: "proj-1"
        items = _query_central("coding_interactive", [], lambda: db, project_id_fn=project_id_fn)
        self.assertEqual(len(items), 1)
        self.assertIn("Central pattern", items[0].title)
        db.close()

    def test_project_id_filter_applied(self):
        db = _make_db_with_extras()
        db.execute(
            """INSERT INTO success_patterns (title, description, category, confidence_score,
               usage_count, first_seen, project_id)
               VALUES ('Proj1 pattern', 'D', 'architect', 0.85, 5, datetime('now'), 'proj-1')"""
        )
        db.execute(
            """INSERT INTO success_patterns (title, description, category, confidence_score,
               usage_count, first_seen, project_id)
               VALUES ('Proj2 pattern', 'D', 'architect', 0.85, 5, datetime('now'), 'proj-2')"""
        )
        db.commit()
        items = _query_central("coding_interactive", [], lambda: db, project_id_fn=lambda: "proj-1")
        self.assertEqual(len(items), 1)
        self.assertIn("Proj1 pattern", items[0].title)
        db.close()

    def test_item_class_is_proven_pattern(self):
        db = _make_db_with_extras()
        db.execute(
            """INSERT INTO success_patterns (title, description, category, confidence_score,
               usage_count, first_seen, project_id)
               VALUES ('Pattern', 'D', 'architect', 0.8, 3, datetime('now'), 'p')"""
        )
        db.commit()
        items = _query_central("coding_interactive", [], lambda: db, project_id_fn=lambda: "p")
        self.assertTrue(all(i.item_class == "proven_pattern" for i in items))
        db.close()


class TestQueryProvenPatterns(unittest.TestCase):
    def setUp(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def tearDown(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def test_unset_env_uses_per_project(self):
        db = _make_db()
        _seed(db, title="Per-project", confidence=0.9)
        items = query_proven_patterns(
            db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: None,
            reconcile_fn=_no_reconcile,
        )
        self.assertEqual(len(items), 1)
        self.assertIn("Per-project", items[0].title)
        db.close()

    def test_central_env_returns_central(self):
        os.environ["VNX_USE_CENTRAL_DB"] = "1"
        db = _make_db_with_extras()
        db.execute(
            """INSERT INTO success_patterns (title, description, category, confidence_score,
               usage_count, first_seen, project_id)
               VALUES ('Central', 'D', 'architect', 0.85, 5, datetime('now'), 'proj-x')"""
        )
        db.commit()
        per_project_db = _make_db()
        items = query_proven_patterns(
            per_project_db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: db,
            reconcile_fn=_no_reconcile,
            project_id_fn=lambda: "proj-x",
        )
        self.assertTrue(any("Central" in i.title for i in items))
        per_project_db.close()
        db.close()

    def test_shadow_env_returns_per_project(self):
        os.environ["VNX_USE_CENTRAL_DB"] = "shadow"
        db = _make_db()
        _seed(db, title="Per-project shadow", confidence=0.9)
        items = query_proven_patterns(
            db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: None,
            reconcile_fn=_no_reconcile,
        )
        self.assertTrue(any("Per-project shadow" in i.title for i in items))
        db.close()
