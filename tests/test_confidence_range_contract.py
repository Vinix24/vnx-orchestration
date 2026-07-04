#!/usr/bin/env python3
"""Pin the confidence range contract introduced in D1 (2026-07-04).

Contract:
  pattern_usage.confidence  — unclamped accumulator (up to 2.0 via learning_loop).
                              Readers see the raw value; no clamping in readers.
  success_patterns.confidence_score — clamped to [0.0, 1.0] at the reconcile
                                      write boundary (confidence_reconcile.py).

Three aspects are pinned here:
  1. Accumulator stays unclamped in pattern_usage after reconcile.
  2. confidence_score is clamped ≤ 1.0 regardless of accumulator magnitude.
  3. Subprocess-lane delta path (_update_pattern_confidence) still updates
     confidence_score via the fixed +0.05/-0.10 delta (not retired).

Additionally: recommendation_aggregator declining-set and confidence_reconcile
legacy fallback are behaviour-preserved (no silent change in reader semantics).
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
VNX_ROOT = TESTS_DIR.parent
SCRIPTS_LIB = VNX_ROOT / "scripts" / "lib"
SCRIPTS_DIR = VNX_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(SCRIPTS_DIR))

from confidence_reconcile import (  # noqa: E402
    SUCCESS_PATTERN_PREFIX,
    reconcile_pattern_confidence,
)
from recommendation_aggregator import _read_confidence_trends  # noqa: E402
from subprocess_dispatch_internals.pattern_confidence import (  # noqa: E402
    _update_pattern_confidence,
)


# ---------------------------------------------------------------------------
# Shared schema helpers
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS success_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    description TEXT,
    category TEXT,
    pattern_type TEXT,
    pattern_data TEXT,
    confidence_score REAL DEFAULT 0.5,
    usage_count INTEGER DEFAULT 0,
    source_dispatch_ids TEXT,
    first_seen TEXT,
    last_used TEXT,
    valid_from DATETIME DEFAULT NULL,
    valid_until DATETIME DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS pattern_usage (
    pattern_id TEXT PRIMARY KEY,
    pattern_title TEXT NOT NULL,
    pattern_hash TEXT NOT NULL,
    used_count INTEGER DEFAULT 0,
    ignored_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    last_used TEXT,
    last_offered TEXT,
    confidence REAL DEFAULT 1.0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    dispatch_id TEXT
);
CREATE TABLE IF NOT EXISTS dispatch_pattern_offered (
    dispatch_id   TEXT NOT NULL,
    pattern_id    TEXT NOT NULL,
    pattern_title TEXT NOT NULL,
    offered_at    TEXT NOT NULL,
    PRIMARY KEY (dispatch_id, pattern_id)
);
"""


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _seed_success_pattern(conn: sqlite3.Connection, title: str,
                          confidence: float = 0.5) -> int:
    cur = conn.execute(
        "INSERT INTO success_patterns (title, description, category, "
        "pattern_type, pattern_data, confidence_score) VALUES (?,?,?,?,?,?)",
        (title, title, "test", "approach", "{}", confidence),
    )
    conn.commit()
    return cur.lastrowid


def _seed_pattern_usage(conn: sqlite3.Connection, sp_id: int, *,
                        confidence: float = 1.0,
                        used_count: int = 0,
                        success_count: int = 0,
                        failure_count: int = 0) -> str:
    pid = f"{SUCCESS_PATTERN_PREFIX}{sp_id}"
    conn.execute(
        "INSERT INTO pattern_usage "
        "(pattern_id, pattern_title, pattern_hash, confidence, "
        " used_count, success_count, failure_count) "
        "VALUES (?,?,?,?,?,?,?)",
        (pid, f"title-{sp_id}", f"hash-{sp_id}", confidence,
         used_count, success_count, failure_count),
    )
    conn.commit()
    return pid


def _read_score(conn: sqlite3.Connection, sp_id: int) -> float:
    row = conn.execute(
        "SELECT confidence_score FROM success_patterns WHERE id = ?",
        (sp_id,),
    ).fetchone()
    return float(row[0])


def _read_accumulator(conn: sqlite3.Connection, pattern_id: str) -> float:
    row = conn.execute(
        "SELECT confidence FROM pattern_usage WHERE pattern_id = ?",
        (pattern_id,),
    ).fetchone()
    return float(row[0])


