#!/usr/bin/env python3
"""Tests for vnx pause / resume commands.

Tests invoke the cmd_pause / cmd_resume functions directly (source + call) to
avoid bin/vnx path-override machinery (.vnx-data/.env_override etc.).

Test matrix:
  1. pause with no daemons running — idempotent, PAUSED file created
  2. pause with daemons running   — stops them, PAUSED file created
  3. pause when already paused    — exit 0, PAUSED file unchanged
  4. resume without PAUSED file   — exit 1 (error)
  5. resume with PAUSED file      — removes file, daemons started, event appended
  6. pause then immediate resume  — round-trip clean, events in correct format
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

VNX_ROOT = Path(__file__).resolve().parent.parent
PAUSE_SH = VNX_ROOT / "scripts" / "commands" / "pause.sh"
RESUME_SH = VNX_ROOT / "scripts" / "commands" / "resume.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dirs(tmp_path: Path) -> dict:
    """Create minimal .vnx-data layout and return env var mapping.

    Sets VNX_SKIP_DAEMON_SPAWN=1 so cmd_resume does not spawn real nohup
    daemons. Without this, sourced resume.sh would launch operator-grade
    dispatcher_supervisor.sh + receipt_processor_supervisor.sh that survive
    the test and proliferate against real .vnx-data state.
    """
    data = tmp_path / ".vnx-data"
    dirs = {
        "VNX_DATA_DIR": data,
        "VNX_STATE_DIR": data / "state",
        "VNX_PIDS_DIR": data / "pids",
        "VNX_LOGS_DIR": data / "logs",
        "VNX_LOCKS_DIR": data / "locks",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    (data / "events").mkdir(parents=True, exist_ok=True)
    env = {k: str(v) for k, v in dirs.items()}
    env["VNX_SKIP_DAEMON_SPAWN"] = "1"
    return env


def _run_cmd(cmd_name: str, env_vars: dict, args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Source the command file and call cmd_<cmd_name> directly (no bin/vnx)."""
    cmd_file = VNX_ROOT / "scripts" / "commands" / f"{cmd_name}.sh"
    exports = "\n".join(f'export {k}="{v}"' for k, v in env_vars.items())
    bash_script = f"""#!/bin/bash
set -euo pipefail
{exports}
mkdir -p "$VNX_STATE_DIR" "$VNX_PIDS_DIR" "$VNX_LOGS_DIR" "${{VNX_DATA_DIR}}/events"
log() {{ echo "[log] $*"; }}
err() {{ echo "[err] $*" >&2; }}
source "{cmd_file}"
cmd_{cmd_name} {" ".join(args or [])}
"""
    return subprocess.run(
        ["bash", "-c", bash_script],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _write_pid_file(pids_dir: str | Path, name: str, pid: int) -> None:
    Path(pids_dir).mkdir(parents=True, exist_ok=True)
    (Path(pids_dir) / f"{name}.pid").write_text(str(pid))


def _spawn_dummy_process() -> subprocess.Popen:
    return subprocess.Popen(
        ["sleep", "600"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# ---------------------------------------------------------------------------
# Test 1: pause with no daemons running
# ---------------------------------------------------------------------------

def test_pause_no_daemons_creates_paused_file(tmp_path: Path):
    """pause exits 0 and writes PAUSED file when no daemons are running."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-001"
    result = _run_cmd("pause", env)

    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}\n{result.stderr}"

    paused_file = Path(env["VNX_STATE_DIR"]) / "PAUSED"
    assert paused_file.exists(), "PAUSED marker file not created"

    content = json.loads(paused_file.read_text())
    assert "paused_at" in content
    assert content["by_dispatch_id"] == "test-dispatch-001"
    assert content["reason"] == "migration_cutover"


def test_pause_no_daemons_appends_lifecycle_event(tmp_path: Path):
    """pause appends a service_paused event to lifecycle.ndjson."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-001"
    _run_cmd("pause", env)

    lifecycle = Path(env["VNX_DATA_DIR"]) / "events" / "lifecycle.ndjson"
    assert lifecycle.exists(), "lifecycle.ndjson not created"

    lines = [l for l in lifecycle.read_text().splitlines() if l.strip()]
    assert len(lines) >= 1

    event = json.loads(lines[-1])
    assert event["event_type"] == "service_paused"
    assert "timestamp" in event
    assert event["by_dispatch_id"] == "test-dispatch-001"


def test_pause_writes_atomic_paused_file(tmp_path: Path):
    """PAUSED marker is valid JSON with all required fields."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "dispatch-x"
    _run_cmd("pause", env, args=["my_reason"])

    content = json.loads((Path(env["VNX_STATE_DIR"]) / "PAUSED").read_text())
    assert content["reason"] == "my_reason"
    assert content["by_dispatch_id"] == "dispatch-x"
    assert "paused_at" in content
    # No tmp file left behind
    assert not list(Path(env["VNX_STATE_DIR"]).glob("PAUSED.tmp.*"))


# ---------------------------------------------------------------------------
# Test 2: pause with daemons running
# ---------------------------------------------------------------------------

def test_pause_stops_running_daemons(tmp_path: Path):
    """pause sends SIGTERM to running daemons and creates PAUSED file."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-002"
    pids_dir = env["VNX_PIDS_DIR"]

    procs = {}
    for name in ["dispatcher", "receipt_processor", "queue_watcher"]:
        p = _spawn_dummy_process()
        procs[name] = p
        _write_pid_file(pids_dir, name, p.pid)

    try:
        result = _run_cmd("pause", env)
        assert result.returncode == 0, f"Expected exit 0\n{result.stderr}"

        for name, proc in procs.items():
            proc.wait(timeout=15)
            assert not _is_running(proc.pid), f"{name} (PID {proc.pid}) still running"

        paused_file = Path(env["VNX_STATE_DIR"]) / "PAUSED"
        assert paused_file.exists(), "PAUSED marker not written after stopping daemons"

    finally:
        for proc in procs.values():
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception as e:
                logger.warning('cleanup failed: %s', e)


def test_pause_cleans_stale_pid_files(tmp_path: Path):
    """pause removes stale PID files pointing to dead processes."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-003"

    _write_pid_file(env["VNX_PIDS_DIR"], "dispatcher", 99999999)

    result = _run_cmd("pause", env)
    assert result.returncode == 0, f"Expected exit 0\n{result.stderr}"

    paused_file = Path(env["VNX_STATE_DIR"]) / "PAUSED"
    assert paused_file.exists(), "PAUSED file not created after stale-PID cleanup"


# ---------------------------------------------------------------------------
# Test 3: pause when already paused
# ---------------------------------------------------------------------------

def test_pause_idempotent_when_already_paused(tmp_path: Path):
    """Calling pause twice returns exit 0 the second time without modifying the file."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-004"

    r1 = _run_cmd("pause", env)
    assert r1.returncode == 0

    paused_file = Path(env["VNX_STATE_DIR"]) / "PAUSED"
    assert paused_file.exists()
    first_content = paused_file.read_text()

    r2 = _run_cmd("pause", env)
    assert r2.returncode == 0, f"Second pause should exit 0\n{r2.stderr}"

    # PAUSED file must be unchanged (not rewritten)
    assert paused_file.read_text() == first_content, "PAUSED marker was modified on second pause"


# ---------------------------------------------------------------------------
# Test 4: resume without PAUSED file
# ---------------------------------------------------------------------------

def test_resume_fails_when_not_paused(tmp_path: Path):
    """resume exits 1 when PAUSED marker is absent."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-005"

    result = _run_cmd("resume", env)
    assert result.returncode == 1, f"Expected exit 1, got {result.returncode}\n{result.stdout}"

    stderr_lower = result.stderr.lower()
    assert "not paused" in stderr_lower or "does not exist" in stderr_lower, \
        f"Expected error about not being paused\n{result.stderr}"


# ---------------------------------------------------------------------------
# Test 5: resume with PAUSED file
# ---------------------------------------------------------------------------

def test_resume_removes_paused_marker(tmp_path: Path):
    """resume removes the PAUSED marker file on success."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-006"
    # VNX_HOME not needed when no actual scripts to start — skip missing-file errors
    env["VNX_HOME"] = str(VNX_ROOT)

    paused_file = Path(env["VNX_STATE_DIR"]) / "PAUSED"
    paused_file.write_text('{"paused_at":"2026-01-01T00:00:00Z","by_dispatch_id":"test","reason":"test"}\n')

    # Resume will try to start daemons; they may fail (scripts not in VNX_ROOT/scripts)
    # but the marker removal must still happen before the daemon start attempts.
    # We only check the marker is removed if exit code is 0.
    result = _run_cmd("resume", env)

    if result.returncode == 0:
        assert not paused_file.exists(), "PAUSED marker not removed after resume"
    else:
        # If it failed because scripts aren't present, verify the marker was
        # removed (it is removed before starting daemons in cmd_resume).
        # Actually in the implementation, marker is removed AFTER daemons start.
        # If dispatcher_supervisor.sh or dispatcher_v8_minimal.sh is missing, resume fails.
        # With VNX_HOME pointing to VNX_ROOT, the scripts DO exist, so expect exit 0.
        pytest.fail(f"resume failed unexpectedly\n{result.stderr}")


def test_resume_appends_lifecycle_event(tmp_path: Path):
    """resume appends a service_resumed event to lifecycle.ndjson."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-007"
    env["VNX_HOME"] = str(VNX_ROOT)

    lifecycle = Path(env["VNX_DATA_DIR"]) / "events" / "lifecycle.ndjson"
    pause_event = '{"event_type":"service_paused","timestamp":"2026-01-01T00:00:00Z","by_dispatch_id":"test","reason":"test"}'
    lifecycle.write_text(pause_event + "\n")

    paused_file = Path(env["VNX_STATE_DIR"]) / "PAUSED"
    paused_file.write_text('{"paused_at":"2026-01-01T00:00:00Z","by_dispatch_id":"test","reason":"test"}\n')

    result = _run_cmd("resume", env)
    assert result.returncode == 0, f"Expected exit 0\n{result.stderr}"

    lines = [l for l in lifecycle.read_text().splitlines() if l.strip()]
    assert len(lines) >= 2

    last_event = json.loads(lines[-1])
    assert last_event["event_type"] == "service_resumed"
    assert "timestamp" in last_event
    assert last_event["by_dispatch_id"] == "test-dispatch-007"


# ---------------------------------------------------------------------------
# Test 6: pause then resume round-trip
# ---------------------------------------------------------------------------

def test_pause_then_resume_round_trip(tmp_path: Path):
    """Full round-trip: pause → verify stopped → resume → verify events."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-008"
    env["VNX_HOME"] = str(VNX_ROOT)

    lifecycle = Path(env["VNX_DATA_DIR"]) / "events" / "lifecycle.ndjson"
    paused_file = Path(env["VNX_STATE_DIR"]) / "PAUSED"
    pids_dir = env["VNX_PIDS_DIR"]

    proc = _spawn_dummy_process()
    _write_pid_file(pids_dir, "dispatcher", proc.pid)

    try:
        r_pause = _run_cmd("pause", env)
        assert r_pause.returncode == 0, f"pause failed\n{r_pause.stderr}"
        assert paused_file.exists(), "PAUSED file missing after pause"

        proc.wait(timeout=15)
        assert not _is_running(proc.pid), "dispatcher not stopped after pause"

        r_resume = _run_cmd("resume", env)
        assert r_resume.returncode == 0, f"resume failed\n{r_resume.stderr}"
        assert not paused_file.exists(), "PAUSED file still present after resume"

        lines = [l for l in lifecycle.read_text().splitlines() if l.strip()]
        assert len(lines) >= 2, f"Expected at least 2 lifecycle events, got: {lines}"

        events = [json.loads(l) for l in lines]
        event_types = [e["event_type"] for e in events]
        assert "service_paused" in event_types, f"Missing service_paused: {event_types}"
        assert "service_resumed" in event_types, f"Missing service_resumed: {event_types}"

        paused_idx = next(i for i, e in enumerate(events) if e["event_type"] == "service_paused")
        resumed_idx = next(i for i, e in enumerate(events) if e["event_type"] == "service_resumed")
        assert paused_idx < resumed_idx, "service_paused must precede service_resumed"

    finally:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception as e:
            logger.warning('cleanup failed: %s', e)


def test_lifecycle_events_are_valid_ndjson(tmp_path: Path):
    """Every line in lifecycle.ndjson is valid JSON with required fields."""
    env = _make_dirs(tmp_path)
    env["VNX_DISPATCH_ID"] = "test-dispatch-009"
    env["VNX_HOME"] = str(VNX_ROOT)

    lifecycle = Path(env["VNX_DATA_DIR"]) / "events" / "lifecycle.ndjson"
    paused_file = Path(env["VNX_STATE_DIR"]) / "PAUSED"

    r_pause = _run_cmd("pause", env)
    assert r_pause.returncode == 0
    assert paused_file.exists()

    r_resume = _run_cmd("resume", env)
    assert r_resume.returncode == 0

    assert lifecycle.exists(), "lifecycle.ndjson not created"
    lines = [l for l in lifecycle.read_text().splitlines() if l.strip()]
    assert len(lines) >= 2, f"Expected at least 2 events, got {len(lines)}"

    required_fields = {"event_type", "timestamp", "by_dispatch_id"}
    for i, line in enumerate(lines):
        event = json.loads(line)
        missing = required_fields - set(event.keys())
        assert not missing, f"Event {i} missing fields {missing}: {event}"
        assert event["event_type"] in {"service_paused", "service_resumed"}, \
            f"Unknown event_type in event {i}: {event['event_type']}"
