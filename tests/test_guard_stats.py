"""Tests for the guard-fired counter (scripts/lib/guard_stats.py) and its
observe-only instrumentation of phantom_guard and plan_gate_enforcement.

RED against origin/main: guard_stats does not exist there and neither guard
records evaluations, so the counter assertions fail. GREEN on this branch.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

import guard_stats  # noqa: E402
import phantom_guard  # noqa: E402
import plan_gate_enforcement  # noqa: E402


def _read_evals(state_dir: Path) -> list:
    path = state_dir / guard_stats.GUARD_EVALUATIONS_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _iso_days_ago(days: float) -> str:
    return datetime.fromtimestamp(time.time() - days * 86400, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The counter itself
# ---------------------------------------------------------------------------


def test_record_and_summarize(tmp_path: Path) -> None:
    assert guard_stats.record_guard_evaluation("g1", True, state_dir=tmp_path) is True
    guard_stats.record_guard_evaluation("g1", False, state_dir=tmp_path)
    guard_stats.record_guard_evaluation("g1", False, state_dir=tmp_path)
    guard_stats.record_guard_evaluation("g2", False, state_dir=tmp_path)

    records = _read_evals(tmp_path)
    assert all(r["event_type"] == "guard_evaluation" for r in records)

    summary = guard_stats.summarize(tmp_path)
    assert summary["guards"]["g1"]["evaluations"] == 3
    assert summary["guards"]["g1"]["fired"] == 1
    assert summary["guards"]["g1"]["fired_pct"] == 33.3
    assert summary["guards"]["g1"]["suspect_silent"] is False


def test_summarize_flags_guard_silent_for_30_days(tmp_path: Path) -> None:
    """"This guard has been 100% False for 30 days" must be a statistic, not
    a logger.debug line that nobody reads."""
    path = tmp_path / guard_stats.GUARD_EVALUATIONS_FILENAME
    lines = []
    for days in (40, 30, 20, 10, 1):
        lines.append(
            json.dumps(
                {
                    "timestamp": _iso_days_ago(days),
                    "event_type": "guard_evaluation",
                    "guard": "never_fires",
                    "fired": False,
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = guard_stats.summarize(tmp_path)
    guard = summary["guards"]["never_fires"]
    assert guard["evaluations"] == 5
    assert guard["fired"] == 0
    assert guard["fired_pct"] == 0.0
    assert guard["suspect_silent"] is True


def test_record_is_observe_only_and_never_raises(tmp_path: Path) -> None:
    # An unwritable state dir (a file where a directory is needed) must not
    # propagate — the counter can never break the guard it observes.
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    assert guard_stats.record_guard_evaluation("g", True, state_dir=blocker / "sub") is False


# ---------------------------------------------------------------------------
# Instrumentation: verdict semantics UNCHANGED, evaluations recorded
# ---------------------------------------------------------------------------


def test_phantom_guard_phantom_verdict_unchanged_and_counted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    verdict = phantom_guard.phantom_guard(status="done", worktree_diff="", role="developer")
    assert verdict.is_phantom is True  # semantics unchanged
    records = _read_evals(tmp_path)
    assert len(records) == 1
    assert records[0]["guard"] == "phantom_guard"
    assert records[0]["fired"] is True


def test_phantom_guard_ok_verdict_unchanged_and_counted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path))
    verdict = phantom_guard.phantom_guard(status="done", worktree_diff="", role="code-reviewer")
    assert verdict.is_phantom is False  # review role exemption intact
    verdict2 = phantom_guard.phantom_guard(status="done", worktree_diff="+ real change", role="developer")
    assert verdict2.is_phantom is False  # non-empty diff exemption intact
    records = _read_evals(tmp_path)
    assert [r["fired"] for r in records] == [False, False]


def test_plan_gate_state_verdict_unchanged_and_counted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VNX_STATE_DIR", str(tmp_path / "state"))
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE track_open_items (track_id TEXT, project_id TEXT, oi_id TEXT, "
            "link_type TEXT, resolved_at TEXT)"
        )
        conn.execute(
            "INSERT INTO track_open_items VALUES ('trk-1', 'proj', 'OI-PLAN-trk-1', 'blocks', NULL)"
        )
        conn.execute(
            "INSERT INTO track_open_items VALUES ('trk-2', 'proj', 'OI-PLAN-trk-2', 'blocks', '2026-07-01')"
        )
        conn.commit()
    finally:
        conn.close()

    assert plan_gate_enforcement.plan_gate_state(db, "trk-1", "proj") == plan_gate_enforcement.UNRESOLVED
    assert plan_gate_enforcement.plan_gate_state(db, "trk-2", "proj") == plan_gate_enforcement.PASSED

    records = _read_evals(tmp_path / "state")
    assert [r["guard"] for r in records] == ["plan_gate_state", "plan_gate_state"]
    assert [r["fired"] for r in records] == [True, False]
