#!/usr/bin/env python3
"""Canary divergence test pack for Wave 1 shadow-mode (PR-W1.5).

Validates all 6 hard metrics fire correctly with deliberate-divergence fixtures
BEFORE shadow mode is used in production. All tests use synthetic in-memory
fixtures — no production DB access, no env-var side effects.

Per claudedocs/2026-05-09-wave1-design.md §7 (pilot run plan): these tests
must pass before the pilot flag is flipped on any project.

Metric reference (Wave 1 design §3):
  1 — Wrong-project rows: tolerance 0 (cross-tenant contamination)
  2 — PR-scoped blocking findings parity: tolerance 0
  3 — IntelligenceSelector top-N parity: tolerance 0 in top 3
  4 — Row count + content checksum: 0 count drift; <0.01% checksum drift
  5 — Lease-key collisions across projects: tolerance 0
  6 — p95 read latency budget: central <= 1.5x per-project p95
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import shadow_verifier as sv  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_PROJECT = "seocrawler-v2"
_OTHER = "mission-control"
_SITE = "canary.test"
_SQL = "SELECT * FROM t WHERE project_id = :p"


def row(**kw: Any) -> dict[str, Any]:
    return dict(kw)


def cmp(
    metric_id: int,
    legacy: list,
    central: list,
    project_id: str = _PROJECT,
    read_site: str = _SITE,
    sql: str = _SQL,
    legacy_ms: float = 10.0,
    central_ms: float = 10.0,
    table: str | None = None,
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
        table=table,
    )


# ---------------------------------------------------------------------------
# Metric 1 — wrong-project rows
# ---------------------------------------------------------------------------


class TestMetric1WrongProjectRows:
    def test_canary_metric_1_legacy_clean_central_has_wrong_project_row(self) -> None:
        """Central returns a row with project_id='other' — metric 1 fires HARD."""
        legacy = [row(project_id=_PROJECT, title="pattern-a")]
        central = [
            row(project_id=_PROJECT, title="pattern-a"),
            row(project_id=_OTHER, title="leaked-pattern"),  # wrong-project row
        ]
        result = cmp(1, legacy, central)
        assert result.divergences, "expected HARD divergence for wrong-project row"
        assert result.divergences[0].metric_id == 1
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        assert result.divergences[0].detail["wrong_central_count"] == 1

    def test_canary_metric_1_both_clean(self) -> None:
        """Both DBs return only matching project_id rows — no divergence."""
        rows = [row(project_id=_PROJECT, title=f"p{i}") for i in range(5)]
        result = cmp(1, rows, rows)
        assert not result.divergences, "expected no divergence when both DBs are clean"

    def test_canary_metric_1_legacy_has_wrong_project_row(self) -> None:
        """Legacy has a wrong-project row — metric 1 fires HARD for legacy side."""
        legacy = [
            row(project_id=_PROJECT, title="pattern-a"),
            row(project_id=_OTHER, title="stale-row"),  # wrong-project in legacy
        ]
        central = [row(project_id=_PROJECT, title="pattern-a")]
        result = cmp(1, legacy, central)
        assert result.divergences
        assert result.divergences[0].metric_id == 1
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        assert result.divergences[0].detail["wrong_legacy_count"] == 1


# ---------------------------------------------------------------------------
# Metric 2 — blocking findings parity
# ---------------------------------------------------------------------------


class TestMetric2BlockingFindings:
    def _finding(self, hash_val: str) -> dict:
        return row(finding_hash=hash_val, severity="blocker", project_id=_PROJECT)

    def test_canary_metric_2_legacy_has_blocking_central_does_not(self) -> None:
        """Legacy has a blocking finding that central is missing — HARD divergence."""
        legacy = [self._finding("aaa"), self._finding("bbb")]
        central = [self._finding("aaa")]
        result = cmp(2, legacy, central)
        assert result.divergences
        assert result.divergences[0].metric_id == 2
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        missing = result.divergences[0].detail["missing_in_central"]
        assert len(missing) == 1

    def test_canary_metric_2_central_has_extra_blocking(self) -> None:
        """Central has an extra blocking finding not in legacy — HARD divergence."""
        legacy = [self._finding("aaa")]
        central = [self._finding("aaa"), self._finding("ccc")]
        result = cmp(2, legacy, central)
        assert result.divergences
        assert result.divergences[0].metric_id == 2
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        extra = result.divergences[0].detail["extra_in_central"]
        assert len(extra) == 1

    def test_canary_metric_2_identical_blocking_set(self) -> None:
        """Both DBs have the same blocking findings — no divergence."""
        findings = [self._finding("x"), self._finding("y"), self._finding("z")]
        result = cmp(2, findings, findings)
        assert not result.divergences


# ---------------------------------------------------------------------------
# Metric 3 — top-N parity
# ---------------------------------------------------------------------------


class TestMetric3TopNParity:
    def _items(self, ids: list[str]) -> list[dict]:
        return [row(item_id=i, project_id=_PROJECT) for i in ids]

    def test_canary_metric_3_top_3_reorder(self) -> None:
        """Items 1+2 swap positions — HARD divergence for metric 3."""
        legacy = self._items(["alpha", "beta", "gamma", "delta"])
        central = self._items(["beta", "alpha", "gamma", "delta"])  # swap top-2
        result = cmp(3, legacy, central)
        assert result.divergences
        assert result.divergences[0].metric_id == 3
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        assert 0 in result.divergences[0].detail["divergent_positions"]
        assert 1 in result.divergences[0].detail["divergent_positions"]

    def test_canary_metric_3_below_top_3_difference_ignored(self) -> None:
        """Items 4-5 differ but top-3 is identical — no divergence."""
        legacy = self._items(["alpha", "beta", "gamma", "delta", "epsilon"])
        central = self._items(["alpha", "beta", "gamma", "zeta", "eta"])
        result = cmp(3, legacy[:3], central[:3])  # verifier compares top-3 slices
        assert not result.divergences

    def test_canary_metric_3_completely_different_top_3(self) -> None:
        """Completely different top-3 set — HARD divergence at all positions."""
        legacy = self._items(["a", "b", "c"])
        central = self._items(["x", "y", "z"])
        result = cmp(3, legacy, central)
        assert result.divergences
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        assert len(result.divergences[0].detail["divergent_positions"]) == 3


# ---------------------------------------------------------------------------
# Metric 4 — count + checksum
# ---------------------------------------------------------------------------


class TestMetric4CountAndChecksum:
    def _dispatch_rows(self, n: int, project_id: str = _PROJECT) -> list[dict]:
        return [
            row(dispatch_id=f"d-{i}", project_id=project_id, status="done", cqs=0.8)
            for i in range(n)
        ]

    def test_canary_metric_4_count_drift_one_extra_row(self) -> None:
        """Central has one extra row — count drift → HARD severity (zero-tolerance)."""
        legacy = self._dispatch_rows(10)
        central = self._dispatch_rows(11)  # one extra
        result = cmp(4, legacy, central, table="dispatch_metadata")
        assert result.divergences
        assert result.divergences[0].metric_id == 4
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        assert result.divergences[0].detail["kind"] == "count_mismatch"
        assert result.divergences[0].detail["count_drift"] == 1

    def test_canary_metric_4_checksum_drift_under_0_01_pct(self) -> None:
        """Tiny checksum drift (1/10001 rows = 0.009999% < 0.01%) — SOFT severity.

        CHECKSUM_DRIFT_TOLERANCE is 0.0001 (0.01%), checked with strict <.
        So 1/10001 < 0.0001 → SOFT; 1/10000 = 0.0001 is NOT < tolerance → HARD.
        """
        rows_a = [row(dispatch_id=f"d-{i}", project_id=_PROJECT, cqs=0.8) for i in range(10001)]
        rows_b = [row(dispatch_id=f"d-{i}", project_id=_PROJECT, cqs=0.8) for i in range(10001)]
        # 1 of 10001 rows differs → drift = 1/10001 ≈ 0.009999% < 0.01% → SOFT
        rows_b[0] = row(dispatch_id="d-0", project_id=_PROJECT, cqs=0.801)
        result = cmp(4, rows_a, rows_b, table="dispatch_metadata")
        assert result.divergences
        assert result.divergences[0].metric_id == 4
        assert result.divergences[0].severity == sv.SEVERITY_SOFT
        assert result.divergences[0].detail["within_tolerance"] is True

    def test_canary_metric_4_checksum_drift_above_0_01_pct(self) -> None:
        """2 of 100 rows differ (2% drift, above 0.01% tolerance) — HARD severity."""
        rows_a = [row(dispatch_id=f"d-{i}", project_id=_PROJECT, cqs=0.8) for i in range(100)]
        rows_b = list(rows_a)
        rows_b[0] = row(dispatch_id="d-0", project_id=_PROJECT, cqs=0.9)
        rows_b[1] = row(dispatch_id="d-1", project_id=_PROJECT, cqs=0.9)
        result = cmp(4, rows_a, rows_b, table="dispatch_metadata")
        assert result.divergences
        assert result.divergences[0].metric_id == 4
        assert result.divergences[0].severity == sv.SEVERITY_HARD
        assert "within_tolerance" not in result.divergences[0].detail

    def test_canary_metric_4_identical_rows_no_divergence(self) -> None:
        """Identical rows — no divergence."""
        rows = self._dispatch_rows(50)
        result = cmp(4, rows, rows, table="dispatch_metadata")
        assert not result.divergences

    def test_canary_metric_4_missing_table_name_emits_advisory(self) -> None:
        """Caller omits table= — ADVISORY divergence emitted (missing contract)."""
        rows = self._dispatch_rows(5)
        result = cmp(4, rows, rows, table=None)
        assert result.divergences
        assert result.divergences[0].severity == sv.SEVERITY_ADVISORY
        assert result.divergences[0].detail["reason"] == "table_identity_missing"


# ---------------------------------------------------------------------------
# Metric 5 — lease collisions
# ---------------------------------------------------------------------------


class TestMetric5LeaseCollisions:
    def test_canary_metric_5_two_projects_same_lease_key(self) -> None:
        """Two projects hold the same lease_key simultaneously — HARD divergence."""
        leases = [
            row(lease_key="T1", project_id=_PROJECT, status="active"),
            row(lease_key="T1", project_id=_OTHER, status="active"),  # collision
        ]
        result = cmp(5, [], leases)
        assert result.divergences
        assert result.divergences[-1].metric_id == 5
        assert result.divergences[-1].severity == sv.SEVERITY_HARD
        collisions = result.divergences[-1].detail.get("collisions", [])
        assert any(c["lease_key"] == "T1" for c in collisions)

    def test_canary_metric_5_lease_row_missing_project_id(self) -> None:
        """Central lease row has no project_id — HARD divergence (PR-W1.1 fix)."""
        leases = [
            row(lease_key="T2", project_id=None, status="active"),
        ]
        result = cmp(5, [], leases)
        missing_pid_events = [
            d for d in result.divergences
            if d.detail.get("reason") == "missing_project_id_in_lease_row"
        ]
        assert missing_pid_events, "expected HARD event for missing project_id in lease row"
        assert missing_pid_events[0].severity == sv.SEVERITY_HARD

    def test_canary_metric_5_clean_leases_no_divergence(self) -> None:
        """Each project holds its own distinct lease keys — no divergence."""
        leases = [
            row(lease_key="T1", project_id=_PROJECT, status="active"),
            row(lease_key="T2", project_id=_OTHER, status="active"),
        ]
        result = cmp(5, leases, leases)
        collision_events = [d for d in result.divergences if d.detail.get("collisions")]
        assert not collision_events

    def test_canary_metric_5_same_project_multiple_leases_ok(self) -> None:
        """Same project holds multiple lease keys — not a collision."""
        leases = [
            row(lease_key="T1", project_id=_PROJECT, status="active"),
            row(lease_key="T2", project_id=_PROJECT, status="active"),
        ]
        result = cmp(5, [], leases)
        collision_events = [d for d in result.divergences if d.detail.get("collisions")]
        assert not collision_events


# ---------------------------------------------------------------------------
# Metric 6 — latency
# ---------------------------------------------------------------------------


class TestMetric6Latency:
    def test_canary_metric_6_central_2x_slower(self) -> None:
        """Central is 2× slower than legacy p95 (exceeds 1.5× threshold) — SOFT."""
        result = sv.compare(
            legacy_rows=[],
            central_rows=[],
            project_id=_PROJECT,
            read_site=_SITE,
            sql_template=_SQL,
            metric_id=6,
            legacy_latency_ms=100.0,
            central_latency_ms=200.0,  # 2× = exceeds 1.5×
        )
        assert result.divergences
        assert result.divergences[0].metric_id == 6
        assert result.divergences[0].severity == sv.SEVERITY_SOFT
        assert result.divergences[0].detail["actual_factor"] == pytest.approx(2.0)

    def test_canary_metric_6_within_threshold(self) -> None:
        """Central ≤ 1.5× per-project p95 — no divergence."""
        result = sv.compare(
            legacy_rows=[],
            central_rows=[],
            project_id=_PROJECT,
            read_site=_SITE,
            sql_template=_SQL,
            metric_id=6,
            legacy_latency_ms=100.0,
            central_latency_ms=149.0,  # 1.49× < 1.5× threshold
        )
        assert not result.divergences

    def test_canary_metric_6_exactly_at_threshold_no_divergence(self) -> None:
        """Central = exactly 1.5× legacy — boundary is inclusive, no divergence."""
        result = sv.compare(
            legacy_rows=[],
            central_rows=[],
            project_id=_PROJECT,
            read_site=_SITE,
            sql_template=_SQL,
            metric_id=6,
            legacy_latency_ms=100.0,
            central_latency_ms=150.0,  # exactly 1.5× — not strictly greater
        )
        assert not result.divergences

    def test_canary_metric_6_zero_legacy_latency_no_divergence(self) -> None:
        """Legacy latency of 0 — metric 6 skipped (division guard)."""
        result = sv.compare(
            legacy_rows=[],
            central_rows=[],
            project_id=_PROJECT,
            read_site=_SITE,
            sql_template=_SQL,
            metric_id=6,
            legacy_latency_ms=0.0,
            central_latency_ms=9999.0,
        )
        assert not result.divergences


# ---------------------------------------------------------------------------
# Cross-cutting: has_hard_divergence helper
# ---------------------------------------------------------------------------


class TestComparisonResultAPI:
    def test_has_hard_divergence_true_when_hard_present(self) -> None:
        result = cmp(1, [row(project_id=_OTHER)], [], project_id=_PROJECT)
        assert result.has_hard_divergence() is True

    def test_has_hard_divergence_false_when_only_soft(self) -> None:
        result = sv.compare(
            legacy_rows=[],
            central_rows=[],
            project_id=_PROJECT,
            read_site=_SITE,
            sql_template=_SQL,
            metric_id=6,
            legacy_latency_ms=100.0,
            central_latency_ms=200.0,
        )
        assert result.divergences[0].severity == sv.SEVERITY_SOFT
        assert result.has_hard_divergence() is False

    def test_no_divergences_clean_comparison(self) -> None:
        clean = [row(project_id=_PROJECT, title=f"p{i}") for i in range(5)]
        result = cmp(4, clean, clean, table="success_patterns")
        assert not result.divergences
        assert not result.has_hard_divergence()