# ---------------------------------------------------------------------------
# 1. Unclamped accumulator — stays >1.0 in pattern_usage after reconcile
# ---------------------------------------------------------------------------

class TestAccumulatorStaysUnclamped(unittest.TestCase):
    """pattern_usage.confidence is NOT modified by reconcile_pattern_confidence."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Path(self.tmp.name)
        self.conn = _make_db(self.db)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_accumulator_above_1_preserved_after_reconcile(self):
        """A learning_loop-boosted accumulator (1.5) stays 1.5 post-reconcile."""
        sp_id = _seed_success_pattern(self.conn, "boosted", confidence=0.8)
        pid = _seed_pattern_usage(
            self.conn, sp_id,
            confidence=1.5,       # above 1.0 — learning_loop can push to 2.0
            used_count=3,
            success_count=0,
            failure_count=0,
        )
        reconcile_pattern_confidence(self.db)
        # The raw accumulator in pattern_usage must not be overwritten.
        acc = _read_accumulator(self.conn, pid)
        self.assertAlmostEqual(acc, 1.5, places=6,
            msg="reconcile must not overwrite pattern_usage.confidence")

    def test_accumulator_at_2_preserved_after_reconcile(self):
        """Maximum learning_loop boost (2.0) also preserved."""
        sp_id = _seed_success_pattern(self.conn, "max-boost", confidence=0.5)
        pid = _seed_pattern_usage(
            self.conn, sp_id,
            confidence=2.0,
            used_count=1,
            success_count=0,
            failure_count=0,
        )
        reconcile_pattern_confidence(self.db)
        acc = _read_accumulator(self.conn, pid)
        self.assertAlmostEqual(acc, 2.0, places=6)


# ---------------------------------------------------------------------------
# 2. confidence_score clamped ≤ 1.0 at write boundary
# ---------------------------------------------------------------------------

class TestConfidenceScoreClamped(unittest.TestCase):
    """success_patterns.confidence_score is always in [0.0, 1.0] after reconcile."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Path(self.tmp.name)
        self.conn = _make_db(self.db)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_legacy_fallback_clamps_boosted_accumulator(self):
        """Legacy path (used_count>0, no s/f counts): conf=1.8 → score=1.0."""
        sp_id = _seed_success_pattern(self.conn, "legacy-high", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            confidence=1.8,   # above 1.0
            used_count=5,
            success_count=0,
            failure_count=0,
        )
        reconcile_pattern_confidence(self.db)
        score = _read_score(self.conn, sp_id)
        self.assertLessEqual(score, 1.0,
            msg="legacy fallback must clamp confidence_score ≤ 1.0")
        self.assertAlmostEqual(score, 1.0, places=6,
            msg="confidence=1.8 should clamp to exactly 1.0")

    def test_beta_path_always_leq_1(self):
        """Beta-Laplace score with all successes is < 1.0 (can't reach 1.0 via beta)."""
        sp_id = _seed_success_pattern(self.conn, "beta-high", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=100,
            success_count=100,
            failure_count=0,
        )
        reconcile_pattern_confidence(self.db)
        score = _read_score(self.conn, sp_id)
        # (100+1)/(100+0+2) = 101/102 ≈ 0.990
        self.assertLessEqual(score, 1.0)
        self.assertGreater(score, 0.9)

    def test_score_never_negative(self):
        """All-failure pattern stays at 0.0 floor, never negative."""
        sp_id = _seed_success_pattern(self.conn, "all-fail", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=10,
            success_count=0,
            failure_count=10,
        )
        reconcile_pattern_confidence(self.db)
        score = _read_score(self.conn, sp_id)
        self.assertGreaterEqual(score, 0.0)


# ---------------------------------------------------------------------------
# 3. Subprocess-lane delta path — still fires and updates confidence_score
# ---------------------------------------------------------------------------

