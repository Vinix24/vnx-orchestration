#!/usr/bin/env python3
"""PR-SR-2 round-2 — Shell injection regression tests for check_route.sh.

Verifies that provider/model values containing shell metacharacters (single
quotes, double quotes, backticks, $(), semicolons) do NOT break out of the
Python -c invocation.  The script should exit with code 1 (constraint_enforcer
rejects unknown providers) or code 0, never with a Python SyntaxError or
shell expansion.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "guardrails" / "check_route.sh"
REPO_ROOT = SCRIPT.parent.parent.parent


def _run_check_route(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


class TestShellInjectionGuard:

    def test_single_quote_in_provider(self):
        result = _run_check_route("--provider", "o'malley")
        assert result.returncode in (0, 1), f"unexpected exit code {result.returncode}: {result.stderr}"
        assert "SyntaxError" not in result.stderr

    def test_double_quote_in_model(self):
        result = _run_check_route("--provider", "claude", "--model", 'model"break')
        assert result.returncode in (0, 1), f"unexpected exit code {result.returncode}: {result.stderr}"
        assert "SyntaxError" not in result.stderr

    def test_backtick_in_provider(self):
        result = _run_check_route("--provider", "test`whoami`")
        assert result.returncode in (0, 1), f"unexpected exit code {result.returncode}: {result.stderr}"
        assert "SyntaxError" not in result.stderr

    def test_dollar_parens_in_model(self):
        result = _run_check_route("--provider", "claude", "--model", "$(echo pwned)")
        assert result.returncode in (0, 1), f"unexpected exit code {result.returncode}: {result.stderr}"
        assert "SyntaxError" not in result.stderr

    def test_semicolon_in_via(self):
        result = _run_check_route("--provider", "claude", "--via", "cli;echo pwned")
        assert result.returncode in (0, 1), f"unexpected exit code {result.returncode}: {result.stderr}"
        assert "SyntaxError" not in result.stderr

    def test_normal_route_still_works(self):
        result = _run_check_route("--provider", "claude", "--model", "claude-opus-4-7", "--terminal-id", "T0")
        assert result.returncode == 0, f"expected exit 0 for valid route: {result.stderr}"
        assert "route allowed" in result.stdout

    def test_blocked_route_exits_1(self):
        result = _run_check_route(
            "--provider", "litellm", "--sub-provider", "moonshot", "--via", "api",
        )
        assert result.returncode == 1, f"expected exit 1 for blocked route: {result.stderr}"
        assert "BLOCKED" in result.stderr
