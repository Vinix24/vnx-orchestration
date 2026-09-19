#!/usr/bin/env python3
"""tmux_command_runner.py — thin injectable transport for real ``tmux`` calls.

Shared by the worker-permission relay (``worker_permission_relay`` and
``permission_relay_cli``), which drive tmux sessions through an object runner
(``runner.run(args) -> TmuxResult``, ``runner.available() -> bool``) so tests can
inject a fake instead of spawning a real tmux.

This module used to live inside the tmux dispatch lane. It moved here when that
lane was removed (2026-09-18), because the relay is session management and keeps
using the transport.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class TmuxResult:
    """Result of a single ``tmux`` invocation."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class TmuxCommandRunner:
    """Thin wrapper around real ``tmux`` subprocess calls."""

    def run(
        self,
        args: list[str],
        *,
        timeout: int = 10,
        input_text: "str | None" = None,
    ) -> TmuxResult:
        proc = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
        return TmuxResult(proc.returncode, proc.stdout, proc.stderr)

    def available(self) -> bool:
        return shutil.which("tmux") is not None
