#!/usr/bin/env python3
"""Wave 1 PR #454 fix-forward tests.

Verifies:
  1. Central SQL SELECT clauses now carry project_id → metric 1 can detect
     wrong-project contamination in central rows.
  2. VNX_USE_CENTRAL_DB="1" routes exclusively to central (no per-project fallback).
  3. /api/operator/system-health respects the cutover flag.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(SCRIPTS_LIB))
sys.path.insert(0, str(DASHBOARD_DIR))

import shadow_verifier as sv
import api_intelligence as ai
import api_operator as ao


# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------

def _make_per_project_db(
    path: Path, n_patterns: int = 3, project_id: str = "local-project"
) -> None:
    """Create a per-project quality_intelligence.db with project_id column (post-ADR-007)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE success_patterns (
            project_id TEXT, title TEXT, confidence_score REAL, category TEXT,
            usage_count INT, last_used TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE antipatterns (
            project_id TEXT, title TEXT, severity TEXT, occurrence_count INT, last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE prevention_rules (name TEXT, rule TEXT)
    """)
    conn.execute("""
        CREATE TABLE dispatch_metadata (dispatch_id TEXT, status TEXT)
    """)
    for i in range(n_patterns):
        conn.execute(
            "INSERT INTO success_patterns VALUES (?,?,?,?,?,?)",
            (project_id, f"local-pattern-{i}", 0.8 - i * 0.1, "test", i + 1, "2026-01-01"),
        )
    conn.commit()
    conn.close()


def _make_central_db(
    path: Path,
    project_id: str,
    n_patterns: int = 7,
    wrong_project_row: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE success_patterns (
            project_id TEXT, title TEXT, confidence_score REAL,
            category TEXT, usage_count INT, last_used TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE antipatterns (
            project_id TEXT, title TEXT, severity TEXT,
            occurrence_count INT, last_seen TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE prevention_rules (project_id TEXT, name TEXT, rule TEXT)
    """)
    conn.execute("""
        CREATE TABLE dispatch_metadata (project_id TEXT, dispatch_id TEXT, status TEXT)
    """)
    for i in range(n_patterns):
        conn.execute(
            "INSERT INTO success_patterns VALUES (?,?,?,?,?,?)",
            (project_id, f"central-pattern-{i}", 0.9 - i * 0.05, "test", i + 2, "2026-01-02"),
        )
        conn.execute(
            "INSERT INTO dispatch_metadata VALUES (?,?,?)",
            (project_id, f"d-{i}", "done"),
        )
    if wrong_project_row:
        conn.execute(
            "INSERT INTO success_patterns VALUES (?,?,?,?,?,?)",
            ("wrong-project", "leaked-pattern", 0.3, "leak", 1, "2026-01-01"),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test 1 — project_id propagation + metric 1 detection
# ---------------------------------------------------------------------------


class TestDashboardIntelligenceShadowPropagatesProjectId:
    """BLOCKING fix: central SQL now selects project_id; metric 1 can fire."""

    PROJECT_ID = "seocrawler-v2"

    def test_central_sql_rows_carry_project_id(self, tmp_path: Path) -> None:
        """After SQL fix, _fetch_success_patterns central rows include project_id key."""
        central_db = tmp_path / "central.db"
        conn = sqlite3.connect(str(central_db))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE success_patterns (
                project_id TEXT, title TEXT, confidence_score REAL,
                category TEXT, usage_count INT, last_used TEXT
            )
        """)
        conn.execute(
            "INSERT INTO success_patterns VALUES (?,?,?,?,?,?)",
            (self.PROJECT_ID, "good-pattern", 0.9, "test", 5, "2026-01-01"),
        )
        conn.commit()

        raw_rows, _ = ai._fetch_success_patterns(conn, 10, project_id=self.PROJECT_ID)
        conn.close()

        assert raw_rows, "central fetch must return rows"
        assert "project_id" in raw_rows[0], "row must carry project_id after SQL fix"
        assert raw_rows[0]["project_id"] == self.PROJECT_ID

    def test_metric_1_fires_with_wrong_project_row(self) -> None:
        """metric_id=1 detects wrong-project contamination when rows carry project_id."""
        correct = {
            "project_id": self.PROJECT_ID,
            "title": "ok-pattern",
            "confidence_score": 0.9,
        }
        leaked = {
            "project_id": "other-project",
            "title": "leaked-pattern",
            "confidence_score": 0.3,
        }

        result = sv.compare(
            legacy_rows=[],
            central_rows=[correct, leaked],
            project_id=self.PROJECT_ID,
            read_site="test.intelligence_patterns.success_patterns",
            sql_template=ai._PATTERNS_SUCCESS_CENTRAL_SQL,
            metric_id=1,
        )

        assert result.divergences, "metric 1 must fire when central has wrong-project row"
        assert result.divergences[0].metric_id == 1
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        assert result.divergences[0].detail["wrong_central_count"] == 1

    def test_metric_1_clean_when_all_rows_match_project(self) -> None:
        """No divergence when all central rows have the correct project_id."""
        rows = [
            {"project_id": self.PROJECT_ID, "title": f"p-{i}", "confidence_score": 0.8}
            for i in range(5)
        ]
        result = sv.compare(
            legacy_rows=[],
            central_rows=rows,
            project_id=self.PROJECT_ID,
            read_site="test.intelligence_patterns.success_patterns",
            sql_template=ai._PATTERNS_SUCCESS_CENTRAL_SQL,
            metric_id=1,
        )
        assert not result.divergences, "metric 1 must not fire when all rows are clean"

    def test_antipattern_central_sql_carries_project_id(self, tmp_path: Path) -> None:
        """_fetch_antipatterns central rows include project_id after SQL fix."""
        central_db = tmp_path / "central.db"
        conn = sqlite3.connect(str(central_db))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE antipatterns (
                project_id TEXT, title TEXT, severity TEXT,
                occurrence_count INT, last_seen TEXT
            )
        """)
        conn.execute(
            "INSERT INTO antipatterns VALUES (?,?,?,?,?)",
            (self.PROJECT_ID, "bad-thing", "high", 3, "2026-01-01"),
        )
        conn.commit()

        raw_rows, _ = ai._fetch_antipatterns(conn, 10, project_id=self.PROJECT_ID)
        conn.close()

        assert raw_rows
        assert "project_id" in raw_rows[0], "antipattern row must carry project_id"
        assert raw_rows[0]["project_id"] == self.PROJECT_ID


# ---------------------------------------------------------------------------
# Test 2 — Cutover mode (flag == "1") uses central path exclusively
# ---------------------------------------------------------------------------


class TestDashboardCutoverModeUsesCentralPath:
    """ADVISORY 1 fix: VNX_USE_CENTRAL_DB=1 must read from central, not per-project."""

    PROJECT_ID = "seocrawler-v2"

    def test_cutover_reads_from_central_when_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """flag=1 with central available → response reflects central data count."""
        per_proj_db = tmp_path / "local.db"
        central_db = tmp_path / "central" / "quality_intelligence.db"

        _make_per_project_db(per_proj_db, n_patterns=2)
        _make_central_db(central_db, self.PROJECT_ID, n_patterns=7)

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")

        mock_sd = MagicMock()
        mock_sd.DB_PATH = per_proj_db

        with (
            patch.object(ai, "_sd", return_value=mock_sd),
            patch.object(ai, "_dashboard_project_id", return_value=self.PROJECT_ID),
            patch.object(ai, "_central_qi_db", return_value=central_db),
            patch.object(ai, "_shadow_logger", None),
        ):
            result = ai._intelligence_get_patterns({})

        assert len(result["success_patterns"]) == 7, (
            "flag=1 must read from central (7 patterns), not per-project (2 patterns)"
        )

    def test_cutover_returns_empty_when_central_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """flag=1 with central unavailable → empty response (no per-project fallback)."""
        per_proj_db = tmp_path / "local.db"
        _make_per_project_db(per_proj_db, n_patterns=5)

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")

        mock_sd = MagicMock()
        mock_sd.DB_PATH = per_proj_db

        with (
            patch.object(ai, "_sd", return_value=mock_sd),
            patch.object(ai, "_dashboard_project_id", return_value=self.PROJECT_ID),
            patch.object(ai, "_central_qi_db", return_value=None),
            patch.object(ai, "_shadow_logger", None),
        ):
            result = ai._intelligence_get_patterns({})

        assert result["success_patterns"] == [], (
            "flag=1 must NOT fall back to per-project when central is unavailable"
        )
        assert result["antipatterns"] == []


# ---------------------------------------------------------------------------
# Test 3 — /api/operator/system-health honours the cutover flag
# ---------------------------------------------------------------------------


class TestOperatorSystemHealthHonorsCutoverFlag:
    """ADVISORY 2 fix: system-health intelligence counts come from central when flag=1."""

    PROJECT_ID = "seocrawler-v2"

    def test_cutover_reads_intelligence_counts_from_central(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """flag=1 → intelligence_db details reflect central row counts, not per-project."""
        per_proj_db = tmp_path / "local.db"
        central_db = tmp_path / "central" / "quality_intelligence.db"

        _make_per_project_db(per_proj_db, n_patterns=3)
        _make_central_db(central_db, self.PROJECT_ID, n_patterns=11)

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")

        with (
            patch.object(ao, "DB_PATH", per_proj_db),
            patch.object(ao, "_op_central_qi_db", return_value=central_db),
            patch.object(ao, "_op_dashboard_project_id", return_value=self.PROJECT_ID),
            patch.object(ao, "CANONICAL_STATE_DIR", tmp_path / "state"),
            patch.object(ao, "RECEIPTS_PATH", tmp_path / "receipts.ndjson"),
            patch.object(ao, "REPORTS_DIR", tmp_path / "reports"),
        ):
            result = ao._operator_get_system_health()

        intel = result["components"]["intelligence_db"]
        assert intel["status"] != "dead", "should read from central, not report missing"
        dispatch_count = intel["details"].get("dispatch_metadata", 0)
        assert dispatch_count == 11, (
            f"flag=1 must count dispatch_metadata from central (11), got {dispatch_count}"
        )

    def test_cutover_reports_dead_when_central_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """flag=1 with no central → intelligence_db status=dead (no per-project fallback)."""
        per_proj_db = tmp_path / "local.db"
        _make_per_project_db(per_proj_db, n_patterns=5)

        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")

        with (
            patch.object(ao, "DB_PATH", per_proj_db),
            patch.object(ao, "_op_central_qi_db", return_value=None),
            patch.object(ao, "_op_dashboard_project_id", return_value=self.PROJECT_ID),
            patch.object(ao, "CANONICAL_STATE_DIR", tmp_path / "state"),
            patch.object(ao, "RECEIPTS_PATH", tmp_path / "receipts.ndjson"),
            patch.object(ao, "REPORTS_DIR", tmp_path / "reports"),
        ):
            result = ao._operator_get_system_health()

        intel = result["components"]["intelligence_db"]
        assert intel["status"] == "dead", "flag=1 with no central must report dead"
        assert "error" in intel["details"]


# ---------------------------------------------------------------------------
# Test 4 — Symmetric project_id columns prevent metric-4 false positives
# ---------------------------------------------------------------------------


class TestLegacyAndCentralRowsSymmetricColumns:
    """BLOCKING #1 fix: legacy SQL now selects project_id — metric 4 must not false-positive."""

    PROJECT_ID = "seocrawler-v2"
    _SAME_SP = [
        ("seocrawler-v2", "pattern-alpha", 0.9, "test", 5, "2026-01-01"),
        ("seocrawler-v2", "pattern-beta", 0.8, "refactor", 3, "2026-01-02"),
    ]
    _SAME_AP = [
        ("seocrawler-v2", "bad-thing", "high", 3, "2026-01-01"),
    ]

    def _build_db(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.execute("""
            CREATE TABLE success_patterns (
                project_id TEXT, title TEXT, confidence_score REAL,
                category TEXT, usage_count INT, last_used TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE antipatterns (
                project_id TEXT, title TEXT, severity TEXT,
                occurrence_count INT, last_seen TEXT
            )
        """)
        for row in self._SAME_SP:
            conn.execute("INSERT INTO success_patterns VALUES (?,?,?,?,?,?)", row)
        for row in self._SAME_AP:
            conn.execute("INSERT INTO antipatterns VALUES (?,?,?,?,?)", row)
        conn.commit()
        conn.close()

    def test_metric_4_no_false_positive_success_patterns(self, tmp_path: Path) -> None:
        """Identical data in legacy + central must yield no hard divergence on metric 4."""
        per_proj = tmp_path / "local.db"
        central = tmp_path / "central.db"
        self._build_db(per_proj)
        self._build_db(central)

        pconn = sqlite3.connect(str(per_proj))
        pconn.row_factory = sqlite3.Row
        cconn = sqlite3.connect(str(central))
        cconn.row_factory = sqlite3.Row
        legacy_raw, _ = ai._fetch_success_patterns(pconn, 50)
        central_raw, _ = ai._fetch_success_patterns(cconn, 50, project_id=self.PROJECT_ID)
        pconn.close()
        cconn.close()

        assert legacy_raw, "legacy fetch must return rows — project_id column now present"
        assert "project_id" in legacy_raw[0], "legacy row must carry project_id after SQL fix"

        result = sv.compare(
            legacy_raw, central_raw,
            project_id=self.PROJECT_ID,
            read_site="test.symmetry.success_patterns",
            sql_template=ai._PATTERNS_SUCCESS_SQL,
            metric_id=4,
            table="success_patterns",
        )
        hard = [d for d in result.divergences if d.severity == sv.SEVERITY_HARD]
        assert not hard, f"metric 4 must not false-positive when data is identical: {hard}"

    def test_metric_4_no_false_positive_antipatterns(self, tmp_path: Path) -> None:
        """Identical antipattern data in legacy + central must yield no hard divergence."""
        per_proj = tmp_path / "local.db"
        central = tmp_path / "central.db"
        self._build_db(per_proj)
        self._build_db(central)

        pconn = sqlite3.connect(str(per_proj))
        pconn.row_factory = sqlite3.Row
        cconn = sqlite3.connect(str(central))
        cconn.row_factory = sqlite3.Row
        legacy_raw, _ = ai._fetch_antipatterns(pconn, 50)
        central_raw, _ = ai._fetch_antipatterns(cconn, 50, project_id=self.PROJECT_ID)
        pconn.close()
        cconn.close()

        assert legacy_raw, "legacy antipatterns fetch must return rows"
        assert "project_id" in legacy_raw[0], "antipattern legacy row must carry project_id"

        result = sv.compare(
            legacy_raw, central_raw,
            project_id=self.PROJECT_ID,
            read_site="test.symmetry.antipatterns",
            sql_template=ai._PATTERNS_ANTI_SQL,
            metric_id=4,
            table="antipatterns",
        )
        hard = [d for d in result.divergences if d.severity == sv.SEVERITY_HARD]
        assert not hard, f"metric 4 must not false-positive for antipatterns: {hard}"


# ---------------------------------------------------------------------------
# Test 5 — Unknown VNX_USE_CENTRAL_DB value falls back to legacy + logs warning
# ---------------------------------------------------------------------------


class TestUnknownFlagValueFallsBackToLegacy:
    """BLOCKING #2 fix: any value outside {'', '1', 'shadow'} must go legacy + warn."""

    def test_flag_zero_does_not_activate_shadow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """flag='0' must NOT activate shadow mode; must return per-project data."""
        per_proj_db = tmp_path / "local.db"
        _make_per_project_db(per_proj_db, n_patterns=4)
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "0")

        mock_sd = MagicMock()
        mock_sd.DB_PATH = per_proj_db

        shadow_compare_calls: list = []

        with (
            patch.object(ai, "_sd", return_value=mock_sd),
            patch.object(ai, "_shadow_verifier", MagicMock(
                compare=lambda *a, **kw: shadow_compare_calls.append(1) or sv.ComparisonResult(),
            )),
        ):
            result = ai._intelligence_get_patterns({})

        assert not shadow_compare_calls, (
            "shadow_verifier.compare must NOT be called when flag='0'"
        )
        assert len(result["success_patterns"]) == 4, (
            "flag='0' must return per-project data (4 patterns), not empty"
        )

    def test_flag_zero_logs_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """flag='0' must emit a warning that includes the unknown value."""
        per_proj_db = tmp_path / "local.db"
        _make_per_project_db(per_proj_db, n_patterns=1)
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "0")

        mock_sd = MagicMock()
        mock_sd.DB_PATH = per_proj_db

        import logging as _logging
        with caplog.at_level(_logging.WARNING, logger="api_intelligence"):
            with patch.object(ai, "_sd", return_value=mock_sd):
                ai._intelligence_get_patterns({})

        assert any("0" in r.message for r in caplog.records), (
            "warning must mention the unknown flag value '0'"
        )

    def test_arbitrary_flag_string_falls_back_to_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any non-contract flag value must return per-project data, not empty."""
        per_proj_db = tmp_path / "local.db"
        _make_per_project_db(per_proj_db, n_patterns=2)
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "disabled")

        mock_sd = MagicMock()
        mock_sd.DB_PATH = per_proj_db

        with patch.object(ai, "_sd", return_value=mock_sd):
            result = ai._intelligence_get_patterns({})

        assert len(result["success_patterns"]) == 2, (
            "arbitrary flag must fall back to legacy (2 patterns), not empty"
        )
