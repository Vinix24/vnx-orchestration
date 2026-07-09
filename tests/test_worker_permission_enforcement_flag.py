"""ADR-012 worker-permission enforcement feature flag — default-OFF safety tests.

Covers both launch lanes:
  * tmux interactive lane (_default_launch_command)
  * provider/headless lane (subprocess_adapter._build_worker_scope_args)

Verifies:
  - Flag OFF/absent → byte-for-byte current behavior (--dangerously-skip-permissions).
  - Flag ON → role-scoped --allowedTools / --permission-mode and no skip flag.
  - Unknown role falls back to the functional code-worker profile.
  - Receipt marker is only emitted when the flag is ON.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from tmux_interactive_dispatch import _default_launch_command
from worker_permissions import (
    build_claude_scope_args,
    default_code_worker_profile,
    worker_permission_enforcement_enabled,
)

# Import subprocess_adapter helpers separately so we can patch env without
# reloading the whole module.
from subprocess_adapter import _build_worker_scope_args


def _set_enforcement(enforce: bool):
    """Patch environment helpers consistently across modules."""
    val = "1" if enforce else "0"
    env = {"VNX_ENFORCE_WORKER_PERMISSIONS": val, "VNX_WORKER_SCOPED": "0"}
    return patch.dict(os.environ, env, clear=False)


def _extract_receipt_from_protocol(protocol: str) -> dict:
    """Parse the receipt JSON from the done/failed bash block."""
    blocks = re.findall(r"```bash\n(.+?)\n```", protocol, re.DOTALL)
    assert blocks, "No bash blocks found in protocol"
    for block in blocks:
        for line in block.splitlines():
            if "--receipt" in line:
                m = re.search(r'--receipt\s+"((?:[^"\\]|\\.)*)"', line)
                if m:
                    raw = (
                        m.group(1)
                        .replace('\\"', '"')
                        .replace("$_VNX_TS", "2099-01-01T00:00:00Z")
                    )
                    return json.loads(raw)
    raise AssertionError("Could not find receipt in protocol")


# ---------------------------------------------------------------------------
# worker_permission_enforcement_enabled()
# ---------------------------------------------------------------------------

class TestWorkerPermissionEnforcementEnabled:
    def test_default_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_ENFORCE_WORKER_PERMISSIONS", None)
            assert worker_permission_enforcement_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "on"])
    def test_truthy_values(self, truthy):
        with patch.dict(os.environ, {"VNX_ENFORCE_WORKER_PERMISSIONS": truthy}, clear=False):
            assert worker_permission_enforcement_enabled() is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", ""])
    def test_falsy_values(self, falsy):
        with patch.dict(os.environ, {"VNX_ENFORCE_WORKER_PERMISSIONS": falsy}, clear=False):
            assert worker_permission_enforcement_enabled() is False


# ---------------------------------------------------------------------------
# Provider/headless lane: subprocess_adapter._build_worker_scope_args
# ---------------------------------------------------------------------------

class TestProviderLaneScopeArgs:
    def test_flag_off_uses_legacy_skip_flag(self):
        with _set_enforcement(False):
            args = _build_worker_scope_args("backend-developer")

        assert args == ["--dangerously-skip-permissions"]
        assert "--allowedTools" not in args
        assert "--permission-mode" not in args

    def test_flag_on_uses_role_scoped_args(self):
        with _set_enforcement(True):
            args = _build_worker_scope_args("backend-developer")

        assert "--dangerously-skip-permissions" not in args
        assert "--permission-mode" in args
        assert "--allowedTools" in args
        assert "Read" in args[args.index("--allowedTools") + 1]

    def test_flag_on_with_requires_mcp_omits_empty_mcp(self):
        with _set_enforcement(True):
            args = _build_worker_scope_args("backend-developer", requires_mcp=True)

        assert "--dangerously-skip-permissions" not in args
        assert "--permission-mode" in args
        assert "--allowedTools" in args
        assert "--strict-mcp-config" not in args
        assert "--mcp-config" not in args

    def test_flag_on_unknown_role_falls_back_to_code_worker(self):
        with _set_enforcement(True):
            args = _build_worker_scope_args("nonexistent-role-xyz")

        assert "--dangerously-skip-permissions" not in args
        assert "--permission-mode" in args
        assert "--allowedTools" in args
        # Fallback code-worker profile denies WebSearch/WebFetch
        assert "WebSearch" in args[args.index("--disallowedTools") + 1]


# ---------------------------------------------------------------------------
# Tmux interactive lane: _default_launch_command
# ---------------------------------------------------------------------------

class TestTmuxLaneLaunchCommand:
    def test_flag_off_uses_legacy_skip_flag(self):
        with _set_enforcement(False):
            cmd = _default_launch_command("sonnet", skip_permissions=True, role="backend-developer")

        assert "--dangerously-skip-permissions" in cmd
        assert "--allowedTools" not in cmd
        assert "--permission-mode" not in cmd

    def test_flag_on_uses_role_scoped_args(self):
        with _set_enforcement(True):
            cmd = _default_launch_command("sonnet", skip_permissions=True, role="backend-developer")

        assert "--dangerously-skip-permissions" not in cmd
        assert "--permission-mode" in cmd
        assert "--allowedTools" in cmd
        assert "WebSearch" in cmd  # denied_tools from backend-developer profile

    def test_flag_on_unknown_role_falls_back_to_code_worker(self):
        with _set_enforcement(True):
            cmd = _default_launch_command("sonnet", skip_permissions=True, role="unknown-role")

        assert "--dangerously-skip-permissions" not in cmd
        assert "--permission-mode" in cmd
        assert "--allowedTools" in cmd
        # Fallback denies WebSearch/WebFetch
        assert "WebSearch" in cmd

    def test_flag_off_args_are_byte_identical_to_legacy(self):
        """Default-OFF must produce the exact same launch line as before the feature."""
        with _set_enforcement(False):
            cmd = _default_launch_command("sonnet", skip_permissions=True, role="backend-developer")

        expected = (
            "source ~/.zshrc 2>/dev/null; claude --model sonnet --dangerously-skip-permissions"
        )
        assert cmd == expected


# ---------------------------------------------------------------------------
# Provider dispatch benchmark wrapper
# ---------------------------------------------------------------------------

class TestProviderDispatchBenchmarkWrapper:
    def _run_benchmark(self, enforce: bool) -> MagicMock:
        import provider_dispatch

        with _set_enforcement(enforce):
            with patch("provider_dispatch._prepare_provider_workdir") as mock_prep:
                mock_prep.return_value = (None, Path("/tmp"))
                with patch(
                    "provider_spawns.claude_spawn.spawn_claude"
                ) as mock_spawn:
                    mock_spawn.return_value = MagicMock(
                        error=None, timed_out=False, returncode=0
                    )
                    with patch("provider_dispatch._emit_governance"):
                        with patch("provider_dispatch._finish_provider_worktree"):
                            args = MagicMock()
                            args.role = "backend-developer"
                            args.model = "sonnet"
                            args.dispatch_id = "disp-001"
                            args.terminal_id = "T1"
                            args.instruction = "noop"
                            args.max_retries = 3
                            args.no_auto_commit = False
                            args.gate = ""
                            args.dispatch_paths = ""
                            args.pr_id = None
                            provider_dispatch._dispatch_claude_benchmark(args)
        return mock_spawn

    def test_benchmark_passes_skip_permissions_when_flag_off(self):
        mock_spawn = self._run_benchmark(enforce=False)
        call_kwargs = mock_spawn.call_args.kwargs
        assert call_kwargs["skip_permissions"] is True

    def test_benchmark_passes_scoped_when_flag_on(self):
        mock_spawn = self._run_benchmark(enforce=True)
        call_kwargs = mock_spawn.call_args.kwargs
        assert call_kwargs["skip_permissions"] is False


# ---------------------------------------------------------------------------
# Receipt marker
# ---------------------------------------------------------------------------

class TestReceiptMarker:
    def test_emit_dispatch_receipt_has_marker_when_flag_on(self, tmp_path: Path):
        from governance_emit import emit_dispatch_receipt

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with _set_enforcement(True):
            path = emit_dispatch_receipt(
                dispatch_id="disp-marker",
                terminal_id="T1",
                provider="claude",
                model="sonnet",
                pr_id=None,
                status="success",
                completion_pct=100,
                risk=0.0,
                findings=[],
                duration_seconds=1.0,
                token_usage={"input": 0, "output": 0, "cache_hit": 0},
                cost_usd=0.0,
                state_dir=state_dir,
                permission_enforcement="enforced",
            )

        lines = path.read_text().strip().splitlines()
        receipt = json.loads(lines[-1])
        assert receipt["permission_enforcement"] == "enforced"

    def test_emit_dispatch_receipt_omits_marker_when_not_provided(self, tmp_path: Path):
        from governance_emit import emit_dispatch_receipt

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        path = emit_dispatch_receipt(
            dispatch_id="disp-no-marker",
            terminal_id="T1",
            provider="claude",
            model="sonnet",
            pr_id=None,
            status="success",
            completion_pct=100,
            risk=0.0,
            findings=[],
            duration_seconds=1.0,
            token_usage={"input": 0, "output": 0, "cache_hit": 0},
            cost_usd=0.0,
            state_dir=state_dir,
        )

        lines = path.read_text().strip().splitlines()
        receipt = json.loads(lines[-1])
        assert "permission_enforcement" not in receipt

    def test_tmux_completion_protocol_includes_marker_when_flag_on(self):
        from tmux_interactive_dispatch import TmuxInteractiveDispatch

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            lane = TmuxInteractiveDispatch(state_dir, project_root=state_dir)
            with _set_enforcement(True):
                protocol = lane._build_completion_protocol("disp-003", "T1", model="sonnet")

            receipt = _extract_receipt_from_protocol(protocol)
            assert receipt["permission_enforcement"] == "enforced"

    def test_tmux_completion_protocol_omits_marker_when_flag_off(self):
        from tmux_interactive_dispatch import TmuxInteractiveDispatch

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            lane = TmuxInteractiveDispatch(state_dir, project_root=state_dir)
            with _set_enforcement(False):
                protocol = lane._build_completion_protocol("disp-004", "T1", model="sonnet")

            receipt = _extract_receipt_from_protocol(protocol)
            assert "permission_enforcement" not in receipt


# ---------------------------------------------------------------------------
# build_claude_scope_args integration
# ---------------------------------------------------------------------------

class TestBuildClaudeScopeArgs:
    def test_backend_profile_generates_expected_args(self):
        profile = default_code_worker_profile()
        args = build_claude_scope_args(profile)

        assert args[0:2] == ["--permission-mode", "acceptEdits"]
        assert "--allowedTools" in args
        assert "Read" in args[args.index("--allowedTools") + 1]
        assert "--disallowedTools" in args
        assert "WebSearch" in args[args.index("--disallowedTools") + 1]
