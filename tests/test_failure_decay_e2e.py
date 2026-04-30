#!/usr/bin/env python3
"""End-to-end synthetic test for the failure-decay chain (ARC-2).

Pins the full chain post-#326 (dispatch_id stamping at injection time):

  record_injection                          # stamps source_dispatch_ids
    -> success_patterns.source_dispatch_ids
       -> update_confidence_from_outcome    # WHERE source_dispatch_ids LIKE
          -> success_patterns.confidence_score (decreased on failure)
          -> confidence_events INSERT (outcome=failure, decayed>=1, change<0)

If any layer regresses, this test fails with a layer-specific assertion.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from intelligence_selector import IntelligenceSelector  # noqa: E402
from intelligence_persist import update_confidence_from_outcome  # noqa: E402
from confidence_reconcile import beta_score  # noqa: E402
from runtime_coordination import init_schema  # noqa: E402


def _build_quality_db(path: Path) -> None:
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
        CREATE TABLE confidence_events (
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
    conn.close()


def _seed_pattern(db_path: Path, *, confidence: float, usage_count: int = 5) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """INSERT INTO success_patterns
               (pattern_type, category, title, description, pattern_data,
                confidence_score, usage_count, source_dispatch_ids,
                first_seen, last_used)
               VALUES ('approach', 'architect', 'E2E pattern', 'Synthetic.', '{}',
                       ?, ?, NULL, '2026-04-01', '2026-04-01')""",
            (confidence, usage_count),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _row(db_path: Path, sql: str, *args) -> sqlite3.Row | None:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


class FailureDecayE2ETest(unittest.TestCase):
    """Inject a synthetic pattern, fire a failure outcome, verify the chain."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.db_path = base / "quality_intelligence.db"
        self.state_dir = base / "state"
        self.state_dir.mkdir()
        init_schema(str(self.state_dir))
        _build_quality_db(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _inject(self, dispatch_id: str) -> None:
        selector = IntelligenceSelector(
            quality_db_path=self.db_path,
            coord_db_state_dir=self.state_dir,
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

    def test_failure_decay_full_chain(self) -> None:
        # Layer 1: stamping at injection
        pattern_id = _seed_pattern(self.db_path, confidence=0.7)
        self._inject("D-e2e-fail")

        stamped = _row(
            self.db_path,
            "SELECT source_dispatch_ids FROM success_patterns WHERE id = ?",
            pattern_id,
        )
        self.assertIsNotNone(stamped, "pattern row must exist post-injection")
        stamped_ids = json.loads(stamped["source_dispatch_ids"] or "[]")
        self.assertIn(
            "D-e2e-fail",
            stamped_ids,
            "Layer 1 broken: record_injection did not stamp dispatch_id "
            "on success_patterns.source_dispatch_ids (#326 regression)",
        )

        # Layer 2: failure outcome decays the pattern
        before_conf = float(
            _row(
                self.db_path,
                "SELECT confidence_score FROM success_patterns WHERE id = ?",
                pattern_id,
            )["confidence_score"]
        )

        result = update_confidence_from_outcome(
            self.db_path,
            dispatch_id="D-e2e-fail",
            terminal="T1",
            status="failure",
        )
        self.assertEqual(
            result["decayed"],
            1,
            "Layer 2 broken: update_confidence_from_outcome did not match "
            "the stamped pattern via source_dispatch_ids LIKE",
        )
        self.assertEqual(result["boosted"], 0)

        after_conf = float(
            _row(
                self.db_path,
                "SELECT confidence_score FROM success_patterns WHERE id = ?",
                pattern_id,
            )["confidence_score"]
        )
        self.assertLess(
            after_conf,
            before_conf,
            "Layer 3 broken: failure outcome did not lower "
            "success_patterns.confidence_score",
        )
        # Beta(0,1) = (0+1)/(0+1+2) = 1/3
        self.assertAlmostEqual(after_conf, beta_score(0, 1), places=6)

        # Layer 4: confidence_events audit row
        event = _row(
            self.db_path,
            "SELECT outcome, patterns_boosted, patterns_decayed, confidence_change "
            "FROM confidence_events WHERE dispatch_id = ? AND outcome = ?",
            "D-e2e-fail",
            "failure",
        )
        self.assertIsNotNone(
            event,
            "Layer 4 broken: confidence_events row not written for failure outcome",
        )
        self.assertEqual(event["outcome"], "failure")
        self.assertEqual(event["patterns_decayed"], 1)
        self.assertEqual(event["patterns_boosted"], 0)
        self.assertLess(
            event["confidence_change"],
            0,
            "confidence_change must be negative for a decay event",
        )


if __name__ == "__main__":
    unittest.main()
