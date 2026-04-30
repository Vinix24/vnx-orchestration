#!/usr/bin/env python3
"""Tests for scripts/lib/confidence_reconcile.py.

Verifies that pattern_usage learning state is correctly synced into
success_patterns.confidence_score so intelligence_selector reads the
current Beta-Laplace posterior rather than a static initial value.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from confidence_reconcile import (  # noqa: E402
    RECONCILE_CACHE_TTL_SECONDS,
    SUCCESS_PATTERN_PREFIX,
    beta_score,
    maybe_reconcile,
    reconcile_pattern_confidence,
)


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT,
            confidence_score REAL DEFAULT 0.5,
            usage_count INTEGER DEFAULT 0,
            source_dispatch_ids TEXT,
            first_seen DATETIME, last_used DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT NOT NULL,
            pattern_hash TEXT NOT NULL,
            used_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            last_used TIMESTAMP, last_offered TIMESTAMP,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    return conn


def _seed_success_pattern(
    conn: sqlite3.Connection,
    title: str,
    confidence: float = 0.5,
    category: str = "test",
) -> int:
    cur = conn.execute(
        "INSERT INTO success_patterns "
        "(pattern_type, category, title, description, pattern_data, "
        " confidence_score) VALUES (?, ?, ?, ?, ?, ?)",
        ("approach", category, title, title, "{}", confidence),
    )
    conn.commit()
    return cur.lastrowid


def _seed_pattern_usage(
    conn: sqlite3.Connection,
    success_pattern_id: int,
    *,
    used_count: int = 0,
    success_count: int = 0,
    failure_count: int = 0,
    confidence: float = 1.0,
) -> str:
    pattern_id = f"{SUCCESS_PATTERN_PREFIX}{success_pattern_id}"
    conn.execute(
        "INSERT INTO pattern_usage "
        "(pattern_id, pattern_title, pattern_hash, used_count, "
        " success_count, failure_count, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pattern_id, f"title-{success_pattern_id}", f"hash-{success_pattern_id}",
         used_count, success_count, failure_count, confidence),
    )
    conn.commit()
    return pattern_id


def _read_score(conn: sqlite3.Connection, sp_id: int) -> float:
    row = conn.execute(
        "SELECT confidence_score FROM success_patterns WHERE id = ?",
        (sp_id,),
    ).fetchone()
    return float(row[0])


class TestBetaScore(unittest.TestCase):
    def test_neutral_prior(self):
        self.assertAlmostEqual(beta_score(0, 0), 0.5, places=6)

    def test_high_success(self):
        # 8 successes / 2 failures → (8+1) / (10+2) = 0.75
        self.assertAlmostEqual(beta_score(8, 2), 0.75, places=6)

    def test_high_failure(self):
        # 0 successes / 5 failures → 1/7 ≈ 0.143
        self.assertAlmostEqual(beta_score(0, 5), 1 / 7, places=6)


class TestReconcilePatternConfidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = Path(self.tmp.name)
        self.conn = _make_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_case_a_all_success_boosts_score(self):
        sp_id = _seed_success_pattern(self.conn, "A", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=5, success_count=5, failure_count=0,
        )
        updated = reconcile_pattern_confidence(self.db_path)
        self.assertEqual(updated, 1)
        score = _read_score(self.conn, sp_id)
        # (5+1) / (5+0+2) = 6/7 ≈ 0.857
        self.assertAlmostEqual(score, 6 / 7, places=4)

    def test_case_b_all_failure_decays_score(self):
        sp_id = _seed_success_pattern(self.conn, "B", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=5, success_count=0, failure_count=5,
        )
        reconcile_pattern_confidence(self.db_path)
        score = _read_score(self.conn, sp_id)
        # (0+1) / (0+5+2) = 1/7 ≈ 0.143
        self.assertAlmostEqual(score, 1 / 7, places=4)

    def test_case_c_no_usage_keeps_score(self):
        sp_id = _seed_success_pattern(self.conn, "C", confidence=0.42)
        # No pattern_usage row at all.
        updated = reconcile_pattern_confidence(self.db_path)
        self.assertEqual(updated, 0)
        self.assertAlmostEqual(_read_score(self.conn, sp_id), 0.42, places=6)

    def test_case_c2_zero_usage_keeps_score(self):
        # pattern_usage row exists but no success/failure events recorded
        # AND used_count = 0 — treat as "no data, don't reset".
        sp_id = _seed_success_pattern(self.conn, "C2", confidence=0.42)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=0, success_count=0, failure_count=0, confidence=1.0,
        )
        updated = reconcile_pattern_confidence(self.db_path)
        self.assertEqual(updated, 0)
        self.assertAlmostEqual(_read_score(self.conn, sp_id), 0.42, places=6)

    def test_case_d_idempotent(self):
        sp_id = _seed_success_pattern(self.conn, "D", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=4, success_count=3, failure_count=1,
        )
        first = reconcile_pattern_confidence(self.db_path)
        score_after_first = _read_score(self.conn, sp_id)

        second = reconcile_pattern_confidence(self.db_path)
        score_after_second = _read_score(self.conn, sp_id)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)  # nothing changed → no update
        self.assertAlmostEqual(score_after_first, score_after_second, places=6)

    def test_case_e_volume_weighting_via_beta(self):
        # High-volume strong-success pattern.
        sp_high = _seed_success_pattern(self.conn, "high-volume", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_high,
            used_count=100, success_count=90, failure_count=10,
        )
        # Low-volume single-failure pattern.
        sp_low = _seed_success_pattern(self.conn, "low-volume", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_low,
            used_count=1, success_count=0, failure_count=1,
        )
        reconcile_pattern_confidence(self.db_path)

        score_high = _read_score(self.conn, sp_high)
        score_low = _read_score(self.conn, sp_low)

        # High-volume good: (90+1)/(100+2) ≈ 0.892
        self.assertAlmostEqual(score_high, 91 / 102, places=4)
        # Low-volume bad: (0+1)/(1+2) ≈ 0.333 — Beta keeps it near the prior
        # because the evidence is thin, demonstrating volume weighting.
        self.assertAlmostEqual(score_low, 1 / 3, places=4)
        self.assertGreater(score_high, score_low + 0.5)

    def test_case_f_integration_with_learning_loop(self):
        sp_id = _seed_success_pattern(self.conn, "integration", confidence=0.5)
        # Fake usage rows that the daily learning_loop would have produced.
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=7, success_count=6, failure_count=1, confidence=0.8,
        )

        # Simulate the new step in daily_learning_cycle by importing the
        # same hook the production code uses.
        from confidence_reconcile import (  # noqa: WPS433
            reconcile_pattern_confidence as production_hook,
        )
        production_hook(self.db_path)

        score = _read_score(self.conn, sp_id)
        # (6+1)/(7+2) = 7/9 ≈ 0.778
        self.assertAlmostEqual(score, 7 / 9, places=4)

    def test_legacy_confidence_when_no_success_failure_counts(self):
        # Older rows that pre-date success/failure tracking still need to
        # propagate their pattern_usage.confidence value when used_count > 0.
        sp_id = _seed_success_pattern(self.conn, "legacy", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=3, success_count=0, failure_count=0, confidence=0.72,
        )
        reconcile_pattern_confidence(self.db_path)
        self.assertAlmostEqual(_read_score(self.conn, sp_id), 0.72, places=4)


class TestMaybeReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.tmp_dir) / "qi.db"
        self.conn = _make_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        for f in Path(self.tmp_dir).iterdir():
            f.unlink()
        os.rmdir(self.tmp_dir)

    def test_first_call_runs_reconcile(self):
        sp_id = _seed_success_pattern(self.conn, "first", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=2, success_count=2, failure_count=0,
        )
        ran = maybe_reconcile(self.db_path)
        self.assertTrue(ran)
        # Score should have been updated by the reconcile.
        score = _read_score(self.conn, sp_id)
        self.assertAlmostEqual(score, 3 / 4, places=4)

    def test_within_ttl_skips_reconcile(self):
        sp_id = _seed_success_pattern(self.conn, "skip", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=2, success_count=2, failure_count=0,
        )
        first = maybe_reconcile(self.db_path)
        self.assertTrue(first)

        # Add MORE usage — but within TTL, the second call should skip,
        # leaving the first reconcile's score in place.
        self.conn.execute(
            "UPDATE pattern_usage SET success_count = 100 WHERE pattern_id = ?",
            (f"{SUCCESS_PATTERN_PREFIX}{sp_id}",),
        )
        self.conn.commit()

        second = maybe_reconcile(self.db_path)
        self.assertFalse(second)
        score = _read_score(self.conn, sp_id)
        self.assertAlmostEqual(score, 3 / 4, places=4)  # unchanged

    def test_expired_ttl_runs_reconcile(self):
        sp_id = _seed_success_pattern(self.conn, "expired", confidence=0.5)
        _seed_pattern_usage(
            self.conn, sp_id,
            used_count=2, success_count=2, failure_count=0,
        )
        maybe_reconcile(self.db_path)

        # Backdate the timestamp file past the TTL.
        ts_file = self.db_path.parent / ".last_confidence_reconcile_ts"
        ts_file.write_text(str(time.time() - RECONCILE_CACHE_TTL_SECONDS - 1))

        # New usage data.
        self.conn.execute(
            "UPDATE pattern_usage SET success_count = 8, failure_count = 2, "
            "used_count = 10 WHERE pattern_id = ?",
            (f"{SUCCESS_PATTERN_PREFIX}{sp_id}",),
        )
        self.conn.commit()

        ran = maybe_reconcile(self.db_path)
        self.assertTrue(ran)
        score = _read_score(self.conn, sp_id)
        self.assertAlmostEqual(score, 9 / 12, places=4)


if __name__ == "__main__":
    unittest.main()
