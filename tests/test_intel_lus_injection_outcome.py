#!/usr/bin/env python3
"""Tests for the smallest closed loop in the intelligence layer — the
pattern_injection_outcome writer.

Dispatch: 20260920-171500-intel-lus.

PUNT 1: a test that calls ``_record_one_injection_outcome`` with a fabricated
injection + report text and proves a row lands:
  - used=1 when the report reflects the pattern content (token overlap)
  - used=0 plus a classified reason when it does not

These exercise the single-pattern unit directly (not the
``record_adoption_from_receipt`` wrapper) so the assertion fails on the
*content* of the row, not on a missing symbol. A TypeError/KeyError on a
missing symbol is not a red run; the signature must be intact and the
assertion must fail on the content.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_SCRIPTS, _LIB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gather_intelligence as gi  # noqa: E402
from gather_intelligence import T0IntelligenceGatherer  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_with_outcome() -> sqlite3.Connection:
    """In-memory DB with pattern_usage + pattern_injection_outcome."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT,
            pattern_hash TEXT,
            used_count INTEGER DEFAULT 0,
            confidence REAL DEFAULT 1.0,
            last_offered TIMESTAMP,
            last_used TIMESTAMP,
            updated_at TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE pattern_injection_outcome (
            id          INTEGER PRIMARY KEY,
            dispatch_id TEXT    NOT NULL,
            pattern_id  TEXT    NOT NULL,
            pattern_hash TEXT,
            used        INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1)),
            reason      TEXT,
            evidence     TEXT,
            project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
            created_at  TEXT    NOT NULL,
            UNIQUE (project_id, dispatch_id, pattern_id)
        )
    """)
    conn.commit()
    return conn


def _make_gatherer(state_dir: Path, db: sqlite3.Connection,
                   events_path: Path) -> T0IntelligenceGatherer:
    """Construct T0IntelligenceGatherer bypassing __init__."""
    g = object.__new__(T0IntelligenceGatherer)
    g.quality_db = db
    g.quality_db_path = state_dir / "quality_intelligence.db"
    g.tag_engine = None
    g.agent_directory = []
    g.vnx_path = state_dir
    g.project_root = state_dir
    g._usage_log_path = lambda: state_dir / "intelligence_usage.ndjson"
    # Redirect the audit-event writer to a temp file so it does not touch
    # the real events/ directory.
    gi._pattern_injection_outcome_events_path = lambda: events_path
    return g


_PATTERN_CONTENT = (
    "Ensure proper cleanup of SSE connections to prevent memory leaks "
    "via timeout handlers"
)


# ---------------------------------------------------------------------------
# PUNT 1 — the first row lands
# ---------------------------------------------------------------------------

class TestRecordOneInjectionOutcome:
    """Directly exercise _record_one_injection_outcome — the single-pattern
    write unit — and assert on the *content* of the landed row."""

    def test_used_1_when_report_reflects_pattern_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = _db_with_outcome()
        events = tmp_path / "events" / "pattern_injection_outcome.ndjson"
        g = _make_gatherer(tmp_path, db, events)

        meta = {
            "file_path": "",
            "title": "SSE cleanup",
            "content": _PATTERN_CONTENT,
        }
        report_text = (
            "Updated scripts/sse_pipeline.py to add proper cleanup of SSE "
            "connections with timeout handlers to prevent memory leaks."
        )
        g._record_one_injection_outcome(
            dispatch_id="d-used",
            pattern_id="pat-sse",
            meta=meta,
            report_files=set(),
            report_text=report_text,
            project_id="vnx-dev",
            now=datetime.now().isoformat(),
        )
        db.commit()

        row = db.execute(
            "SELECT used, reason, evidence FROM pattern_injection_outcome "
            "WHERE dispatch_id = ? AND pattern_id = ?",
            ("d-used", "pat-sse"),
        ).fetchone()
        # Signature-intact red run would be: row is None on the old (no-writer)
        # state. Here the writer ran, so the row must exist and carry used=1.
        assert row is not None, "row must land — old state (0 rows) would fail here"
        assert row["used"] == 1, f"expected used=1 on overlap, got {row['used']}"
        assert row["reason"] is None

    def test_used_0_with_reason_when_report_does_not_reflect_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = _db_with_outcome()
        events = tmp_path / "events" / "pattern_injection_outcome.ndjson"
        g = _make_gatherer(tmp_path, db, events)

        meta = {
            "file_path": "",
            "title": "SSE cleanup",
            "content": _PATTERN_CONTENT,
        }
        report_text = "Fixed a typo in a docstring, no functional changes."
        g._record_one_injection_outcome(
            dispatch_id="d-unused",
            pattern_id="pat-sse",
            meta=meta,
            report_files=set(),
            report_text=report_text,
            project_id="vnx-dev",
            now=datetime.now().isoformat(),
        )
        db.commit()

        row = db.execute(
            "SELECT used, reason, evidence FROM pattern_injection_outcome "
            "WHERE dispatch_id = ? AND pattern_id = ?",
            ("d-unused", "pat-sse"),
        ).fetchone()
        assert row is not None, "row must land"
        assert row["used"] == 0, f"expected used=0 without overlap, got {row['used']}"
        assert row["reason"] in gi.NON_ADOPTION_REASONS, (
            f"reason must be a classified non-adoption reason, got {row['reason']!r}"
        )
        assert row["evidence"], "evidence must accompany the reason"

    def test_two_rows_for_two_dispatches_independent(self, tmp_path, monkeypatch):
        """Two distinct (dispatch_id, pattern_id) triples land two rows."""
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = _db_with_outcome()
        events = tmp_path / "events" / "pattern_injection_outcome.ndjson"
        g = _make_gatherer(tmp_path, db, events)

        meta = {"file_path": "", "title": "SSE cleanup", "content": _PATTERN_CONTENT}
        now = datetime.now().isoformat()
        g._record_one_injection_outcome(
            dispatch_id="d1", pattern_id="pat-sse", meta=meta,
            report_files=set(), report_text="cleanup of SSE connections",
            project_id="vnx-dev", now=now,
        )
        g._record_one_injection_outcome(
            dispatch_id="d2", pattern_id="pat-sse", meta=meta,
            report_files=set(), report_text="nothing relevant here",
            project_id="vnx-dev", now=now,
        )
        db.commit()
        assert db.execute(
            "SELECT COUNT(*) FROM pattern_injection_outcome"
        ).fetchone()[0] == 2


# ---------------------------------------------------------------------------
# PUNT 1 (end-to-end) — real-store data, against a COPY, never ~/.vnx-data
# ---------------------------------------------------------------------------

class TestEndToEndOnCopiedStoreData:
    """Exercise the real write path (record_adoption_from_receipt ->
    _record_injection_why -> _record_one_injection_outcome) on a *copy* of
    real offers from the live store, with VNX_INJECTION_WHY_ENABLED=1.

    Never touches ~/.vnx-data: the offers are copied into a temp DB by the
    fixture below, and the report is a fabricated file in tmp_path.
    """

    @pytest.fixture
    def copied_offers_db(self, tmp_path: Path) -> sqlite3.Connection:
        """Copy the real dispatch_pattern_offered + pattern_usage rows for a
        few real dispatch ids into an in-memory DB, so the end-to-end path
        runs on real data without touching the live store."""
        live_state = Path(os.path.expanduser("~/.vnx-data/vnx-dev/state"))
        live_qi = live_state / "quality_intelligence.db"
        if not live_qi.exists():
            pytest.skip("live quality_intelligence.db not present on this machine")
        # Read-only attach of the live DB to copy rows out.
        live = sqlite3.connect(str(live_qi))
        live.row_factory = sqlite3.Row
        try:
            offers = live.execute(
                "SELECT dispatch_id, pattern_id, pattern_title, offered_at "
                "FROM dispatch_pattern_offered ORDER BY offered_at DESC LIMIT 5"
            ).fetchall()
            if not offers:
                pytest.skip("no dispatch_pattern_offered rows in live store")
            pids = [r["pattern_id"] for r in offers]
            placeholders = ",".join("?" * len(pids))
            usage = live.execute(
                f"SELECT pattern_id, used_count, confidence, last_offered, "
                f"last_used FROM pattern_usage WHERE pattern_id IN ({placeholders})",
                pids,
            ).fetchall()
        finally:
            live.close()

        db = _db_with_outcome()
        db.execute(
            "CREATE TABLE dispatch_pattern_offered ("
            "dispatch_id TEXT NOT NULL, pattern_id TEXT NOT NULL, "
            "pattern_title TEXT NOT NULL, offered_at TEXT NOT NULL, "
            "PRIMARY KEY (dispatch_id, pattern_id))"
        )
        for r in offers:
            db.execute(
                "INSERT INTO dispatch_pattern_offered "
                "(dispatch_id, pattern_id, pattern_title, offered_at) "
                "VALUES (?, ?, ?, ?)",
                (r["dispatch_id"], r["pattern_id"], r["pattern_title"], r["offered_at"]),
            )
        for r in usage:
            db.execute(
                "INSERT INTO pattern_usage "
                "(pattern_id, used_count, confidence, last_offered, last_used) "
                "VALUES (?, ?, ?, ?, ?)",
                (r["pattern_id"], r["used_count"], r["confidence"],
                 r["last_offered"], r["last_used"]),
            )
        db.commit()
        return db

    def test_e2e_lands_rows_on_real_offers(self, tmp_path, monkeypatch, copied_offers_db):
        """With the flag on, record_adoption_from_receipt must land at least
        one pattern_injection_outcome row for the copied real offers."""
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = copied_offers_db
        events = tmp_path / "events" / "pattern_injection_outcome.ndjson"
        g = _make_gatherer(tmp_path, db, events)

        # Pick the first dispatch_id from the copied offers.
        first = db.execute(
            "SELECT dispatch_id FROM dispatch_pattern_offered LIMIT 1"
        ).fetchone()
        assert first is not None
        dispatch_id = first["dispatch_id"]

        before = db.execute(
            "SELECT COUNT(*) FROM pattern_injection_outcome"
        ).fetchone()[0]

        report = tmp_path / "report.md"
        report.write_text(
            "## Summary\nFabricated report touching none of the offered pattern "
            "content, so every offered pattern should land used=0 with a reason.\n"
        )

        result = g.record_adoption_from_receipt(dispatch_id, "T1", str(report))

        after = db.execute(
            "SELECT COUNT(*) FROM pattern_injection_outcome"
        ).fetchone()[0]
        assert after > before, (
            f"end-to-end writer did not land rows: before={before} after={after} "
            f"checked={result.get('checked')}"
        )

        # Every landed row for this dispatch must be used=0 with a reason,
        # since the report reflects none of the pattern content.
        rows = db.execute(
            "SELECT used, reason FROM pattern_injection_outcome "
            "WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchall()
        assert rows, "rows must exist for this dispatch"
        for r in rows:
            assert r["used"] == 0
            assert r["reason"] in gi.NON_ADOPTION_REASONS


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
