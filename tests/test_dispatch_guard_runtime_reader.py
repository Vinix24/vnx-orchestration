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

D5 (fabric-state-herstel): ``vnx status --json`` no longer carries
``terminals`` and t0_state no longer carries ``queues.active_count``. What
runs comes from ``live_work`` (dispatches table + occupancy flock): any
``live``, ``starting``, ``stale`` or ``unmeasured`` row is WAIT, and a
missing or unavailable ``live_work`` is Missing state, never GO.
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


def _live_row(dispatch_id: str, state: str = "running", lock: str = "held") -> dict:
    return {
        "dispatch_id": dispatch_id,
        "state": state,
        "age_seconds": 600,
        "lock": lock,
        "track": "fabric-state-herstel",
        "gate": None,
        "started_at": "2026-09-29T08:00:00+00:00",
        "pr": None,
    }


def _idle_live_work() -> dict:
    return {
        "available": True,
        "in_flight_states": ["accepted", "claimed", "delivering", "running"],
        "startup_grace_seconds": 120,
        "live": [],
        "starting": [],
        "stale": [],
        "unmeasured": [],
        "counts": {"live": 0, "starting": 0, "stale": 0, "unmeasured": 0},
        "open_prs": [],
    }


def _healthy_state() -> dict:
    return {
        "live_work": _idle_live_work(),
        "queues": {"pending_count": 0, "completed_last_hour": 0, "conflict_count": 0},
        "system_health": {"status": "healthy"},
    }


def _with_row(state: dict, bucket: str, row: dict) -> dict:
    state["live_work"][bucket].append(row)
    state["live_work"]["counts"][bucket] += 1
    return state


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
        """A held lock is work in the air: WAIT, named as live_work."""
        state = _with_row(_healthy_state(), "live", _live_row("20260929-live-a"))
        result = _run_guard(tmp_path, state)
        assert result.returncode == 2
        assert "busy: live_work live=1 starting=0 stale=0 unmeasured=0" in result.stdout
        assert "20260929-live-a" in result.stdout

    @pytest.mark.parametrize("bucket", ["starting", "stale"])
    def test_guard_waits_on_starting_or_stale_row(self, tmp_path, bucket):
        state = _with_row(_healthy_state(), bucket, _live_row("20260929-b", lock="absent"))
        result = _run_guard(tmp_path, state)
        assert result.returncode == 2
        assert f"{bucket}=1" in result.stdout
        assert "GO" not in result.stdout

    def test_guard_waits_on_unmeasured_row(self, tmp_path):
        """A lock probe that raised cannot be measured, so it is not GO."""
        state = _with_row(
            _healthy_state(), "unmeasured", _live_row("20260929-unm", lock="probe_error"),
        )
        result = _run_guard(tmp_path, state)
        assert result.returncode == 2
        assert "unmeasured=1" in result.stdout
        assert "GO" not in result.stdout

    def test_guard_missing_live_work_is_missing_state(self, tmp_path):
        state = _healthy_state()
        del state["live_work"]
        result = _run_guard(tmp_path, state)
        assert result.returncode == 1
        assert "Missing state" in result.stderr
        assert "live_work" in result.stderr
        assert "GO" not in result.stdout

    def test_guard_unavailable_live_work_is_missing_state(self, tmp_path):
        state = _healthy_state()
        state["live_work"] = {"available": False, "reason": "db_locked: OperationalError"}
        result = _run_guard(tmp_path, state, "json")
        assert result.returncode == 1
        assert "db_locked" in result.stderr
        assert "GO" not in result.stdout

    def test_guard_live_work_without_bucket_list_is_missing_state(self, tmp_path):
        """An absent bucket must not read as zero rows (the old ``// 0`` trap)."""
        state = _healthy_state()
        del state["live_work"]["unmeasured"]
        result = _run_guard(tmp_path, state)
        assert result.returncode == 1
        assert "Missing state" in result.stderr

    def test_other_project_live_work_does_not_change_outcome(self, tmp_path):
        """ADR-007: a colliding dispatch_id in another project's store leaks nowhere."""
        colliding = "20260929-collide"
        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"

        busy_b = _with_row(_healthy_state(), "live", _live_row(colliding))
        _run_guard(project_b, busy_b)
        idle_a = _run_guard(project_a, _healthy_state())
        assert idle_a.returncode == 0, idle_a.stdout + idle_a.stderr
        assert colliding not in idle_a.stdout

        stale_a = _with_row(_healthy_state(), "stale", _live_row(colliding, lock="absent"))
        _run_guard(project_b, _healthy_state())
        busy_a = _run_guard(project_a, stale_a)
        assert busy_a.returncode == 2
        assert "stale=1" in busy_a.stdout
        assert "live=0" in busy_a.stdout

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
        assert payload["queues"]["pending_count"] == 0
        assert payload["live_work"]["counts"] == {
            "live": 0, "starting": 0, "stale": 0, "unmeasured": 0,
        }
        assert "terminals" not in payload

    def test_guard_json_mode_waits_with_live_work_reason(self, tmp_path):
        state = _with_row(_healthy_state(), "unmeasured", _live_row("20260929-j", lock="probe_error"))
        result = _run_guard(tmp_path, state, "json")
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload["decision"] == "WAIT"
        assert payload["reason"] == "busy: live_work live=0 starting=0 stale=0 unmeasured=1"
        assert payload["live_work"]["unmeasured"][0]["dispatch_id"] == "20260929-j"

    def test_both_skill_copies_are_identical(self):
        assert CLAUDE_GUARD.read_text(encoding="utf-8") == GUARD.read_text(encoding="utf-8")
