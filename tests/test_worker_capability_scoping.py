#!/usr/bin/env python3
"""Tests for the interim worker-capability scoping.

Covers WORKER-CAPABILITY-SCOPING-DESIGN.md §5 (interim, pre-full-binding):
  - generate_claude_settings(default profile) yields a code-worker allow-list
  - build_claude_scoping_args: empty ambient MCP + acceptEdits + allow-list,
    and NO --dangerously-skip-permissions
  - VNX_WORKER_SCOPED flag toggles scoped vs legacy posture (default ON)
  - SubprocessAdapter.deliver() spawn argv reflects the scoped posture
  - tmux _default_launch_command() detached spawn reflects the scoped posture
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from worker_permissions import (  # noqa: E402
    DEFAULT_CODE_WORKER_TOOLS,
    EMPTY_MCP_CONFIG,
    build_claude_scoping_args,
    default_code_worker_profile,
    generate_claude_settings,
    worker_scoping_enabled,
)

CODE_ESSENTIALS = {"Read", "Write", "Edit", "MultiEdit", "Bash", "Glob", "Grep"}


# ---------------------------------------------------------------------------
# generate_claude_settings / default profile
# ---------------------------------------------------------------------------

class TestDefaultProfileSettings:
    def test_default_profile_allowlist_covers_code_essentials(self):
        settings = generate_claude_settings(default_code_worker_profile())
        allowed = set(settings["allowedTools"])
        # The interim must NOT strip a code worker's ability to write/commit.
        assert CODE_ESSENTIALS <= allowed, f"missing: {CODE_ESSENTIALS - allowed}"

    def test_default_tools_constant_matches_essentials(self):
        assert set(DEFAULT_CODE_WORKER_TOOLS) == CODE_ESSENTIALS

    def test_denied_tools_excluded_from_allowlist(self):
        from worker_permissions import PermissionProfile

        prof = PermissionProfile(
            role="backend-developer",
            allowed_tools=["Read", "Write", "WebSearch"],
            denied_tools=["WebSearch"],
        )
        allowed = generate_claude_settings(prof)["allowedTools"]
        assert "WebSearch" not in allowed
        assert "Read" in allowed and "Write" in allowed


# ---------------------------------------------------------------------------
# build_claude_scoping_args
# ---------------------------------------------------------------------------

class TestBuildScopingArgs:
    def test_args_isolate_mcp_and_drop_skip_permissions(self):
        args = build_claude_scoping_args()
        # Core security win: zero ambient MCP.
        assert "--strict-mcp-config" in args
        idx = args.index("--mcp-config")
        assert args[idx + 1] == EMPTY_MCP_CONFIG == '{"mcpServers":{}}'
        # No blanket bypass.
        assert "--dangerously-skip-permissions" not in args

    def test_args_use_accept_edits_by_default(self):
        args = build_claude_scoping_args()
        idx = args.index("--permission-mode")
        assert args[idx + 1] == "acceptEdits"

    def test_args_carry_code_worker_allowlist(self):
        args = build_claude_scoping_args()
        idx = args.index("--allowedTools")
        allowed = set(args[idx + 1].split(","))
        assert CODE_ESSENTIALS <= allowed

    def test_permission_mode_override(self):
        args = build_claude_scoping_args(permission_mode="default")
        idx = args.index("--permission-mode")
        assert args[idx + 1] == "default"


# ---------------------------------------------------------------------------
# VNX_WORKER_SCOPED flag
# ---------------------------------------------------------------------------

class TestWorkerScopingFlag:
    def test_default_is_enabled(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        assert worker_scoping_enabled() is True

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("VNX_WORKER_SCOPED", "1")
        assert worker_scoping_enabled() is True

    def test_off_values_disable(self, monkeypatch):
        for val in ("0", "false", "no", "off", "OFF", "False"):
            monkeypatch.setenv("VNX_WORKER_SCOPED", val)
            assert worker_scoping_enabled() is False, val


# ---------------------------------------------------------------------------
# SubprocessAdapter.deliver() spawn argv
# ---------------------------------------------------------------------------

def _make_alive_process(pid: int = 4321) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = pid
    proc.poll.return_value = None
    proc.returncode = None
    return proc


class TestSubprocessAdapterArgv:
    def test_scoped_argv_when_enabled(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        from subprocess_adapter import SubprocessAdapter

        adapter = SubprocessAdapter()
        adapter.spawn("T1", {"model": "opus", "instruction": "do work"})
        with patch("subprocess.Popen", return_value=_make_alive_process()) as mock_popen:
            adapter.deliver("T1", "dispatch-scoped")

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "claude"
        # MCP isolation present, skip-permissions gone.
        assert "--strict-mcp-config" in cmd
        midx = cmd.index("--mcp-config")
        assert cmd[midx + 1] == '{"mcpServers":{}}'
        assert "--dangerously-skip-permissions" not in cmd
        # Worker still functional: headless stream flags + model intact.
        assert "-p" in cmd
        assert "--output-format" in cmd and "stream-json" in cmd
        midx = cmd.index("--model")
        assert cmd[midx + 1] == "opus"
        # Allow-list keeps the code-worker tools.
        aidx = cmd.index("--allowedTools")
        assert CODE_ESSENTIALS <= set(cmd[aidx + 1].split(","))

    def test_legacy_argv_when_disabled(self, monkeypatch):
        monkeypatch.setenv("VNX_WORKER_SCOPED", "0")
        from subprocess_adapter import SubprocessAdapter

        adapter = SubprocessAdapter()
        adapter.spawn("T1", {"model": "sonnet"})
        with patch("subprocess.Popen", return_value=_make_alive_process()) as mock_popen:
            adapter.deliver("T1", "dispatch-legacy")

        cmd = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in cmd
        assert "--strict-mcp-config" not in cmd
        assert "-p" in cmd


# ---------------------------------------------------------------------------
# tmux ephemeral _default_launch_command()
# ---------------------------------------------------------------------------

class TestTmuxLaunchCommand:
    def test_detached_scoped_when_enabled(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        from tmux_interactive_dispatch import _default_launch_command

        cmd = _default_launch_command("sonnet", skip_permissions=True)
        assert "claude --model sonnet" in cmd
        assert "--strict-mcp-config" in cmd
        assert "--mcp-config" in cmd
        assert "--permission-mode acceptEdits" in cmd
        assert "--dangerously-skip-permissions" not in cmd
        # The empty-MCP JSON survives shell quoting and re-parses to one token.
        assert '{"mcpServers":{}}' in shlex.split(cmd)
        # Interactive lane guarantee preserved: no headless flag.
        assert "-p" not in shlex.split(cmd)

    def test_detached_legacy_when_disabled(self, monkeypatch):
        monkeypatch.setenv("VNX_WORKER_SCOPED", "0")
        from tmux_interactive_dispatch import _default_launch_command

        cmd = _default_launch_command("sonnet", skip_permissions=True)
        assert "--dangerously-skip-permissions" in cmd
        assert "--strict-mcp-config" not in cmd

    def test_attached_run_has_no_scoping_and_no_skip(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        from tmux_interactive_dispatch import _default_launch_command

        # Attached (human-in-the-loop) run: skip_permissions False — unchanged.
        cmd = _default_launch_command("sonnet", skip_permissions=False)
        assert "--dangerously-skip-permissions" not in cmd
        assert "--strict-mcp-config" not in cmd
        assert "claude --model sonnet" in cmd
