#!/usr/bin/env python3
"""Tests for intelligence_sources/failure_prevention.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_sources.failure_prevention import (
    _query_antipatterns,
    _query_central,
    _query_per_project,
    _query_prevention_rules,
    query_failure_prevention,
)
from intelligence_sources._common import (
    PATTERN_CATEGORY_ANTIPATTERN_EVIDENCE,
    PATTERN_CATEGORY_PROCESS,
)


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            why_problematic TEXT, better_alternative TEXT,
            occurrence_count INTEGER DEFAULT 0, severity TEXT DEFAULT 'medium',
            source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT, rule_type TEXT, description TEXT,
            recommendation TEXT, confidence REAL DEFAULT 0.0,
            created_at TEXT, triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
        );
    """)
    conn.commit()
    return conn


def _seed_antipattern(conn, title="Bad pattern", category="architect",
                       severity="high", occurrence_count=3,
                       why="Wrong approach", better="Better approach"):
    cur = conn.execute(
        """INSERT INTO antipatterns (title, description, category, severity,
           why_problematic, better_alternative, occurrence_count, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (title, title, category, severity, why, better, occurrence_count),
    )
    conn.commit()
    return cur.lastrowid


def _seed_rule(conn, description="Rule", recommendation="Do this", confidence=0.7,
               tag_combination=None, triggered_count=2):
    cur = conn.execute(
        """INSERT INTO prevention_rules (tag_combination, rule_type, description,
           recommendation, confidence, created_at, triggered_count, last_triggered)
           VALUES (?, 'tag', ?, ?, ?, datetime('now'), ?, datetime('now'))""",
        (tag_combination, description, recommendation, confidence, triggered_count),
    )
    conn.commit()
    return cur.lastrowid


def _no_column(table, col):
    return False


class TestQueryAntipatterns(unittest.TestCase):
    def test_empty_db_returns_empty(self):
        db = _make_db()
        items = _query_antipatterns(db, [], _no_column)
        self.assertEqual(items, [])
        db.close()

    def test_returns_antipattern_item(self):
        db = _make_db()
        _seed_antipattern(db, title="Cowboy commit")
        items = _query_antipatterns(db, [], _no_column)
        self.assertEqual(len(items), 1)
        self.assertIn("Cowboy commit", items[0].title)
        db.close()

    def test_item_class_is_failure_prevention(self):
        db = _make_db()
        _seed_antipattern(db)
        items = _query_antipatterns(db, [], _no_column)
        self.assertEqual(items[0].item_class, "failure_prevention")
        db.close()

    def test_pattern_category_is_antipattern_evidence(self):
        db = _make_db()
        _seed_antipattern(db)
        items = _query_antipatterns(db, [], _no_column)
        self.assertEqual(items[0].pattern_category, PATTERN_CATEGORY_ANTIPATTERN_EVIDENCE)
        db.close()

    def test_critical_severity_gets_high_confidence(self):
        db = _make_db()
        _seed_antipattern(db, severity="critical")
        items = _query_antipatterns(db, [], _no_column)
        self.assertGreaterEqual(items[0].confidence, 0.9)
        db.close()

    def test_expired_antipattern_excluded(self):
        db = _make_db()
        db.execute(
            """INSERT INTO antipatterns (title, occurrence_count, severity,
               valid_until, first_seen, last_seen)
               VALUES ('Expired', 2, 'high', datetime('now', '-1 day'), datetime('now'), datetime('now'))"""
        )
        db.commit()
        items = _query_antipatterns(db, [], _no_column)
        self.assertEqual(items, [])
        db.close()

    def test_scope_filter_applied(self):
        db = _make_db()
        _seed_antipattern(db, category="reviewer", severity="critical")
        items = _query_antipatterns(db, ["architect"], _no_column)
        self.assertEqual(items, [])
        db.close()

    def test_content_builds_why_and_better(self):
        db = _make_db()
        _seed_antipattern(db, why="This is wrong", better="Do this instead")
        items = _query_antipatterns(db, [], _no_column)
        self.assertIn("This is wrong", items[0].content)
        self.assertIn("Do this instead", items[0].content)
        db.close()


class TestQueryPreventionRules(unittest.TestCase):
    def test_empty_db_returns_empty(self):
        db = _make_db()
        items = _query_prevention_rules(db, [], _no_column)
        self.assertEqual(items, [])
        db.close()

    def test_returns_rule_item(self):
        db = _make_db()
        _seed_rule(db, description="Don't mock the DB")
        items = _query_prevention_rules(db, [], _no_column)
        self.assertEqual(len(items), 1)
        db.close()

    def test_item_class_is_failure_prevention(self):
        db = _make_db()
        _seed_rule(db)
        items = _query_prevention_rules(db, [], _no_column)
        self.assertEqual(items[0].item_class, "failure_prevention")
        db.close()

    def test_pattern_category_is_process(self):
        db = _make_db()
        _seed_rule(db)
        items = _query_prevention_rules(db, [], _no_column)
        self.assertEqual(items[0].pattern_category, PATTERN_CATEGORY_PROCESS)
        db.close()

    def test_json_array_tag_combination_parsed(self):
        db = _make_db()
        import json
        _seed_rule(db, tag_combination=json.dumps(["architect", "coding_interactive"]))
        items = _query_prevention_rules(db, ["architect"], _no_column)
        self.assertEqual(len(items), 1)
        db.close()

    def test_comma_separated_tag_combination_parsed(self):
        db = _make_db()
        _seed_rule(db, tag_combination="architect,coding_interactive")
        items = _query_prevention_rules(db, ["architect"], _no_column)
        self.assertEqual(len(items), 1)
        db.close()

    def test_no_matching_scope_returns_empty(self):
        db = _make_db()
        _seed_rule(db, tag_combination=json.dumps(["reviewer"]))
        items = _query_prevention_rules(db, ["architect"], _no_column)
        self.assertEqual(items, [])
        db.close()

    def test_empty_tag_combination_matches_all(self):
        db = _make_db()
        _seed_rule(db, tag_combination=None)
        items = _query_prevention_rules(db, ["architect"], _no_column)
        self.assertEqual(len(items), 1)
        db.close()


class TestQueryFailurePrevention(unittest.TestCase):
    def setUp(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def tearDown(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def test_unset_env_uses_per_project(self):
        db = _make_db()
        _seed_antipattern(db, title="Per-project AP")
        items = query_failure_prevention(
            db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: None,
        )
        self.assertTrue(any("Per-project AP" in i.title for i in items))
        db.close()

    def test_central_env_returns_central(self):
        os.environ["VNX_USE_CENTRAL_DB"] = "1"
        central_db = sqlite3.connect(":memory:")
        central_db.row_factory = sqlite3.Row
        central_db.executescript("""
            CREATE TABLE antipatterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT, title TEXT, description TEXT,
                why_problematic TEXT, better_alternative TEXT,
                occurrence_count INTEGER DEFAULT 0, severity TEXT DEFAULT 'medium',
                first_seen DATETIME, last_seen DATETIME,
                valid_until DATETIME DEFAULT NULL, project_id TEXT
            );
        """)
        central_db.execute(
            """INSERT INTO antipatterns (title, description, category, severity,
               why_problematic, better_alternative, occurrence_count,
               first_seen, last_seen, project_id)
               VALUES ('Central AP', 'x', 'architect', 'high', 'Bad', 'Better', 3,
               datetime('now'), datetime('now'), 'proj-c')"""
        )
        central_db.commit()
        per_db = _make_db()
        items = query_failure_prevention(
            per_db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: central_db,
            project_id_fn=lambda: "proj-c",
        )
        self.assertTrue(any("Central AP" in i.title for i in items))
        per_db.close()
        central_db.close()

    def test_returns_both_antipatterns_and_rules(self):
        db = _make_db()
        _seed_antipattern(db, title="AP item")
        _seed_rule(db, description="Rule item", tag_combination=None)
        items = query_failure_prevention(
            db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: None,
        )
        classes = {i.item_class for i in items}
        self.assertIn("failure_prevention", classes)
        db.close()
