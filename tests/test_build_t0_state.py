#!/usr/bin/env python3
"""Tests for Wave 1 shadow-read wiring in build_t0_state.py.

Covers all 4 instrumented read sites:
  - _collect_open_items          (open_items_digest.json)
  - _collect_recent_dispatches   (dispatch_metadata table, quality_intelligence.db)
  - _collect_intelligence_brief  (success_patterns table, quality_intelligence.db)
  - _collect_dispatch_insights   (dispatch_experiments via DispatchParameterTracker)

For each site: 3-state flag tests (unset / shadow+diverge / 1=central).
Plus end-to-end shadow build + p95 latency regression guard.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

for _p in (str(SCRIPTS_DIR), str(LIB_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Import module under test — side-effects (ensure_env) run at import time but
# are harmless in the test environment.
import build_t0_state as bts  # noqa: E402
import shadow_verifier as sv   # noqa: E402
import shadow_logger as sl     # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers — minimal SQLite DB creation
# ---------------------------------------------------------------------------

SAMPLE_PROJECT_ID = "test-project"
SAMPLE_DISPATCH = {
    "dispatch_id": "d-001",
    "terminal": "T1",
    "track": "A",
    "role": "backend-developer",
    "gate": "",
    "priority": "P1",
    "pr_id": "PR-001",
    "dispatched_at": "2026-05-01T10:00:00Z",
    "completed_at": "2026-05-01T11:00:00Z",
    "outcome_status": "success",
}
SAMPLE_PATTERN = {
    "pattern_type": "approach",
    "category": "testing",
    "title": "Shadow Write Pattern",
    "description": "Use atomic writes for state files",
    "success_rate": 0.95,
    "confidence_score": 0.92,
}


def _create_qi_db(
    path: Path,
    dispatches: Optional[List[Dict[str, Any]]] = None,
    patterns: Optional[List[Dict[str, Any]]] = None,
    *,
    has_project_id: bool = False,
    project_id: str = SAMPLE_PROJECT_ID,
) -> Path:
    """Create a minimal quality_intelligence.db with dispatch_metadata + success_patterns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        pid_col = ", project_id TEXT" if has_project_id else ""
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS dispatch_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dispatch_id TEXT NOT NULL UNIQUE,
                terminal TEXT, track TEXT, role TEXT, gate TEXT,
                priority TEXT, pr_id TEXT,
                dispatched_at TEXT, completed_at TEXT, outcome_status TEXT
                {pid_col}
            );
            CREATE TABLE IF NOT EXISTS success_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT, category TEXT, title TEXT,
                description TEXT, success_rate REAL, confidence_score REAL
                {pid_col}
            );
        """)
        for d in (dispatches or []):
            if has_project_id:
                conn.execute(
                    "INSERT INTO dispatch_metadata "
                    "(dispatch_id,terminal,track,role,gate,priority,pr_id,"
                    "dispatched_at,completed_at,outcome_status,project_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        d["dispatch_id"], d.get("terminal", "T1"),
                        d.get("track", "A"), d.get("role", "worker"),
                        d.get("gate", ""), d.get("priority", "P1"),
                        d.get("pr_id", ""), d.get("dispatched_at", ""),
                        d.get("completed_at", ""), d.get("outcome_status", "success"),
                        project_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO dispatch_metadata "
                    "(dispatch_id,terminal,track,role,gate,priority,pr_id,"
                    "dispatched_at,completed_at,outcome_status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        d["dispatch_id"], d.get("terminal", "T1"),
                        d.get("track", "A"), d.get("role", "worker"),
                        d.get("gate", ""), d.get("priority", "P1"),
                        d.get("pr_id", ""), d.get("dispatched_at", ""),
                        d.get("completed_at", ""), d.get("outcome_status", "success"),
                    ),
                )
        for p in (patterns or []):
            if has_project_id:
                conn.execute(
                    "INSERT INTO success_patterns "
                    "(pattern_type,category,title,description,success_rate,confidence_score,project_id) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        p.get("pattern_type", "approach"), p.get("category", "test"),
                        p.get("title", "T"), p.get("description", "D"),
                        p.get("success_rate", 0.9), p.get("confidence_score", 0.9),
                        project_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO success_patterns "
                    "(pattern_type,category,title,description,success_rate,confidence_score) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        p.get("pattern_type", "approach"), p.get("category", "test"),
                        p.get("title", "T"), p.get("description", "D"),
                        p.get("success_rate", 0.9), p.get("confidence_score", 0.9),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return path


def _write_open_items_digest(state_dir: Path, open_count: int = 3, blocker_count: int = 1) -> None:
    digest = {
        "summary": {"open_count": open_count, "blocker_count": blocker_count},
        "top_blockers": [{"id": f"OI-{i}", "title": f"Blocker {i}"} for i in range(blocker_count)],
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "open_items_digest.json").write_text(json.dumps(digest), encoding="utf-8")


class _CaptureShadowLogger:
    """Minimal shadow_logger stand-in that records comparison results."""

    def __init__(self) -> None:
        self.calls: List[sv.ComparisonResult] = []

    def write_comparison_result(
        self,
        cmp: sv.ComparisonResult,
        project_id: str,
        read_site: str,
        *,
        ledger_path: Optional[Path] = None,
    ) -> int:
        self.calls.append(cmp)
        return len(cmp.divergences)


# ---------------------------------------------------------------------------
# _collect_open_items
# ---------------------------------------------------------------------------


class TestCollectOpenItems:
    def test_collect_open_items_unset_uses_per_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
        _write_open_items_digest(tmp_path, open_count=5, blocker_count=2)

        result = bts._collect_open_items(SAMPLE_PROJECT_ID, tmp_path)

        assert result["open_count"] == 5
        assert result["blocker_count"] == 2

    def test_collect_open_items_shadow_logs_divergence_when_central_diverges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "shadow")
        _write_open_items_digest(tmp_path, open_count=5, blocker_count=2)

        # Central state dir with different counts
        central_state = tmp_path / "central_state"
        _write_open_items_digest(central_state, open_count=999, blocker_count=10)

        capture = _CaptureShadowLogger()
        monkeypatch.setattr(bts, "_shadow_logger", capture)

        def _fake_central(pid: str) -> Dict[str, Any]:
            return bts._collect_open_items_per_project(pid, central_state)

        monkeypatch.setattr(bts, "_collect_open_items_central", _fake_central)

        result = bts._collect_open_items(SAMPLE_PROJECT_ID, tmp_path)

        # Legacy result is authoritative
        assert result["open_count"] == 5
        # Divergence must be logged
        assert len(capture.calls) == 1
        assert len(capture.calls[0].divergences) >= 1

    def test_collect_open_items_authoritative_uses_central(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        _write_open_items_digest(tmp_path, open_count=5, blocker_count=2)

        central_state = tmp_path / "central_state"
        _write_open_items_digest(central_state, open_count=42, blocker_count=7)

        def _fake_central(pid: str) -> Dict[str, Any]:
            return bts._collect_open_items_per_project(pid, central_state)

        monkeypatch.setattr(bts, "_collect_open_items_central", _fake_central)

        result = bts._collect_open_items(SAMPLE_PROJECT_ID, tmp_path)

        assert result["open_count"] == 42
        assert result["blocker_count"] == 7


# ---------------------------------------------------------------------------
# _collect_recent_dispatches
# ---------------------------------------------------------------------------


class TestCollectRecentDispatches:
    def test_collect_recent_dispatches_unset_uses_per_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
        _create_qi_db(
            tmp_path / "quality_intelligence.db",
            dispatches=[SAMPLE_DISPATCH],
        )

        result = bts._collect_recent_dispatches(SAMPLE_PROJECT_ID, tmp_path)

        assert len(result) == 1
        assert result[0]["dispatch_id"] == "d-001"

    def test_collect_recent_dispatches_shadow_logs_divergence_when_central_diverges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "shadow")
        _create_qi_db(
            tmp_path / "quality_intelligence.db",
            dispatches=[SAMPLE_DISPATCH],
        )

        # Central DB with extra dispatch → count diverges
        central_state = tmp_path / "central"
        _create_qi_db(
            central_state / "quality_intelligence.db",
            dispatches=[
                SAMPLE_DISPATCH,
                {**SAMPLE_DISPATCH, "dispatch_id": "d-002"},
            ],
            has_project_id=True,
        )

        capture = _CaptureShadowLogger()
        monkeypatch.setattr(bts, "_shadow_logger", capture)

        def _fake_central(pid: str) -> List[Dict[str, Any]]:
            return bts._collect_recent_dispatches_per_project(pid, central_state)

        monkeypatch.setattr(bts, "_collect_recent_dispatches_central", _fake_central)

        result = bts._collect_recent_dispatches(SAMPLE_PROJECT_ID, tmp_path)

        # Legacy is authoritative
        assert len(result) == 1
        # Divergence logged (count mismatch: 1 vs 2)
        assert len(capture.calls) == 1
        assert len(capture.calls[0].divergences) >= 1
        assert any(d.metric_id == 4 for d in capture.calls[0].divergences)

    def test_collect_recent_dispatches_authoritative_uses_central(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        _create_qi_db(
            tmp_path / "quality_intelligence.db",
            dispatches=[SAMPLE_DISPATCH],
        )

        central_state = tmp_path / "central"
        _create_qi_db(
            central_state / "quality_intelligence.db",
            dispatches=[
                {**SAMPLE_DISPATCH, "dispatch_id": "d-central-1"},
                {**SAMPLE_DISPATCH, "dispatch_id": "d-central-2"},
            ],
            has_project_id=True,
        )

        def _fake_central(pid: str) -> List[Dict[str, Any]]:
            return bts._collect_recent_dispatches_per_project(pid, central_state)

        monkeypatch.setattr(bts, "_collect_recent_dispatches_central", _fake_central)

        result = bts._collect_recent_dispatches(SAMPLE_PROJECT_ID, tmp_path)

        assert len(result) == 2
        ids = {r["dispatch_id"] for r in result}
        assert "d-central-1" in ids


# ---------------------------------------------------------------------------
# _collect_intelligence_brief
# ---------------------------------------------------------------------------


class TestCollectIntelligenceBrief:
    def test_collect_intelligence_brief_unset_uses_per_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
        _create_qi_db(
            tmp_path / "quality_intelligence.db",
            patterns=[SAMPLE_PATTERN],
        )

        result = bts._collect_intelligence_brief(SAMPLE_PROJECT_ID, tmp_path)

        assert len(result) == 1
        assert result[0]["title"] == "Shadow Write Pattern"

    def test_collect_intelligence_brief_shadow_logs_divergence_when_central_diverges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "shadow")
        # Per-project: one pattern with id=1
        _create_qi_db(
            tmp_path / "quality_intelligence.db",
            patterns=[SAMPLE_PATTERN],
        )

        # Central DB: seed a dummy first so the real pattern gets id=2 → top-N IDs differ
        # (metric 3 compares item IDs, not content; per-project top-1 id=1 vs central id=2)
        central_state = tmp_path / "central"
        _create_qi_db(
            central_state / "quality_intelligence.db",
            patterns=[
                {**SAMPLE_PATTERN, "title": "Dummy Low", "confidence_score": 0.1},
                {**SAMPLE_PATTERN, "title": "Divergent High", "confidence_score": 0.99},
            ],
            has_project_id=True,
        )

        capture = _CaptureShadowLogger()
        monkeypatch.setattr(bts, "_shadow_logger", capture)

        def _fake_central(pid: str) -> List[Dict[str, Any]]:
            return bts._collect_intelligence_brief_per_project(pid, central_state)

        monkeypatch.setattr(bts, "_collect_intelligence_brief_central", _fake_central)

        result = bts._collect_intelligence_brief(SAMPLE_PROJECT_ID, tmp_path)

        # Legacy is authoritative
        assert result[0]["title"] == "Shadow Write Pattern"
        # Metric 3 divergence logged (top-1 id=1 per-project vs id=2 central)
        assert len(capture.calls) == 1
        assert any(d.metric_id == 3 for d in capture.calls[0].divergences)

    def test_collect_intelligence_brief_authoritative_uses_central(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")
        _create_qi_db(
            tmp_path / "quality_intelligence.db",
            patterns=[SAMPLE_PATTERN],
        )

        central_state = tmp_path / "central"
        _create_qi_db(
            central_state / "quality_intelligence.db",
            patterns=[{**SAMPLE_PATTERN, "title": "Central Pattern"}],
            has_project_id=True,
        )

        def _fake_central(pid: str) -> List[Dict[str, Any]]:
            return bts._collect_intelligence_brief_per_project(pid, central_state)

        monkeypatch.setattr(bts, "_collect_intelligence_brief_central", _fake_central)

        result = bts._collect_intelligence_brief(SAMPLE_PROJECT_ID, tmp_path)

        assert result[0]["title"] == "Central Pattern"


# ---------------------------------------------------------------------------
# _collect_dispatch_insights
# ---------------------------------------------------------------------------


class TestCollectDispatchInsights:
    def test_collect_dispatch_insights_unset_uses_per_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
        # No experiments in DB → returns empty fallback
        result = bts._collect_dispatch_insights(SAMPLE_PROJECT_ID, tmp_path)

        assert result["available"] is False
        assert result["insights"] == []

    def test_collect_dispatch_insights_shadow_logs_divergence_when_central_diverges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "shadow")
        capture = _CaptureShadowLogger()
        monkeypatch.setattr(bts, "_shadow_logger", capture)

        # Per-project: empty
        # Central: different non-empty result → diverges
        def _fake_central(pid: str) -> Dict[str, Any]:
            return {
                "available": True,
                "insights": [{"dimension": "role", "group_a": "A", "group_b": "B",
                               "metric": "avg_cqs", "value_a": 0.9, "value_b": 0.7,
                               "sample_a": 10, "sample_b": 10}],
                "experiment_count": 25,
            }

        monkeypatch.setattr(bts, "_collect_dispatch_insights_central", _fake_central)

        result = bts._collect_dispatch_insights(SAMPLE_PROJECT_ID, tmp_path)

        assert result["available"] is False  # legacy is authoritative
        # Divergence logged — dicts differ
        assert len(capture.calls) == 1
        assert len(capture.calls[0].divergences) >= 1

    def test_collect_dispatch_insights_authoritative_uses_central(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VNX_USE_CENTRAL_DB", "1")

        def _fake_central(pid: str) -> Dict[str, Any]:
            return {"available": True, "insights": ["sentinel"], "experiment_count": 99}

        monkeypatch.setattr(bts, "_collect_dispatch_insights_central", _fake_central)

        result = bts._collect_dispatch_insights(SAMPLE_PROJECT_ID, tmp_path)

        assert result["available"] is True
        assert result["experiment_count"] == 99


# ---------------------------------------------------------------------------
# End-to-end: full shadow build
# ---------------------------------------------------------------------------


def test_full_state_build_in_shadow_mode_runs_without_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_t0_state() must not raise under VNX_USE_CENTRAL_DB=shadow."""
    monkeypatch.setenv("VNX_USE_CENTRAL_DB", "shadow")
    monkeypatch.setenv("VNX_PROJECT_ID", SAMPLE_PROJECT_ID)

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    (dispatch_dir / "pending").mkdir(parents=True)
    (dispatch_dir / "active").mkdir()
    (dispatch_dir / "conflicts").mkdir()

    # Plant minimal per-project data
    _write_open_items_digest(state_dir, open_count=2, blocker_count=0)
    _create_qi_db(state_dir / "quality_intelligence.db", dispatches=[SAMPLE_DISPATCH])

    # Stub central functions to return empty so shadow compares without error
    monkeypatch.setattr(bts, "_collect_open_items_central",
                        lambda pid: {"open_count": 0, "blocker_count": 0, "top_blockers": []})
    monkeypatch.setattr(bts, "_collect_recent_dispatches_central", lambda pid: [])
    monkeypatch.setattr(bts, "_collect_intelligence_brief_central", lambda pid: [])
    monkeypatch.setattr(bts, "_collect_dispatch_insights_central",
                        lambda pid: {"available": False, "insights": [], "experiment_count": 0})

    capture = _CaptureShadowLogger()
    monkeypatch.setattr(bts, "_shadow_logger", capture)

    # Must not raise
    result = bts.build_t0_state(state_dir, dispatch_dir)

    assert "open_items" in result
    assert "recent_dispatches" in result
    assert "intelligence_brief" in result
    assert "dispatch_insights" in result
    # Divergence was logged for recent_dispatches (1 vs 0)
    assert any(c.divergences for c in capture.calls)


