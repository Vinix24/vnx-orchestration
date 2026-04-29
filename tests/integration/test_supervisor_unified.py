#!/usr/bin/env python3
"""End-to-end integration tests for VNX supervisors (SUP-PR4).

Covers both `dispatcher_supervisor.sh` and `receipt_processor_supervisor.sh`
end-to-end by launching the real wrapper script with a fake child target,
sending real SIGKILL signals, and asserting respawn within bounded time.

Run: python3 -m pytest tests/integration/test_supervisor_unified.py -xvs -m integration
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
DISPATCHER_SUPERVISOR = SCRIPTS_DIR / "dispatcher_supervisor.sh"
RECEIPT_SUPERVISOR = SCRIPTS_DIR / "receipt_processor_supervisor.sh"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vnx_env(tmp_dir: Path) -> dict:
    """Build a minimal env dict that satisfies vnx_paths.sh."""
    env = dict(os.environ)
    env["VNX_DATA_DIR"] = str(tmp_dir)
    env["VNX_STATE_DIR"] = str(tmp_dir / "state")
    env["VNX_DISPATCH_DIR"] = str(tmp_dir / "dispatches")
    env["VNX_LOGS_DIR"] = str(tmp_dir / "logs")
    env["VNX_PIDS_DIR"] = str(tmp_dir / "pids")
    env["VNX_LOCKS_DIR"] = str(tmp_dir / "locks")
    env["VNX_REPORTS_DIR"] = str(tmp_dir / "unified_reports")
    env["VNX_DB_DIR"] = str(tmp_dir / "database")
    env["VNX_SUPERVISOR_BACKOFF_INIT"] = "1"
    env["VNX_SUPERVISOR_BACKOFF_MAX"] = "2"
    env["VNX_SUPERVISOR_BACKOFF_STABLE"] = "999"
    return env


def _make_dirs(tmp_dir: Path) -> None:
    for sub in ("state", "dispatches", "logs", "pids", "locks", "unified_reports", "database"):
        (tmp_dir / sub).mkdir(parents=True, exist_ok=True)


def _write_fake_child(path: Path, exit_code: int | None = None, sleep_secs: int = 60) -> None:
    """Write a fake child script.

    If exit_code is None: long-running sleep (sleep_secs).
    If exit_code is set: exit immediately with that code.
    Records its own PID to <path>.pid each time it runs.
    """
    pid_record = str(path) + ".pid"
    runs_record = str(path) + ".runs"
    if exit_code is None:
        body = (
            f'echo $$ > "{pid_record}"\n'
            f'echo $$ >> "{runs_record}"\n'
            f'sleep {sleep_secs}\n'
        )
    else:
        body = (
            f'echo $$ > "{pid_record}"\n'
            f'echo $$ >> "{runs_record}"\n'
            f'exit {exit_code}\n'
        )
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _read_pid(pid_path: Path, timeout: float = 5.0) -> int:
    """Wait for a PID file to appear and return the int PID."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid_path.exists():
            txt = pid_path.read_text().strip()
            if txt:
                try:
                    return int(txt.splitlines()[-1])
                except ValueError:
                    pass
        time.sleep(0.1)
    raise TimeoutError(f"PID file did not appear: {pid_path}")


def _wait_pid_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.1)
    return False


def _stop_supervisor(proc: subprocess.Popen) -> None:
    """Send SIGTERM, then SIGKILL if needed."""
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                pass


def _launch_supervisor_with_fake_child(
    tmp_dir: Path,
    supervisor_sh: Path,
    fake_child: Path,
    real_child_name: str,
    once: bool = False,
) -> tuple[subprocess.Popen, dict]:
    """Launch a supervisor whose child target is replaced by a symlink to fake_child.

    The supervisor scripts hardcode the child path as
    `$SCRIPT_DIR/<child>.sh` — to swap in a fake we write a wrapper script
    in a temp scripts dir that sources lib/ from the real repo but exposes
    a fake child file.
    """
    # Build a self-contained mock VNX install rooted at <tmp>/scripts/.
    # Using "scripts/" (instead of "fake_scripts/") matters because
    # vnx_paths.sh derives VNX_HOME from <_VNX_PATHS_DIR>/.. and the supervisor
    # loads singleton_enforcer.sh via "$VNX_HOME/scripts/singleton_enforcer.sh".
    fake_scripts = tmp_dir / "scripts"
    fake_scripts.mkdir(exist_ok=True)
    (fake_scripts / "lib").mkdir(exist_ok=True)
    real_lib = SCRIPTS_DIR / "lib"
    for entry in real_lib.iterdir():
        target = fake_scripts / "lib" / entry.name
        if not target.exists():
            target.symlink_to(entry)
    singleton_link = fake_scripts / "singleton_enforcer.sh"
    if not singleton_link.exists():
        singleton_link.symlink_to(SCRIPTS_DIR / "singleton_enforcer.sh")
    # Drop the fake child at both locations: the canonical path AND the
    # lib/ path the supervisor currently resolves to (process_lifecycle.sh
    # clobbers SCRIPT_DIR before the child path is computed).
    fake_body = fake_child.read_text()
    for placement in (fake_scripts / f"{real_child_name}.sh",
                      fake_scripts / "lib" / f"{real_child_name}.sh"):
        placement.write_text(fake_body)
        placement.chmod(0o755)
    # Copy the supervisor script itself unchanged
    fake_supervisor = fake_scripts / supervisor_sh.name
    fake_supervisor.write_text(supervisor_sh.read_text())
    fake_supervisor.chmod(0o755)

    env = _vnx_env(tmp_dir)
    # Do NOT set VNX_HOME — vnx_paths.sh resets it (and unsets the data path
    # overrides) when inherited VNX_HOME mismatches the script-derived value.
    cmd = ["bash", str(fake_supervisor)]
    if once:
        cmd.append("--once")
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_dir),
        start_new_session=True,
    )
    return proc, env


