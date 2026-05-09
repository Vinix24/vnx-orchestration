#!/usr/bin/env python3
"""Tests for the Wave 1 shadow-mode divergence comparator.

Covers:
  - Verifier independence (no import from migrate_to_central_vnx)
  - PRAGMA-based schema introspection
  - All 6 hard metrics + aggregate count path
  - Severity routing (hard / soft / aggregate)
  - ComparisonResult.has_hard_divergence() API
"""

from __future__ import annotations

import ast
import importlib
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR / "lib"))

import shadow_verifier as sv  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def row(**kwargs: Any) -> dict[str, Any]:
    return dict(kwargs)


def _cmp(
    metric_id: int,
    legacy: list,
    central: list,
    project_id: str = "proj_a",
    read_site: str = "test_site",
    sql: str = "SELECT * FROM t WHERE project_id = ?",
    legacy_ms: float = 10.0,
    central_ms: float = 10.0,
) -> sv.ComparisonResult:
    return sv.compare(
        legacy_rows=legacy,
        central_rows=central,
        project_id=project_id,
        read_site=read_site,
        sql_template=sql,
        metric_id=metric_id,
        legacy_latency_ms=legacy_ms,
        central_latency_ms=central_ms,
    )


# ---------------------------------------------------------------------------
# Verifier independence
# ---------------------------------------------------------------------------


class TestVerifierIndependence:
    def test_no_import_from_migrate_to_central_vnx(self):
        """AST + runtime: shadow_verifier must not import migrate_to_central_vnx."""
        sv_path = SCRIPTS_DIR / "lib" / "shadow_verifier.py"
        tree = ast.parse(sv_path.read_text())

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "migrate_to_central_vnx" not in alias.name, (
                        f"Direct import of migrate_to_central_vnx: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "migrate_to_central_vnx" not in module, (
                    f"Import-from migrate_to_central_vnx: {module}"
                )

        # Runtime check: transitive import must not pull in the migration module
        saved = sys.modules.pop("shadow_verifier", None)
        sys.modules.pop("migrate_to_central_vnx", None)
        try:
            importlib.import_module("shadow_verifier")
            assert "migrate_to_central_vnx" not in sys.modules, (
                "shadow_verifier transitively imported migrate_to_central_vnx"
            )
        finally:
            if saved is not None:
                sys.modules["shadow_verifier"] = saved

    def test_introspect_uses_pragma_not_hardcoded_columns(self, tmp_path: Path):
        """PRAGMA introspection returns updated column list after ALTER TABLE."""
        db = tmp_path / "introspect_test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL)"
        )
        conn.commit()

        cols_before = sv._introspect_table_columns(conn, "items")
        assert "id" in cols_before
        assert "project_id" in cols_before
        assert "extra_col" not in cols_before

        conn.execute("ALTER TABLE items ADD COLUMN extra_col TEXT")
        conn.commit()

        cols_after = sv._introspect_table_columns(conn, "items")
        assert "extra_col" in cols_after, (
            "PRAGMA introspection did not pick up the new column — "
            "indicates hardcoded list rather than live PRAGMA read"
        )
        assert len(cols_after) == len(cols_before) + 1

        conn.close()


# ---------------------------------------------------------------------------
# Metric 1 — Wrong-project rows
# ---------------------------------------------------------------------------


class TestMetric1WrongProjectRows:
    def test_metric_1_zero_violations_when_all_correct_project(self):
        rows = [row(project_id="proj_a", val=1), row(project_id="proj_a", val=2)]
        result = _cmp(1, rows, rows)
        assert not result.divergences
        assert not result.has_hard_divergence()

    def test_metric_1_detects_wrong_project_row(self):
        legacy = [row(project_id="proj_a", val=1)]
        central = [row(project_id="proj_b", val=1)]  # cross-tenant contamination
        result = _cmp(1, legacy, central, project_id="proj_a")
        assert len(result.divergences) == 1
        e = result.divergences[0]
        assert e.metric_id == 1
        assert e.severity == sv.SEVERITY_HARD
        assert e.detail["wrong_central_count"] == 1
        assert result.has_hard_divergence()


# ---------------------------------------------------------------------------
# Metric 2 — PR-scoped blocking findings parity
# ---------------------------------------------------------------------------


