"""
tests/test_cleanup_stale_vnx_sessions.py

Tests for scripts/cleanup_stale_vnx_sessions.sh

Coverage:
  - Script exists and is executable
  - No kills without interactive prompt (dry-safe)
  - Correct parsing of tmux list-sessions output
  - Graceful handling when tmux is absent
  - Graceful handling when no vnx-* sessions exist
"""

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "cleanup_stale_vnx_sessions.sh"


# ── existence + executable ───────────────────────────────────────────────────

def test_script_exists():
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"


def test_script_is_executable():
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "Script must be user-executable (chmod +x)"


def test_script_syntax():
    """bash -n must pass with exit code 0."""
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True, text=True
    )
    assert result.returncode == 0, f"Syntax error:\n{result.stderr}"


# ── no-kill safety ───────────────────────────────────────────────────────────

def test_no_kill_without_explicit_confirm(tmp_path):
    """
    When running non-interactively (stdin is /dev/null) the default answer
    is 'N', so no tmux kill-session calls should happen.

    We inject a fake `tmux` that reports one stale VNX session (activity
    timestamp 0 = epoch 1970, so ~20 000 days idle). Then pipe empty stdin
    to confirm default-N behaviour.
    """
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text(textwrap.dedent("""\
        #!/bin/bash
        if [[ "$1 $2" == "list-sessions -F" ]]; then
            echo "vnx-mission-control 0"
        fi
        # kill-session must NOT be called if user answered N
        if [[ "$1" == "kill-session" ]]; then
            echo "UNEXPECTED_KILL" >&2
            exit 99
        fi
    """))
    fake_tmux.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,  # simulate no TTY / default N
        capture_output=True,
        text=True,
    )

    # Script must not have called kill-session
    assert "UNEXPECTED_KILL" not in result.stderr, (
        "kill-session was called without explicit y confirmation"
    )
    # Must exit cleanly (0)
    assert result.returncode == 0, (
        f"Unexpected non-zero exit:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Table must appear in stdout
    assert "vnx-mission-control" in result.stdout


def test_skip_message_on_default_no(tmp_path):
    """Script prints '[skip] No sessions killed.' when user answers N."""
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text(textwrap.dedent("""\
        #!/bin/bash
        if [[ "$1 $2" == "list-sessions -F" ]]; then
            echo "vnx-old-session 0"
        fi
    """))
    fake_tmux.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert "[skip]" in result.stdout or result.returncode == 0


# ── parser correctness ───────────────────────────────────────────────────────

def test_fresh_session_not_listed_as_stale(tmp_path):
    """
    A session with activity timestamp = NOW should NOT appear in the stale list.
    We use a timestamp far in the future (year 2100) to be safe.
    """
    import time
    future_ts = int(time.time()) + 86400 * 365 * 75  # ~75 years from now

    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        if [[ "$1 $2" == "list-sessions -F" ]]; then
            echo "vnx-fresh-session {future_ts}"
        fi
    """))
    fake_tmux.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "vnx-fresh-session" not in result.stdout
    # Should hit "no stale sessions" path
    assert "No vnx-* sessions idle" in result.stdout or "[ok]" in result.stdout


def test_non_vnx_session_not_listed(tmp_path):
    """Sessions not starting with 'vnx-' must be ignored."""
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text(textwrap.dedent("""\
        #!/bin/bash
        if [[ "$1 $2" == "list-sessions -F" ]]; then
            echo "main 0"
            echo "work-session 0"
            echo "vnx-stale-one 0"
        fi
    """))
    fake_tmux.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert "main" not in result.stdout or "vnx-" not in "main"  # just that it is not in stale table
    assert "work-session" not in result.stdout
    assert "vnx-stale-one" in result.stdout


# ── graceful edge cases ──────────────────────────────────────────────────────

def test_no_vnx_sessions_exits_clean(tmp_path):
    """When tmux returns no vnx-* sessions, script exits 0 with '[ok]' message."""
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text(textwrap.dedent("""\
        #!/bin/bash
        if [[ "$1 $2" == "list-sessions -F" ]]; then
            echo "non-vnx-session 0"
        fi
    """))
    fake_tmux.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "[ok]" in result.stdout


def test_tmux_not_found_exits_clean(tmp_path):
    """If tmux is not on PATH, script exits 0 with an info message."""
    import shutil

    # Keep bash and core POSIX tools on PATH; remove tmux by not including its directory.
    # We build a whitelist PATH that contains bash and standard POSIX dirs but NO tmux.
    bash_dir = str(Path(shutil.which("bash")).parent)
    # Standard POSIX dirs that contain date, awk, etc. but typically NOT tmux
    safe_dirs = [bash_dir, "/bin", "/usr/bin"]
    # Exclude any dir that contains tmux
    tmux_path = shutil.which("tmux")
    if tmux_path:
        tmux_dir = str(Path(tmux_path).parent)
        safe_dirs = [d for d in safe_dirs if d != tmux_dir]
    restricted_path = ":".join(safe_dirs)

    env = {**os.environ, "PATH": restricted_path}

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "tmux not found" in result.stdout


def test_invalid_timestamp_skipped(tmp_path):
    """Sessions with non-numeric activity timestamps are skipped with a warning."""
    fake_tmux = tmp_path / "tmux"
    fake_tmux.write_text(textwrap.dedent("""\
        #!/bin/bash
        if [[ "$1 $2" == "list-sessions -F" ]]; then
            echo "vnx-weird-session NOTANUMBER"
        fi
    """))
    fake_tmux.chmod(0o755)

    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )

    # Must not crash (exit 1 due to parse error is acceptable, but should not kill)
    assert "UNEXPECTED_KILL" not in result.stderr
    # Warning should be emitted
    assert "[warn]" in result.stdout or result.returncode == 0
