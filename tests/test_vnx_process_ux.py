#!/usr/bin/env python3
"""Tests for VNX Status and Process UX (PR-7).

Tests the status, ps, cleanup, and restart commands plus PID metadata.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
sys.path.insert(0, str(SCRIPT_DIR))

from vnx_process_ux import (
    PidMetadata,
    cmd_status,
    cmd_ps,
    cmd_cleanup,
    cmd_restart,
    _is_pid_alive,
    _read_pid_file,
    _read_fingerprint,
    MANAGED_PROCESSES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def vnx_paths(tmp_path):
    """Create a minimal VNX paths structure."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pids_dir = tmp_path / "pids"
    pids_dir.mkdir()
    locks_dir = tmp_path / "locks"
    locks_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    dispatch_dir = tmp_path / "dispatches"
    for sub in ("pending", "active", "completed"):
        (dispatch_dir / sub).mkdir(parents=True)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    return {
        "PROJECT_ROOT": str(tmp_path),
        "VNX_HOME": str(tmp_path),
        "VNX_DATA_DIR": str(tmp_path),
        "VNX_STATE_DIR": str(state_dir),
        "VNX_PIDS_DIR": str(pids_dir),
        "VNX_LOCKS_DIR": str(locks_dir),
        "VNX_LOGS_DIR": str(logs_dir),
        "VNX_DISPATCH_DIR": str(dispatch_dir),
    }


@pytest.fixture
def write_pid(vnx_paths):
    """Helper to write a PID file."""
    def _write(name: str, pid: int, fingerprint: str = "/fake/script.sh"):
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        (pids_dir / f"{name}.pid").write_text(str(pid) + "\n")
        (pids_dir / f"{name}.pid.fingerprint").write_text(fingerprint + "\n")
    return _write


# ---------------------------------------------------------------------------
# PidMetadata
# ---------------------------------------------------------------------------

class TestPidMetadata:

    def test_write_and_read(self, vnx_paths):
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        current_pid = os.getpid()
        PidMetadata.write(pids_dir, "test_proc", current_pid)

        meta = PidMetadata.read(pids_dir, "test_proc")
        assert meta is not None
        assert meta["name"] == "test_proc"
        assert meta["pid"] == current_pid
        assert "started_at" in meta
        assert meta["ppid"] is not None

    def test_read_nonexistent(self, vnx_paths):
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        assert PidMetadata.read(pids_dir, "no_such_proc") is None

    def test_remove(self, vnx_paths):
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        PidMetadata.write(pids_dir, "removable", os.getpid())
        PidMetadata.remove(pids_dir, "removable")
        assert PidMetadata.read(pids_dir, "removable") is None

    def test_metadata_has_required_fields(self, vnx_paths):
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        PidMetadata.write(pids_dir, "full_meta", os.getpid())
        meta = PidMetadata.read(pids_dir, "full_meta")
        for field in ("name", "pid", "ppid", "started_at", "owner", "command"):
            assert field in meta


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_is_pid_alive_current(self):
        assert _is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_dead(self):
        assert _is_pid_alive(99999999) is False

    def test_read_pid_file(self, tmp_path):
        pf = tmp_path / "test.pid"
        pf.write_text("12345\n")
        assert _read_pid_file(pf) == 12345

    def test_read_pid_file_invalid(self, tmp_path):
        pf = tmp_path / "bad.pid"
        pf.write_text("not_a_number\n")
        assert _read_pid_file(pf) is None

    def test_read_pid_file_missing(self, tmp_path):
        assert _read_pid_file(tmp_path / "nope.pid") is None

    def test_read_fingerprint(self, tmp_path):
        pf = tmp_path / "proc.pid"
        pf.write_text("123\n")
        fp = tmp_path / "proc.pid.fingerprint"
        fp.write_text("/path/to/script.sh\n")
        assert _read_fingerprint(pf) == "/path/to/script.sh"

    def test_read_fingerprint_missing(self, tmp_path):
        pf = tmp_path / "proc.pid"
        pf.write_text("123\n")
        assert _read_fingerprint(pf) is None


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestCmdStatus:

    @patch("vnx_process_ux.subprocess.run")
    def test_status_with_terminal_state(self, mock_run, vnx_paths, capsys):
        mock_run.return_value = type("R", (), {"returncode": 1})()
        state_dir = Path(vnx_paths["VNX_STATE_DIR"])

        ts = {
            "schema_version": 1,
            "terminals": {
                "T1": {"terminal_id": "T1", "status": "working", "claimed_by": "D-123", "version": 1, "last_activity": "2026-03-23T00:00:00Z"},
                "T2": {"terminal_id": "T2", "status": "idle", "claimed_by": None, "version": 1, "last_activity": "2026-03-23T00:00:00Z"},
            }
        }
        (state_dir / "terminal_state.json").write_text(json.dumps(ts))

        result = cmd_status(vnx_paths)
        assert result == 0
        captured = capsys.readouterr()
        assert "T1" in captured.out
        assert "T2" in captured.out
        assert "working" in captured.out

    @patch("vnx_process_ux.subprocess.run")
    def test_status_with_queue(self, mock_run, vnx_paths, capsys):
        mock_run.return_value = type("R", (), {"returncode": 1})()
        state_dir = Path(vnx_paths["VNX_STATE_DIR"])

        queue = {
            "prs": [
                {"id": "PR-0", "status": "completed"},
                {"id": "PR-1", "status": "completed"},
                {"id": "PR-2", "status": "queued"},
            ],
            "active": ["PR-2"],
        }
        (state_dir / "pr_queue_state.json").write_text(json.dumps(queue))

        result = cmd_status(vnx_paths)
        captured = capsys.readouterr()
        assert "66%" in captured.out or "2/3" in captured.out

    @patch("vnx_process_ux.subprocess.run")
    def test_status_with_open_items(self, mock_run, vnx_paths, capsys):
        mock_run.return_value = type("R", (), {"returncode": 1})()
        state_dir = Path(vnx_paths["VNX_STATE_DIR"])

        oi = {
            "items": [
                {"id": "OI-001", "status": "open", "severity": "blocker", "title": "Bug"},
                {"id": "OI-002", "status": "open", "severity": "warn", "title": "Warning"},
                {"id": "OI-003", "status": "done", "severity": "blocker", "title": "Fixed"},
            ]
        }
        (state_dir / "open_items.json").write_text(json.dumps(oi))

        result = cmd_status(vnx_paths)
        captured = capsys.readouterr()
        assert "2 total" in captured.out
        assert "1 blocker" in captured.out

    @patch("vnx_process_ux.subprocess.run")
    def test_status_empty(self, mock_run, vnx_paths, capsys):
        mock_run.return_value = type("R", (), {"returncode": 1})()
        result = cmd_status(vnx_paths)
        assert result == 0


