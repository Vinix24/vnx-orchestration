"""ADR-012 worker-permission enforcement feature flag — default-OFF tests (15-08).

Covers the provider/headless lane (subprocess_adapter._build_worker_scope_args).
The tmux interactive lane's launch-command and completion-protocol tests went with
that lane on 2026-09-18.

Verifies:
  - Flag absent (default OFF, since the 15-08 flip was reverted pending a
    remeasurement) → blanket skip; truthy → scoped spawn, no skip flag.
  - Explicit opt-outs (VNX_WORKER_ENFORCEMENT_SKIP=1, or falsy
    VNX_ENFORCE_WORKER_PERMISSIONS) → byte-for-byte blanket skip.
  - A role absent from EVERY register (not in worker_permissions.yaml's
    profiles, no agents/<role>/) refuses via UnknownRoleError (OI-1069 pt.5).
  - A role present in the agents/ registry but with no worker_permissions.yaml
    profile still falls back to the functional code-worker profile
    (is_fallback=True) — that is the OI-1100 fallback, unchanged.
  - Receipt marker is emitted only when the flag is explicitly ON.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from worker_permissions import (
    UnknownRoleError,
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


# ---------------------------------------------------------------------------
# worker_permission_enforcement_enabled()
# ---------------------------------------------------------------------------

class TestWorkerPermissionEnforcementEnabled:
    def test_default_off(self):
        # Both VNX_WORKER_ENFORCEMENT_SKIP and VNX_ENFORCE_WORKER_PERMISSIONS
        # unset → enforcement is OFF (the 15-08 flip was reverted pending a
        # remeasurement of the outside-rate with directory matching repaired).
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VNX_ENFORCE_WORKER_PERMISSIONS", None)
            os.environ.pop("VNX_WORKER_ENFORCEMENT_SKIP", None)
            assert worker_permission_enforcement_enabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "True", "yes", "on"])
    def test_truthy_values(self, truthy):
        # Explicit opt-IN while the default is OFF.
        with patch.dict(os.environ, {"VNX_ENFORCE_WORKER_PERMISSIONS": truthy}, clear=False):
            os.environ.pop("VNX_WORKER_ENFORCEMENT_SKIP", None)
            assert worker_permission_enforcement_enabled() is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", ""])
    def test_falsy_values(self, falsy):
        with patch.dict(
            os.environ, {"VNX_ENFORCE_WORKER_PERMISSIONS": falsy}, clear=False
        ):
            os.environ.pop("VNX_WORKER_ENFORCEMENT_SKIP", None)
            assert worker_permission_enforcement_enabled() is False

    @pytest.mark.parametrize("skip", ["1", "true", "True", "yes", "on"])
    def test_worker_enforcement_skip_optout(self, skip):
        # Explicit per-dispatch opt-out stays a hard off regardless of default.
        with patch.dict(os.environ, {"VNX_WORKER_ENFORCEMENT_SKIP": skip}, clear=False):
            os.environ.pop("VNX_ENFORCE_WORKER_PERMISSIONS", None)
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

    def test_flag_on_unknown_role_refuses_with_unknown_role_error(self):
        # A role absent from EVERY register (not a worker_permissions.yaml
        # profile key, not an agents/<role>/ entry) is not a fallback case
        # (OI-1069 pt.5) — it must refuse, not silently hand out a tool scope
        # nobody chose. Red on pre-fix code: this used to fall back.
        with _set_enforcement(True):
            with pytest.raises(UnknownRoleError):
                _build_worker_scope_args("nonexistent-role-xyz")

    def test_flag_on_role_known_elsewhere_without_yaml_profile_falls_back(self):
        # blog-writer is a real agents/blog-writer/ entry with no profile in
        # .vnx/worker_permissions.yaml — that is NOT the same failure as a role
        # absent from every register; it keeps the OI-1100 explicit fallback.
        with _set_enforcement(True):
            args = _build_worker_scope_args("blog-writer")

        assert "--dangerously-skip-permissions" not in args
        assert "--permission-mode" in args
        assert "--allowedTools" in args
        # Fallback code-worker profile denies WebSearch/WebFetch
        assert "WebSearch" in args[args.index("--disallowedTools") + 1]


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
                receipt_kind="dispatch",
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
            receipt_kind="dispatch",
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
