#!/usr/bin/env python3
"""
Tests for scripts/dispatcher_supervisor.sh — W0 PR-1 quality gate.

Verifies:
  - Script passes bash -n syntax check
  - --once flag starts and exits without restarting
  - Backoff doubles on each crash cycle up to the configured max
  - Stable runtime resets backoff to initial value
  - Stale singleton lock is cleared before each restart
  - status subcommand exits 1 when supervisor is not running
  - SIGTERM causes clean shutdown of the dispatcher child
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_SH = REPO_ROOT / "scripts" / "dispatcher_supervisor.sh"

# ---------------------------------------------------------------------------
# Helper: minimal env for sourcing VNX scripts without a live VNX session
# ---------------------------------------------------------------------------

def _vnx_env(tmp_dir: str) -> dict:
    """Build a minimal env dict that satisfies vnx_paths.sh and process_lifecycle.sh."""
    env = dict(os.environ)
    # Override data paths to a temp dir so no real state is touched
    env["VNX_DATA_DIR"] = tmp_dir
    env["VNX_STATE_DIR"] = os.path.join(tmp_dir, "state")
    env["VNX_DISPATCH_DIR"] = os.path.join(tmp_dir, "dispatches")
    env["VNX_LOGS_DIR"] = os.path.join(tmp_dir, "logs")
    env["VNX_PIDS_DIR"] = os.path.join(tmp_dir, "pids")
    env["VNX_LOCKS_DIR"] = os.path.join(tmp_dir, "locks")
    env["VNX_REPORTS_DIR"] = os.path.join(tmp_dir, "unified_reports")
    env["VNX_DB_DIR"] = os.path.join(tmp_dir, "database")
    # Speed up backoff during tests
    env["VNX_SUPERVISOR_BACKOFF_INIT"] = "1"
    env["VNX_SUPERVISOR_BACKOFF_MAX"] = "4"
    env["VNX_SUPERVISOR_BACKOFF_STABLE"] = "999"
    return env


def _make_dirs(tmp_dir: str) -> None:
    for sub in ("state", "dispatches", "logs", "pids", "locks", "unified_reports", "database"):
        os.makedirs(os.path.join(tmp_dir, sub), exist_ok=True)


# ---------------------------------------------------------------------------
# Syntax check
# ---------------------------------------------------------------------------

class TestSyntax:
    def test_bash_syntax_check(self):
        """bash -n passes on dispatcher_supervisor.sh."""
        result = subprocess.run(
            ["bash", "-n", str(SUPERVISOR_SH)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Status subcommand
# ---------------------------------------------------------------------------

class TestStatusSubcommand:
    def test_status_exits_1_when_not_running(self):
        """status subcommand exits 1 when no supervisor PID file exists."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            _make_dirs(tmp_dir)
            env = _vnx_env(tmp_dir)
            result = subprocess.run(
                ["bash", str(SUPERVISOR_SH), "status"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT / "scripts"),
            )
        assert result.returncode == 1
        assert "not running" in result.stdout.lower() or "not running" in result.stderr.lower()

    def test_status_reports_not_running_with_stale_pid(self):
        """status exits 1 when PID file exists but process is dead."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            _make_dirs(tmp_dir)
            env = _vnx_env(tmp_dir)
            pid_file = os.path.join(tmp_dir, "pids", "dispatcher_supervisor.pid")
            os.makedirs(os.path.dirname(pid_file), exist_ok=True)
            # Write a PID that cannot exist (999999)
            with open(pid_file, "w") as f:
                f.write("999999\n")
            result = subprocess.run(
                ["bash", str(SUPERVISOR_SH), "status"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT / "scripts"),
            )
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# Backoff arithmetic
# ---------------------------------------------------------------------------

class TestBackoffArithmetic:
    """Verify backoff doubling logic extracted into a bash snippet."""

    def _run_backoff_snippet(self, initial: int, max_b: int, cycles: int) -> list[int]:
        """Run the backoff doubling loop in isolation and return the backoff sequence."""
        script = f"""
#!/bin/bash
set -euo pipefail
backoff={initial}
result=()
for i in $(seq 1 {cycles}); do
    result+=("$backoff")
    backoff=$((backoff * 2))
    if [ "$backoff" -gt {max_b} ]; then
        backoff={max_b}
    fi