def test_shadow_mode_p95_latency_within_2x_per_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shadow-mode latency for the 4 collect functions must stay within 2x per-project."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_open_items_digest(state_dir)
    _create_qi_db(
        state_dir / "quality_intelligence.db",
        dispatches=[{**SAMPLE_DISPATCH, "dispatch_id": f"d-{i}"} for i in range(20)],
        patterns=[SAMPLE_PATTERN] * 5,
    )

    # Stub central to return empty (no actual central DB on disk)
    monkeypatch.setattr(bts, "_collect_open_items_central",
                        lambda pid: {"open_count": 0, "blocker_count": 0, "top_blockers": []})
    monkeypatch.setattr(bts, "_collect_recent_dispatches_central", lambda pid: [])
    monkeypatch.setattr(bts, "_collect_intelligence_brief_central", lambda pid: [])
    monkeypatch.setattr(bts, "_collect_dispatch_insights_central",
                        lambda pid: {"available": False, "insights": [], "experiment_count": 0})
    monkeypatch.setattr(bts, "_shadow_logger", _CaptureShadowLogger())

    n = 10
    per_project_times: List[float] = []
    shadow_times: List[float] = []

    monkeypatch.delenv("VNX_USE_CENTRAL_DB", raising=False)
    for _ in range(n):
        t0 = time.perf_counter()
        bts._collect_open_items(SAMPLE_PROJECT_ID, state_dir)
        bts._collect_recent_dispatches(SAMPLE_PROJECT_ID, state_dir)
        bts._collect_intelligence_brief(SAMPLE_PROJECT_ID, state_dir)
        bts._collect_dispatch_insights(SAMPLE_PROJECT_ID, state_dir)
        per_project_times.append(time.perf_counter() - t0)

    monkeypatch.setenv("VNX_USE_CENTRAL_DB", "shadow")
    for _ in range(n):
        t0 = time.perf_counter()
        bts._collect_open_items(SAMPLE_PROJECT_ID, state_dir)
        bts._collect_recent_dispatches(SAMPLE_PROJECT_ID, state_dir)
        bts._collect_intelligence_brief(SAMPLE_PROJECT_ID, state_dir)
        bts._collect_dispatch_insights(SAMPLE_PROJECT_ID, state_dir)
        shadow_times.append(time.perf_counter() - t0)

    per_project_times.sort()
    shadow_times.sort()
    p95_idx = int(0.95 * n) - 1
    p95_per_project = per_project_times[p95_idx]
    p95_shadow = shadow_times[p95_idx]

    assert p95_shadow <= 2.0 * p95_per_project, (
        f"Shadow p95 {p95_shadow*1000:.1f}ms exceeds 2× per-project p95 {p95_per_project*1000:.1f}ms"
    )


