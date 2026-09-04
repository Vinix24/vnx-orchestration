#!/usr/bin/env python3
"""tests/test_sessionstart_hook_state_freshness.py — golf 3A ("absence-is-loud"
criterion 1, OI-1512 fix-forward): hooks/sessionstart.sh must make the age of
the artifacts it loads for T0 visible, and must refuse — via an unmissable
textual directive, the only mechanism a SessionStart hook has — to let T0
orchestrate on state older than one working session without saying so.

Defect this closes: on 2026-08-29 a T0 session orchestrated a merge against a
`t0_state.json` that was 22 days old (`dashboard_status.json` was 63 days,
`t0_recommendations.json` 73 days) and the SessionStart context said nothing
about it — the age was invisible until the merge was attempted. Re-measured
directly against this hook (not trusted from the incident report, per this
project's own "een getal zonder herkomst is een claim" rule): before this
fix, hooks/sessionstart.sh does not read t0_state.json, dashboard_status.json
or t0_recommendations.json at all, and its ONLY per-terminal read
(`terminal_state_${_t}.json`) targets a filename `terminal_state_shadow.py`
never writes (it writes the singular `terminal_state.json`) — a second dead
read discovered by the same measurement discipline this dispatch demands.

Harness pattern (env isolation, `_run_hook`, `_make_terminal_project`) copied
from tests/test_sessionstart_hook_beacon_health.py, the sibling test for the
same hook's beacon-health section — same VNX_DATA_HOME redirection, same
"never touch this repo's own .vnx-data or the live ~/.vnx-data/<project>/
store" rule.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "sessionstart.sh"
LIB = REPO / "scripts" / "lib"
sys.path.insert(0, str(LIB))

import vnx_paths  # noqa: E402
from session_state_freshness import SESSION_STALE_AFTER_HOURS  # noqa: E402

_VNX_ENV_VARS = (
    "VNX_DATA_DIR",
    "VNX_STATE_DIR",
    "VNX_DATA_DIR_EXPLICIT",
    "VNX_DATA_HOME",
    "VNX_PROJECT_ID",
    "VNX_HOME",
    "VNX_BIN",
    "VNX_EXECUTABLE",
    "VNX_CANONICAL_ROOT",
    "VNX_PROJECT_ROOT",
)


def _clean_env(monkeypatch) -> None:
    for var in _VNX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _make_terminal_project(tmp_path: Path, name: str = "project") -> Path:
    root = tmp_path / name
    (root / ".vnx").mkdir(parents=True)
    t0_dir = root / ".claude" / "terminals" / "T0"
    t0_dir.mkdir(parents=True)
    return t0_dir


def _run_hook(cwd: Path, env: dict) -> dict:
    r = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True, text=True, cwd=str(cwd), env=env, timeout=10,
    )
    assert r.returncode == 0, f"hook must exit 0: rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}"
    return json.loads(r.stdout)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _state_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("VNX_DATA_HOME", str(tmp_path / "vnx-data-home"))
    return Path(vnx_paths.resolve_paths()["VNX_STATE_DIR"])


class TestStateFreshnessDigestSurfacesAge:
    """Klaar-wanneer: age is visible, and staleness is a directive T0 cannot
    scroll past, not a footnote."""

    def test_stale_t0_state_produces_a_blocked_directive(self, tmp_path, monkeypatch):
        """The exact incident shape: t0_state.json built 22 days ago (the
        measured 2026-08-29 figure). Must surface as STALE with an unmissable
        BLOCKED directive, not silence."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        env = dict(os.environ)

        old_ts = datetime.now(timezone.utc) - timedelta(days=22)
        _write_json(state_dir / "t0_state.json", {"generated_at": _iso(old_ts), "schema_version": 1})

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "STATE FRESHNESS: BLOCKED" in ctx, ctx
        assert "[STALE] t0_state" in ctx, ctx
        assert "22 day" in ctx, ctx

    def test_fresh_state_says_ok_explicitly_not_silently(self, tmp_path, monkeypatch):
        """A healthy fleet must say so explicitly — silence would be
        indistinguishable from 'freshness unavailable' (same discipline as
        the beacon digest's 'all ok' branch)."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        env = dict(os.environ)

        now_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
        _write_json(state_dir / "t0_state.json", {"generated_at": _iso(now_ts)})
        _write_json(state_dir / "open_items.json", {"last_updated": _iso(now_ts).replace("Z", "")})
        _write_json(state_dir / "dashboard_status.json", {"timestamp": _iso(now_ts)})
        _write_json(state_dir / "t0_recommendations.json", {"timestamp": _iso(now_ts).replace("Z", "")})
        _write_json(state_dir / "terminal_state.json", {"terminals": {}})

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "STATE FRESHNESS: ok" in ctx, ctx
        assert "BLOCKED" not in ctx, ctx

    def test_missing_artifact_is_a_third_branch_not_stale_not_fresh(self, tmp_path, monkeypatch):
        """Ontbrekend is niet hetzelfde als oud, en niet hetzelfde als vers —
        a project that never populated dashboard_status.json must not read as
        either 'the data says it's fine' or 'the data is dangerously old'."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        env = dict(os.environ)

        now_ts = datetime.now(timezone.utc) - timedelta(minutes=1)
        _write_json(state_dir / "t0_state.json", {"generated_at": _iso(now_ts)})
        # dashboard_status.json, t0_recommendations.json, open_items.json,
        # terminal_state.json deliberately absent.

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "[missing] dashboard_status: not found" in ctx, ctx
        # A missing artifact alone (nothing genuinely stale) must not trip
        # the BLOCKED directive — that would conflate "never populated" with
        # "went stale under our feet".
        assert "STATE FRESHNESS: BLOCKED" not in ctx, ctx

    def test_threshold_boundary_23h_fresh_25h_stale(self, tmp_path, monkeypatch):
        """SESSION_STALE_AFTER_HOURS = 24 is exercised at its own boundary,
        not just on an incident-scale outlier."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        env = dict(os.environ)
        assert SESSION_STALE_AFTER_HOURS == 24.0  # guards the boundary picked below

        fresh_ts = datetime.now(timezone.utc) - timedelta(hours=23)
        stale_ts = datetime.now(timezone.utc) - timedelta(hours=25)
        _write_json(state_dir / "t0_state.json", {"generated_at": _iso(fresh_ts)})
        _write_json(state_dir / "open_items.json", {"last_updated": _iso(stale_ts).replace("Z", "")})

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "[fresh] t0_state" in ctx, ctx
        assert "[STALE] open_items" in ctx, ctx

    def test_terminal_state_reads_the_real_schema_not_a_dead_suffix_path(self, tmp_path, monkeypatch):
        """Regression for the second dead read this dispatch's own measurement
        found: terminal_state_shadow.py writes the singular
        `terminal_state.json` with a `.terminals.<id>` map — the pre-fix hook
        looked for `terminal_state_T1.json`, which the writer never produces,
        so this section always fell through to 'No terminal state data'."""
        _clean_env(monkeypatch)
        t0_dir = _make_terminal_project(tmp_path)
        state_dir = _state_dir(tmp_path, monkeypatch)
        env = dict(os.environ)

        _write_json(state_dir / "terminal_state.json", {
            "terminals": {
                "T1": {"status": "busy", "last_activity": "2026-09-04T10:00:00+00:00"},
                "T2": {"status": "idle", "last_activity": "2026-08-30T08:18:15+00:00"},
            }
        })

        out = _run_hook(t0_dir, env)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "No terminal state data" not in ctx, ctx
        assert "T1: busy" in ctx, ctx

    def test_hook_is_valid_bash(self):
        result = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
