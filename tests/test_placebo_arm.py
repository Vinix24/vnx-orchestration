#!/usr/bin/env python3
"""Tests for the placebo-arm negative control (dispatch 20260920-191500-placebo-arm).

The adoption metric (``pattern_injection_outcome.used``) is decided by a 25%
token-overlap between the offered pattern's content and the worker's report
text. That metric risks measuring "worked on the same subject" rather than
"adopted the pattern". The placebo arm offers a deliberately cross-domain
pattern so the metric can be tested: if treatment and placebo adoption rates
diverge, the metric discriminates; if they don't, the metric measures subject
overlap, not adoption.

These tests are build-only: nothing here activates or runs the arm in
production. Each component is tested on its own.

Coverage:
1. ``_ab_arm`` returns ``'placebo'`` only when ``VNX_INTEL_PLACEBO_ARM`` is on,
   and takes precedence over the A/B control arm.
2. ``_select_placebo_pattern`` picks the candidate with the LOWEST token
   overlap with the dispatch context, tie-broken toward a different scope-tag
   group, and returns exactly one item with ``ab_arm='placebo'``.
3. Migration v32 adds ``ab_arm`` to ``pattern_injection_outcome`` and
   ``dispatch_pattern_offered`` (default 'treatment', idempotent).
4. ``_record_one_injection_outcome`` stamps ``ab_arm`` onto the outcome row
   and the ADR-005 audit event, inheriting it from the offer's arm label.
5. ``run_placebo`` prints adoption per arm with counts, including the all-zero
   case (the expected state until the arm is activated), and degrades cleanly
   when the ``ab_arm`` column is absent.
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
from gather_intelligence import T0IntelligenceGatherer  # noqa: E402
import intelligence_selector as isel  # noqa: E402
from intelligence_selector import (  # noqa: E402
    IntelligenceItem,
    IntelligenceSelector,
    SuppressionRecord,
    _placebo_overlap_ratio,
    _placebo_tokenize,
)

_SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schemas" / "quality_intelligence.sql"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(item_id: str, title: str, content: str, scope_tags, confidence=0.9, item_class="proven_pattern"):
    return IntelligenceItem(
        item_id=item_id,
        item_class=item_class,
        title=title,
        content=content,
        confidence=confidence,
        evidence_count=3,
        last_seen="2026-09-20T00:00:00Z",
        scope_tags=list(scope_tags),
    )


def _make_gatherer(state_dir: Path, db: sqlite3.Connection,
                   project_root: Path | None = None) -> T0IntelligenceGatherer:
    g = object.__new__(T0IntelligenceGatherer)
    g.quality_db = db
    g.quality_db_path = state_dir / "quality_intelligence.db"
    g.tag_engine = None
    g.agent_directory = []
    g.vnx_path = state_dir
    g.project_root = project_root or state_dir
    g._usage_log_path = lambda: state_dir / "intelligence_usage.ndjson"
    return g


def _db_with_outcome_and_ab_arm() -> sqlite3.Connection:
    """In-memory DB with pattern_usage, dispatch_pattern_offered (incl. ab_arm),
    and pattern_injection_outcome (incl. ab_arm), mirroring the post-v32 schema."""
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
        CREATE TABLE dispatch_pattern_offered (
            dispatch_id   TEXT NOT NULL,
            pattern_id    TEXT NOT NULL,
            pattern_title TEXT NOT NULL,
            offered_at    TEXT NOT NULL,
            project_id    TEXT NOT NULL DEFAULT '',
            ab_arm        TEXT,
            PRIMARY KEY (dispatch_id, pattern_id, project_id)
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
            ab_arm      TEXT,
            UNIQUE (project_id, dispatch_id, pattern_id)
        )
    """)
    conn.commit()
    return conn