# ---------------------------------------------------------------------------
# Auto-dream reviews (ADR-019 / OI-896) — dream_reviews section
# ---------------------------------------------------------------------------
# A consolidation cycle writes a pending-review.json and nothing in the fabric
# reads it: a mandatory T0 review gate that is invisible is skipped by
# construction. These tests pin the reader build_t0_state now provides — and
# the zero case, which must be visible, not absent.

_PENDING_REVIEW_BODY = {
    "cycle_id": "dream-20260730-100000-aaaaaaaa",
    "project_id": "test-project",
    "input_count": 12,
    "consolidation": {"merged": [], "dropped": [], "archived": [], "flagged": []},
    "requires_operator_review": True,
}


def _write_dream_review(data_root: Path, *, cycle_id: str, age_hours: float,
                        project_id: str = SAMPLE_PROJECT_ID) -> Path:
    """Write a pending-review.json with an mtime ``age_hours`` in the past."""
    review_dir = data_root / "state" / "dream"
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / f"{cycle_id}-pending-review.json"
    path.write_text(
        json.dumps({**_PENDING_REVIEW_BODY, "cycle_id": cycle_id, "project_id": project_id}),
        encoding="utf-8",
    )
    ts = time.time() - age_hours * 3600
    os.utime(path, (ts, ts))
    return path