# ---------------------------------------------------------------------------
# cmd_ps
# ---------------------------------------------------------------------------

class TestCmdPs:

    def test_ps_no_pids(self, vnx_paths, capsys):
        result = cmd_ps(vnx_paths)
        assert result == 0
        captured = capsys.readouterr()
        assert "No managed processes" in captured.out

    def test_ps_with_live_process(self, vnx_paths, write_pid, capsys):
        write_pid("test_proc", os.getpid())
        result = cmd_ps(vnx_paths)
        assert result == 0
        captured = capsys.readouterr()
        assert "test_proc" in captured.out
        assert "ok" in captured.out

    def test_ps_with_dead_process(self, vnx_paths, write_pid, capsys):
        write_pid("dead_proc", 99999999)
        result = cmd_ps(vnx_paths)
        assert result == 0
        captured = capsys.readouterr()
        assert "dead_proc" in captured.out
        assert "DEAD" in captured.out

    def test_ps_json_output(self, vnx_paths, write_pid, capsys):
        write_pid("json_proc", os.getpid())
        result = cmd_ps(vnx_paths, json_output=True)
        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "processes" in data
        assert len(data["processes"]) == 1
        assert data["processes"][0]["name"] == "json_proc"
        assert data["processes"][0]["alive"] is True

    def test_ps_json_has_ppid(self, vnx_paths, write_pid, capsys):
        write_pid("ppid_proc", os.getpid())
        PidMetadata.write(Path(vnx_paths["VNX_PIDS_DIR"]), "ppid_proc", os.getpid())
        result = cmd_ps(vnx_paths, json_output=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        proc = data["processes"][0]
        assert "ppid" in proc
        assert proc["ppid"] is not None

    def test_ps_no_dir(self, vnx_paths, capsys):
        import shutil
        shutil.rmtree(vnx_paths["VNX_PIDS_DIR"])
        result = cmd_ps(vnx_paths)
        assert result == 0


# ---------------------------------------------------------------------------
# cmd_cleanup
# ---------------------------------------------------------------------------

class TestCmdCleanup:

    def test_cleanup_no_orphans(self, vnx_paths, write_pid, capsys):
        write_pid("live", os.getpid())
        result = cmd_cleanup(vnx_paths)
        assert result == 0
        captured = capsys.readouterr()
        assert "0 orphan" in captured.out

    def test_cleanup_removes_dead_pids(self, vnx_paths, write_pid, capsys):
        write_pid("dead", 99999999)
        result = cmd_cleanup(vnx_paths)
        assert result == 0
        captured = capsys.readouterr()
        assert "1 orphan" in captured.out
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        assert not (pids_dir / "dead.pid").exists()
        assert not (pids_dir / "dead.pid.fingerprint").exists()

    def test_cleanup_preserves_live_pids(self, vnx_paths, write_pid):
        write_pid("live", os.getpid())
        cmd_cleanup(vnx_paths)
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        assert (pids_dir / "live.pid").exists()

    def test_cleanup_dry_run(self, vnx_paths, write_pid, capsys):
        write_pid("dead", 99999999)
        result = cmd_cleanup(vnx_paths, dry_run=True)
        assert result == 0
        captured = capsys.readouterr()
        assert "dry-run" in captured.out
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        assert (pids_dir / "dead.pid").exists()  # not removed

    def test_cleanup_stale_locks(self, vnx_paths, capsys):
        locks_dir = Path(vnx_paths["VNX_LOCKS_DIR"])
        lock = locks_dir / "stale.lock"
        lock.mkdir()
        (lock / "pid").write_text("99999999\n")
        (lock / "fingerprint").write_text("/fake\n")

        result = cmd_cleanup(vnx_paths)
        captured = capsys.readouterr()
        assert "1 stale lock" in captured.out
        assert not lock.exists()

    def test_cleanup_preserves_live_locks(self, vnx_paths):
        locks_dir = Path(vnx_paths["VNX_LOCKS_DIR"])
        lock = locks_dir / "live.lock"
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()) + "\n")

        cmd_cleanup(vnx_paths)
        assert lock.exists()

    def test_cleanup_empty_pids_dir(self, vnx_paths, capsys):
        result = cmd_cleanup(vnx_paths)
        assert result == 0
        captured = capsys.readouterr()
        assert "0 orphan" in captured.out