def _db_with_outcome_no_ab_arm() -> sqlite3.Connection:
    """Pre-v32 schema: pattern_injection_outcome without ab_arm column."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE pattern_usage (
            pattern_id TEXT PRIMARY KEY,
            pattern_title TEXT, pattern_hash TEXT,
            used_count INTEGER DEFAULT 0, confidence REAL DEFAULT 1.0,
            last_offered TIMESTAMP, last_used TIMESTAMP, updated_at TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE dispatch_pattern_offered (
            dispatch_id TEXT NOT NULL, pattern_id TEXT NOT NULL,
            pattern_title TEXT NOT NULL, offered_at TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (dispatch_id, pattern_id, project_id)
        )
    """)
    conn.execute("""
        CREATE TABLE pattern_injection_outcome (
            id INTEGER PRIMARY KEY, dispatch_id TEXT NOT NULL,
            pattern_id TEXT NOT NULL, pattern_hash TEXT,
            used INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1)),
            reason TEXT, evidence TEXT,
            project_id TEXT NOT NULL DEFAULT 'vnx-dev',
            created_at TEXT NOT NULL,
            UNIQUE (project_id, dispatch_id, pattern_id)
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 1. _ab_arm + placebo flag
# ---------------------------------------------------------------------------

class TestAbArmPlacebo:
    def test_default_returns_treatment(self, monkeypatch):
        monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
        monkeypatch.delenv("VNX_INTEL_AB_TEST", raising=False)
        assert isel._ab_arm() == "treatment"

    def test_placebo_flag_on_returns_placebo(self, monkeypatch):
        monkeypatch.setenv("VNX_INTEL_PLACEBO_ARM", "1")
        monkeypatch.delenv("VNX_INTEL_AB_TEST", raising=False)
        assert isel._ab_arm() == "placebo"

    def test_placebo_takes_precedence_over_control(self, monkeypatch):
        """Placebo must not be contaminated by control-arm suppression."""
        monkeypatch.setenv("VNX_INTEL_PLACEBO_ARM", "1")
        monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
        with patch("intelligence_selector.random.random", return_value=0.01):
            assert isel._ab_arm() == "placebo"

    def test_placebo_flag_off_ab_test_still_control(self, monkeypatch):
        monkeypatch.delenv("VNX_INTEL_PLACEBO_ARM", raising=False)
        monkeypatch.setenv("VNX_INTEL_AB_TEST", "1")
        with patch("intelligence_selector.random.random", return_value=0.05):
            assert isel._ab_arm() == "control"


# ---------------------------------------------------------------------------
# 2. _select_placebo_pattern — lowest overlap, different domain, one item
# ---------------------------------------------------------------------------

class TestSelectPlaceboPattern:
    def test_token_overlap_helpers(self):
        assert _placebo_overlap_ratio("", "x") == 0.0
        assert _placebo_overlap_ratio("alpha", "") == 0.0
        assert _placebo_overlap_ratio("crawling pipeline", "crawling pipeline") == 1.0
        # shared subject word drives overlap up
        high = _placebo_overlap_ratio("crawling pipeline memory", "crawling pipeline leak")
        low = _placebo_overlap_ratio("storage indexing batch", "crawling pipeline leak")
        assert low < high

    def test_picks_lowest_overlap_candidate(self):
        sel = object.__new__(IntelligenceSelector)
        dispatch_ctx = "crawler pipeline memory leak cleanup"
        candidates = {
            "proven_pattern": [
                _item("p1", "Crawler cleanup", "crawler pipeline memory leak cleanup pattern",
                      ["crawler", "pipeline"]),
                _item("p2", "Storage indexing", "storage batch indexing strategy",
                      ["storage", "indexing"]),
            ],
            "failure_prevention": [],
            "recent_comparable": [],
        }
        selected, suppressed = sel._select_placebo_pattern(
            candidates, dispatch_ctx, ["crawler", "pipeline"],
        )
        assert len(selected) == 1
        # p2 has the lowest overlap with the crawler dispatch context
        assert selected[0].item_id == "p2"
        assert all(isinstance(s, SuppressionRecord) for s in suppressed)
        assert len(suppressed) == 1

    def test_tie_breaks_toward_different_scope_group(self):
        """When two candidates have equally low word overlap, the one whose
        scope_tags share NO element with the dispatch scope wins (different
        domain), even if it appears later in the confidence-ranked pool."""
        sel = object.__new__(IntelligenceSelector)
        dispatch_ctx = "scheduler lease sweep"  # rare words -> both ~0 overlap
        same_domain = _item("p-same", "Scheduler retry", "scheduler retry guard",
                            ["scheduler", "lease"])  # shares scope
        other_domain = _item("p-other", "Frontend render", "frontend svelte render hydration",
                             ["frontend", "ui"])  # different scope
        candidates = {
            "proven_pattern": [same_domain, other_domain],
            "failure_prevention": [],
            "recent_comparable": [],
        }
        selected, _ = sel._select_placebo_pattern(
            candidates, dispatch_ctx, ["scheduler", "lease"],
        )
        assert selected[0].item_id == "p-other"

    def test_empty_candidates_returns_empty(self):
        sel = object.__new__(IntelligenceSelector)
        candidates = {"proven_pattern": [], "failure_prevention": [], "recent_comparable": []}
        selected, suppressed = sel._select_placebo_pattern(candidates, "ctx", [])
        assert selected == []
        assert suppressed == []

    def test_select_placebo_sets_ab_arm_and_single_item(self):
        sel = object.__new__(IntelligenceSelector)
        sel._quality_db_path = None
        sel._quality_db = None
        sel._coord_state_dir = None
        with patch.object(IntelligenceSelector, "_query_candidates", return_value={
            "proven_pattern": [_item("p1", "Crawler", "crawler pipeline", ["crawler"])],
            "failure_prevention": [],
            "recent_comparable": [],
        }), patch.object(IntelligenceSelector, "_query_recent_injected_ids", return_value=frozenset()):
            result = sel.select_placebo(
                dispatch_id="d-1",
                injection_point="dispatch_create",
                instruction_text="crawler pipeline memory leak",
                scope_tags=["crawler"],
            )
        assert result.ab_arm == "placebo"
        assert len(result.items) == 1


# ---------------------------------------------------------------------------
# 3. Migration v32 — ab_arm columns
# ---------------------------------------------------------------------------

class TestMigrationV32:
    def test_ab_arm_columns_added(self, tmp_path):
        db_path = tmp_path / "qi.db"
        assert bootstrap_qi_db(db_path, schema_file=_SCHEMA_FILE) is True

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == HIGHEST_QI_VERSION

            pio_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(pattern_injection_outcome)").fetchall()}
            assert "ab_arm" in pio_cols

            dpo_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(dispatch_pattern_offered)").fetchall()}
            assert "ab_arm" in dpo_cols
        finally:
            conn.close()

    def test_ab_arm_defaults_to_null_for_historical_rows(self, tmp_path):
        """A row written without an explicit ab_arm (the historical case: a
        row that predates the v32 arm label) reads NULL, not a fabricated
        'treatment'. Giving every historical row a invented treatment arm is
        the measurement defect this migration repairs — a row whose arm we
        do not know is NULL, which the per-arm report surfaces as 'unknown'.
        """
        db_path = tmp_path / "qi.db"
        assert bootstrap_qi_db(db_path, schema_file=_SCHEMA_FILE) is True
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "INSERT INTO pattern_injection_outcome "
                "(dispatch_id, pattern_id, pattern_hash, used, project_id, created_at) "
                "VALUES ('d1', 'p1', 'p1', 1, 'proj', '2026-09-20T00:00:00Z')"
            )
            # dispatch_pattern_offered has no project_id column in the base
            # schema (v17); ab_arm is added by v32 as a nullable column, so
            # a row inserted without an explicit arm stays NULL.
            conn.execute(
                "INSERT INTO dispatch_pattern_offered "
                "(dispatch_id, pattern_id, pattern_title, offered_at) "
                "VALUES ('d1', 'p1', 't', '2026-09-20T00:00:00Z')"
            )
            conn.commit()
            pio_arm = conn.execute(
                "SELECT ab_arm FROM pattern_injection_outcome WHERE dispatch_id='d1'"
            ).fetchone()[0]
            dpo_arm = conn.execute(
                "SELECT ab_arm FROM dispatch_pattern_offered WHERE dispatch_id='d1'"
            ).fetchone()[0]
            assert pio_arm is None
            assert dpo_arm is None
        finally:
            conn.close()

    def test_idempotent_rerun(self, tmp_path):
        db_path = tmp_path / "qi.db"
        for _ in range(3):
            assert bootstrap_qi_db(db_path, schema_file=_SCHEMA_FILE) is True
        conn = sqlite3.connect(str(db_path))
        try:
            # exactly one ab_arm column (no duplicate ADDs)
            pio_arms = [r[1] for r in conn.execute(
                "PRAGMA table_info(pattern_injection_outcome)").fetchall()].count("ab_arm")
            dpo_arms = [r[1] for r in conn.execute(
                "PRAGMA table_info(dispatch_pattern_offered)").fetchall()].count("ab_arm")
            assert pio_arms == 1
            assert dpo_arms == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 4. _record_one_injection_outcome stamps ab_arm
# ---------------------------------------------------------------------------

class TestOutcomeAbArmStamping:
    def test_outcome_row_inherits_placebo_arm(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        events_path = tmp_path / "events" / "pattern_injection_outcome.ndjson"
        monkeypatch.setattr(gi, "_pattern_injection_outcome_events_path", lambda: events_path)

        db = _db_with_outcome_and_ab_arm()
        g = _make_gatherer(tmp_path, db=db)
        # Offer the pattern via the canonical junction, stamped as placebo.
        db.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id, ab_arm) "
            "VALUES ('d-pl', 'p-pl', 'Storage indexing', '2026-09-20T00:00:00Z', 'vnx-dev', 'placebo')"
        )
        db.commit()
        report = tmp_path / "report.md"
        # Report reflects the dispatch SUBJECT (crawler), not the placebo
        # pattern (storage indexing), so the content-overlap metric reads 0.
        report.write_text(
            "## Changes\nWorked on crawler pipeline cleanup and memory leak handling.\n"
        )

        g.record_adoption_from_receipt("d-pl", "T1", str(report))

        row = db.execute(
            "SELECT used, ab_arm, reason FROM pattern_injection_outcome "
            "WHERE dispatch_id = ? AND pattern_id = ?",
            ("d-pl", "p-pl"),
        ).fetchone()
        assert row is not None
        assert row["ab_arm"] == "placebo"
        # The report doesn't reflect the placebo pattern -> used=0
        assert row["used"] == 0

    def test_outcome_row_inherits_treatment_arm(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = _db_with_outcome_and_ab_arm()
        g = _make_gatherer(tmp_path, db=db)
        db.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id, ab_arm) "
            "VALUES ('d-tr', 'p-tr', 'Crawler cleanup', '2026-09-20T00:00:00Z', 'vnx-dev', 'treatment')"
        )
        db.commit()
        report = tmp_path / "report.md"
        report.write_text("## Changes\nImplemented crawler cleanup pipeline memory leak handling.\n")

        g.record_adoption_from_receipt("d-tr", "T1", str(report))

        row = db.execute(
            "SELECT used, ab_arm FROM pattern_injection_outcome "
            "WHERE dispatch_id='d-tr' AND pattern_id='p-tr'"
        ).fetchone()
        assert row is not None
        assert row["ab_arm"] == "treatment"
        assert row["used"] == 1

    def test_audit_event_carries_ab_arm(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        events_path = tmp_path / "events" / "pattern_injection_outcome.ndjson"
        monkeypatch.setattr(gi, "_pattern_injection_outcome_events_path", lambda: events_path)

        db = _db_with_outcome_and_ab_arm()
        g = _make_gatherer(tmp_path, db=db)
        db.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id, ab_arm) "
            "VALUES ('d-evt-pl', 'p-evt-pl', 'Storage indexing', '2026-09-20T00:00:00Z', 'vnx-dev', 'placebo')"
        )
        db.commit()
        report = tmp_path / "report.md"
        report.write_text("crawler pipeline work and memory leak cleanup\n")
        g.record_adoption_from_receipt("d-evt-pl", "T1", str(report))

        event = json.loads(events_path.read_text(encoding="utf-8").strip())
        assert event["ab_arm"] == "placebo"

    def test_pre_v32_db_writes_without_ab_arm_column(self, tmp_path, monkeypatch):
        """A DB predating v32 has no ab_arm column on pattern_injection_outcome;
        the outcome write must still succeed (degrade, not crash)."""
        monkeypatch.setenv("VNX_INJECTION_WHY_ENABLED", "1")
        db = _db_with_outcome_no_ab_arm()
        g = _make_gatherer(tmp_path, db=db)
        db.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id) "
            "VALUES ('d-old', 'p-old', 'Crawler cleanup', '2026-09-20T00:00:00Z', 'vnx-dev')"
        )
        db.commit()
        report = tmp_path / "report.md"
        report.write_text("crawler cleanup pipeline memory leak handling\n")
        g.record_adoption_from_receipt("d-old", "T1", str(report))

        row = db.execute(
            "SELECT used FROM pattern_injection_outcome "
            "WHERE dispatch_id='d-old' AND pattern_id='p-old'"
        ).fetchone()
        assert row is not None
        assert row["used"] == 1


# ---------------------------------------------------------------------------
# 5. run_placebo — per-arm adoption readout, incl. all-zero + no-column cases
# ---------------------------------------------------------------------------

class TestRunPlacebo:
    def _import_run_placebo(self):
        sys.path.insert(0, str(_SCRIPTS))
        import importlib
        import intel_injection_join as iij
        importlib.reload(iij)
        return iij

    def test_prints_cleanly_against_zero_rows(self, tmp_path, capsys):
        iij = self._import_run_placebo()
        qi = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(qi))
        conn.executescript("""
            CREATE TABLE dispatch_pattern_offered (
                dispatch_id TEXT, pattern_id TEXT, pattern_title TEXT,
                offered_at TEXT, project_id TEXT DEFAULT '',
                ab_arm TEXT,
                PRIMARY KEY (dispatch_id, pattern_id, project_id)
            );
            CREATE TABLE pattern_injection_outcome (
                id INTEGER PRIMARY KEY, dispatch_id TEXT, pattern_id TEXT,
                pattern_hash TEXT, used INTEGER, reason TEXT, evidence TEXT,
                project_id TEXT DEFAULT 'vnx-dev', created_at TEXT,
                ab_arm TEXT,
                UNIQUE (project_id, dispatch_id, pattern_id)
            );
        """)
        conn.commit()
        conn.close()

        rc = iij.run_placebo(qi)
        out = capsys.readouterr().out
        assert rc == 0
        assert "arm" in out and "offered" in out and "adopt%" in out
        # all arms present even at zero, including the unknown bucket
        for arm in ("placebo", "treatment", "control", "unknown"):
            assert arm in out
        # no_outcome column is present in the header even at zero
        assert "no_out" in out

    def test_side_by_side_with_data(self, tmp_path, capsys):
        iij = self._import_run_placebo()
        qi = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(qi))
        conn.executescript("""
            CREATE TABLE dispatch_pattern_offered (
                dispatch_id TEXT, pattern_id TEXT, pattern_title TEXT,
                offered_at TEXT, project_id TEXT DEFAULT '',
                ab_arm TEXT,
                PRIMARY KEY (dispatch_id, pattern_id, project_id)
            );
            CREATE TABLE pattern_injection_outcome (
                id INTEGER PRIMARY KEY, dispatch_id TEXT, pattern_id TEXT,
                pattern_hash TEXT, used INTEGER, reason TEXT, evidence TEXT,
                project_id TEXT DEFAULT 'vnx-dev', created_at TEXT,
                ab_arm TEXT,
                UNIQUE (project_id, dispatch_id, pattern_id)
            );
        """)
        # 2 treatment offers, 1 used; 2 placebo offers, 0 used.
        for i in range(2):
            conn.execute(
                "INSERT INTO dispatch_pattern_offered "
                "(dispatch_id, pattern_id, pattern_title, offered_at, project_id, ab_arm) "
                "VALUES (?, ?, 't', '2026-09-20T00:00:00Z', 'p', 'treatment')",
                (f"dtr{i}", f"ptr{i}"),
            )
            conn.execute(
                "INSERT INTO dispatch_pattern_offered "
                "(dispatch_id, pattern_id, pattern_title, offered_at, project_id, ab_arm) "
                "VALUES (?, ?, 't', '2026-09-20T00:00:00Z', 'p', 'placebo')",
                (f"dpl{i}", f"ppl{i}"),
            )
        # treatment: 1 used, 1 ignored
        conn.execute(
            "INSERT INTO pattern_injection_outcome "
            "(dispatch_id, pattern_id, pattern_hash, used, project_id, created_at, ab_arm) "
            "VALUES ('dtr0', 'ptr0', 'ptr0', 1, 'p', '2026-09-20T00:00:00Z', 'treatment')"
        )
        conn.execute(
            "INSERT INTO pattern_injection_outcome "
            "(dispatch_id, pattern_id, pattern_hash, used, reason, project_id, created_at, ab_arm) "
            "VALUES ('dtr1', 'ptr1', 'ptr1', 0, 'low-signal', 'p', '2026-09-20T00:00:00Z', 'treatment')"
        )
        # placebo: both ignored
        for i in range(2):
            conn.execute(
                "INSERT INTO pattern_injection_outcome "
                "(dispatch_id, pattern_id, pattern_hash, used, reason, project_id, created_at, ab_arm) "
                "VALUES (?, ?, ?, 0, 'low-signal', 'p', '2026-09-20T00:00:00Z', 'placebo')",
                (f"dpl{i}", f"ppl{i}", f"ppl{i}"),
            )
        conn.commit()
        conn.close()

        rc = iij.run_placebo(qi)
        out = capsys.readouterr().out
        assert rc == 0
        # treatment 50%, placebo 0%, delta +50.0
        assert "treatment" in out and "placebo" in out
        assert "50.0" in out
        assert "+50.0" in out  # delta readout

    def test_degrades_when_ab_arm_column_absent(self, tmp_path, capsys):
        iij = self._import_run_placebo()
        qi = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(qi))
        conn.executescript("""
            CREATE TABLE dispatch_pattern_offered (
                dispatch_id TEXT, pattern_id TEXT, pattern_title TEXT,
                offered_at TEXT, project_id TEXT DEFAULT '',
                PRIMARY KEY (dispatch_id, pattern_id, project_id)
            );
            CREATE TABLE pattern_injection_outcome (
                id INTEGER PRIMARY KEY, dispatch_id TEXT, pattern_id TEXT,
                pattern_hash TEXT, used INTEGER, reason TEXT, evidence TEXT,
                project_id TEXT DEFAULT 'vnx-dev', created_at TEXT,
                UNIQUE (project_id, dispatch_id, pattern_id)
            );
        """)
        conn.commit()
        conn.close()

        rc = iij.run_placebo(qi)
        out = capsys.readouterr().out
        assert rc == 0
        # explicit note that the column is absent, and every offer reads as
        # "unknown" (the report no longer fabricates a 'treatment' arm for
        # rows that predate the arm label)
        assert "ABSENT" in out
        assert "unknown" in out

    # The OLD query grouped on the OUTCOME's arm with COALESCE(pio.ab_arm,
    # 'treatment'), so a placebo offer that produced no outcome row fell
    # through the COALESCE into the 'treatment' bucket. The NEW query groups
    # on the OFFER's arm, so the same offer counts in 'placebo' and surfaces
    # as no_outcome. This test pins both: the old SQL misclassifies, the new
    # run_placebo does not.
    _OLD_PLACEBO_SQL = """
SELECT
    COALESCE(pio.ab_arm, 'treatment')                       AS ab_arm,
    COUNT(*)                                                AS offered,
    SUM(CASE WHEN pio.used = 1 THEN 1 ELSE 0 END)           AS used,
    SUM(CASE WHEN pio.used = 0 THEN 1 ELSE 0 END)           AS ignored,
    SUM(CASE WHEN pio.used IS NULL THEN 1 ELSE 0 END)       AS no_outcome
FROM dispatch_pattern_offered o
LEFT JOIN pattern_injection_outcome pio
    ON pio.dispatch_id = o.dispatch_id
   AND pio.pattern_id  = o.pattern_id
GROUP BY COALESCE(pio.ab_arm, 'treatment')
ORDER BY ab_arm
"""

    def test_placebo_offer_without_outcome_counts_as_placebo_not_treatment(
        self, tmp_path, capsys
    ):
        iij = self._import_run_placebo()
        qi = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(qi))
        conn.executescript("""
            CREATE TABLE dispatch_pattern_offered (
                dispatch_id TEXT, pattern_id TEXT, pattern_title TEXT,
                offered_at TEXT, project_id TEXT DEFAULT '',
                ab_arm TEXT,
                PRIMARY KEY (dispatch_id, pattern_id, project_id)
            );
            CREATE TABLE pattern_injection_outcome (
                id INTEGER PRIMARY KEY, dispatch_id TEXT, pattern_id TEXT,
                pattern_hash TEXT, used INTEGER, reason TEXT, evidence TEXT,
                project_id TEXT DEFAULT 'vnx-dev', created_at TEXT,
                ab_arm TEXT,
                UNIQUE (project_id, dispatch_id, pattern_id)
            );
        """)
        # One placebo offer, NO outcome row (dispatch failed / no report /
        # writer never ran). Plus one treatment offer WITH a used=1 outcome.
        conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id, ab_arm) "
            "VALUES ('d-pl', 'p-pl', 'Placebo pattern', '2026-09-20T00:00:00Z', 'p', 'placebo')"
        )
        conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id, ab_arm) "
            "VALUES ('d-tr', 'p-tr', 'Treatment pattern', '2026-09-20T00:00:00Z', 'p', 'treatment')"
        )
        conn.execute(
            "INSERT INTO pattern_injection_outcome "
            "(dispatch_id, pattern_id, pattern_hash, used, project_id, created_at, ab_arm) "
            "VALUES ('d-tr', 'p-tr', 'p-tr', 1, 'p', '2026-09-20T00:00:00Z', 'treatment')"
        )
        conn.commit()
        conn.close()

        # --- OLD query: groups on the outcome's arm with COALESCE to treatment ---
        old_conn = sqlite3.connect(str(qi))
        old_conn.row_factory = sqlite3.Row
        old_rows = {
            r["ab_arm"]: dict(r)
            for r in old_conn.execute(self._OLD_PLACEBO_SQL).fetchall()
        }
        old_conn.close()
        # The old query drops the no-outcome placebo offer into 'treatment':
        # pio.ab_arm is NULL for the unmatched placebo row, COALESCE makes it
        # 'treatment'. So 'treatment' gets offered=2 (the real treatment offer
        # PLUS the misclassified placebo offer), and 'placebo' gets offered=0.
        assert old_rows["treatment"]["offered"] == 2
        assert "placebo" not in old_rows or old_rows["placebo"]["offered"] == 0
        assert old_rows["treatment"]["no_outcome"] == 1  # the leaked placebo

        # --- NEW run_placebo: groups on the OFFER's arm ---
        rc = iij.run_placebo(qi)
        out = capsys.readouterr().out
        assert rc == 0
        # The placebo offer counts in placebo (offered=1, no_outcome=1), not
        # in treatment. Treatment keeps offered=1, used=1.
        # Parse the per-arm rows from the printed table.
        assert "placebo" in out
        # The placebo line: offered 1, used 0, ignored 0, no_out 1, adopt% n/a
        placebo_line = [
            ln for ln in out.splitlines() if ln.lstrip().startswith("placebo")
        ][0]
        parts = placebo_line.split()
        # columns: arm offered used ignored no_out adopt%
        assert parts[1] == "1"   # offered
        assert parts[2] == "0"   # used
        assert parts[3] == "0"   # ignored
        assert parts[4] == "1"   # no_outcome
        # treatment line: offered 1, used 1, ignored 0, no_out 0, adopt% 100.0
        treatment_line = [
            ln for ln in out.splitlines() if ln.lstrip().startswith("treatment")
        ][0]
        tparts = treatment_line.split()
        assert tparts[1] == "1"  # offered
        assert tparts[2] == "1"  # used
        assert tparts[4] == "0"  # no_outcome

    def test_historical_offer_without_arm_shown_as_unknown(self, tmp_path, capsys):
        """An offer whose ab_arm is NULL (predates the v32 arm label) shows
        up in the 'unknown' bucket, not folded into 'treatment'. This is the
        three-billion-rows defense: a historical row is not a treatment row."""
        iij = self._import_run_placebo()
        qi = tmp_path / "quality_intelligence.db"
        conn = sqlite3.connect(str(qi))
        conn.executescript("""
            CREATE TABLE dispatch_pattern_offered (
                dispatch_id TEXT, pattern_id TEXT, pattern_title TEXT,
                offered_at TEXT, project_id TEXT DEFAULT '',
                ab_arm TEXT,
                PRIMARY KEY (dispatch_id, pattern_id, project_id)
            );
            CREATE TABLE pattern_injection_outcome (
                id INTEGER PRIMARY KEY, dispatch_id TEXT, pattern_id TEXT,
                pattern_hash TEXT, used INTEGER, reason TEXT, evidence TEXT,
                project_id TEXT DEFAULT 'vnx-dev', created_at TEXT,
                ab_arm TEXT,
                UNIQUE (project_id, dispatch_id, pattern_id)
            );
        """)
        # A historical offer: no ab_arm written (NULL).
        conn.execute(
            "INSERT INTO dispatch_pattern_offered "
            "(dispatch_id, pattern_id, pattern_title, offered_at, project_id) "
            "VALUES ('d-hist', 'p-hist', 'Old pattern', '2026-09-20T00:00:00Z', 'p')"
        )
        conn.commit()
        conn.close()

        rc = iij.run_placebo(qi)
        out = capsys.readouterr().out
        assert rc == 0
        # The unknown bucket carries the historical offer.
        unknown_line = [
            ln for ln in out.splitlines() if ln.lstrip().startswith("unknown")
        ][0]
        parts = unknown_line.split()
        assert parts[1] == "1"  # offered
        assert parts[4] == "1"  # no_outcome (no outcome row either)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))