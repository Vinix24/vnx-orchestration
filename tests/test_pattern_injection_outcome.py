#!/usr/bin/env python3
"""Tests for pattern_injection_outcome — the injection-effectiveness WHY instrumentation
(dispatch: injection-effectiveness-eval-loop PR-A).

Covers:
1. Migration: pattern_injection_outcome is created with the ADR-007 composite UNIQUE
   (project_id, dispatch_id, pattern_id); idempotent re-run; tenant isolation.
2. Reason classifier: classify_non_adoption_reason maps deterministic signals to exactly
   one of the six reasons, checked in priority order.
3. Influence check: a report whose content clearly reflects the injected pattern -> used=1;
   a report that touches a same-named file but does NOT reflect the pattern content ->
   used=0 with a plausible (non-filename) reason — guards against the filename false-positive
   that the pre-existing pattern_usage.used_count signal is prone to.
4. Flag gating: VNX_INJECTION_WHY_ENABLED=0 (default) leaves record_adoption_from_receipt's
   reads/writes/return value byte-for-byte unchanged — no pattern_injection_outcome rows,
   no dispatch_pattern_offered read.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_LIB = _SCRIPTS / "lib"
for _p in (_SCRIPTS, _LIB):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from quality_db_init import bootstrap_qi_db, HIGHEST_QI_VERSION  # noqa: E402
import gather_intelligence as gi  # noqa: E402
from gather_intelligence import T0IntelligenceGatherer, classify_non_adoption_reason  # noqa: E402

_SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schemas" / "quality_intelligence.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gatherer(state_dir: Path, db: sqlite3.Connection = None,
                    project_root: Path = None) -> T0IntelligenceGatherer:
    """Construct T0IntelligenceGatherer bypassing __init__ (mirrors the established
    bypass pattern in test_gather_intelligence_exception_handling.py)."""
    g = object.__new__(T0IntelligenceGatherer)
    g.quality_db = db
    g.quality_db_path = state_dir / "quality_intelligence.db"
    g.tag_engine = None
    g.agent_directory = []
    g.vnx_path = state_dir
    g.project_root = project_root or state_dir
    g._usage_log_path = lambda: state_dir / "intelligence_usage.ndjson"
    return g


def _write_offer(state_dir: Path, dispatch_id: str, pattern_id: str,
                  file_path: str = "", title: str = "", content: str = "") -> None:
    event = {
        "timestamp": datetime.now().isoformat(),
        "event_type": "offer",
        "pattern_id": pattern_id,
        "terminal": "T1",
        "dispatch_id": dispatch_id,
        "file_path": file_path,
        "title": title,
        "content": content,
    }
    with open(state_dir / "intelligence_usage.ndjson", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _db_with_outcome_table() -> sqlite3.Connection:
    """In-memory DB with pattern_usage + pattern_injection_outcome (mirrors _migrate_v28)."""
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
            evidence    TEXT,
            project_id  TEXT    NOT NULL DEFAULT 'vnx-dev',
            created_at  TEXT    NOT NULL,
            UNIQUE (project_id, dispatch_id, pattern_id)
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_table_created_with_adr007_composite_unique(self, tmp_path):
        db_path = tmp_path / "qi.db"
        assert bootstrap_qi_db(db_path, schema_file=_SCHEMA_FILE) is True

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == HIGHEST_QI_VERSION

            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pattern_injection_outcome'"
            ).fetchone()
            assert row is not None, "pattern_injection_outcome table must exist"

            cols = {r[1] for r in conn.execute("PRAGMA table_info(pattern_injection_outcome)")}
            for expected in ("id", "dispatch_id", "pattern_id", "pattern_hash",
                              "used", "reason", "evidence", "project_id", "created_at"):
                assert expected in cols, f"missing column {expected}"

            # ADR-007: a UNIQUE constraint whose column set is exactly (project_id, dispatch_id, pattern_id)
            unique_sets = []
            for idx in conn.execute("PRAGMA index_list(pattern_injection_outcome)"):
                if idx[2]:  # unique flag
                    idx_cols = {r[2] for r in conn.execute(f"PRAGMA index_info({idx[1]})")}
                    unique_sets.append(idx_cols)
            assert {"project_id", "dispatch_id", "pattern_id"} in unique_sets
        finally:
            conn.close()

    def test_idempotent_rerun(self, tmp_path):
        db_path = tmp_path / "qi.db"
        assert bootstrap_qi_db(db_path, schema_file=_SCHEMA_FILE) is True
        assert bootstrap_qi_db(db_path, schema_file=_SCHEMA_FILE) is True
        assert bootstrap_qi_db(db_path, schema_file=_SCHEMA_FILE) is True

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == HIGHEST_QI_VERSION
            assert conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='pattern_injection_outcome'"
            ).fetchone()[0] == 1
        finally:
            conn.close()

    def test_composite_unique_enforces_tenant_isolation_and_dedup(self, tmp_path):
        db_path = tmp_path / "qi.db"
        assert bootstrap_qi_db(db_path, schema_file=_SCHEMA_FILE) is True
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO pattern_injection_outcome "
                "(dispatch_id, pattern_id, pattern_hash, used, project_id, created_at) "
                "VALUES ('d1', 'p1', 'p1', 1, 'proj-a', '2026-07-13T00:00:00Z')"
            )
            # Same dispatch/pattern under a different project_id coexists.
            conn.execute(
                "INSERT INTO pattern_injection_outcome "
                "(dispatch_id, pattern_id, pattern_hash, used, project_id, created_at) "
                "VALUES ('d1', 'p1', 'p1', 0, 'proj-b', '2026-07-13T00:00:00Z')"
            )
            conn.commit()
            assert conn.execute(
                "SELECT COUNT(*) FROM pattern_injection_outcome"
            ).fetchone()[0] == 2

            # Same (project_id, dispatch_id, pattern_id) triple violates the UNIQUE constraint.
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO pattern_injection_outcome "
                    "(dispatch_id, pattern_id, pattern_hash, used, project_id, created_at) "
                    "VALUES ('d1', 'p1', 'p1', 1, 'proj-a', '2026-07-13T00:01:00Z')"
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 2. Reason classifier — one case per category, checked in priority order
# ---------------------------------------------------------------------------

class TestReasonClassifier:
    def test_wrong_file_affinity(self):
        reason, evidence = classify_non_adoption_reason(file_touched=False)
        assert reason == "wrong-file-affinity"
        assert evidence

    def test_already_known(self):
        reason, _ = classify_non_adoption_reason(
            file_touched=True, already_known_overlap=0.9,
        )
        assert reason == "already-known"

    def test_stale(self):
        reason, _ = classify_non_adoption_reason(
            file_touched=True, already_known_overlap=0.0, last_offered_age_days=45.0,
        )
        assert reason == "stale"

    def test_irrelevant_to_task(self):
        reason, _ = classify_non_adoption_reason(
            file_touched=True, already_known_overlap=0.0, last_offered_age_days=1.0,
            task_overlap=False,
        )
        assert reason == "irrelevant-to-task"

    def test_bad_timing(self):
        reason, evidence = classify_non_adoption_reason(
            file_touched=True, already_known_overlap=0.0, last_offered_age_days=1.0,
            task_overlap=True,
            offered_at=datetime(2026, 7, 13, 12, 0, 0),
            edit_window_end=datetime(2026, 7, 13, 10, 0, 0),
        )
        assert reason == "bad-timing"
        assert "edit window" in evidence

    def test_low_signal_is_the_terminal_fallback(self):
        reason, evidence = classify_non_adoption_reason(
            file_touched=True, already_known_overlap=0.0, last_offered_age_days=1.0,
            task_overlap=True, pattern_confidence=0.3, content_overlap=0.05,
        )
        assert reason == "low-signal"
        assert "0.3" in evidence

    def test_priority_wrong_file_affinity_wins_over_already_known(self):
        """First match wins — wrong-file-affinity is checked before already-known."""
        reason, _ = classify_non_adoption_reason(
            file_touched=False, already_known_overlap=0.99,
        )
        assert reason == "wrong-file-affinity"

    def test_always_returns_a_known_reason(self):
        reason, _ = classify_non_adoption_reason(file_touched=True)
        assert reason in gi.NON_ADOPTION_REASONS


# ---------------------------------------------------------------------------
# 3. Influence check — content-overlap decides used, not filename alone
# ---------------------------------------------------------------------------

_PATTERN_CONTENT = (
    "Ensure proper cleanup of SSE connections to prevent memory leaks "
    "via timeout handlers"
)


class TestInfluenceCheck:
    def test_report_reflecting_pattern_content_marks_used(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = _db_with_outcome_table()
        g = _make_gatherer(tmp_path, db=db)

        _write_offer(tmp_path, "d-used", "pat-sse", file_path="scripts/sse_pipeline.py",
                     title="SSE cleanup", content=_PATTERN_CONTENT)

        report = tmp_path / "report.md"
        report.write_text(
            "## Changes\nUpdated scripts/sse_pipeline.py to add proper cleanup of SSE "
            "connections with timeout handlers to prevent memory leaks.\n"
        )

        result = g.record_adoption_from_receipt("d-used", "T1", str(report))
        assert result["checked"] == 1

        row = db.execute(
            "SELECT used, reason FROM pattern_injection_outcome WHERE dispatch_id = ? AND pattern_id = ?",
            ("d-used", "pat-sse"),
        ).fetchone()
        assert row is not None
        assert row["used"] == 1
        assert row["reason"] is None

    def test_same_named_file_without_content_marks_unused_with_plausible_reason(
        self, tmp_path, monkeypatch
    ):
        """Guards against the filename false-positive: touching sse_pipeline.py without
        the report reflecting the pattern's actual content must NOT be recorded as used."""
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = _db_with_outcome_table()
        g = _make_gatherer(tmp_path, db=db)

        _write_offer(tmp_path, "d-unused", "pat-sse", file_path="scripts/sse_pipeline.py",
                     title="SSE cleanup", content=_PATTERN_CONTENT)

        report = tmp_path / "report.md"
        report.write_text(
            "## Changes\nFixed a typo in scripts/sse_pipeline.py docstring, no functional "
            "changes.\n## Summary\nMinor documentation fix only.\n"
        )

        result = g.record_adoption_from_receipt("d-unused", "T1", str(report))
        # Legacy filename-only signal still fires (pattern_usage.used_count path) —
        # this is exactly the false-positive the WHY table must not repeat.
        assert result["adoptions"] == 1

        row = db.execute(
            "SELECT used, reason, evidence FROM pattern_injection_outcome "
            "WHERE dispatch_id = ? AND pattern_id = ?",
            ("d-unused", "pat-sse"),
        ).fetchone()
        assert row is not None
        assert row["used"] == 0
        assert row["reason"] in gi.NON_ADOPTION_REASONS
        assert row["reason"] != "wrong-file-affinity"  # the file WAS touched
        assert row["evidence"]

    def test_token_overlap_ratio_direct(self):
        assert gi._token_overlap_ratio("", "anything") == 0.0
        assert gi._token_overlap_ratio("something", "") == 0.0
        ratio = gi._token_overlap_ratio(_PATTERN_CONTENT, _PATTERN_CONTENT)
        assert ratio == 1.0