class TestBuildDreamReviews:
    def test_pending_count_and_oldest_age(self, tmp_path: Path) -> None:
        data_root = tmp_path
        state_dir = data_root / "state"
        state_dir.mkdir()
        _write_dream_review(data_root, cycle_id="dream-old", age_hours=72)
        _write_dream_review(data_root, cycle_id="dream-recent", age_hours=5)

        section = bts._build_dream_reviews(state_dir, SAMPLE_PROJECT_ID)

        assert section["available"] is True
        assert section["pending_count"] == 2
        assert section["oldest_age_seconds"] == pytest.approx(72 * 3600, abs=60)
        by_cycle = {r["cycle_id"]: r for r in section["pending_reviews"]}
        assert by_cycle["dream-old"]["age_seconds"] == pytest.approx(72 * 3600, abs=60)
        assert by_cycle["dream-recent"]["age_seconds"] == pytest.approx(5 * 3600, abs=60)
        assert by_cycle["dream-old"]["input_count"] == 12

    def test_zero_pending_is_visible(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        section = bts._build_dream_reviews(state_dir, SAMPLE_PROJECT_ID)

        # The zero is a signal: the key must be present, not absent.
        assert section["available"] is True
        assert section["pending_count"] == 0
        assert section["oldest_age_seconds"] is None
        assert section["pending_reviews"] == []

    def test_filters_other_projects_and_resolved_reviews(self, tmp_path: Path) -> None:
        data_root = tmp_path
        state_dir = data_root / "state"
        state_dir.mkdir()
        _write_dream_review(data_root, cycle_id="mine", age_hours=5)
        # Same project but already resolved (not awaiting review).
        resolved = data_root / "state" / "dream" / "resolved-pending-review.json"
        resolved.write_text(
            json.dumps({**_PENDING_REVIEW_BODY, "cycle_id": "resolved",
                        "requires_operator_review": False}),
            encoding="utf-8",
        )
        # A different project's pending review must be excluded (ADR-007).
        _write_dream_review(data_root, cycle_id="other", age_hours=2, project_id="other-project")

        section = bts._build_dream_reviews(state_dir, SAMPLE_PROJECT_ID)

        assert section["pending_count"] == 1
        assert section["pending_reviews"][0]["cycle_id"] == "mine"

    def test_full_state_includes_dream_reviews_key(self, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
        """t0_state.json carries the dream key at every kickoff, even at zero."""
        monkeypatch.setenv("VNX_PROJECT_ID", SAMPLE_PROJECT_ID)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        dispatch_dir = tmp_path / "dispatches"
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir()
        (dispatch_dir / "conflicts").mkdir()
        _write_open_items_digest(state_dir, open_count=0, blocker_count=0)
        _create_qi_db(state_dir / "quality_intelligence.db", dispatches=[SAMPLE_DISPATCH])

        result = bts.build_t0_state(state_dir, dispatch_dir)

        assert "dream_reviews" in result
        assert result["dream_reviews"]["available"] is True
        assert result["dream_reviews"]["pending_count"] == 0


# ---------------------------------------------------------------------------
# Permission escalations (OI-1414) — permission_escalations section
# ---------------------------------------------------------------------------
# A pending worker-permission escalation is written by worker_permission_relay
# whenever a detached tmux worker's prompt escalates, but nothing before this
# reader surfaced it at SessionStart — vnx permission escalations was a
# standalone CLI nobody ran proactively. These tests pin the reader that now
# puts the backlog in t0_state.json, and the zero case, which must be visible.

import worker_permission_relay as wpr  # noqa: E402


class TestBuildPermissionEscalations:
    def test_pending_count_and_oldest_age(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        now = time.time()
        wpr.write_escalation(
            "old-dispatch", "rm -rf:*", "catastrophic",
            state_dir=state_dir, now=now - 19 * 86400,
        )
        wpr.write_escalation(
            "recent-dispatch", "chmod:*", "awaiting_permission",
            state_dir=state_dir, now=now - 3600,
        )

        section = bts._build_permission_escalations(state_dir)

        assert section["available"] is True
        assert section["pending_count"] == 2
        assert section["oldest_age_days"] == pytest.approx(19.0, abs=0.1)
        by_dispatch = {r["dispatch_id"]: r for r in section["pending_escalations"]}
        assert by_dispatch["old-dispatch"]["command"] == "rm -rf:*"
        assert by_dispatch["old-dispatch"]["reason"] == "catastrophic"
        assert by_dispatch["old-dispatch"]["age_days"] == pytest.approx(19.0, abs=0.1)
        assert by_dispatch["recent-dispatch"]["age_days"] == pytest.approx(3600 / 86400, abs=0.1)
        # Oldest first.
        assert section["pending_escalations"][0]["dispatch_id"] == "old-dispatch"

    def test_zero_pending_is_visible(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        section = bts._build_permission_escalations(state_dir)

        assert section["available"] is True
        assert section["pending_count"] == 0
        assert section["oldest_age_days"] is None
        assert section["pending_escalations"] == []

    def test_resolved_escalations_excluded(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        wpr.write_escalation("d-pending", "pytest -q", "window_closed", state_dir=state_dir)
        wpr.write_escalation("d-resolved", "pytest -q", "window_closed", state_dir=state_dir)
        wpr.resolve_escalation("d-resolved", approved=False, state_dir=state_dir)

        section = bts._build_permission_escalations(state_dir)

        assert section["pending_count"] == 1
        assert section["pending_escalations"][0]["dispatch_id"] == "d-pending"

    def test_full_state_includes_permission_escalations_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """t0_state.json carries the escalations key at every kickoff, even at zero."""
        monkeypatch.setenv("VNX_PROJECT_ID", SAMPLE_PROJECT_ID)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        dispatch_dir = tmp_path / "dispatches"
        (dispatch_dir / "pending").mkdir(parents=True)
        (dispatch_dir / "active").mkdir()
        (dispatch_dir / "conflicts").mkdir()
        _write_open_items_digest(state_dir, open_count=0, blocker_count=0)
        _create_qi_db(state_dir / "quality_intelligence.db", dispatches=[SAMPLE_DISPATCH])
        wpr.write_escalation("d-live", "rm -rf:*", "catastrophic", state_dir=state_dir)

        result = bts.build_t0_state(state_dir, dispatch_dir)

        assert "permission_escalations" in result
        assert result["permission_escalations"]["available"] is True
        assert result["permission_escalations"]["pending_count"] == 1
        assert result["permission_escalations"]["pending_escalations"][0]["dispatch_id"] == "d-live"
