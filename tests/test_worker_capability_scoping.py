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

    def test_instruction_precedes_variadic_scope_flags(self):
        # Regression: --allowedTools / --disallowedTools are variadic (<tools...>).
        # If the instruction is appended AFTER them it is swallowed as a tool value,
        # leaving claude --print with no prompt -> "Input must be provided" -> exit 1.
        # The prompt must therefore come BEFORE the scope flags.
        adapter = SubprocessAdapter()
        proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            adapter.deliver("T1", "dispatch-cap-3", instruction="the instruction")
        cmd = mock_popen.call_args[0][0]
        assert "the instruction" in cmd
        instr_idx = cmd.index("the instruction")
        allow_idx = cmd.index("--allowedTools")
        assert instr_idx < allow_idx, (
            "instruction must precede --allowedTools or it is consumed as a tool value"
        )
        if "--disallowedTools" in cmd:
            assert instr_idx < cmd.index("--disallowedTools")

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


# ---------------------------------------------------------------------------
# 7. FIX 1: detached tmux spawn must include --allowedTools (no-stall)
# ---------------------------------------------------------------------------

class TestTmuxDetachedNoStall:
    """Detached worker gets --allowedTools so it can proceed without TTY prompts."""

    def test_detached_spawn_has_allowed_tools(self):
        cmd = _default_launch_command("sonnet", skip_permissions=True)
        assert "--allowedTools" in cmd
        assert SKIP_FLAG not in cmd

    def test_detached_spawn_allowed_tools_cover_code_essentials(self):
        import shlex as _shlex
        cmd = _default_launch_command("sonnet", skip_permissions=True)
        tokens = _shlex.split(cmd)
        idx = tokens.index("--allowedTools")
        allowed = set(tokens[idx + 1].split(","))
        assert CODE_WORKER_ESSENTIALS.issubset(allowed), (
            f"essentials missing from --allowedTools: {CODE_WORKER_ESSENTIALS - allowed}"
        )

    def test_detached_spawn_no_dangerously_skip_permissions(self):
        cmd = _default_launch_command("sonnet", skip_permissions=True)
        assert SKIP_FLAG not in cmd

    def test_attached_spawn_no_allowed_tools_injected(self):
        # Attached (human-in-loop) sessions are unchanged; no allowedTools injected.
        cmd = _default_launch_command("sonnet", skip_permissions=False)
        assert "--allowedTools" not in cmd
        assert SKIP_FLAG not in cmd


# ---------------------------------------------------------------------------
# 8. FIX 2: requires_mcp threading — MCP kept vs force-emptied
# ---------------------------------------------------------------------------

class TestRequiresMcpScoping:
    """Dispatches that declare Requires-MCP:true must NOT get force-empty MCP."""

    def test_requires_mcp_false_empties_mcp_in_scope_args(self):
        args = build_claude_scope_args(default_code_worker_profile(), requires_mcp=False)
        assert "--strict-mcp-config" in args
        idx = args.index("--mcp-config")
        assert json.loads(args[idx + 1]) == {"mcpServers": {}}

    def test_requires_mcp_true_does_not_empty_mcp_in_scope_args(self):
        args = build_claude_scope_args(default_code_worker_profile(), requires_mcp=True)
        assert "--strict-mcp-config" not in args
        assert "--mcp-config" not in args

    def test_requires_mcp_true_still_has_permission_mode_and_tools(self):
        args = build_claude_scope_args(default_code_worker_profile(), requires_mcp=True)
        assert "--permission-mode" in args
        assert "--allowedTools" in args

    def test_adapter_deliver_requires_mcp_false_empties_mcp(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            adapter.deliver("T1", "dispatch-mcp-1", instruction="do work", requires_mcp=False)
        cmd = mock_popen.call_args[0][0]
        assert "--strict-mcp-config" in cmd
        idx = cmd.index("--mcp-config")
        assert json.loads(cmd[idx + 1]) == {"mcpServers": {}}

    def test_adapter_deliver_requires_mcp_true_keeps_mcp(self):
        adapter = SubprocessAdapter()
        proc = _make_alive_process()
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            adapter.deliver("T1", "dispatch-mcp-2", instruction="do work", requires_mcp=True)
        cmd = mock_popen.call_args[0][0]
        assert "--strict-mcp-config" not in cmd
        assert "--mcp-config" not in cmd


# ---------------------------------------------------------------------------
# 9. FIX 3: role forwarded into SubprocessAdapter.deliver on claude_spawn path
# ---------------------------------------------------------------------------

class TestClaudeSpawnRoleForwarding:
    """spawn_claude() must forward role into SubprocessAdapter.deliver."""

    def test_role_forwarded_to_deliver(self):
        # spawn_claude forwards the role kwarg to adapter.deliver; verify that
        # a named role produces a different (role-scoped) allowedTools than the
        # default code-worker fallback, proving role is not silently dropped.
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib" / "provider_spawns"))
        from claude_spawn import spawn_claude
        from subprocess_adapter import SubprocessAdapter

        role_calls = []

        original_deliver = SubprocessAdapter.deliver

        def capturing_deliver(self, terminal_id, dispatch_id, *args, role=None, **kwargs):
            role_calls.append(role)
            # Return a minimal DeliveryResult so spawn_claude doesn't crash.
            from adapter_types import DeliveryResult
            return DeliveryResult(
                success=False,
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                pane_id=None,
                path_used="none",
                failure_reason="test-intercept",
            )

        with patch.object(SubprocessAdapter, "deliver", capturing_deliver):
            spawn_claude(
                prompt="test prompt",
                model="sonnet",
                dispatch_id="dispatch-role-1",
                terminal_id="T1",
                role="backend-developer",
            )

        assert len(role_calls) == 1
        assert role_calls[0] == "backend-developer", (
            f"role was not forwarded to deliver; got {role_calls[0]!r}"
        )

    def test_none_role_safe_fallback(self):
        # When role is not provided, spawn_claude must not crash and must still
        # pass role=None to deliver (triggering the default code-worker fallback).
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib" / "provider_spawns"))
        from claude_spawn import spawn_claude
        from subprocess_adapter import SubprocessAdapter

        role_calls = []

        def capturing_deliver(self, terminal_id, dispatch_id, *args, role=None, **kwargs):
            role_calls.append(role)
            from adapter_types import DeliveryResult
            return DeliveryResult(
                success=False,
                terminal_id=terminal_id,
                dispatch_id=dispatch_id,
                pane_id=None,
                path_used="none",
                failure_reason="test-intercept",
            )

        with patch.object(SubprocessAdapter, "deliver", capturing_deliver):
            spawn_claude(
                prompt="test prompt",
                model="sonnet",
                dispatch_id="dispatch-role-2",
                terminal_id="T1",
            )

        assert len(role_calls) == 1
        assert role_calls[0] is None, (
            f"expected None role for no-role spawn; got {role_calls[0]!r}"
        )