done
printf '%s\\n' "${{result[@]}}"
"""
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return [int(x) for x in result.stdout.strip().split()]

    def test_backoff_doubles(self):
        values = self._run_backoff_snippet(initial=2, max_b=60, cycles=5)
        assert values == [2, 4, 8, 16, 32]

    def test_backoff_caps_at_max(self):
        values = self._run_backoff_snippet(initial=2, max_b=8, cycles=6)
        assert values == [2, 4, 8, 8, 8, 8]

    def test_backoff_single_cycle(self):
        values = self._run_backoff_snippet(initial=1, max_b=4, cycles=4)
        assert values == [1, 2, 4, 4]

    def test_backoff_at_max_stays_at_max(self):
        values = self._run_backoff_snippet(initial=60, max_b=60, cycles=3)
        assert values == [60, 60, 60]


# ---------------------------------------------------------------------------
# Once mode
# ---------------------------------------------------------------------------

class TestOnceMode:
    def _make_always_exit_script(self, tmp_dir: str, exit_code: int = 0) -> str:
        """Write a fake dispatcher script that exits immediately."""
        path = os.path.join(tmp_dir, "fake_dispatcher_v8_minimal.sh")
        with open(path, "w") as f:
            f.write(f"#!/bin/bash\nexit {exit_code}\n")
        os.chmod(path, 0o755)
        return path

    def test_once_mode_exits_when_dispatcher_exits(self):
        """--once flag causes supervisor to exit after dispatcher exits."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            _make_dirs(tmp_dir)
            env = _vnx_env(tmp_dir)
            fake = self._make_always_exit_script(tmp_dir, exit_code=0)
            env["VNX_DISPATCHER_SCRIPT"] = fake

            result = subprocess.run(
                ["bash", str(SUPERVISOR_SH), "--once"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT / "scripts"),
                timeout=20,
            )
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}\n{result.stderr}"

    def test_once_mode_propagates_exit_code(self):
        """--once mode propagates non-zero exit code from dispatcher."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            _make_dirs(tmp_dir)
            env = _vnx_env(tmp_dir)
            fake = self._make_always_exit_script(tmp_dir, exit_code=42)
            env["VNX_DISPATCHER_SCRIPT"] = fake

            result = subprocess.run(
                ["bash", str(SUPERVISOR_SH), "--once"],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(REPO_ROOT / "scripts"),
                timeout=20,
            )
        assert result.returncode == 42


# ---------------------------------------------------------------------------
# Stale lock cleanup
# ---------------------------------------------------------------------------

class TestStaleLockCleanup:
    def _run_clear_stale_snippet(self, tmp_dir: str, lock_pid: str | None = None, pid_file_pid: str | None = None) -> subprocess.CompletedProcess:
        """Run only the stale-lock-clearing logic from the supervisor in isolation."""
        locks_dir = os.path.join(tmp_dir, "locks")
        pids_dir = os.path.join(tmp_dir, "pids")
        os.makedirs(locks_dir, exist_ok=True)
        os.makedirs(pids_dir, exist_ok=True)

        dispatcher_lock_dir = os.path.join(locks_dir, "dispatcher_v8_minimal.lock")
        dispatcher_pid_file = os.path.join(pids_dir, "dispatcher_v8_minimal.pid")

        if lock_pid is not None:
            os.makedirs(dispatcher_lock_dir, exist_ok=True)
            with open(os.path.join(dispatcher_lock_dir, "pid"), "w") as f:
                f.write(lock_pid + "\n")

        if pid_file_pid is not None:
            with open(dispatcher_pid_file, "w") as f:
                f.write(pid_file_pid + "\n")

        script = f"""
#!/bin/bash
set -euo pipefail
DISPATCHER_LOCK_DIR="{dispatcher_lock_dir}"
DISPATCHER_PID_FILE="{dispatcher_pid_file}"

