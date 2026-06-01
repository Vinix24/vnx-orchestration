#!/usr/bin/env python3
"""test_provider_dispatch_deepseek_harness.py — routing + constraint + argv.

Covers the wiring that turns spawn_deepseek_harness into a governed lane:

  - provider_dispatch registers deepseek-harness as implemented
  - main() routes --provider deepseek-harness to _dispatch_deepseek_harness
  - the handler fast-fails (EX_USAGE) when DEEPSEEK_API_KEY is absent
  - constraint enforcer: own-key key-auth via PASSES; OAuth-subscription via is BLOCKED
  - SubprocessAdapter.deliver actually injects the MCP-off flags into the claude argv
    (real adapter code, subprocess.Popen mocked) — between --model and the instruction
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

import provider_dispatch as pd  # noqa: E402
from provider_spawns.deepseek_harness_spawn import (  # noqa: E402
    build_harness_cli_args,
    build_harness_env,
)
from subprocess_adapter import SubprocessAdapter  # noqa: E402

_CONSTRAINTS = Path(__file__).parent.parent / "scripts" / "lib" / "providers" / "provider_constraints.yaml"
_FAKE_KEY = "sk-deepseek-test-key-1234567890abcd"


# ---------------------------------------------------------------------------
# Registration + routing
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_deepseek_harness_is_implemented(self):
        assert "deepseek-harness" in pd._IMPLEMENTED_PROVIDERS

    def test_registry_key_maps_to_deepseek_pricing(self):
        assert pd._PROVIDER_TO_REGISTRY_KEY.get("deepseek-harness") == "deepseek"


class TestRouting:
    def _args(self):
        ns = MagicMock()
        ns.provider = "deepseek-harness"
        ns.terminal_id = "T1"
        ns.dispatch_id = "d-routing"
        ns.instruction = "Reply OK."
        ns.model = "sonnet"
        ns.role = "backend-developer"
        ns.dispatch_paths = ""
        ns.auto_route = False
        return ns

    def test_main_routes_to_deepseek_harness_handler(self):
        called = {"hit": False}

        def _fake_handler(args):
            called["hit"] = True
            return 0

        with patch.object(pd, "_build_parser") as MockParser, \
                patch.object(pd, "_dispatch_deepseek_harness", _fake_handler), \
                patch("env_loader.load_env", lambda: None), \
                patch.dict("os.environ", {"DEEPSEEK_API_KEY": _FAKE_KEY}):
            MockParser.return_value.parse_args.return_value = self._args()
            rc = pd.main(["--provider", "deepseek-harness"])

        assert called["hit"] is True
        assert rc == 0

    def test_handler_fast_fails_without_key(self):
        import os
        args = self._args()
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            rc = pd._dispatch_deepseek_harness(args)
        assert rc == pd._EX_USAGE


# ---------------------------------------------------------------------------
# Constraint enforcement: key-auth passes, OAuth blocked
# ---------------------------------------------------------------------------

class TestConstraint:
    def _enforcer(self):
        from constraint_enforcer import ConstraintEnforcer
        return ConstraintEnforcer(path=_CONSTRAINTS)

    def test_keyed_harness_via_is_allowed(self):
        enf = self._enforcer()
        # Must not raise — this is the measured-safe own-key key-auth lane.
        # via=claude_harness_keyed matches the SSOT vocabulary that
        # provider_dispatch._constraint_via_for_provider emits for this lane.
        enf.enforce(
            provider="deepseek-harness",
            sub_provider="deepseek",
            model="deepseek-v4-pro",
            via="claude_harness_keyed",
        )

    def test_subscription_harness_via_is_blocked(self):
        from constraint_enforcer import HardConstraintViolation
        enf = self._enforcer()
        with pytest.raises(HardConstraintViolation) as exc:
            enf.enforce(
                provider="deepseek-harness",
                sub_provider="deepseek",
                model="deepseek-v4-pro",
                via="claude_harness_subscription",
            )
        assert exc.value.constraint_id == "deepseek-harness-subscription-blocked"

    def test_constraint_file_has_renamed_id(self):
        import yaml
        data = yaml.safe_load(_CONSTRAINTS.read_text())
        ids = {c["id"] for c in data["constraints"]}
        assert "deepseek-harness-subscription-blocked" in ids
        assert "deepseek-path-d-blocked" not in ids, "stale Path-D-blocked must be removed"


# ---------------------------------------------------------------------------
# Real adapter argv: MCP-off flags land between --model and instruction
# ---------------------------------------------------------------------------

class TestAdapterArgvInjection:
    def test_extra_cli_args_injected_into_claude_argv(self):
        adapter = SubprocessAdapter()
        captured = {}

        class _FakeProc:
            pid = 4321
            stdout = MagicMock()
            stderr = MagicMock()

            def poll(self):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        with patch("subprocess_adapter.subprocess.Popen", _fake_popen), \
                patch("subprocess_adapter.os.setsid", lambda: None):
            adapter._get_event_store = lambda: None
            result = adapter.deliver(
                "T1",
                "d-argv",
                instruction="Reply OK.",
                model="deepseek-v4-pro",
                extra_env=build_harness_env(_FAKE_KEY),
                extra_cli_args=build_harness_cli_args(),
            )

        assert result.success is True
        cmd = captured["cmd"]
        # MCP-off flags present, JSON value directly after --mcp-config
        mcp_idx = cmd.index("--mcp-config")
        assert cmd[mcp_idx + 1] == '{"mcpServers":{}}'
        # boolean --strict-mcp-config terminates the variadic before the prompt
        strict_idx = cmd.index("--strict-mcp-config")
        assert strict_idx == mcp_idx + 2, "boolean must immediately follow the JSON value"
        # model flag present with deepseek model
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "deepseek-v4-pro"
        # MCP flags come after --model; instruction appears after MCP-off flags (#770 invariant)
        assert model_idx < mcp_idx
        instr_idx = cmd.index("Reply OK.")
        assert instr_idx > strict_idx, "instruction must follow the MCP-off flags"
        assert instr_idx < len(cmd) - 1, "scope args must follow instruction (#770 invariant)"
        # key-auth env reached Popen
        assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
        assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == _FAKE_KEY
        assert captured["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"

    def test_default_claude_argv_unchanged_without_extra_cli_args(self):
        """Backward-compat: no extra_cli_args => no extra flags between --model and instruction."""
        adapter = SubprocessAdapter()
        captured = {}

        class _FakeProc:
            pid = 1
            stdout = MagicMock()
            stderr = MagicMock()

            def poll(self):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc()

        with patch("subprocess_adapter.subprocess.Popen", _fake_popen), \
                patch("subprocess_adapter.os.setsid", lambda: None):
            adapter._get_event_store = lambda: None
            adapter.deliver("T1", "d-plain", instruction="hi", model="sonnet")

        cmd = captured["cmd"]
        instr_idx = cmd.index("hi")
        model_idx = cmd.index("--model")
        # no extra flags injected between --model value and instruction without extra_cli_args
        assert instr_idx == model_idx + 2, "no extra flags between --model and instruction"
        # instruction precedes scope flags (#770 invariant)
        assert instr_idx < len(cmd) - 1, "scope args must follow instruction (#770 invariant)"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
