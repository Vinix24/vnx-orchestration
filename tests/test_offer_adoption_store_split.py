#!/usr/bin/env python3
"""Regression tests for OI-894 — offer-writer / adoption-reader store split.

The live intelligence-injection path writes per-dispatch offers to the
``dispatch_pattern_offered`` DB junction (intelligence_sources/_recording.py),
while ``record_adoption_from_receipt`` historically read offers only from
``intelligence_usage.ndjson`` — a log the primary dispatcher never populates
because its ``gather`` call passes no dispatch_id (dispatcher_minimal.sh).
Result: adoptions could never fire and ``used_count`` stayed frozen while
``ignored_count`` kept rising.

These tests pin the repaired contract:
  1. the adoption reader sees DB-junction offers (single source of truth),
  2. DB and ndjson offers merge without double-counting,
  3. an adoption UPDATE that matches zero rows is visible (WARNING),
  4. the adoption UPDATE matches the live ``pattern_id`` space, not only
     ``pattern_hash``.

On the unfixed code every test in this file fails.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
LIB_DIR = SCRIPT_DIR / "lib"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(LIB_DIR))

from gather_intelligence import T0IntelligenceGatherer


def _make_gatherer(db_path: Path) -> T0IntelligenceGatherer:
    """Gatherer wired to a real (temp) quality DB, bypassing VNX env setup."""
    obj = object.__new__(T0IntelligenceGatherer)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    obj.quality_db = conn
    obj.quality_db_path = db_path
    obj.tag_engine = None
    obj.agent_directory = []
    obj.vnx_path = Path("/tmp/vnx")
    obj.project_root = Path("/tmp/project")
    return obj


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT,
            pattern_hash TEXT,
            used_count INTEGER DEFAULT 0,
            ignored_count INTEGER DEFAULT 0,
            last_offered TEXT,
            last_used TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE dispatch_pattern_offered (
            dispatch_id TEXT NOT NULL,
            pattern_id TEXT NOT NULL,
            pattern_title TEXT NOT NULL,
            offered_at TEXT NOT NULL,
            PRIMARY KEY (dispatch_id, pattern_id)
        );
        CREATE TABLE antipatterns (
            id INTEGER PRIMARY KEY,
            title TEXT,
            description TEXT
        );
        CREATE TABLE success_patterns (
            id INTEGER PRIMARY KEY,
            title TEXT,
            description TEXT
        );
        """
    )
    conn.commit()


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.db_path = tmp / "quality_intelligence.db"
        conn = sqlite3.connect(str(self.db_path))
        _create_schema(conn)
        conn.close()
        self.g = _make_gatherer(self.db_path)
        # Point the legacy ndjson log at a path that does not exist unless a
        # test creates it — the DB junction must be sufficient on its own.
        self.ndjson_path = tmp / "intelligence_usage.ndjson"
        self.report_path = tmp / "report.md"
        # The WHY instrumentation is additive and off by default; keep it off.
        env_patch = patch.dict("os.environ", {"VNX_INJECTION_WHY_ENABLED": "0"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        log_patch = patch.object(self.g, "_usage_log_path", return_value=self.ndjson_path)
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _used_count(self, pattern_id: str) -> int:
        row = self.g.quality_db.execute(
            "SELECT used_count FROM pattern_usage WHERE pattern_id = ?",
            (pattern_id,),
        ).fetchone()
        return row["used_count"] if row else -1


class TestDbJunctionOffersDriveAdoption(_Base):
    """Reader must see the offers the live writer actually wrote (DB junction)."""

    def test_db_junction_offer_adopted_increments_used_count(self) -> None:
        self.g.quality_db.execute(
            "INSERT INTO antipatterns (id, title, description) VALUES (42, ?, ?)",
            (
                "Always validate hook payloads before dispatch",
                "Unvalidated hook payloads corrupt the dispatch ledger",
            ),
        )
        self.g.quality_db.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash,"
            " used_count, ignored_count) VALUES (?, ?, ?, 0, 5)",
            ("intel_ap_42", "Always validate hook payloads before dispatch",
             _sha1("intel_ap_42")),
        )
        self.g.quality_db.execute(
            "INSERT INTO dispatch_pattern_offered"
            " (dispatch_id, pattern_id, pattern_title, offered_at)"
            " VALUES ('d-1', 'intel_ap_42', 'Always validate hook payloads"
            " before dispatch', '2026-07-31T10:00:00')",
        )
        self.g.quality_db.commit()
        self.report_path.write_text(
            "# Completion report\n"
            "Validated the hook payloads before dispatch; the ledger stays"
            " consistent and no corrupt payload reached dispatch.\n"
        )

        result = self.g.record_adoption_from_receipt("d-1", "T1", str(self.report_path))

        # The reader must see exactly the offers the DB writer wrote ...
        self.assertEqual(result["checked"], 1)
        # ... and the adoption must actually land on the feedback counters.
        self.assertEqual(result["adoptions"], 1)
        self.assertEqual(self._used_count("intel_ap_42"), 1)
        row = self.g.quality_db.execute(
            "SELECT last_used FROM pattern_usage WHERE pattern_id = 'intel_ap_42'"
        ).fetchone()
        self.assertIsNotNone(row["last_used"])

    def test_checked_counts_db_offers_without_ndjson(self) -> None:
        for i in (1, 2, 3):
            self.g.quality_db.execute(
                "INSERT INTO dispatch_pattern_offered VALUES (?, ?, ?, ?)",
                ("d-2", f"intel_ap_{i}", f"title {i}", "2026-07-31T10:00:00"),
            )
        self.g.quality_db.commit()
        self.report_path.write_text("nothing adopted here\n")
        result = self.g.record_adoption_from_receipt("d-2", "T1", str(self.report_path))
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["adoptions"], 0)