class TestSubprocessDeltaPath(unittest.TestCase):
    """_update_pattern_confidence applies the fixed delta for subprocess lane."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Path(self.tmp.name)
        self.conn = _make_db(self.db)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_success_boost_via_delta(self):
        """Success dispatch boosts confidence_score by +0.05."""
        sp_id = _seed_success_pattern(self.conn, "pattern-delta-s", confidence=0.70)
        _seed_pattern_usage(self.conn, sp_id, confidence=0.70, used_count=1)
        self.conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at) VALUES (?,?,?,datetime('now'))",
            ("d-delta-1", f"{SUCCESS_PATTERN_PREFIX}{sp_id}", "pattern-delta-s"),
        )
        self.conn.commit()

        updated = _update_pattern_confidence("d-delta-1", "success", self.db)
        self.assertEqual(updated, 1, msg="should update exactly 1 pattern")
        score = _read_score(self.conn, sp_id)
        self.assertAlmostEqual(score, 0.75, places=6,
            msg="0.70 + 0.05 = 0.75")

    def test_failure_decay_via_delta(self):
        """Failure dispatch decays confidence_score by -0.10."""
        sp_id = _seed_success_pattern(self.conn, "pattern-delta-f", confidence=0.50)
        _seed_pattern_usage(self.conn, sp_id, confidence=0.50, used_count=1)
        self.conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at) VALUES (?,?,?,datetime('now'))",
            ("d-delta-2", f"{SUCCESS_PATTERN_PREFIX}{sp_id}", "pattern-delta-f"),
        )
        self.conn.commit()

        updated = _update_pattern_confidence("d-delta-2", "failure", self.db)
        self.assertEqual(updated, 1)
        score = _read_score(self.conn, sp_id)
        self.assertAlmostEqual(score, 0.40, places=6,
            msg="0.50 - 0.10 = 0.40")

    def test_delta_caps_at_1(self):
        """Delta boost caps at 1.0, never exceeds it."""
        sp_id = _seed_success_pattern(self.conn, "cap-test", confidence=0.98)
        _seed_pattern_usage(self.conn, sp_id, confidence=0.98, used_count=1)
        self.conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at) VALUES (?,?,?,datetime('now'))",
            ("d-cap-1", f"{SUCCESS_PATTERN_PREFIX}{sp_id}", "cap-test"),
        )
        self.conn.commit()

        _update_pattern_confidence("d-cap-1", "success", self.db)
        score = _read_score(self.conn, sp_id)
        self.assertLessEqual(score, 1.0,
            msg="delta boost must cap at 1.0")
        self.assertAlmostEqual(score, 1.0, places=6)

    def test_delta_floors_at_0(self):
        """Delta decay floors at 0.0, never goes negative."""
        sp_id = _seed_success_pattern(self.conn, "floor-test", confidence=0.05)
        _seed_pattern_usage(self.conn, sp_id, confidence=0.05, used_count=1)
        self.conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at) VALUES (?,?,?,datetime('now'))",
            ("d-floor-1", f"{SUCCESS_PATTERN_PREFIX}{sp_id}", "floor-test"),
        )
        self.conn.commit()

        _update_pattern_confidence("d-floor-1", "failure", self.db)
        score = _read_score(self.conn, sp_id)
        self.assertGreaterEqual(score, 0.0,
            msg="delta decay must floor at 0.0")

    def test_delta_does_not_touch_success_failure_counts(self):
        """Delta path must NOT increment success_count or failure_count."""
        sp_id = _seed_success_pattern(self.conn, "counts-test", confidence=0.60)
        _seed_pattern_usage(
            self.conn, sp_id,
            confidence=0.60,
            used_count=1,
            success_count=0,
            failure_count=0,
        )
        pid = f"{SUCCESS_PATTERN_PREFIX}{sp_id}"
        self.conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at) VALUES (?,?,?,datetime('now'))",
            ("d-counts-1", pid, "counts-test"),
        )
        self.conn.commit()

        _update_pattern_confidence("d-counts-1", "success", self.db)

        row = self.conn.execute(
            "SELECT success_count, failure_count, used_count "
            "FROM pattern_usage WHERE pattern_id = ?",
            (pid,),
        ).fetchone()
        self.assertEqual(row[0], 0, msg="success_count must not be incremented by delta path")
        self.assertEqual(row[1], 0, msg="failure_count must not be incremented by delta path")
        self.assertEqual(row[2], 1, msg="used_count must not be changed by delta path")


# ---------------------------------------------------------------------------
# 4. Recommendation aggregator declining-set — behaviour preserved
# ---------------------------------------------------------------------------

class TestRecommendationAggregatorDecliningSet(unittest.TestCase):
    """_read_confidence_trends returns rows with raw confidence < 0.95 only."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.state_dir = Path(self.tmp_dir)
        self.db = self.state_dir / "quality_intelligence.db"
        conn = sqlite3.connect(str(self.db))
        conn.executescript(
            """
            CREATE TABLE pattern_usage (
                pattern_id TEXT PRIMARY KEY,
                pattern_title TEXT,
                confidence REAL DEFAULT 1.0,
                failure_count INTEGER DEFAULT 0,
                used_count INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, confidence, "
            "failure_count, used_count) VALUES (?,?,?,?,?)",
            ("p-high", "high-confidence", 1.5, 0, 3),  # above 0.95, above 1.0
        )
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, confidence, "
            "failure_count, used_count) VALUES (?,?,?,?,?)",
            ("p-mid", "mid-confidence", 0.80, 2, 5),  # below 0.95 — declining
        )
        conn.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, confidence, "
            "failure_count, used_count) VALUES (?,?,?,?,?)",
            ("p-low", "low-confidence", 0.30, 5, 5),  # well below 0.95 — declining
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_boosted_accumulator_not_in_declining_set(self):
        """Pattern with confidence > 1.0 is excluded from declining-set (>= 0.95)."""
        trends = _read_confidence_trends(self.state_dir)
        pids = [t["pattern_id"] for t in trends]
        self.assertNotIn("p-high", pids,
            msg="confidence=1.5 must not appear in declining-set (< 0.95 filter)")

    def test_declining_patterns_returned(self):
        """Patterns with confidence < 0.95 do appear in declining-set."""
        trends = _read_confidence_trends(self.state_dir)
        pids = [t["pattern_id"] for t in trends]
        self.assertIn("p-mid", pids)
        self.assertIn("p-low", pids)

    def test_declining_set_ordered_ascending(self):
        """declining-set is ordered by raw confidence ASC (lowest first)."""
        trends = _read_confidence_trends(self.state_dir)
        confs = [float(t["confidence"]) for t in trends]
        self.assertEqual(confs, sorted(confs),
            msg="declining-set must be ordered by confidence ASC")

    def test_raw_value_preserved_in_declining_set(self):
        """The raw accumulator value (not clamped) is what the reader sees."""
        trends = _read_confidence_trends(self.state_dir)
        by_id = {t["pattern_id"]: float(t["confidence"]) for t in trends}
        # p-mid has confidence = 0.80 — reader must see 0.80, not a clamped version
        self.assertAlmostEqual(by_id.get("p-mid", -1), 0.80, places=6)


# ---------------------------------------------------------------------------
# 5. Reconcile legacy fallback — behaviour preserved
# ---------------------------------------------------------------------------

class TestLegacyFallbackBehaviourPreserved(unittest.TestCase):
    """Reconcile legacy fallback branch: used_count>0, no s/f counts."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Path(self.tmp.name)
        self.conn = _make_db(self.db)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_legacy_fallback_reads_confidence_and_clamps(self):
        """Legacy rows (used_count>0, succ=fail=0) use confidence as proxy, clamped."""
        sp_id = _seed_success_pattern(self.conn, "legacy-a", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            confidence=0.72,
            used_count=3,
            success_count=0,
            failure_count=0,
        )
        reconcile_pattern_confidence(self.db)
        score = _read_score(self.conn, sp_id)
        self.assertAlmostEqual(score, 0.72, places=4,
            msg="legacy fallback: confidence=0.72 maps to score=0.72")

    def test_legacy_fallback_zero_used_count_skipped(self):
        """Legacy rows with used_count=0 are skipped — existing score preserved."""
        sp_id = _seed_success_pattern(self.conn, "legacy-b", confidence=0.42)
        _seed_pattern_usage(
            self.conn, sp_id,
            confidence=0.99,
            used_count=0,
            success_count=0,
            failure_count=0,
        )
        updated = reconcile_pattern_confidence(self.db)
        self.assertEqual(updated, 0)
        self.assertAlmostEqual(_read_score(self.conn, sp_id), 0.42, places=6)


if __name__ == "__main__":
    unittest.main()
