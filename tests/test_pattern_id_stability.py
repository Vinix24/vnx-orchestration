#!/usr/bin/env python3
"""Tests for content-addressable pattern_id stability (CFX-5).

Background (claudedocs/2026-04-30-intelligence-system-audit.md, OI-CFX-5):
  The intelligence selector previously emitted ``intel_sp_<row_id>`` as the
  pattern_id offered to dispatches. When the same logical pattern lived
  under multiple ``success_patterns`` rows, each row produced its own
  pattern_id and pattern_usage exploded into N rows for one underlying
  pattern. Migration 0012 introduces a ``content_hash`` column; the
  selector now resolves the *canonical* (smallest-id) row sharing a
  content_hash so duplicates collapse onto one pattern_id without breaking
  legacy callers that referenced the original ``intel_sp_<id>`` shape.

Cases covered:
  A. Same content injected to two dispatches → same pattern_id reused
  B. Different content → distinct pattern_ids
  C. pattern_usage rows aggregate across injections (counters, not rows)
  D. Migration 0012 is idempotent (apply column + backfill twice = no-op)
  E. Backfill correctness — content_hash populated for legacy rows
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from intelligence_selector import (  # noqa: E402
    IntelligenceSelector,
    _short_content_hash,
)
from pattern_dedup import (  # noqa: E402
    CONTENT_HASH_PREFIX_LEN,
    _column_exists,
    backfill_content_hash,
    ensure_content_hash_column,
    ensure_pattern_category_columns,
)


# ---------------------------------------------------------------------------
# Schema fixture mirroring the production quality_intelligence schema
# ---------------------------------------------------------------------------

def _create_quality_db(path: Path, *, with_migration_0012: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, code_example TEXT, prerequisites TEXT, outcomes TEXT,
            success_rate REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
            avg_completion_time INTEGER, confidence_score REAL DEFAULT 0.0,
            source_dispatch_ids TEXT, source_receipts TEXT,
            first_seen DATETIME, last_used DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, problem_example TEXT, why_problematic TEXT,
            better_alternative TEXT, occurrence_count INTEGER DEFAULT 0,
            avg_resolution_time INTEGER, severity TEXT DEFAULT 'medium',
            source_dispatch_ids TEXT, first_seen DATETIME, last_seen DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE prevention_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_combination TEXT, rule_type TEXT, description TEXT,
            recommendation TEXT, confidence REAL DEFAULT 0.0,
            created_at TEXT, triggered_count INTEGER DEFAULT 0,
            last_triggered TEXT,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE dispatch_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT UNIQUE, terminal TEXT, track TEXT,
            role TEXT, skill_name TEXT, gate TEXT, cognition TEXT DEFAULT 'normal',
            priority TEXT DEFAULT 'P1', pr_id TEXT, parent_dispatch TEXT,
            pattern_count INTEGER DEFAULT 0, prevention_rule_count INTEGER DEFAULT 0,
            intelligence_json TEXT, instruction_char_count INTEGER DEFAULT 0,
            context_file_count INTEGER DEFAULT 0,
            dispatched_at DATETIME, completed_at DATETIME,
            outcome_status TEXT, outcome_report_path TEXT, session_id TEXT
        );
        CREATE TABLE pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            used_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_used TIMESTAMP,
            last_offered TIMESTAMP,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    if with_migration_0012:
        ensure_pattern_category_columns(conn)
        ensure_content_hash_column(conn)
    conn.close()


def _insert_pattern(
    db_path: Path,
    title: str,
    description: str,
    *,
    category: str = "backend-developer",
    confidence: float = 0.85,
    usage_count: int = 3,
    populate_hash: bool = True,
) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        """
        INSERT INTO success_patterns
            (title, description, category, confidence_score, usage_count,
             pattern_data, first_seen, last_used)
        VALUES (?, ?, ?, ?, ?, '{}', '2026-04-01T00:00:00Z', '2026-04-29T00:00:00Z')
        """,
        (title, description, category, confidence, usage_count),
    )
    pid = cur.lastrowid
    if populate_hash and _column_exists(conn, "success_patterns", "content_hash"):
        h = _short_content_hash(title, description)
        conn.execute(
            "UPDATE success_patterns SET content_hash = ? WHERE id = ?",
            (h, pid),
        )
    conn.commit()
    conn.close()
    return pid


# ---------------------------------------------------------------------------
# Case A: same content reuses pattern_id across injections
# ---------------------------------------------------------------------------

class TestSamePatternIdAcrossInjections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_rows_collapse_to_canonical_pattern_id(self):
        title = "Use a structured output schema"
        desc = "Define a JSON schema and validate model output."
        first_id = _insert_pattern(self.db_path, title, desc, confidence=0.9)
        second_id = _insert_pattern(self.db_path, title, desc, confidence=0.85)
        self.assertNotEqual(first_id, second_id,
                            "fixture must seed two distinct rows")

        selector = IntelligenceSelector(quality_db_path=self.db_path)
        try:
            result = selector.select(
                dispatch_id="dispatch-A",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
        finally:
            selector.close()

        proven = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(proven), 1, "diversity filter must collapse")
        # Canonical id is the smallest-id row sharing the content_hash.
        self.assertEqual(proven[0].item_id, f"intel_sp_{first_id}")

    def test_two_dispatches_get_same_pattern_id_for_same_content(self):
        title = "Cache external API responses"
        desc = "Memoize at the gateway layer to reduce vendor latency."
        canonical = _insert_pattern(self.db_path, title, desc)

        selector = IntelligenceSelector(quality_db_path=self.db_path)
        try:
            result_a = selector.select(
                dispatch_id="dispatch-A",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
            result_b = selector.select(
                dispatch_id="dispatch-B",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
        finally:
            selector.close()

        ids_a = [i.item_id for i in result_a.items if i.item_class == "proven_pattern"]
        ids_b = [i.item_id for i in result_b.items if i.item_class == "proven_pattern"]
        self.assertEqual(ids_a, [f"intel_sp_{canonical}"])
        self.assertEqual(ids_a, ids_b, "same content must yield same pattern_id")


# ---------------------------------------------------------------------------
# Case B: different content → distinct pattern_ids
# ---------------------------------------------------------------------------

class TestDistinctContentDistinctIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_distinct_content_yields_distinct_ids(self):
        a_id = _insert_pattern(
            self.db_path,
            "Use structured output",
            "Schema-validated model output reduces parsing errors.",
            confidence=0.9, usage_count=4,
        )
        b_id = _insert_pattern(
            self.db_path,
            "Cache external API responses",
            "Memoize at the gateway layer to reduce vendor latency.",
            confidence=0.88, usage_count=4,
        )

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT id, content_hash FROM success_patterns ORDER BY id"
            ).fetchall()
        hashes = {row[0]: row[1] for row in rows}
        self.assertEqual(len(set(hashes.values())), 2,
                         "distinct content must hash distinctly")

        selector = IntelligenceSelector(quality_db_path=self.db_path)
        try:
            result = selector.select(
                dispatch_id="dispatch-distinct",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
        finally:
            selector.close()

        ids = sorted(i.item_id for i in result.items if i.item_class == "proven_pattern")
        # Selection retains at most one proven_pattern slot per FP-C; the
        # winner must be one of the two seeded rows, and never an aggregate
        # phantom id like "intel_sp_<hash>".
        self.assertEqual(len(ids), 1)
        self.assertIn(ids[0], (f"intel_sp_{a_id}", f"intel_sp_{b_id}"))


# ---------------------------------------------------------------------------
# Case C: pattern_usage aggregation across injections
# ---------------------------------------------------------------------------

class TestPatternUsageAggregation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_dispatches_produce_one_pattern_usage_row(self):
        title = "Validate inputs at boundaries"
        desc = "Trust internal callers; check user-supplied data once at entry."
        canonical = _insert_pattern(self.db_path, title, desc)
        # Add a near-duplicate row to verify the aggregation works even when
        # the source DB has not yet been deduplicated.
        _insert_pattern(self.db_path, title, desc, confidence=0.7)

        selector = IntelligenceSelector(quality_db_path=self.db_path)
        try:
            for did in ("dispatch-1", "dispatch-2", "dispatch-3"):
                result = selector.select(
                    dispatch_id=did,
                    injection_point="dispatch_create",
                    skill_name="backend-developer",
                )
                # record_injection requires a coord_state_dir to write the
                # audit row, but the per-pattern usage upsert lives entirely
                # in quality_intelligence.db. Call the internal helper so the
                # test exercises the path that matters.
                selector._record_pattern_usage(result)
        finally:
            selector.close()

        with sqlite3.connect(str(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT pattern_id, last_offered FROM pattern_usage"
            ).fetchall()
        self.assertEqual(
            len(rows), 1,
            f"three injections of same pattern must produce one row, got {rows}",
        )
        self.assertEqual(rows[0][0], f"intel_sp_{canonical}")


# ---------------------------------------------------------------------------
# Case D: migration is idempotent
# ---------------------------------------------------------------------------

class TestMigrationIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        # Start without migration 0012 so we exercise the ADD COLUMN path.
        _create_quality_db(self.db_path, with_migration_0012=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_apply_then_apply_no_error(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            ensure_content_hash_column(conn)
            ensure_content_hash_column(conn)  # re-apply
            self.assertTrue(_column_exists(conn, "success_patterns", "content_hash"))

    def test_backfill_idempotent_and_short_hash(self):
        # Seed with the column missing so backfill must run.
        a = _insert_pattern(self.db_path, "Title A", "Desc A", populate_hash=False)
        b = _insert_pattern(self.db_path, "Title B", "Desc B", populate_hash=False)

        with sqlite3.connect(str(self.db_path)) as conn:
            first = backfill_content_hash(conn)
            second = backfill_content_hash(conn)
            rows = conn.execute(
                "SELECT id, content_hash FROM success_patterns ORDER BY id"
            ).fetchall()

        self.assertEqual(first, 2, "first backfill must hash both rows")
        self.assertEqual(second, 0, "second backfill must be a no-op")
        self.assertEqual({r[0] for r in rows}, {a, b})
        for _, h in rows:
            self.assertEqual(len(h), CONTENT_HASH_PREFIX_LEN)
            int(h, 16)  # raises if not hex

    def test_idempotent_index_creation(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            ensure_content_hash_column(conn)
            # Calling a second time must not error on the existing index.
            ensure_content_hash_column(conn)
            idx = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_success_patterns_content_hash'"
            ).fetchone()
        self.assertIsNotNone(idx)


# ---------------------------------------------------------------------------
# Case E: backfill correctness for legacy rows
# ---------------------------------------------------------------------------

class TestBackfillCorrectness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path, with_migration_0012=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backfill_matches_in_memory_hash(self):
        title = "Stream JSON parsing"
        desc = "Parse incrementally to bound memory on large payloads."
        pid = _insert_pattern(self.db_path, title, desc, populate_hash=False)

        with sqlite3.connect(str(self.db_path)) as conn:
            updated = backfill_content_hash(conn)
            (stored,) = conn.execute(
                "SELECT content_hash FROM success_patterns WHERE id = ?",
                (pid,),
            ).fetchone()

        self.assertEqual(updated, 1)
        self.assertEqual(stored, _short_content_hash(title, desc))

    def test_legacy_row_without_hash_falls_back_to_row_id(self):
        # Selector must still produce a valid item_id when the column is
        # absent (i.e. migration 0012 has not been applied yet).
        title = "Legacy pattern"
        desc = "Pre-migration row."
        pid = _insert_pattern(self.db_path, title, desc, populate_hash=False)

        selector = IntelligenceSelector(quality_db_path=self.db_path)
        try:
            result = selector.select(
                dispatch_id="legacy-dispatch",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
        finally:
            selector.close()

        proven = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(proven), 1)
        self.assertEqual(proven[0].item_id, f"intel_sp_{pid}")


if __name__ == "__main__":
    unittest.main()