# ---------------------------------------------------------------------------
# cmd_restart
# ---------------------------------------------------------------------------

class TestCmdRestart:

    def test_restart_unknown_process(self, vnx_paths, capsys):
        result = cmd_restart(vnx_paths, "nonexistent")
        assert result == 1
        captured = capsys.readouterr()
        assert "Unknown process" in captured.out

    def test_restart_missing_script(self, vnx_paths, capsys):
        result = cmd_restart(vnx_paths, "dispatcher")
        assert result == 1
        captured = capsys.readouterr()
        assert "Script not found" in captured.out

    def test_restart_starts_process(self, vnx_paths, capsys):
        """Test that restart can start a simple process."""
        scripts_dir = Path(vnx_paths["VNX_HOME"]) / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        # Create a simple script that sleeps
        script = scripts_dir / "dispatcher_v8_minimal.sh"
        script.write_text("#!/bin/bash\nsleep 60\n")
        script.chmod(0o755)

        result = cmd_restart(vnx_paths, "dispatcher")
        pids_dir = Path(vnx_paths["VNX_PIDS_DIR"])
        pid_file = pids_dir / "dispatcher.pid"

        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            # Clean up the spawned process
            try:
                os.kill(pid, 9)
            except (OSError, ProcessLookupError):
                pass

        captured = capsys.readouterr()
        assert "Starting dispatcher" in captured.out


# ---------------------------------------------------------------------------
# MANAGED_PROCESSES constant
# ---------------------------------------------------------------------------

class TestManagedProcesses:

    def test_known_processes(self):
        assert "dispatcher" in MANAGED_PROCESSES
        assert "receipt_processor" in MANAGED_PROCESSES
        assert "smart_tap" in MANAGED_PROCESSES

    def test_all_scripts_have_extension(self):
        for name, script in MANAGED_PROCESSES.items():
            assert script.endswith(".sh") or script.endswith(".py"), f"{name} script has no extension"
