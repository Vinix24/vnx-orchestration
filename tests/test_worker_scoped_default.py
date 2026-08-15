"""mcp-scoped-default: scoped worker-mode is the fabric default (14-08).

Per operator directive 2026-08-14, blanket ``--dangerously-skip-permissions``
is no longer the default: headless workers spawn scoped by default (empty
ambient MCP + ``acceptEdits`` + role allow-list), because worktree isolation
bounds the filesystem, not the network, and an MCP server talks to a service
outside the checkout. This module tests the BEHAVIOR, not the flag labels.

All spawn tests call the real ``_default_launch_command`` / the real
``worker_scoped_enabled`` (no mocked predicates):

  1. worker_scoped_enabled() is True with no env vars set
  2. VNX_WORKER_BLANKET_SKIP=1 -> spawn line carries --dangerously-skip-permissions
  3. default spawn line has --mcp-config '{"mcpServers":{}}' BEFORE --strict-mcp-config
  4. requires_mcp=True omits the MCP flags (still scoped otherwise)
  5. the import-fallback stub delivers the scoped posture, not blanket-skip
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[1] / "scripts" / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from tmux_interactive_dispatch import _default_launch_command  # noqa: E402
from worker_permissions import EMPTY_MCP_CONFIG, worker_scoped_enabled  # noqa: E402

SKIP_FLAG = "--dangerously-skip-permissions"


class TestWorkerScopedEnabledDefault:
    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        monkeypatch.delenv("VNX_WORKER_BLANKET_SKIP", raising=False)
        assert worker_scoped_enabled() is True

    def test_legacy_falsey_opt_out_disables(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_BLANKET_SKIP", raising=False)
        monkeypatch.setenv("VNX_WORKER_SCOPED", "0")
        assert worker_scoped_enabled() is False


class TestBlanketSkipOptOutSpawn:
    def test_blanket_skip_flag_emits_skip(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        monkeypatch.delenv("VNX_ENFORCE_WORKER_PERMISSIONS", raising=False)
        monkeypatch.setenv("VNX_WORKER_BLANKET_SKIP", "1")
        cmd = _default_launch_command("sonnet", skip_permissions=True)
        assert SKIP_FLAG in cmd
        assert "--mcp-config" not in cmd
        assert "--strict-mcp-config" not in cmd
        assert "--allowedTools" not in cmd


class TestDefaultSpawnMcpOrder:
    def test_default_spawn_has_mcp_config_before_strict(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        monkeypatch.delenv("VNX_WORKER_BLANKET_SKIP", raising=False)
        monkeypatch.delenv("VNX_ENFORCE_WORKER_PERMISSIONS", raising=False)
        cmd = _default_launch_command("sonnet", skip_permissions=True)
        assert SKIP_FLAG not in cmd
        assert "--mcp-config" in cmd
        assert "--strict-mcp-config" in cmd
        assert cmd.index("--mcp-config") < cmd.index("--strict-mcp-config")
        tokens = shlex.split(cmd)
        idx = tokens.index("--mcp-config")
        assert tokens[idx + 1] == EMPTY_MCP_CONFIG

    def test_requires_mcp_true_omits_mcp_flags(self, monkeypatch):
        monkeypatch.delenv("VNX_WORKER_SCOPED", raising=False)
        monkeypatch.delenv("VNX_WORKER_BLANKET_SKIP", raising=False)
        monkeypatch.delenv("VNX_ENFORCE_WORKER_PERMISSIONS", raising=False)
        cmd = _default_launch_command("sonnet", skip_permissions=True, requires_mcp=True)
        assert "--mcp-config" not in cmd
        assert "--strict-mcp-config" not in cmd
        # Still scoped — only the MCP-clearing pair is dropped.
        assert "--permission-mode" in cmd
        assert "--allowedTools" in cmd
        assert SKIP_FLAG not in cmd


class TestImportFallbackStubScoped:
    def test_fallback_delivers_scoped_posture(self):
        # Force the `import worker_permissions` inside tmux_interactive_dispatch
        # to fail, then call the real _default_launch_command: the fallback stub
        # must assemble a scoped spawn (never blanket skip).
        code = (
            "import sys\n"
            f"sys.path.insert(0, {_LIB!r})\n"
            "import builtins\n"
            "_real_import = builtins.__import__\n"
            "def _blocked(name, *a, **k):\n"
            "    if name == 'worker_permissions':\n"
            "        raise ImportError('blocked for test')\n"
            "    return _real_import(name, *a, **k)\n"
            "builtins.__import__ = _blocked\n"
            "import tmux_interactive_dispatch as t\n"
            "assert t._WP_AVAILABLE is False, 'worker_permissions import was not blocked'\n"
            "cmd = t._default_launch_command('sonnet', skip_permissions=True)\n"
            "print('CMD=' + cmd)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr
        cmd_line = next(
            line[len("CMD="):]
            for line in proc.stdout.splitlines()
            if line.startswith("CMD=")
        )
        assert SKIP_FLAG not in cmd_line
        assert "--permission-mode" in cmd_line
        assert "--allowedTools" in cmd_line
        assert cmd_line.index("--mcp-config") < cmd_line.index("--strict-mcp-config")
        # The import-fault fallback must also deny the whole mcp__ namespace
        # (extension bridges are out of the empty --mcp-config's reach).
        assert "--disallowedTools" in cmd_line
        assert "mcp__*" in cmd_line
