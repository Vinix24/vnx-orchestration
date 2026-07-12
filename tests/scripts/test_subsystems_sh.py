#!/usr/bin/env python3
"""Tests for scripts/commands/subsystems.sh + its bin/vnx wiring
(framework-status-audit-and-cockpit PR-3 — dual-CLI parity).

Invokes the real `bin/vnx subsystems` entrypoint via subprocess (not a
reimplementation) so a broken wrapper or a missing `bin/vnx` case branch
fails this test.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BIN_VNX = REPO_ROOT / "bin" / "vnx"


def test_bash_wrapper_invokes_python_cli_and_returns_zero():
    result = subprocess.run(
        [str(BIN_VNX), "subsystems", "--json"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "subsystems" in payload
    assert len(payload["subsystems"]) >= 20


def test_bash_wrapper_md_flag_emits_ledger_table():
    result = subprocess.run(
        [str(BIN_VNX), "subsystems", "--md"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("| subsystem | what | flag | status | health |")


def test_commands_file_defines_cmd_subsystems():
    sh_path = REPO_ROOT / "scripts" / "commands" / "subsystems.sh"
    assert sh_path.is_file()
    text = sh_path.read_text(encoding="utf-8")
    assert "cmd_subsystems()" in text

    # bash -n syntax check — matches the worker's "every modified .sh file
    # must pass syntax check" rule.
    check = subprocess.run(["bash", "-n", str(sh_path)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_bin_vnx_wires_subsystems_command():
    text = BIN_VNX.read_text(encoding="utf-8")
    assert "subsystems)" in text
    assert "cmd_subsystems" in text
