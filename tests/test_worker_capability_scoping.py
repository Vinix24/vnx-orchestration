#!/usr/bin/env python3
"""Tests for the INTERIM worker capability-scoping fix.

Per WORKER-CAPABILITY-SCOPING-DESIGN.md §5: ephemeral workers must spawn WITHOUT
``--dangerously-skip-permissions`` and WITHOUT ambient MCP, while remaining fully
functional code workers (Read/Write/Edit/Bash/Glob/Grep + git).

Covers:
  1. generate_claude_settings(default_profile) yields the code-worker allow-list
  2. build_claude_scope_args() — empty MCP, acceptEdits, allow/deny lists, no skip flag
  3. SubprocessAdapter.deliver() spawn argv: empty MCP, no skip-permissions
  4. resolve_worker_profile() fallback for unknown / empty roles
  5. VNX_WORKER_SCOPED=0 feature flag restores the legacy skip-permissions posture
  6. tmux _default_launch_command() detached spawn is scoped (no skip flag by default)
  7. negative-path: empty profile yields a still-valid scope argv
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from subprocess_adapter import SubprocessAdapter
from tmux_interactive_dispatch import _default_launch_command
from worker_permissions import (
    DEFAULT_CODE_WORKER_TOOLS,
    EMPTY_MCP_CONFIG,
    PermissionProfile,
    build_claude_scope_args,
    default_code_worker_profile,
    generate_claude_settings,
    resolve_worker_profile,
    worker_scoped_enabled,
)

CODE_WORKER_ESSENTIALS = {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}
SKIP_FLAG = "--dangerously-skip-permissions"


def _make_alive_process(pid: int = 4242) -> MagicMock:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = pid
    proc.poll.return_value = None
    proc.returncode = None
    return proc


# ---------------------------------------------------------------------------
# 1. generate_claude_settings — the previously-dead enforcement seam, now live
# ---------------------------------------------------------------------------

class TestGenerateClaudeSettings:
    def test_default_profile_covers_code_worker_essentials(self):
        settings = generate_claude_settings(default_code_worker_profile())
        allowed = set(settings["allowedTools"])
        assert CODE_WORKER_ESSENTIALS.issubset(allowed), (
            f"missing essentials: {CODE_WORKER_ESSENTIALS - allowed}"
        )
        # MultiEdit is a code-worker tool too.
        assert "MultiEdit" in allowed

    def test_denied_tools_excluded_from_allow_list(self):
        profile = PermissionProfile(
            role="r",
            allowed_tools=["Read", "Bash", "WebSearch"],
            denied_tools=["WebSearch"],
        )
        allowed = generate_claude_settings(profile)["allowedTools"]
        assert "WebSearch" not in allowed
        assert "Read" in allowed and "Bash" in allowed

    def test_default_profile_constant_matches(self):
        assert default_code_worker_profile().allowed_tools == DEFAULT_CODE_WORKER_TOOLS


# ---------------------------------------------------------------------------
# 2. build_claude_scope_args — per-adapter materialization
# ---------------------------------------------------------------------------

class TestBuildClaudeScopeArgs:
    def test_empty_mcp_and_strict_flag(self):
        args = build_claude_scope_args(default_code_worker_profile())
        assert "--strict-mcp-config" in args
        idx = args.index("--mcp-config")
        assert json.loads(args[idx + 1]) == {"mcpServers": {}}
        assert args[idx + 1] == EMPTY_MCP_CONFIG

    def test_no_skip_permissions_flag(self):
        args = build_claude_scope_args(default_code_worker_profile())
        assert SKIP_FLAG not in args

    def test_accept_edits_permission_mode(self):
        args = build_claude_scope_args(default_code_worker_profile())
        idx = args.index("--permission-mode")
        assert args[idx + 1] == "acceptEdits"

    def test_allowed_and_disallowed_tools_present(self):
        args = build_claude_scope_args(default_code_worker_profile())
        allow_idx = args.index("--allowedTools")
        allowed = args[allow_idx + 1].split(",")
        assert CODE_WORKER_ESSENTIALS.issubset(set(allowed))
        deny_idx = args.index("--disallowedTools")
        denied = args[deny_idx + 1].split(",")
        assert "WebSearch" in denied and "WebFetch" in denied

    def test_permission_mode_override(self):
        args = build_claude_scope_args(
            default_code_worker_profile(), permission_mode="default"
        )
        idx = args.index("--permission-mode")
        assert args[idx + 1] == "default"

    def test_empty_profile_still_scoped(self):
        # Negative path: a profile with no tools must still produce a valid,
        # MCP-isolated argv (never crash, never skip-permissions).
        args = build_claude_scope_args(PermissionProfile(role="empty"))
        assert "--strict-mcp-config" in args
        assert SKIP_FLAG not in args
        assert "--allowedTools" not in args  # nothing to allow-list


# ---------------------------------------------------------------------------
# 3. resolve_worker_profile — role resolution + fallback
# ---------------------------------------------------------------------------

class TestResolveWorkerProfile:
    def test_unknown_role_falls_back_to_code_worker(self):
        profile = resolve_worker_profile("does-not-exist")
        assert set(profile.allowed_tools) == set(DEFAULT_CODE_WORKER_TOOLS)

    def test_none_role_falls_back_to_code_worker(self):
        profile = resolve_worker_profile(None)
        assert set(profile.allowed_tools) == set(DEFAULT_CODE_WORKER_TOOLS)

    def test_known_role_loaded_from_yaml(self, tmp_path):
        yaml_file = tmp_path / "worker_permissions.yaml"
        yaml_file.write_text(
            "version: 1\n"
            "profiles:\n"
            "  custom-role:\n"
            "    allowed_tools: [Read, Bash]\n"
            "    denied_tools: [WebSearch]\n"
        )
        profile = resolve_worker_profile("custom-role", yaml_path=yaml_file)
        assert profile.allowed_tools == ["Read", "Bash"]
        assert profile.denied_tools == ["WebSearch"]


# ---------------------------------------------------------------------------
# 4. SubprocessAdapter.deliver() spawn argv
# ---------------------------------------------------------------------------

class TestDeliverSpawnArgv:
    def test_argv_has_empty_mcp_and_no_skip_permissions(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            adapter.deliver("T1", "dispatch-cap-1", instruction="do work")
        cmd = mock_popen.call_args[0][0]

        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert SKIP_FLAG not in cmd, "skip-permissions must be gone for the default worker"
        assert "--strict-mcp-config" in cmd
        idx = cmd.index("--mcp-config")
        assert json.loads(cmd[idx + 1]) == {"mcpServers": {}}

    def test_argv_keeps_functional_code_worker_tools(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            adapter.deliver("T1", "dispatch-cap-2", instruction="do work")
        cmd = mock_popen.call_args[0][0]
        allow_idx = cmd.index("--allowedTools")
        allowed = set(cmd[allow_idx + 1].split(","))
        assert CODE_WORKER_ESSENTIALS.issubset(allowed)

    def test_instruction_still_last_arg(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            adapter.deliver("T1", "dispatch-cap-3", instruction="the instruction")
        cmd = mock_popen.call_args[0][0]
        assert cmd[-1] == "the instruction"

    def test_feature_flag_off_restores_skip_permissions(self, monkeypatch):
        monkeypatch.setenv("VNX_WORKER_SCOPED", "0")
        adapter = SubprocessAdapter()
        proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            adapter.deliver("T1", "dispatch-cap-4", instruction="do work")
        cmd = mock_popen.call_args[0][0]
        assert SKIP_FLAG in cmd
        assert "--strict-mcp-config" not in cmd


# ---------------------------------------------------------------------------
# 5. tmux detached spawn
# ---------------------------------------------------------------------------

class TestTmuxDetachedSpawn:
    def test_detached_default_is_scoped(self):
        cmd = _default_launch_command("sonnet", skip_permissions=True)
        assert SKIP_FLAG not in cmd
        assert "--strict-mcp-config" in cmd
        assert "--permission-mode acceptEdits" in cmd
        assert '{"mcpServers":{}}' in cmd

    def test_attached_run_unchanged(self):
        cmd = _default_launch_command("sonnet", skip_permissions=False)
        assert SKIP_FLAG not in cmd
        assert "--strict-mcp-config" not in cmd
        assert cmd == "source ~/.zshrc 2>/dev/null; claude --model sonnet"

    def test_detached_flag_off_restores_skip(self, monkeypatch):
        monkeypatch.setenv("VNX_WORKER_SCOPED", "0")
        cmd = _default_launch_command("sonnet", skip_permissions=True)
        assert SKIP_FLAG in cmd
        assert "--strict-mcp-config" not in cmd


# ---------------------------------------------------------------------------
# 6. feature flag helper
# ---------------------------------------------------------------------------

class TestWorkerScopedEnabled:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        assert worker_scoped_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF", "False"])
    def test_falsey_values_disable(self, monkeypatch, val):
        monkeypatch.setenv("VNX_WORKER_SCOPED", val)
        assert worker_scoped_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_truthy_values_enable(self, monkeypatch, val):
        monkeypatch.setenv("VNX_WORKER_SCOPED", val)
        assert worker_scoped_enabled() is True
