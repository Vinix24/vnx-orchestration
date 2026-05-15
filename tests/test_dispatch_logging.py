"""Tests for scripts/lib/dispatch_logging.sh — log() stderr/stdout separation."""
from __future__ import annotations

import subprocess
from pathlib import Path

DISPATCH_LOGGING = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "dispatch_logging.sh"


def _run_log(msg: str) -> subprocess.CompletedProcess:
    script = f"""
source "{DISPATCH_LOGGING}"
log "{msg}"
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )


def test_log_writes_to_stderr_not_stdout():
    result = _run_log("hello from log")
    assert "hello from log" in result.stderr
    assert result.stdout == ""


def test_log_stderr_contains_timestamp():
    result = _run_log("ts-check")
    # Timestamp format: [YYYY-MM-DD HH:MM:SS]
    import re
    assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", result.stderr)


def test_log_does_not_pollute_command_substitution():
    """Simulates the concrete failure: skill=$( log "..."; echo "skill-name" ).
    The captured value must be only 'skill-name', not the log line."""
    script = f"""
source "{DISPATCH_LOGGING}"
captured=$(log "should not appear in stdout"; echo "skill-name")
echo "$captured"
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.stdout.strip() == "skill-name"
    assert "should not appear in stdout" in result.stderr
