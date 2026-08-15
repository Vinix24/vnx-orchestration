#!/usr/bin/env python3
"""tests/test_t0_state_health.py — OI-1058: t0_state.json staleness + refresh hook.

Tests the shared ``scripts/lib/t0_state_health`` assessment directly (real code,
no reimplementation) and the ``check_t0_state_freshness`` render in
``scripts/vnx_doctor.py``. The failure mode under test: a project whose
``t0_state.json`` stops refreshing while dispatches keep being created, so the
T0 role silently plans against months-old state.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_LIB = _ROOT / "scripts" / "lib"
_SCRIPTS = _ROOT / "scripts"

for p in (_LIB, _SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from t0_state_health import (  # noqa: E402
    STALE_AFTER_DAYS,
    assess_t0_state_health,
)

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_t0_state(state_dir: Path, generated_at: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "t0_state.json"
    path.write_text(json.dumps({"generated_at": generated_at, "schema_version": "2.2"}))
    return path


def _age_t0_state_file(path: Path, days: float) -> None:
    """Backdate the file's mtime so the mtime fallback path is exercised."""
    ts = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


def _write_dispatch(state_dir: Path, created_at: datetime) -> None:
    """Create a minimal runtime_coordination.db with one dispatch."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(state_dir / "runtime_coordination.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS dispatches (dispatch_id TEXT, created_at TEXT)")
    conn.execute(
        "INSERT INTO dispatches (dispatch_id, created_at) VALUES (?, ?)",
        ("d-1", _iso(created_at)),
    )
    conn.commit()
    conn.close()


def _write_settings(project_root: Path, with_hook: bool) -> None:
    (project_root / ".claude").mkdir(parents=True, exist_ok=True)
    session_start = []
    if with_hook:
        session_start = [{
            "matcher": "terminals/T0",
            "hooks": [{
                "type": "command",
                "command": "bash -c 'exec bash \"${VNX_HOME}/scripts/hooks/build_t0_state_hook.sh\"'",
            }],
        }]
    (project_root / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": session_start}, "permissions": {}})
    )


def _assess(tmp_path: Path, *, with_hook: bool = True):
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    return assess_t0_state_health(state_dir, project_root, now=NOW)


# ---------------------------------------------------------------------------
# Freshness facts
# ---------------------------------------------------------------------------

def test_no_state_file_is_clean(tmp_path):
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert assessment["exists"] is False
    assert assessment["findings"] == []


def test_fresh_state_with_hook_passes(tmp_path):
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(hours=1)))
    _write_settings(project_root, with_hook=True)
    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert assessment["exists"] is True
    assert assessment["has_refresh_hook"] is True
    assert assessment["age_human"] == "1 hour"
    assert assessment["findings"] == []


def test_stale_state_with_recent_dispatch_warns(tmp_path):
    """The production shape: state 52 days old, dispatches created 3 days ago.

    The message must name the consequence (the role reads stale state), not
    just the fact of staleness.
    """
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(days=52)))
    _write_settings(project_root, with_hook=True)
    _write_dispatch(state_dir, NOW - timedelta(days=3))

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    kinds = [f["kind"] for f in assessment["findings"]]
    assert "stale_while_active" in kinds
    stale = next(f for f in assessment["findings"] if f["kind"] == "stale_while_active")
    assert "52 days old" in stale["message"]
    assert "stale state" in stale["message"]  # consequence, not just the fact
    assert "2026-08-12" in stale["message"]  # the latest-dispatch date is named


def test_stale_state_without_recent_dispatch_does_not_warn(tmp_path):
    """An idle project (state and last dispatch both 52 days old) is not broken:
    its first session will refresh the state. No stale finding."""
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(days=52)))
    _write_settings(project_root, with_hook=True)
    _write_dispatch(state_dir, NOW - timedelta(days=60))  # before the build

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert assessment["findings"] == []


def test_missing_hook_warns_even_when_fresh(tmp_path):
    """The hook being gone is its own finding, independent of staleness: a
    fresh state with no refresh hook will still go stale on the next session."""
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(hours=1)))
    _write_settings(project_root, with_hook=False)

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    kinds = [f["kind"] for f in assessment["findings"]]
    assert kinds == ["missing_hook"]
    assert "build_t0_state_hook" in assessment["findings"][0]["message"]


def test_missing_hook_and_stale_both_findings(tmp_path):
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(days=52)))
    _write_settings(project_root, with_hook=False)
    _write_dispatch(state_dir, NOW - timedelta(days=3))

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    kinds = sorted(f["kind"] for f in assessment["findings"])
    assert kinds == ["missing_hook", "stale_while_active"]


def test_stale_threshold_is_deliberate():
    """N must be a consciously-chosen constant, not a magic inline number."""
    assert STALE_AFTER_DAYS == 7


# ---------------------------------------------------------------------------
# Defensive reads (negative paths)
# ---------------------------------------------------------------------------

def test_unparseable_generated_at_falls_back_to_mtime(tmp_path):
    """A state file whose generated_at is garbage (or absent) still ages via
    mtime — the exact mission-control shape: a pre-generated_at file."""
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    path = _write_t0_state(state_dir, "not-a-valid-iso")
    _age_t0_state_file(path, days=52)
    _write_settings(project_root, with_hook=True)
    _write_dispatch(state_dir, NOW - timedelta(days=3))

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert any(f["kind"] == "stale_while_active" for f in assessment["findings"])


def test_no_generated_at_uses_mtime(tmp_path):
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    path = state_dir / "t0_state.json"
    state_dir.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "2.2"}), encoding="utf-8")
    _age_t0_state_file(path, days=52)
    _write_settings(project_root, with_hook=True)
    _write_dispatch(state_dir, NOW - timedelta(days=3))

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert any(f["kind"] == "stale_while_active" for f in assessment["findings"])


def test_malformed_t0_state_json_does_not_crash(tmp_path):
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "t0_state.json").write_text("{not-json", encoding="utf-8")
    _write_settings(project_root, with_hook=True)

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert assessment["exists"] is True
    # Unparseable body falls back to mtime (the file was just written), so the
    # age is read from mtime rather than crashing or claiming "never built".
    assert assessment["age_human"] == "0 hours"
    assert assessment["findings"] == []


def test_no_runtime_db_suppresses_stale_finding(tmp_path):
    """Without a coordination DB there is no evidence of activity since the
    build, so a stale state alone must not warn (would cry wolf on idle)."""
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(days=52)))
    _write_settings(project_root, with_hook=True)

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert assessment["most_recent_dispatch"] is None
    assert assessment["findings"] == []


def test_malformed_settings_json_does_not_crash(tmp_path):
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(hours=1)))
    (project_root / ".claude").mkdir(parents=True)
    (project_root / ".claude" / "settings.json").write_text("{broken", encoding="utf-8")

    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert assessment["has_refresh_hook"] is False
    assert any(f["kind"] == "missing_hook" for f in assessment["findings"])


def test_missing_settings_file_means_no_hook(tmp_path):
    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(hours=1)))
    # no .claude/settings.json at all
    assessment = assess_t0_state_health(state_dir, project_root, now=NOW)
    assert assessment["has_refresh_hook"] is False
    assert any(f["kind"] == "missing_hook" for f in assessment["findings"])


# ---------------------------------------------------------------------------
# Render through scripts/vnx_doctor.py (the bin/vnx doctor surface)
# ---------------------------------------------------------------------------

def test_doctor_render_warns_on_stale_and_active(tmp_path):
    from vnx_doctor import WARN, check_t0_state_freshness

    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(days=52)))
    _write_settings(project_root, with_hook=True)
    _write_dispatch(state_dir, NOW - timedelta(days=3))

    paths = {
        "PROJECT_ROOT": str(project_root),
        "VNX_STATE_DIR": str(state_dir),
    }
    results = check_t0_state_freshness(paths)
    assert len(results) == 1
    assert results[0].status == WARN
    assert results[0].name == "t0_state"
    assert "stale state" in results[0].message
    assert results[0].remediation


def test_doctor_render_empty_when_no_state(tmp_path):
    from vnx_doctor import check_t0_state_freshness

    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    paths = {
        "PROJECT_ROOT": str(project_root),
        "VNX_STATE_DIR": str(state_dir),
    }
    assert check_t0_state_freshness(paths) == []


def test_doctor_render_pass_when_fresh_and_hooked(tmp_path):
    from vnx_doctor import PASS, check_t0_state_freshness

    project_root = tmp_path / "project"
    state_dir = tmp_path / "state"
    _write_t0_state(state_dir, _iso(NOW - timedelta(hours=1)))
    _write_settings(project_root, with_hook=True)

    paths = {
        "PROJECT_ROOT": str(project_root),
        "VNX_STATE_DIR": str(state_dir),
    }
    results = check_t0_state_freshness(paths)
    assert len(results) == 1
    assert results[0].status == PASS


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
