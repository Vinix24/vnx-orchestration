"""Tests for scripts/lib/process_lifecycle.sh — SCRIPT_DIR must not leak into callers."""
from __future__ import annotations

import subprocess
from pathlib import Path

PROCESS_LIFECYCLE = Path(__file__).resolve().parent.parent / "scripts" / "lib" / "process_lifecycle.sh"


def _source_and_echo_script_dir(caller_script_dir: str) -> subprocess.CompletedProcess:
    """Source process_lifecycle.sh from a caller that has its own SCRIPT_DIR set."""
    script = f"""
set -euo pipefail
SCRIPT_DIR="{caller_script_dir}"
source "{PROCESS_LIFECYCLE}" 2>/dev/null || true
echo "$SCRIPT_DIR"
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
    )


def test_script_dir_not_overwritten_after_source():
    caller_dir = "/tmp/test-caller-script-dir"
    result = _source_and_echo_script_dir(caller_dir)
    actual = result.stdout.strip()
    assert actual == caller_dir, (
        f"SCRIPT_DIR was clobbered after sourcing process_lifecycle.sh: "
        f"expected '{caller_dir}', got '{actual}'"
    )


def test_script_dir_not_overwritten_with_real_path():
    """Second caller value to ensure it's not hardcoded."""
    caller_dir = "/tmp/another-caller"
    result = _source_and_echo_script_dir(caller_dir)
    actual = result.stdout.strip()
    assert actual == caller_dir, (
        f"SCRIPT_DIR was clobbered: expected '{caller_dir}', got '{actual}'"
    )


def test_ops_process_control_does_not_leak_script_dir():
    """ops_process_control.sh (which sources process_lifecycle.sh) must also not clobber SCRIPT_DIR."""
    ops_pc = PROCESS_LIFECYCLE.parent / "ops_process_control.sh"
    caller_dir = "/tmp/test-ops-caller"
    script = f"""
set -euo pipefail
SCRIPT_DIR="{caller_dir}"
source "{ops_pc}" 2>/dev/null || true
echo "$SCRIPT_DIR"
"""
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    actual = result.stdout.strip()
    assert actual == caller_dir, (
        f"SCRIPT_DIR clobbered by ops_process_control.sh: "
        f"expected '{caller_dir}', got '{actual}'"
    )