# ---------------------------------------------------------------------------
# Test A — dispatcher SIGKILL respawn
# ---------------------------------------------------------------------------

def test_a_dispatcher_sigkill_respawn(tmp_path: Path):
    _make_dirs(tmp_path)
    fake_child = tmp_path / "fake_dispatcher_child.sh"
    _write_fake_child(fake_child, exit_code=None, sleep_secs=120)
    pid_record = Path(str(fake_child) + ".pid")
    runs_record = Path(str(fake_child) + ".runs")

    sup, _env = _launch_supervisor_with_fake_child(
        tmp_path, DISPATCHER_SUPERVISOR, fake_child, "dispatcher_v8_minimal"
    )
    try:
        first_pid = _read_pid(pid_record, timeout=10)
        # SIGKILL the child
        os.kill(first_pid, signal.SIGKILL)
        assert _wait_pid_dead(first_pid, timeout=5)

        # Within BACKOFF_INIT*2 = 2s, plus loop overhead, supervisor must respawn.
        deadline = time.time() + 8
        second_pid = first_pid
        while time.time() < deadline:
            try:
                txt = pid_record.read_text().strip()
                if txt:
                    candidate = int(txt.splitlines()[-1])
                    if candidate != first_pid:
                        second_pid = candidate
                        break
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(0.2)
        assert second_pid != first_pid, "Dispatcher supervisor did not respawn child after SIGKILL"
        # And the second child is alive
        os.kill(second_pid, 0)

        runs = runs_record.read_text().strip().splitlines()
        assert len(runs) >= 2, f"Expected >=2 runs after respawn, got {runs}"
    finally:
        _stop_supervisor(sup)


# ---------------------------------------------------------------------------
# Test B — receipt processor SIGKILL respawn
# ---------------------------------------------------------------------------

def test_b_receipt_processor_sigkill_respawn(tmp_path: Path):
    _make_dirs(tmp_path)
    fake_child = tmp_path / "fake_receipt_child.sh"
    _write_fake_child(fake_child, exit_code=None, sleep_secs=120)
    pid_record = Path(str(fake_child) + ".pid")
    runs_record = Path(str(fake_child) + ".runs")

    sup, _env = _launch_supervisor_with_fake_child(
        tmp_path, RECEIPT_SUPERVISOR, fake_child, "receipt_processor_v4"
    )
    try:
        first_pid = _read_pid(pid_record, timeout=10)
        os.kill(first_pid, signal.SIGKILL)
        assert _wait_pid_dead(first_pid, timeout=5)

        deadline = time.time() + 8
        second_pid = first_pid
        while time.time() < deadline:
            try:
                txt = pid_record.read_text().strip()
                if txt:
                    candidate = int(txt.splitlines()[-1])
                    if candidate != first_pid:
                        second_pid = candidate
                        break
            except (FileNotFoundError, ValueError):
                pass
            time.sleep(0.2)
        assert second_pid != first_pid, "Receipt supervisor did not respawn child after SIGKILL"
        os.kill(second_pid, 0)

        runs = runs_record.read_text().strip().splitlines()
        assert len(runs) >= 2, f"Expected >=2 runs after respawn, got {runs}"
    finally:
        _stop_supervisor(sup)


# ---------------------------------------------------------------------------
# Test C — clean exit-0 in --once mode
#
# The supervisor *always* respawns by design. Only --once mode treats a clean
# exit as terminal — the dispatch's "no respawn" requirement maps to that path.
# ---------------------------------------------------------------------------