class TestMetric2BlockingFindings:
    def test_metric_2_zero_violations_when_blocking_findings_match(self):
        rows = [
            row(hash="abc123", severity="blocking"),
            row(hash="def456", severity="blocking"),
        ]
        result = _cmp(2, rows, rows)
        assert not result.divergences

    def test_metric_2_detects_missing_blocking_finding(self):
        legacy = [
            row(hash="abc123", severity="blocking"),
            row(hash="def456", severity="blocking"),
        ]
        central = [row(hash="abc123", severity="blocking")]  # def456 missing
        result = _cmp(2, legacy, central)
        assert len(result.divergences) == 1
        e = result.divergences[0]
        assert e.metric_id == 2
        assert e.severity == sv.SEVERITY_HARD
        assert "def456" in e.detail["missing_in_central"]
        assert not e.detail["extra_in_central"]

    def test_metric_2_detects_extra_blocking_finding(self):
        legacy = [row(hash="abc123", severity="blocking")]
        central = [
            row(hash="abc123", severity="blocking"),
            row(hash="xyz999", severity="blocking"),  # extra
        ]
        result = _cmp(2, legacy, central)
        assert len(result.divergences) == 1
        e = result.divergences[0]
        assert "xyz999" in e.detail["extra_in_central"]
        assert not e.detail["missing_in_central"]


# ---------------------------------------------------------------------------
# Metric 3 — IntelligenceSelector top-N parity
# ---------------------------------------------------------------------------


class TestMetric3TopNParity:
    def _m3(
        self,
        legacy_ids: list[str],
        central_ids: list[str],
        n: int = 3,
        project_id: str = "proj_a",
    ) -> list[sv.DivergenceEvent]:
        legacy = [row(item_id=i) for i in legacy_ids]
        central = [row(item_id=i) for i in central_ids]
        return sv._compare_metric_3_top_n_parity(legacy, central, n, project_id, "test")

    def test_metric_3_top_3_parity_strict(self):
        assert not self._m3(["a", "b", "c", "d"], ["a", "b", "c", "d"])

    def test_metric_3_detects_top_3_reorder(self):
        # Positions 0 and 1 swapped in central — must be a hard divergence
        events = self._m3(["a", "b", "c"], ["b", "a", "c"])
        assert len(events) == 1
        assert events[0].severity == sv.SEVERITY_HARD
        assert 0 in events[0].detail["divergent_positions"]
        assert 1 in events[0].detail["divergent_positions"]

    def test_metric_3_ignores_below_top_3_differences(self):
        # Top 3 match exactly; item at position 3 differs — should be ignored
        events = self._m3(["a", "b", "c", "x"], ["a", "b", "c", "y"], n=3)
        assert not events


# ---------------------------------------------------------------------------
# Metric 4 — Row count + content checksum parity
# ---------------------------------------------------------------------------


class TestMetric4CountAndChecksum:
    def test_metric_4_zero_count_drift_clean(self):
        rows = [
            row(project_id="p", id=1, val="x"),
            row(project_id="p", id=2, val="y"),
        ]
        result = sv._compare_metric_4_count_and_checksum(rows, rows, "p", "tbl", "test")
        assert not result

    def test_metric_4_detects_count_drift(self):
        legacy = [row(project_id="p", id=1), row(project_id="p", id=2)]
        central = [row(project_id="p", id=1)]
        result = sv._compare_metric_4_count_and_checksum(legacy, central, "p", "tbl", "test")
        assert len(result) == 1
        e = result[0]
        assert e.metric_id == 4
        assert e.severity == sv.SEVERITY_SOFT
        assert e.detail["kind"] == "count_mismatch"
        assert e.detail["count_drift"] == -1

    def test_metric_4_checksum_within_tolerance(self):
        # N=20001, K=1 changed → drift = 1/20001 ≈ 0.005% < 0.01% threshold
        n = 20001
        legacy = [row(project_id="p", id=i, val=f"v{i}") for i in range(n)]
        central = list(legacy)
        central[5000] = row(project_id="p", id=5000, val="race_window_write")
        result = sv._compare_metric_4_count_and_checksum(legacy, central, "p", "tbl", "test")
        assert not result, (
            f"Expected no divergence (drift below 0.01% tolerance), got: {result}"
        )

    def test_metric_4_detects_checksum_above_tolerance(self):
        # N=10, K=2 rows differ → drift = 20% >> 0.01%
        legacy = [row(project_id="p", id=i, val=f"v{i}") for i in range(10)]
        central = list(legacy)
        central[0] = row(project_id="p", id=0, val="CHANGED_A")
        central[1] = row(project_id="p", id=1, val="CHANGED_B")
        result = sv._compare_metric_4_count_and_checksum(legacy, central, "p", "tbl", "test")
        assert len(result) == 1
        assert result[0].detail["kind"] == "checksum_drift"
        assert result[0].detail["drift_pct"] > sv.CHECKSUM_DRIFT_TOLERANCE


