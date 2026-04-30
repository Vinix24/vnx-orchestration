#!/usr/bin/env python3
"""Tests for pattern injection diversity + content dedup (B2 dispatch).

Covers:
  A. Dedup collapses byte-identical success_patterns to a single canonical row
  B. Selector prefers code patterns over governance for coding_interactive
  C. Backfill heuristic classifies "gate ... passed" rows as governance
  D. Selector never returns two byte-identical patterns in one batch
  E. Dedup preserves usage_count via aggregation onto the canonical row
  F. Dedup is idempotent — re-running produces no further changes
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
    PATTERN_CATEGORY_CODE,
    PATTERN_CATEGORY_GOVERNANCE,
    IntelligenceSelector,
    classify_pattern_category,
)
from pattern_dedup import (  # noqa: E402
    backfill_pattern_category,
    classify_pattern,
    content_hash,
    dedup_success_patterns,
    ensure_pattern_category_columns,
    main as pattern_dedup_main,
    normalize_content,
)


# ---------------------------------------------------------------------------
# Schema fixture mirroring the production quality_intelligence schema
# ---------------------------------------------------------------------------

def _create_quality_db(path: Path) -> None:
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
    conn.close()


def _seed_pattern(
    db_path: Path,
    title: str,
    description: str,
    *,
    category: str = "backend-developer",
    confidence: float = 0.85,
    usage_count: int = 3,
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
    conn.commit()
    pid = cur.lastrowid
    conn.execute(
        """
        INSERT INTO pattern_usage
            (pattern_id, pattern_title, pattern_hash, used_count, confidence)
        VALUES (?, ?, ?, ?, ?)
        """,
        (f"intel_sp_{pid}", title[:255], "hash_placeholder", usage_count, confidence),
    )
    conn.commit()
    conn.close()
    return pid


# ---------------------------------------------------------------------------
# Pure-function tests (no DB)
# ---------------------------------------------------------------------------

class TestNormalization(unittest.TestCase):
    def test_normalize_lowercases_and_collapses_whitespace(self):
        self.assertEqual(normalize_content("  Gate   X  Passed  "), "gate x passed")

    def test_normalize_handles_none(self):
        self.assertEqual(normalize_content(None), "")

    def test_content_hash_stable(self):
        self.assertEqual(
            content_hash("Hello", "World"),
            content_hash("hello", "world"),
        )

    def test_content_hash_differs_for_different_text(self):
        self.assertNotEqual(content_hash("foo"), content_hash("bar"))


class TestClassification(unittest.TestCase):
    def test_governance_gate_pass_pattern(self):
        self.assertEqual(
            classify_pattern("gate gate_pr0_input_ready_contract passed", ""),
            "governance",
        )

    def test_code_pattern_default(self):
        self.assertEqual(
            classify_pattern("Use structured output", "Improves first-pass success"),
            "code",
        )

    def test_process_pattern_receipt_processor(self):
        self.assertEqual(
            classify_pattern("Receipt processor handler", "receipt processor flow"),
            "process",
        )

    def test_selector_classify_matches_dedup_classify(self):
        title = "gate gate_x passed"
        self.assertEqual(
            classify_pattern_category(title, ""),
            classify_pattern(title, ""),
        )


# ---------------------------------------------------------------------------
# Case A: dedup collapses byte-identical patterns
# ---------------------------------------------------------------------------

class TestDedupCollapses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_five_identical_patterns_collapse_to_one(self):
        title = "gate gate_pr0_input_ready_contract passed"
        desc = "gate gate_pr0_input_ready_contract passed"
        for _ in range(5):
            _seed_pattern(self.db_path, title, desc, usage_count=1)

        report = dedup_success_patterns(self.db_path, apply=True)
        self.assertEqual(len(report), 1, f"expected 1 dedup group, got {report}")
        # 5 members -> 4 collapsed (canonical kept)
        self.assertEqual(sum(report.values()), 4)

        with sqlite3.connect(str(self.db_path)) as conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM success_patterns"
            ).fetchone()
        self.assertEqual(count, 1)

    def test_dry_run_does_not_mutate(self):
        title = "duplicate title"
        desc = "duplicate description"
        for _ in range(3):
            _seed_pattern(self.db_path, title, desc)

        report = dedup_success_patterns(self.db_path, apply=False)
        self.assertEqual(sum(report.values()), 2)

        with sqlite3.connect(str(self.db_path)) as conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM success_patterns"
            ).fetchone()
        self.assertEqual(count, 3, "dry-run should preserve all rows")


# ---------------------------------------------------------------------------
# Case B: selector prefers code over governance
# ---------------------------------------------------------------------------

class TestSelectorPrefersCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)
        with sqlite3.connect(str(self.db_path)) as conn:
            ensure_pattern_category_columns(conn)

    def tearDown(self):
        self.tmp.cleanup()

    def test_code_pattern_wins_over_governance_for_coding(self):
        # Same raw confidence, but governance gets penalty in coding context.
        gov_id = _seed_pattern(
            self.db_path,
            "gate gate_pr0_input_ready_contract passed",
            "gate gate_pr0_input_ready_contract passed",
            confidence=0.9,
            usage_count=10,
        )
        code_id = _seed_pattern(
            self.db_path,
            "Use structured output",
            "Structured output improves first-pass success by 25%.",
            confidence=0.85,
            usage_count=5,
        )

        with sqlite3.connect(str(self.db_path)) as conn:
            backfill_pattern_category(conn)

        selector = IntelligenceSelector(quality_db_path=self.db_path)
        try:
            result = selector.select(
                dispatch_id="test-coding",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
        finally:
            selector.close()

        proven = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(proven), 1)
        self.assertEqual(
            proven[0].item_id,
            f"intel_sp_{code_id}",
            "code pattern should win over governance for coding_interactive",
        )
        # Sanity: the governance pattern still exists, just was demoted
        self.assertIn(gov_id, [int(row[0]) for row in
                                sqlite3.connect(str(self.db_path)).execute(
                                    "SELECT id FROM success_patterns").fetchall()])


# ---------------------------------------------------------------------------
# Case C: backfill correctness
# ---------------------------------------------------------------------------

class TestBackfillCategory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_gate_passed_classified_as_governance(self):
        _seed_pattern(
            self.db_path,
            "gate gate_pr0_input_ready_contract passed",
            "gate gate_pr0_input_ready_contract passed",
        )
        _seed_pattern(
            self.db_path,
            "Use structured output",
            "Improves first-pass success by 25%.",
        )

        with sqlite3.connect(str(self.db_path)) as conn:
            ensure_pattern_category_columns(conn)
            backfill_pattern_category(conn)
            rows = conn.execute(
                "SELECT title, pattern_category FROM success_patterns ORDER BY id"
            ).fetchall()

        self.assertEqual(rows[0][1], "governance")
        self.assertEqual(rows[1][1], "code")


# ---------------------------------------------------------------------------
# Case D: selector enforces no byte-identical content in batch
# ---------------------------------------------------------------------------

class TestSelectorBatchDiversity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)
        with sqlite3.connect(str(self.db_path)) as conn:
            ensure_pattern_category_columns(conn)

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_content_collapses_in_selector(self):
        # Three identical proven_patterns + a recent_comparable that happens to
        # share the same content_hash. Even without dedup applied, the selector
        # must never emit two items with the same content_hash.
        identical_title = "Use a structured output schema"
        identical_desc = "Define a JSON schema and validate model output."
        for _ in range(3):
            _seed_pattern(
                self.db_path,
                identical_title,
                identical_desc,
                confidence=0.9,
                usage_count=4,
            )

        selector = IntelligenceSelector(quality_db_path=self.db_path)
        try:
            result = selector.select(
                dispatch_id="test-diverse",
                injection_point="dispatch_create",
                skill_name="backend-developer",
            )
        finally:
            selector.close()

        hashes = [i.content_hash for i in result.items if i.content_hash]
        self.assertEqual(len(hashes), len(set(hashes)),
                         "no two emitted items may share a content_hash")

        proven = [i for i in result.items if i.item_class == "proven_pattern"]
        self.assertEqual(len(proven), 1, "duplicates must collapse to one slot")


# ---------------------------------------------------------------------------
# Case E: dedup preserves usage_count aggregation
# ---------------------------------------------------------------------------

class TestDedupPreservesUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_usage_counts_aggregate_onto_canonical(self):
        title = "Repeated pattern"
        desc = "Same description"
        ids = [
            _seed_pattern(self.db_path, title, desc, usage_count=2),
            _seed_pattern(self.db_path, title, desc, usage_count=3),
            _seed_pattern(self.db_path, title, desc, usage_count=4),
        ]
        canonical = min(ids)

        dedup_success_patterns(self.db_path, apply=True)

        with sqlite3.connect(str(self.db_path)) as conn:
            (rowcount,) = conn.execute(
                "SELECT COUNT(*) FROM success_patterns"
            ).fetchone()
            (usage_count,) = conn.execute(
                "SELECT usage_count FROM success_patterns WHERE id = ?",
                (canonical,),
            ).fetchone()
            (pu_used,) = conn.execute(
                "SELECT used_count FROM pattern_usage WHERE pattern_id = ?",
                (f"intel_sp_{canonical}",),
            ).fetchone()
            (pu_count,) = conn.execute(
                "SELECT COUNT(*) FROM pattern_usage"
            ).fetchone()

        self.assertEqual(rowcount, 1)
        self.assertEqual(usage_count, 9, "aggregated usage_count must match sum")
        self.assertEqual(pu_used, 9, "pattern_usage rows must merge counters")
        self.assertEqual(pu_count, 1)


# ---------------------------------------------------------------------------
# Case F: idempotency
# ---------------------------------------------------------------------------

class TestDedupIdempotent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "qi.db"
        _create_quality_db(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rerun_is_noop(self):
        title = "Idempotent pattern"
        desc = "Description"
        for _ in range(3):
            _seed_pattern(self.db_path, title, desc)

        first = dedup_success_patterns(self.db_path, apply=True)
        second = dedup_success_patterns(self.db_path, apply=True)

        self.assertEqual(sum(first.values()), 2)
        self.assertEqual(second, {}, "second run must report no duplicates")

    def test_cli_dry_run_then_apply(self):
        title = "CLI pattern"
        desc = "CLI description"
        for _ in range(2):
            _seed_pattern(self.db_path, title, desc)

        rc = pattern_dedup_main(["--db", str(self.db_path), "--dry-run"])
        self.assertEqual(rc, 0)

        with sqlite3.connect(str(self.db_path)) as conn:
            (count_before,) = conn.execute(
                "SELECT COUNT(*) FROM success_patterns"
            ).fetchone()
        self.assertEqual(count_before, 2, "dry-run preserves rows")

        rc = pattern_dedup_main(["--db", str(self.db_path), "--apply"])
        self.assertEqual(rc, 0)

        with sqlite3.connect(str(self.db_path)) as conn:
            (count_after,) = conn.execute(
                "SELECT COUNT(*) FROM success_patterns"
            ).fetchone()
        self.assertEqual(count_after, 1)


if __name__ == "__main__":
    unittest.main()