# ---------------------------------------------------------------------------
# 4. Flag gating — off by default, byte-for-byte unchanged behavior
# ---------------------------------------------------------------------------

class TestFlagGating:
    def test_flag_off_no_outcome_rows_written(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_INJECTION_WHY_ENABLED", raising=False)
        db = _db_with_outcome_table()
        g = _make_gatherer(tmp_path, db=db)

        _write_offer(tmp_path, "d-off", "pat-sse", file_path="scripts/sse_pipeline.py",
                     title="SSE cleanup", content=_PATTERN_CONTENT)
        report = tmp_path / "report.md"
        report.write_text(
            "## Changes\nUpdated scripts/sse_pipeline.py with proper cleanup of SSE "
            "connections and timeout handlers.\n"
        )

        result = g.record_adoption_from_receipt("d-off", "T1", str(report))
        assert result == {"adoptions": 1, "checked": 1}
        assert db.execute("SELECT COUNT(*) FROM pattern_injection_outcome").fetchone()[0] == 0

    def test_flag_off_never_calls_record_injection_why(self, tmp_path, monkeypatch):
        monkeypatch.delenv("VNX_INJECTION_WHY_ENABLED", raising=False)
        db = _db_with_outcome_table()
        g = _make_gatherer(tmp_path, db=db)
        _write_offer(tmp_path, "d-off2", "pat-x", file_path="foo.py")
        report = tmp_path / "report.md"
        report.write_text("touched foo.py\n")

        with patch.object(g, "_record_injection_why") as mock_why:
            g.record_adoption_from_receipt("d-off2", "T1", str(report))
        mock_why.assert_not_called()

    def test_flag_on_calls_record_injection_why(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = _db_with_outcome_table()
        g = _make_gatherer(tmp_path, db=db)
        _write_offer(tmp_path, "d-on", "pat-x", file_path="foo.py")
        report = tmp_path / "report.md"
        report.write_text("touched foo.py\n")

        with patch.object(g, "_record_injection_why") as mock_why:
            g.record_adoption_from_receipt("d-on", "T1", str(report))
        mock_why.assert_called_once()

    def test_flag_off_return_value_identical_to_pre_existing_behavior(self, tmp_path):
        """No env var involved at all — the exact pre-PR-A code path."""
        db = _db_with_outcome_table()
        g = _make_gatherer(tmp_path, db=db)
        _write_offer(tmp_path, "d-legacy", "pat-legacy", file_path="scripts/foo.py")
        report = tmp_path / "report.md"
        report.write_text("Modified scripts/foo.py to add a helper.\n")

        assert os.environ.get("VNX_INJECTION_WHY_ENABLED", "0") != "1"
        result = g.record_adoption_from_receipt("d-legacy", "T1", str(report))
        assert result == {"adoptions": 1, "checked": 1}
        assert db.execute("SELECT COUNT(*) FROM pattern_injection_outcome").fetchone()[0] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