# ---------------------------------------------------------------------------
# Metric 5 — Lease-key collisions
# ---------------------------------------------------------------------------


class TestMetric5LeaseCollisions:
    def test_metric_5_zero_lease_collisions_clean(self):
        leases = [
            row(project_id="proj_a", lease_key="T1"),
            row(project_id="proj_b", lease_key="T2"),
        ]
        result = sv._compare_metric_5_lease_collisions(leases, leases, "proj_a", "test")
        assert not result

    def test_metric_5_detects_lease_collision_two_projects_same_key(self):
        legacy = [row(project_id="proj_a", lease_key="T1")]
        central = [
            row(project_id="proj_a", lease_key="T1"),
            row(project_id="proj_b", lease_key="T1"),  # same key, different project
        ]
        result = sv._compare_metric_5_lease_collisions(legacy, central, "proj_a", "test")
        assert len(result) == 1
        e = result[0]
        assert e.metric_id == 5
        assert e.severity == sv.SEVERITY_HARD
        collision_keys = [c["lease_key"] for c in e.detail["collisions"]]
        assert "T1" in collision_keys
        t1_collision = next(c for c in e.detail["collisions"] if c["lease_key"] == "T1")
        assert "proj_a" in t1_collision["projects"]
        assert "proj_b" in t1_collision["projects"]


# ---------------------------------------------------------------------------
# Metric 6 — Read latency budget
# ---------------------------------------------------------------------------


class TestMetric6Latency:
    def test_metric_6_latency_within_threshold(self):
        # 1.4x < 1.5x threshold → no divergence
        result = sv._compare_metric_6_latency(100.0, 140.0, "proj_a", "test")
        assert not result

    def test_metric_6_detects_latency_above_threshold(self):
        # 2.0x > 1.5x threshold → SOFT divergence
        result = sv._compare_metric_6_latency(100.0, 200.0, "proj_a", "test")
        assert len(result) == 1
        e = result[0]
        assert e.metric_id == 6
        assert e.severity == sv.SEVERITY_SOFT
        assert e.detail["actual_factor"] == pytest.approx(2.0)

    def test_metric_6_exact_threshold_is_not_violation(self):
        # Exactly 1.5x is not a violation (central <= 1.5x legacy)
        result = sv._compare_metric_6_latency(100.0, 150.0, "proj_a", "test")
        assert not result

    def test_metric_6_zero_legacy_latency_skipped(self):
        # Cannot compute ratio when legacy_latency_ms <= 0
        result = sv._compare_metric_6_latency(0.0, 999.0, "proj_a", "test")
        assert not result


# ---------------------------------------------------------------------------
# Aggregate count tolerance
# ---------------------------------------------------------------------------


class TestAggregateCount:
    def test_aggregate_count_tolerance_0_1_pct_acceptable(self):
        # drift = 5/10000 = 0.05% < 0.1% tolerance
        result = sv.compare_aggregate_count(10000, 9995, "proj_a", "test", "SELECT COUNT(*) FROM t")
        assert not result.divergences

    def test_aggregate_count_above_tolerance_fails(self):
        # drift = 20/10000 = 0.2% > 0.1% tolerance
        result = sv.compare_aggregate_count(10000, 9980, "proj_a", "test", "SELECT COUNT(*) FROM t")
        assert len(result.divergences) == 1
        e = result.divergences[0]
        assert e.severity == sv.SEVERITY_AGGREGATE
        assert e.detail["kind"] == "aggregate_count"
        assert e.detail["drift_pct"] > sv.AGGREGATE_DRIFT_TOLERANCE

    def test_aggregate_count_exact_tolerance_boundary(self):
        # drift = exactly 0.1% → should fail (>= tolerance)
        result = sv.compare_aggregate_count(10000, 9990, "proj_a", "test", "SELECT COUNT(*) FROM t")
        assert len(result.divergences) == 1


# ---------------------------------------------------------------------------
# Severity routing
# ---------------------------------------------------------------------------


