#!/usr/bin/env python3
"""Tests for scripts/lib/get_terminal_claimed_by.py (OI-1521/1523 extraction).

Verifies:
  - Returns claimed_by when the terminal is present and claimed
  - Returns empty string when terminal is unclaimed (null/missing)
  - Returns empty string when terminal key is absent from terminal_state.json
  - Returns empty string when the state file does not exist
  - Returns empty string when state_file is malformed JSON
  - Accepts terminal_id as CLI argv[1]
  - Falls back to _VNX_STUCK_TERMINAL env var when argv[1] is absent
  - bash -n passes (trivially — it is a .py file; verified via python3 -m py_compile)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "lib" / "get_terminal_claimed_by.py"
SCRIPTS_LIB = str(REPO_ROOT / "scripts" / "lib")
if SCRIPTS_LIB not in sys.path:
    sys.path.insert(0, SCRIPTS_LIB)

from get_terminal_claimed_by import _get_terminal_claimed_by


# ---------------------------------------------------------------------------
# Unit tests — internal helper
# ---------------------------------------------------------------------------

class TestGetTerminalClaimedByUnit:
    """Direct tests for the _get_terminal_claimed_by() helper."""

    def _write_state(self, tmp_dir: str, data: dict) -> None:
        path = os.path.join(tmp_dir, "terminal_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_returns_claimed_by_when_present(self, tmp_path):
        """claimed_by is returned when the terminal is claimed."""
        self._write_state(
            str(tmp_path),
            {"terminals": {"T1": {"claimed_by": "dispatch-abc-123", "status": "busy"}}},
        )
        result = _get_terminal_claimed_by("T1", str(tmp_path))
        assert result == "dispatch-abc-123"

    def test_returns_empty_when_claimed_by_is_null(self, tmp_path):
        """claimed_by=null → returns empty string (not the string 'None')."""
        self._write_state(
            str(tmp_path),
            {"terminals": {"T1": {"claimed_by": None, "status": "idle"}}},
        )
        result = _get_terminal_claimed_by("T1", str(tmp_path))
        assert result == ""

    def test_returns_empty_when_claimed_by_is_missing(self, tmp_path):
        """Terminal entry exists but claimed_by key absent → empty string."""
        self._write_state(
            str(tmp_path),
            {"terminals": {"T1": {"status": "idle"}}},
        )
        result = _get_terminal_claimed_by("T1", str(tmp_path))
        assert result == ""

    def test_returns_empty_when_terminal_absent(self, tmp_path):
        """Terminal not in state file → empty string."""
        self._write_state(
            str(tmp_path),
            {"terminals": {"T2": {"claimed_by": "dispatch-xyz"}}},
        )
        result = _get_terminal_claimed_by("T1", str(tmp_path))
        assert result == ""

    def test_returns_empty_when_terminals_key_missing(self, tmp_path):
        """State JSON has no 'terminals' key → empty string."""
        self._write_state(str(tmp_path), {"other_key": {}})
        result = _get_terminal_claimed_by("T1", str(tmp_path))
        assert result == ""

    def test_returns_empty_when_state_file_absent(self, tmp_path):
        """No terminal_state.json in state dir → empty string, no exception."""
        result = _get_terminal_claimed_by("T1", str(tmp_path))
        assert result == ""

    def test_returns_empty_when_state_file_malformed(self, tmp_path):
        """Corrupt JSON → empty string, no exception."""
        path = os.path.join(str(tmp_path), "terminal_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        result = _get_terminal_claimed_by("T1", str(tmp_path))
        assert result == ""

    def test_returns_empty_when_state_dir_empty_string(self):
        """state_dir='' → empty string (file will not exist)."""
        result = _get_terminal_claimed_by("T1", "")
        assert result == ""

    def test_multiple_terminals(self, tmp_path):
        """Returns correct claimed_by when multiple terminals are present."""
        self._write_state(
            str(tmp_path),
            {
                "terminals": {
                    "T1": {"claimed_by": "dispatch-t1"},
                    "T2": {"claimed_by": "dispatch-t2"},
                    "T3": {"claimed_by": None},
                }
            },
        )
        assert _get_terminal_claimed_by("T1", str(tmp_path)) == "dispatch-t1"
        assert _get_terminal_claimed_by("T2", str(tmp_path)) == "dispatch-t2"
        assert _get_terminal_claimed_by("T3", str(tmp_path)) == ""


# ---------------------------------------------------------------------------
# CLI / subprocess tests
# ---------------------------------------------------------------------------

class TestGetTerminalClaimedByCLI:
    """Run the script as a subprocess to verify CLI arg handling."""

    def _run(self, args: list[str], env: dict | None = None, input_data: str | None = None) -> subprocess.CompletedProcess:
        cmd = [sys.executable, str(SCRIPT)] + args
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env or os.environ.copy(),
        )

    def _write_state(self, state_dir: str, data: dict) -> None:
        path = os.path.join(state_dir, "terminal_state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_cli_arg_takes_terminal_id(self, tmp_path):
        """Terminal ID passed as argv[1] is resolved correctly."""
        self._write_state(
            str(tmp_path),
            {"terminals": {"T2": {"claimed_by": "dispatch-cli-01"}}},
        )
        env = os.environ.copy()
        env["VNX_STATE_DIR"] = str(tmp_path)
        result = self._run(["T2"], env=env)
        assert result.returncode == 0
        assert result.stdout.strip() == "dispatch-cli-01"

    def test_cli_env_fallback_no_argv(self, tmp_path):
        """Falls back to _VNX_STUCK_TERMINAL env var when no argv[1]."""
        self._write_state(
            str(tmp_path),
            {"terminals": {"T3": {"claimed_by": "dispatch-env-01"}}},
        )
        env = os.environ.copy()
        env["VNX_STATE_DIR"] = str(tmp_path)
        env["_VNX_STUCK_TERMINAL"] = "T3"
        result = self._run([], env=env)
        assert result.returncode == 0
        assert result.stdout.strip() == "dispatch-env-01"

    def test_cli_empty_output_when_unclaimed(self, tmp_path):
        """Prints empty line when terminal is unclaimed."""
        self._write_state(
            str(tmp_path),
            {"terminals": {"T1": {"claimed_by": None}}},
        )
        env = os.environ.copy()
        env["VNX_STATE_DIR"] = str(tmp_path)
        result = self._run(["T1"], env=env)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_cli_exit_0_on_missing_state(self, tmp_path):
        """Exit code is 0 even when state file is absent."""
        env = os.environ.copy()
        env["VNX_STATE_DIR"] = str(tmp_path)
        result = self._run(["T1"], env=env)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_cli_exit_0_on_malformed_json(self, tmp_path):
        """Exit code is 0 on corrupt JSON — caller checks empty output."""
        path = os.path.join(str(tmp_path), "terminal_state.json")
        with open(path, "w") as fh:
            fh.write("NOT JSON")
        env = os.environ.copy()
        env["VNX_STATE_DIR"] = str(tmp_path)
        result = self._run(["T1"], env=env)
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_py_compile_passes(self):
        """Script compiles without syntax errors (regression guard for extraction)."""
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"py_compile failed:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Bash syntax check for dispatcher_supervisor_ticks.sh
# ---------------------------------------------------------------------------

class TestDispatcherSupervisorTicksSyntax:
    """bash -n must pass on the new lib file (OI-1523 extraction guard)."""

    def test_bash_n_passes(self):
        ticks_sh = REPO_ROOT / "scripts" / "lib" / "dispatcher_supervisor_ticks.sh"
        result = subprocess.run(
            ["bash", "-n", str(ticks_sh)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Dispatcher syntax guard after extraction
# ---------------------------------------------------------------------------

class TestDispatcherSyntaxAfterExtraction:
    """bash -n must still pass on the main dispatcher after the refactor."""

    def test_dispatcher_bash_n_passes(self):
        dispatcher = REPO_ROOT / "scripts" / "dispatcher_v8_minimal.sh"
        result = subprocess.run(
            ["bash", "-n", str(dispatcher)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed:\n{result.stderr}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-xvs"]))