def test_c_supervisor_handles_exit_0_gracefully(tmp_path: Path):
    _make_dirs(tmp_path)
    fake_child = tmp_path / "fake_dispatcher_child.sh"
    _write_fake_child(fake_child, exit_code=0)
    runs_record = Path(str(fake_child) + ".runs")

    sup, _env = _launch_supervisor_with_fake_child(
        tmp_path, DISPATCHER_SUPERVISOR, fake_child, "dispatcher_v8_minimal", once=True
    )
    try:
        rc = sup.wait(timeout=15)
    finally:
        _stop_supervisor(sup)

    assert rc == 0, f"Expected supervisor to exit 0 on clean child exit, got {rc}"
    runs = runs_record.read_text().strip().splitlines()
    assert len(runs) == 1, f"Expected exactly 1 run in --once mode, got {len(runs)}: {runs}"


# ---------------------------------------------------------------------------
# Test D — stale lock cleanup
# ---------------------------------------------------------------------------

def test_d_stale_lock_cleanup(tmp_path: Path):
    _make_dirs(tmp_path)
    fake_child = tmp_path / "fake_receipt_child.sh"
    _write_fake_child(fake_child, exit_code=None, sleep_secs=120)
    pid_record = Path(str(fake_child) + ".pid")

    # Pre-seed a stale lock + PID file with bogus PID before launch.
    # Names must match the singleton key used in receipt_processor_v4.sh:
    #   enforce_singleton "receipt_processor_v4.sh"
    locks_dir = tmp_path / "locks"
    pids_dir = tmp_path / "pids"
    stale_lock = locks_dir / "receipt_processor_v4.sh.lock"
    stale_lock.mkdir(parents=True, exist_ok=True)
    (stale_lock / "pid").write_text("999999\n")
    stale_pidfile = pids_dir / "receipt_processor_v4.sh.pid"
    stale_pidfile.write_text("999999\n")

    sup, _env = _launch_supervisor_with_fake_child(
        tmp_path, RECEIPT_SUPERVISOR, fake_child, "receipt_processor_v4"
    )
    try:
        # Supervisor should clean stale state, then spawn the child anyway.
        child_pid = _read_pid(pid_record, timeout=10)
        # Verify cleanup happened — stale lock dir's pid file should have been
        # rewritten to the live PID (or removed and recreated).
        if stale_lock.exists():
            current_lock_pid_file = stale_lock / "pid"
            if current_lock_pid_file.exists():
                live_pid = int(current_lock_pid_file.read_text().strip().splitlines()[-1])
                assert live_pid != 999999, "Stale lock PID was not cleared"
        # Direct positive proof: the child is running.
        os.kill(child_pid, 0)
    finally:
        _stop_supervisor(sup)


# ---------------------------------------------------------------------------
# Test E — supervisor singleton (second start does not double-launch)
#
# `enforce_singleton` is sourced after `exec >> "$LOG_FILE"`, so the singleton
# block message lands in the log file, not stdout. We verify the singleton
# property by asserting only one supervisor lineage spawned a child.
# ---------------------------------------------------------------------------

def test_e_supervisor_singleton(tmp_path: Path):
    _make_dirs(tmp_path)
    fake_child = tmp_path / "fake_receipt_child.sh"
    _write_fake_child(fake_child, exit_code=None, sleep_secs=120)
    runs_record = Path(str(fake_child) + ".runs")

    sup1, _env1 = _launch_supervisor_with_fake_child(
        tmp_path, RECEIPT_SUPERVISOR, fake_child, "receipt_processor_v4"
    )
    try:
        # Wait for first supervisor to spawn its child
        deadline = time.time() + 8
        while time.time() < deadline and not runs_record.exists():
            time.sleep(0.1)
        assert runs_record.exists(), "First supervisor never started its child"
        runs_after_one = len(runs_record.read_text().strip().splitlines())

        # Launch a second supervisor — must not start a second child concurrently.
        sup2, _env2 = _launch_supervisor_with_fake_child(
            tmp_path, RECEIPT_SUPERVISOR, fake_child, "receipt_processor_v4"
        )
        try:
            # Give sup2 a chance to either exit via singleton block or take over.
            try:
                sup2.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass
        finally:
            _stop_supervisor(sup2)

        # Read the log: must show the singleton block message OR
        # the runs count must not have increased while sup1 child was alive.
        log_file = tmp_path / "logs" / "receipt_processor_supervisor.log"
        log_text = log_file.read_text() if log_file.exists() else ""
        runs_after_two = len(runs_record.read_text().strip().splitlines())

        singleton_blocked = "Another instance" in log_text or "lock_held" in log_text
        no_extra_run = runs_after_two == runs_after_one

        assert singleton_blocked or no_extra_run, (
            f"Second supervisor neither logged singleton block nor refrained "
            f"from spawning a child. log={log_text!r} runs={runs_after_one}->{runs_after_two}"
        )
    finally:
        _stop_supervisor(sup1)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-xvs", "-m", "integration"]))