class TestOfferSourcesMerge(_Base):
    """ndjson (legacy) and DB junction merge per pattern_id — no double count."""

    def test_ndjson_and_db_offers_deduped(self) -> None:
        self.ndjson_path.write_text(
            '{"event_type":"offer","dispatch_id":"d-3","pattern_id":"p1",'
            '"file_path":"scripts/foo.py"}\n'
        )
        self.g.quality_db.execute(
            "INSERT INTO antipatterns (id, title, description) VALUES (7, ?, ?)",
            ("zzq qzx vwx unrelated jargon", "kqw vzx qxj more unrelated jargon"),
        )
        self.g.quality_db.executemany(
            "INSERT INTO dispatch_pattern_offered VALUES (?, ?, ?, ?)",
            [
                ("d-3", "p1", "legacy title", "2026-07-31T10:00:00"),
                ("d-3", "intel_ap_7", "zzq qzx vwx unrelated jargon",
                 "2026-07-31T10:00:00"),
            ],
        )
        self.g.quality_db.commit()
        self.report_path.write_text("edited scripts/foo.py as instructed\n")

        result = self.g.record_adoption_from_receipt("d-3", "T1", str(self.report_path))

        # p1 appears in both stores but counts once; intel_ap_7 only in DB.
        self.assertEqual(result["checked"], 2)
        # Filename signal still works for the legacy offer.
        self.assertEqual(result["adoptions"], 1)


class TestAdoptionUpdateVisibility(_Base):
    """Damping removal: a zero-row adoption UPDATE must be visible."""

    def test_zero_row_adoption_update_logs_warning(self) -> None:
        self.g.quality_db.execute(
            "INSERT INTO antipatterns (id, title, description) VALUES (9, ?, ?)",
            ("Rotate credentials before every release", "Stale credentials leak"),
        )
        # Offer exists in the junction but pattern_usage has NO row — the
        # UPDATE must match zero rows and say so.
        self.g.quality_db.execute(
            "INSERT INTO dispatch_pattern_offered VALUES (?, ?, ?, ?)",
            ("d-4", "intel_ap_9", "Rotate credentials before every release",
             "2026-07-31T10:00:00"),
        )
        self.g.quality_db.commit()
        self.report_path.write_text(
            "Rotated the credentials before the release; nothing stale remains.\n"
        )

        with self.assertLogs("gather_intelligence", level="WARNING") as cm:
            result = self.g.record_adoption_from_receipt("d-4", "T1", str(self.report_path))

        self.assertEqual(result["adoptions"], 1)
        self.assertTrue(
            any("intel_ap_9" in line and "no pattern_usage" in line for line in cm.output),
            f"expected zero-row adoption warning, got: {cm.output}",
        )


class TestAdoptionKeySpace(_Base):
    """The adoption UPDATE must match the live pattern_id space, not only hash."""

    def test_adoption_matches_pattern_id_not_only_hash(self) -> None:
        self.g.quality_db.execute(
            "INSERT INTO pattern_usage (pattern_id, pattern_title, pattern_hash,"
            " used_count) VALUES (?, ?, ?, 0)",
            ("intel_ap_5", "some title", _sha1("intel_ap_5")),
        )
        self.g.quality_db.commit()

        self.g.record_pattern_adoption("intel_ap_5", "T1", "d-5")

        self.assertEqual(self._used_count("intel_ap_5"), 1)


if __name__ == "__main__":
    unittest.main()