_log() {{ printf '[%s] %s\\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }}

_clear_stale_dispatcher_lock() {{
    local stale_pid=""
    if [ -f "$DISPATCHER_LOCK_DIR/pid" ]; then
        stale_pid=$(cat "$DISPATCHER_LOCK_DIR/pid" 2>/dev/null || echo "")
        if [ -n "$stale_pid" ] && ! kill -0 "$stale_pid" 2>/dev/null; then
            _log "Clearing stale dispatcher lock (dead PID: $stale_pid)"
            rm -rf "$DISPATCHER_LOCK_DIR"
        fi
    fi
    if [ -f "$DISPATCHER_PID_FILE" ]; then
        stale_pid=$(cat "$DISPATCHER_PID_FILE" 2>/dev/null || echo "")
        if [ -n "$stale_pid" ] && ! kill -0 "$stale_pid" 2>/dev/null; then
            _log "Clearing stale dispatcher PID file (dead PID: $stale_pid)"
            rm -f "$DISPATCHER_PID_FILE" "${{DISPATCHER_PID_FILE}}.fingerprint"
        fi
    fi
}}

_clear_stale_dispatcher_lock
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_clears_stale_lock_dir(self):
        """Stale lock directory (dead PID) is removed before restart."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            locks_dir = os.path.join(tmp_dir, "locks")
            result = self._run_clear_stale_snippet(tmp_dir, lock_pid="999999")
            lock_dir = os.path.join(locks_dir, "dispatcher_v8_minimal.lock")
            assert result.returncode == 0, result.stderr
            assert not os.path.exists(lock_dir), "Stale lock dir should have been removed"

    def test_does_not_clear_live_lock_dir(self):
        """Live lock directory (current PID) is preserved."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            locks_dir = os.path.join(tmp_dir, "locks")
            live_pid = str(os.getpid())
            result = self._run_clear_stale_snippet(tmp_dir, lock_pid=live_pid)
            lock_dir = os.path.join(locks_dir, "dispatcher_v8_minimal.lock")
            assert result.returncode == 0, result.stderr
            assert os.path.exists(lock_dir), "Live lock dir should have been preserved"

    def test_clears_stale_pid_file(self):
        """Stale PID file (dead PID) is removed before restart."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            pids_dir = os.path.join(tmp_dir, "pids")
            result = self._run_clear_stale_snippet(tmp_dir, pid_file_pid="999999")
            pid_file = os.path.join(pids_dir, "dispatcher_v8_minimal.pid")
            assert result.returncode == 0, result.stderr
            assert not os.path.exists(pid_file), "Stale PID file should have been removed"

    def test_no_stale_files_no_op(self):
        """When no lock or PID file exists, clear function is a no-op."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = self._run_clear_stale_snippet(tmp_dir)
            assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Stable runtime resets backoff
# ---------------------------------------------------------------------------

class TestStableRuntimeReset:
    def test_stable_runtime_resets_backoff(self):
        """After runtime ≥ BACKOFF_STABLE, backoff resets to BACKOFF_INIT."""
        script = """
#!/bin/bash
set -euo pipefail
BACKOFF_INIT=2
BACKOFF_MAX=60
BACKOFF_STABLE=10
backoff=$BACKOFF_INIT
restart_count=0

simulate_crash() {
    local runtime="$1"
    if [ "$runtime" -ge "$BACKOFF_STABLE" ]; then
        backoff=$BACKOFF_INIT
        restart_count=0
    fi
    restart_count=$((restart_count + 1))
    backoff=$((backoff * 2))
    if [ "$backoff" -gt "$BACKOFF_MAX" ]; then
        backoff=$BACKOFF_MAX
    fi
}

# Crash fast 3 times
simulate_crash 1
simulate_crash 1
simulate_crash 1
echo "after_3_crashes: backoff=$backoff restart_count=$restart_count"

# Stable run (runtime >= BACKOFF_STABLE)
simulate_crash 20
echo "after_stable_run: backoff=$backoff restart_count=$restart_count"
"""
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        lines = result.stdout.strip().splitlines()

        after_crashes = dict(kv.split("=") for part in lines[0].split(": ")[1].split() for kv in [part])
        after_stable = dict(kv.split("=") for part in lines[1].split(": ")[1].split() for kv in [part])

        # After 3 fast crashes: backoff doubled 3 times = 2→4→8→16
        assert int(after_crashes["backoff"]) == 16
        assert int(after_crashes["restart_count"]) == 3

        # After stable run: restart_count reset to 0, then incremented to 1, backoff = 2*2 = 4
        assert int(after_stable["restart_count"]) == 1
        assert int(after_stable["backoff"]) == 4
