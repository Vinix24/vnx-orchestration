#!/usr/bin/env python3
"""Tests for intelligence_sources/recent_comparable.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_sources.recent_comparable import (
    _query_central,
    _query_per_project,
    _row_to_intelligence_item,
    query_recent_comparable,
)
from intelligence_sources._common import PATTERN_CATEGORY_PROCESS, RECENT_COMPARABLE_DAYS


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT,
            outcome_status TEXT, dispatched_at DATETIME,
            pattern_count INTEGER DEFAULT 0,
            prevention_rule_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    return conn


def _seed_dispatch(conn, dispatch_id="d-001", skill_name="architect",
                   outcome="success", days_ago=1, gate=""):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    conn.execute(
        """INSERT INTO dispatch_metadata
           (dispatch_id, skill_name, gate, outcome_status, dispatched_at,
            pattern_count, prevention_rule_count)
           VALUES (?, ?, ?, ?, ?, 2, 1)""",
        (dispatch_id, skill_name, gate, outcome, ts),
    )
    conn.commit()


def _no_column(table, col):
    return False


class TestRowToIntelligenceItem(unittest.TestCase):
    def _make_row(self, dispatch_id="d-001", skill_name="architect",
                  gate="", track=None, outcome_status="success",
                  pattern_count=2, prevention_rule_count=1):
        ts = datetime.now(timezone.utc).isoformat()
        return {
            "dispatch_id": dispatch_id,
            "skill_name": skill_name,
            "gate": gate,
            "track": track,
            "outcome_status": outcome_status,
            "dispatched_at": ts,
            "pattern_count": pattern_count,
            "prevention_rule_count": prevention_rule_count,
        }

    def test_returns_intelligence_item(self):
        row = self._make_row()
        item = _row_to_intelligence_item(row, [])
        self.assertIsNotNone(item)
        self.assertEqual(item.item_class, "recent_comparable")

    def test_success_gets_higher_confidence(self):
        success_row = self._make_row(outcome_status="success")
        failure_row = self._make_row(outcome_status="failed")
        success_item = _row_to_intelligence_item(success_row, [])
        failure_item = _row_to_intelligence_item(failure_row, [])
        self.assertGreater(success_item.confidence, failure_item.confidence)

    def test_scope_filter_no_match(self):
        row = self._make_row(skill_name="reviewer")
        item = _row_to_intelligence_item(row, ["architect"])
        self.assertIsNone(item)

    def test_scope_filter_match_by_skill(self):
        row = self._make_row(skill_name="architect")
        item = _row_to_intelligence_item(row, ["architect"])
        self.assertIsNotNone(item)

    def test_pattern_category_is_process(self):
        row = self._make_row()
        item = _row_to_intelligence_item(row, [])
        self.assertEqual(item.pattern_category, PATTERN_CATEGORY_PROCESS)

    def test_content_includes_outcome(self):
        row = self._make_row(outcome_status="success")
        item = _row_to_intelligence_item(row, [])
        self.assertIn("success", item.content)

    def test_dispatch_id_in_source_refs(self):
        row = self._make_row(dispatch_id="d-abc-123")
        item = _row_to_intelligence_item(row, [])
        self.assertIn("d-abc-123", item.source_refs)

    def test_track_added_to_scope_tags(self):
        row = self._make_row(track="A")
        item = _row_to_intelligence_item(row, [])
        self.assertIn("Track-A", item.scope_tags)


class TestQueryPerProject(unittest.TestCase):
    def test_empty_db_returns_empty(self):
        db = _make_db()
        items = _query_per_project(db, "coding_interactive", [], _no_column)
        self.assertEqual(items, [])
        db.close()

    def test_recent_dispatch_returned(self):
        db = _make_db()
        _seed_dispatch(db, dispatch_id="d-recent", days_ago=1)
        items = _query_per_project(db, "coding_interactive", [], _no_column)
        self.assertEqual(len(items), 1)
        db.close()

    def test_old_dispatch_excluded(self):
        db = _make_db()
        _seed_dispatch(db, dispatch_id="d-old", days_ago=RECENT_COMPARABLE_DAYS + 5)
        items = _query_per_project(db, "coding_interactive", [], _no_column)
        self.assertEqual(items, [])
        db.close()

    def test_pending_status_excluded(self):
        db = _make_db()
        ts = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO dispatch_metadata (dispatch_id, skill_name, dispatched_at)
               VALUES ('d-pending', 'architect', ?)""",
            (ts,),
        )
        db.commit()
        items = _query_per_project(db, "coding_interactive", [], _no_column)
        self.assertEqual(items, [])
        db.close()

    def test_scope_filter_applied(self):
        db = _make_db()
        _seed_dispatch(db, skill_name="reviewer")
        items = _query_per_project(db, "coding_interactive", ["architect"], _no_column)
        self.assertEqual(items, [])
        db.close()


class TestQueryRecentComparable(unittest.TestCase):
    def setUp(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def tearDown(self):
        os.environ.pop("VNX_USE_CENTRAL_DB", None)

    def test_unset_env_uses_per_project(self):
        db = _make_db()
        _seed_dispatch(db, dispatch_id="d-pp", skill_name="architect")
        items = query_recent_comparable(
            db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: None,
        )
        ids = [i.source_refs[0] for i in items if i.source_refs]
        self.assertIn("d-pp", ids)
        db.close()

    def test_central_env_returns_central(self):
        os.environ["VNX_USE_CENTRAL_DB"] = "1"
        central_db = sqlite3.connect(":memory:")
        central_db.row_factory = sqlite3.Row
        ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        central_db.executescript(f"""
            CREATE TABLE dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
                role TEXT, skill_name TEXT, gate TEXT,
                outcome_status TEXT, dispatched_at DATETIME,
                pattern_count INTEGER DEFAULT 0, prevention_rule_count INTEGER DEFAULT 0,
                project_id TEXT
            );
            INSERT INTO dispatch_metadata
               (dispatch_id, skill_name, gate, outcome_status, dispatched_at, project_id)
               VALUES ('central-d-001', 'architect', '', 'success', '{ts}', 'proj-r');
        """)
        central_db.commit()
        per_db = _make_db()
        items = query_recent_comparable(
            per_db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: central_db,
            project_id_fn=lambda: "proj-r",
        )
        ids = [i.source_refs[0] for i in items if i.source_refs]
        self.assertIn("central-d-001", ids)
        per_db.close()
        central_db.close()

    def test_shadow_env_returns_per_project(self):
        os.environ["VNX_USE_CENTRAL_DB"] = "shadow"
        db = _make_db()
        _seed_dispatch(db, dispatch_id="d-shadow-pp")
        items = query_recent_comparable(
            db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: None,
        )
        ids = [i.source_refs[0] for i in items if i.source_refs]
        self.assertIn("d-shadow-pp", ids)
        db.close()

    def test_no_dispatch_returns_empty(self):
        db = _make_db()
        items = query_recent_comparable(
            db, "coding_interactive", [],
            has_column_fn=_no_column,
            central_conn_fn=lambda: None,
        )
        self.assertEqual(items, [])
        db.close()
