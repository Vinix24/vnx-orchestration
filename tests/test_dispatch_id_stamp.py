#!/usr/bin/env python3
"""Tests for injection-time stamping of dispatch_id on success_patterns.

Background
==========
``intelligence_persist.update_confidence_from_outcome`` finds patterns to
boost/decay by matching ``success_patterns.source_dispatch_ids LIKE
'%dispatch_id%'``. Until we stamped the dispatch_id at injection time, only
the post-hoc ``pattern_extractor`` populated that column — so failure decay
silently no-op'd. These tests pin the new injection-time linkage.

Coverage
========
A. Inject pattern P into D1 → P.source_dispatch_ids contains "D1".
B. Inject the same P into D2 → P.source_dispatch_ids contains both.
C. Inject P twice into D1 → "D1" appears only once (idempotent).
D. End-to-end: inject P into D, write a failure outcome → P.confidence_score
   decreases.
E. Pre-existing entries on source_dispatch_ids are preserved.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))

from intelligence_selector import IntelligenceSelector  # noqa: E402
from intelligence_persist import update_confidence_from_outcome  # noqa: E402
from confidence_reconcile import beta_score  # noqa: E402
from runtime_coordination import init_schema  # noqa: E402


def _setup_quality_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS success_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, code_example TEXT, prerequisites TEXT, outcomes TEXT,
            success_rate REAL DEFAULT 0.0, usage_count INTEGER DEFAULT 0,
            avg_completion_time INTEGER, confidence_score REAL DEFAULT 0.0,
            source_dispatch_ids TEXT, source_receipts TEXT,
            first_seen DATETIME, last_used DATETIME,
            valid_from DATETIME DEFAULT NULL, valid_until DATETIME DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS antipatterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT, category TEXT, title TEXT, description TEXT,
            pattern_data TEXT, problem_example TEXT, why_problematic TEXT,
            better_alternative TEXT, occurrence_count INTEGER DEFAULT 0,
            avg_resolution_time INTEGER, severity TEXT DEFAULT 'medium',
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
        CREATE TABLE IF NOT EXISTS dispatch_metadata (
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
        CREATE TABLE IF NOT EXISTS pattern_usage (
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
        CREATE TABLE IF NOT EXISTS confidence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            terminal TEXT,
            outcome TEXT NOT NULL,
            patterns_boosted INTEGER DEFAULT 0,
            patterns_decayed INTEGER DEFAULT 0,
            confidence_change REAL NOT NULL,
            occurred_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def _seed_pattern(
    conn: sqlite3.Connection,
    *,
    title: str = "Use structured output",
    description: str = "Structured output improves first-pass success.",
    category: str = "architect",
    confidence: float = 0.85,
    usage_count: int = 5,
    source_dispatch_ids: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO success_patterns
           (pattern_type, category, title, description, pattern_data,
            confidence_score, usage_count, source_dispatch_ids,
            first_seen, last_used)
           VALUES ('approach', ?, ?, ?, '{}', ?, ?, ?, '2026-04-01', '2026-04-01')""",
        (category, title, description, confidence, usage_count, source_dispatch_ids),
    )
    conn.commit()
    return int(cur.lastrowid)


def _read_source_ids(db_path: Path, pattern_id: int) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT source_dispatch_ids FROM success_patterns WHERE id = ?",
            (pattern_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return []
    return list(json.loads(row[0]))


def _read_confidence(db_path: Path, pattern_id: int) -> float:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT confidence_score FROM success_patterns WHERE id = ?",
            (pattern_id,),
        ).fetchone()
    finally:
        conn.close()
    return float(row[0]) if row and row[0] is not None else 0.0


class DispatchIdStampTests(unittest.TestCase):
    """All cases share the same fixture pattern: seed quality DB, run select +
    record_injection through the real ``IntelligenceSelector`` API, then
    assert on what landed in ``success_patterns.source_dispatch_ids``."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self._quality_db_path = base / "quality_intelligence.db"
        self._state_dir = base / "state"
        self._state_dir.mkdir()
        init_schema(str(self._state_dir))
        conn = _setup_quality_db(self._quality_db_path)
        conn.close()

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _inject(self, dispatch_id: str) -> None:
        selector = IntelligenceSelector(
            quality_db_path=self._quality_db_path,
            coord_db_state_dir=self._state_dir,
        )
        try:
            result = selector.select(
                dispatch_id=dispatch_id,
                injection_point="dispatch_create",
                skill_name="architect",
            )
            selector.record_injection(result)
        finally:
            selector.close()

    # ------------------------------------------------------------------
    # Case A
    # ------------------------------------------------------------------
    def test_a_first_injection_stamps_dispatch_id(self) -> None:
        pattern_id = _seed_pattern(sqlite3.connect(str(self._quality_db_path)))
        self._inject("D1")
        self.assertEqual(_read_source_ids(self._quality_db_path, pattern_id), ["D1"])

    # ------------------------------------------------------------------
    # Case B
    # ------------------------------------------------------------------
    def test_b_second_injection_appends_new_dispatch_id(self) -> None:
        pattern_id = _seed_pattern(sqlite3.connect(str(self._quality_db_path)))
        self._inject("D1")
        self._inject("D2")
        self.assertEqual(
            _read_source_ids(self._quality_db_path, pattern_id),
            ["D1", "D2"],
        )

    # ------------------------------------------------------------------
    # Case C
    # ------------------------------------------------------------------
    def test_c_repeat_injection_is_idempotent(self) -> None:
        pattern_id = _seed_pattern(sqlite3.connect(str(self._quality_db_path)))
        self._inject("D1")
        self._inject("D1")
        self.assertEqual(_read_source_ids(self._quality_db_path, pattern_id), ["D1"])

    # ------------------------------------------------------------------
    # Case D
    # ------------------------------------------------------------------
    def test_d_failure_outcome_decays_confidence_after_injection(self) -> None:
        pattern_id = _seed_pattern(
            sqlite3.connect(str(self._quality_db_path)),
            confidence=0.85,
        )
        before = _read_confidence(self._quality_db_path, pattern_id)
        self._inject("D-fail")

        result = update_confidence_from_outcome(
            self._quality_db_path,
            dispatch_id="D-fail",
            terminal="T1",
            status="failure",
        )

        after = _read_confidence(self._quality_db_path, pattern_id)
        self.assertEqual(result["decayed"], 1)
        self.assertLess(after, before)
        # Beta(success=0, failure=1) posterior with Laplace smoothing
        # = (0 + 1) / (0 + 1 + 2) = 1/3. update_confidence_from_outcome
        # rebuilds confidence_score from pattern_usage counts (#327
        # reconcile), so the post-decay value is independent of the
        # seeded confidence and follows the Beta posterior.
        self.assertAlmostEqual(after, beta_score(0, 1), places=6)

    # ------------------------------------------------------------------
    # Case E
    # ------------------------------------------------------------------
    def test_e_preexisting_source_dispatch_ids_preserved(self) -> None:
        existing = json.dumps(["D-old-1", "D-old-2"])
        pattern_id = _seed_pattern(
            sqlite3.connect(str(self._quality_db_path)),
            source_dispatch_ids=existing,
        )
        self._inject("D-new")
        self.assertEqual(
            _read_source_ids(self._quality_db_path, pattern_id),
            ["D-old-1", "D-old-2", "D-new"],
        )


if __name__ == "__main__":
    unittest.main()