class TestSeverityRouting:
    def test_severity_hard_for_metrics_1_2_3_5(self):
        # Metric 1
        r1 = _cmp(1, [row(project_id="wrong_proj")], [], project_id="proj_a")
        assert r1.divergences and r1.divergences[0].severity == sv.SEVERITY_HARD

        # Metric 2: missing finding
        r2 = _cmp(2, [row(hash="abc")], [])
        assert r2.divergences and r2.divergences[0].severity == sv.SEVERITY_HARD

        # Metric 3: top-3 reorder
        legacy3 = [row(item_id="a"), row(item_id="b"), row(item_id="c")]
        central3 = [row(item_id="b"), row(item_id="a"), row(item_id="c")]
        r3 = _cmp(3, legacy3, central3)
        assert r3.divergences and r3.divergences[0].severity == sv.SEVERITY_HARD

        # Metric 5: lease collision
        legacy5 = [row(project_id="proj_a", lease_key="T1")]
        central5 = [
            row(project_id="proj_a", lease_key="T1"),
            row(project_id="proj_b", lease_key="T1"),
        ]
        r5 = sv._compare_metric_5_lease_collisions(legacy5, central5, "proj_a", "test")
        assert r5 and r5[0].severity == sv.SEVERITY_HARD

    def test_severity_soft_for_metrics_4_6(self):
        # Metric 4: count mismatch
        r4 = sv._compare_metric_4_count_and_checksum(
            [row(project_id="p", id=1)], [], "p", "tbl", "test"
        )
        assert r4 and r4[0].severity == sv.SEVERITY_SOFT

        # Metric 6: latency violation
        r6 = sv._compare_metric_6_latency(100.0, 200.0, "p", "test")
        assert r6 and r6[0].severity == sv.SEVERITY_SOFT

    def test_severity_aggregate_for_aggregate_count(self):
        result = sv.compare_aggregate_count(1000, 988, "p", "test", "SELECT COUNT(*) FROM t")
        assert result.divergences and result.divergences[0].severity == sv.SEVERITY_AGGREGATE


# ---------------------------------------------------------------------------
# ComparisonResult API
# ---------------------------------------------------------------------------


class TestComparisonResult:
    def _hard_event(self) -> sv.DivergenceEvent:
        return sv.DivergenceEvent(
            metric_id=1,
            severity=sv.SEVERITY_HARD,
            project_id="p",
            read_site="test",
            detail={},
            legacy_count=0,
            central_count=1,
            timestamp_iso="2026-01-01T00:00:00+00:00",
        )

    def _soft_event(self) -> sv.DivergenceEvent:
        return sv.DivergenceEvent(
            metric_id=6,
            severity=sv.SEVERITY_SOFT,
            project_id="p",
            read_site="test",
            detail={},
            legacy_count=10,
            central_count=20,
            timestamp_iso="2026-01-01T00:00:00+00:00",
        )

    def test_has_hard_divergence_true_when_any_hard(self):
        result = sv.ComparisonResult()
        result.divergences.append(self._hard_event())
        assert result.has_hard_divergence()

    def test_has_hard_divergence_false_when_all_soft(self):
        result = sv.ComparisonResult()
        result.divergences.append(self._soft_event())
        assert not result.has_hard_divergence()

    def test_has_hard_divergence_mixed_returns_true(self):
        result = sv.ComparisonResult()
        result.divergences.append(self._soft_event())
        result.divergences.append(self._hard_event())
        assert result.has_hard_divergence()

    def test_has_hard_divergence_empty_list_is_false(self):
        result = sv.ComparisonResult()
        assert not result.has_hard_divergence()


# ---------------------------------------------------------------------------
# SQL template hash helper
# ---------------------------------------------------------------------------


class TestSqlTemplateHash:
    def test_named_params_stripped(self):
        h1 = sv._sql_template_hash("SELECT * FROM t WHERE project_id = :pid AND id = :id")
        h2 = sv._sql_template_hash("SELECT * FROM t WHERE project_id = ? AND id = ?")
        assert h1 == h2

    def test_whitespace_normalized(self):
        h1 = sv._sql_template_hash("SELECT *  FROM  t")
        h2 = sv._sql_template_hash("SELECT * FROM t")
        assert h1 == h2

    def test_different_sql_different_hash(self):
        h1 = sv._sql_template_hash("SELECT * FROM t")
        h2 = sv._sql_template_hash("SELECT * FROM other_table")
        assert h1 != h2
