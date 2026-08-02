"""tests/test_dispatch_guard_runtime_reader.py — OI-859 direction B.

``dispatch_guard.sh`` must read RUNTIME state via the vnx CLI
(``vnx status --json`` / ``vnx pool status --json``) instead of the
repo-local ``.vnx-data/state/t0_brief.json`` presentation cache.

On origin/main the guard reads ``$REPO_ROOT/.vnx-data/state/t0_brief.json``,
which does not exist in a fresh checkout → exit 1 with "Missing file". These
tests are therefore RED on origin/main and GREEN once the guard reads the
runtime CLI.

Every test isolates state via ``VNX_DATA_DIR_EXPLICIT=1`` + ``VNX_DATA_DIR``
pointing at a tmp dir holding a synthetic ``state/t0_state.json``, so the real
central store is never touched and the repo dir stays clean.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "skills" / "t0-orchestrator" / "scripts" / "dispatch_guard.sh"
CLAUDE_GUARD = ROOT / ".claude" / "skills" / "t0-orchestrator" / "scripts" / "dispatch_guard.sh"


def _healthy_state() -> dict:
    return {
        "terminals": {f"T{i}": {"status": "idle"} for i in (1, 2, 3)},
        "queues": {"pending_count": 0, "active_count": 0, "conflict_count": 0},
        "system_health": {"status": "healthy"},
    }


def _run_guard(tmp_path: Path, payload: dict, *args: str) -> subprocess.CompletedProcess:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "t0_state.json").write_text(json.dumps(payload), encoding="utf-8")

    env = dict(os.environ)
    # Neutralise inherited VNX pointers so resolution is fully pinned to tmp_path.
    for key in ("VNX_HOME", "VNX_STATE_DIR", "VNX_DISPATCH_DIR",
                "VNX_LOGS_DIR", "PROJECT_ROOT", "VNX_PROJECT_ROOT", "VNX_BIN"):
        env.pop(key, None)
    env["VNX_DATA_DIR_EXPLICIT"] = "1"
    env["VNX_DATA_DIR"] = str(tmp_path)

    return subprocess.run(
        ["bash", str(GUARD), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )


class TestGuardReadsRuntimeState:
    def test_guard_returns_go_for_healthy_idle(self, tmp_path):
        result = _run_guard(tmp_path, _healthy_state())
        assert result.returncode == 0, f"expected GO, got rc={result.returncode}: {result.stderr}"
        assert "GO: safe to dispatch" in result.stdout

    def test_guard_never_reads_repo_local_brief(self, tmp_path):
        """The guard must not touch .vnx-data/state/t0_brief.json (OI-859)."""
        result = _run_guard(tmp_path, _healthy_state())
        assert result.returncode == 0
        assert "t0_brief.json" not in result.stdout + result.stderr
        assert "Missing file" not in result.stderr

    def test_guard_waits_on_degraded_system_health(self, tmp_path):
        state = _healthy_state()
        state["system_health"] = {"status": "degraded", "warnings": ["db_locked"]}
        result = _run_guard(tmp_path, state)
        assert result.returncode == 2
        assert "WAIT" in result.stdout
        assert "system_degraded" in result.stdout

    def test_guard_waits_on_busy_terminal(self, tmp_path):
        state = _healthy_state()
        state["terminals"]["T1"]["status"] = "working"
        result = _run_guard(tmp_path, state)
        assert result.returncode == 2
        assert "busy: terminals=1" in result.stdout

    def test_guard_waits_on_pending_queue(self, tmp_path):
        state = _healthy_state()
        state["queues"]["pending_count"] = 2
        result = _run_guard(tmp_path, state)
        assert result.returncode == 2
        assert "queue: pending=2" in result.stdout

    def test_guard_waits_on_conflicts(self, tmp_path):
        state = _healthy_state()
        state["queues"]["conflict_count"] = 3
        result = _run_guard(tmp_path, state)
        assert result.returncode == 2
        assert "conflicts: 3" in result.stdout

    def test_guard_fails_closed_when_system_health_missing(self, tmp_path):
        state = _healthy_state()
        del state["system_health"]
        result = _run_guard(tmp_path, state)
        assert result.returncode == 1
        assert "system_health unavailable" in result.stderr

    def test_guard_json_mode_exposes_system_health(self, tmp_path):
        result = _run_guard(tmp_path, _healthy_state(), "json")
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert payload["decision"] == "GO"
        assert payload["system_health"]["status"] == "healthy"

    def test_both_skill_copies_are_identical(self):
        assert CLAUDE_GUARD.read_text(encoding="utf-8") == GUARD.read_text(encoding="utf-8")
