#!/usr/bin/env python3
"""Wave 4.6 / PR-5 — provider_dispatch.py entry-point tests.

Covers:
- Claude provider is explicitly rejected (PR-5: claude is not a provider-lane provider).
- Codex provider routes to _dispatch_codex (PR-4.6.3 wired).
- Gemini provider routes to _dispatch_gemini (PR-4.6.4 wired).
- LiteLLM provider raises SystemExit(64) with PR reference in message.
- Unknown provider triggers argparse error (SystemExit(2)).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_claude_argv(**overrides) -> list[str]:
    base = {
        "--provider": "claude",
        "--terminal-id": "T1",
        "--dispatch-id": "test-pr461-smoke",
        "--instruction": "noop",
        "--model": "sonnet",
    }
    base.update(overrides)
    argv = []
    for k, v in base.items():
        argv.extend([k, v])
    return argv


# ---------------------------------------------------------------------------
# Test: claude provider is rejected from the provider lane (PR-5)
# ---------------------------------------------------------------------------

class TestProviderClaudeRejectedFromProviderLane:
    """PR-5: claude is not a provider-lane provider.

    The single-entry dispatch door (dispatch_cli.py) owns all Claude routing.
    Direct --provider claude invocations via provider_dispatch must be hard-rejected
    so operators get a clear error pointing to the correct path.
    """

    def test_claude_returns_exit_64(self):
        """--provider claude returns exit code EX_USAGE (64), not 0 or 1."""
        result = provider_dispatch.main(_make_claude_argv())
        assert result == 64

    def test_claude_emits_reject_to_stderr(self, capsys):
        """--provider claude prints a REJECT message to stderr."""
        provider_dispatch.main(_make_claude_argv())
        err = capsys.readouterr().err
        assert "REJECT" in err

    def test_claude_does_not_call_subprocess_dispatch(self):
        """--provider claude must NOT delegate to subprocess_dispatch.deliver_with_recovery."""
        with patch("subprocess_dispatch.deliver_with_recovery") as mock_deliver:
            provider_dispatch.main(_make_claude_argv())
        mock_deliver.assert_not_called()

    def test_claude_error_references_dispatch_door(self, capsys):
        """Error message references the single-entry door so the operator knows the next step."""
        provider_dispatch.main(_make_claude_argv())
        err = capsys.readouterr().err
        assert "dispatch" in err.lower()

    def test_claude_with_extra_args_still_rejected(self):
        """--provider claude with extra flags (model, role, etc.) is still rejected."""
        argv = _make_claude_argv(**{"--model": "opus", "--role": "architect"})
        result = provider_dispatch.main(argv)
        assert result == 64


# ---------------------------------------------------------------------------
# Test: codex provider is routed to _dispatch_codex (PR-4.6.3 implemented)
# ---------------------------------------------------------------------------

class TestProviderCodexRouted:

    def test_codex_routes_to_dispatch_codex(self):
        """--provider codex calls _dispatch_codex and returns 0 on success."""
        argv = ["--provider", "codex", "--terminal-id", "T1",
                "--dispatch-id", "test-codex-routed", "--instruction", "noop"]

        with patch("provider_dispatch._dispatch_codex", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(argv)

        assert rc == 0
        mock_dispatch.assert_called_once()

    def test_codex_does_not_raise_system_exit_64(self):
        """--provider codex must no longer raise SystemExit(64)."""
        argv = ["--provider", "codex", "--terminal-id", "T1",
                "--dispatch-id", "test-codex-ok", "--instruction", "noop"]

        with patch("provider_dispatch._dispatch_codex", return_value=0):
            try:
                rc = provider_dispatch.main(argv)
            except SystemExit as exc:
                pytest.fail(f"provider codex raised SystemExit({exc.code}): should be routed now")


# ---------------------------------------------------------------------------
# Test: gemini provider is routed to _dispatch_gemini (PR-4.6.4 implemented)
# ---------------------------------------------------------------------------

class TestProviderGeminiRouted:

    def test_gemini_routes_to_dispatch_gemini(self):
        """--provider gemini calls _dispatch_gemini and returns 0 on success."""
        argv = ["--provider", "gemini", "--terminal-id", "T1",
                "--dispatch-id", "test-gemini-routed", "--instruction", "noop"]

        with patch("provider_dispatch._dispatch_gemini", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(argv)

        assert rc == 0
        mock_dispatch.assert_called_once()

    def test_gemini_does_not_raise_system_exit_64(self):
        """--provider gemini must no longer raise SystemExit(64)."""
        argv = ["--provider", "gemini", "--terminal-id", "T1",
                "--dispatch-id", "test-gemini-ok", "--instruction", "noop"]

        with patch("provider_dispatch._dispatch_gemini", return_value=0):
            try:
                rc = provider_dispatch.main(argv)
            except SystemExit as exc:
                pytest.fail(f"provider gemini raised SystemExit({exc.code}): should be routed now")


# ---------------------------------------------------------------------------
# Test: litellm:<model> routes to _dispatch_litellm (PR-4.6.5 implemented)
# ---------------------------------------------------------------------------

class TestProviderLitellmRouted:

    def test_litellm_routes_to_dispatch_litellm(self):
        """--provider litellm:deepseek calls _dispatch_litellm and returns 0 on success."""
        argv = ["--provider", "litellm:deepseek", "--terminal-id", "T1",
                "--dispatch-id", "test-litellm-routed", "--instruction", "noop"]
        with patch("provider_dispatch._dispatch_litellm", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(argv)
        assert rc == 0
        mock_dispatch.assert_called_once()

    def test_bare_litellm_routes_to_dispatch_litellm(self):
        """--provider litellm (no sub-provider) also routes to _dispatch_litellm."""
        argv = ["--provider", "litellm", "--terminal-id", "T1",
                "--dispatch-id", "test-litellm-bare", "--instruction", "noop"]
        with patch("provider_dispatch._dispatch_litellm", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(argv)
        assert rc == 0
        mock_dispatch.assert_called_once()

    def test_various_litellm_sub_providers_route(self):
        """Various litellm:<sub> values all route to _dispatch_litellm."""
        for provider_str in ("litellm:kimi-k2", "litellm:glm-5.1", "litellm:bedrock"):
            argv = ["--provider", provider_str, "--terminal-id", "T1",
                    "--dispatch-id", "test-litellm-sub", "--instruction", "noop"]
            with patch("provider_dispatch._dispatch_litellm", return_value=0) as mock_dispatch:
                rc = provider_dispatch.main(argv)
            assert rc == 0, f"Expected 0 for {provider_str}"
            mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Test: registry resolution failure raises RuntimeError (codex R1 fix)
# ---------------------------------------------------------------------------

class TestRegistryResolutionFailure:

    def test_provider_dispatch_raises_on_registry_failure(self):
        """_resolve_deepseek_model() raises RuntimeError when registry load fails."""
        with patch("providers.provider_registry.get_default_model",
                   side_effect=FileNotFoundError("wave7_models.yaml missing")):
            with pytest.raises(RuntimeError, match="registry resolution failed"):
                provider_dispatch._resolve_deepseek_model()

    def test_provider_dispatch_raises_on_malformed_registry(self):
        """_resolve_deepseek_model() raises RuntimeError when registry is malformed yaml."""
        with patch("providers.provider_registry.get_default_model",
                   side_effect=ValueError("malformed wave7_models.yaml: mapping error")):
            with pytest.raises(RuntimeError, match="registry resolution failed"):
                provider_dispatch._resolve_deepseek_model()


# ---------------------------------------------------------------------------
# Test: unknown provider triggers argparse error (exit code 2)
# ---------------------------------------------------------------------------

class TestProviderUnknownArgparseError:

    def test_exit_code_2(self):
        argv = ["--provider", "foo", "--terminal-id", "T1",
                "--dispatch-id", "test-unknown", "--instruction", "noop"]
        with pytest.raises(SystemExit) as exc_info:
            provider_dispatch.main(argv)
        assert exc_info.value.code == 2

    def test_error_message_mentions_provider(self, capsys):
        argv = ["--provider", "foo", "--terminal-id", "T1",
                "--dispatch-id", "test-unknown", "--instruction", "noop"]
        with pytest.raises(SystemExit):
            provider_dispatch.main(argv)
        captured = capsys.readouterr()
        assert "foo" in captured.err


# ---------------------------------------------------------------------------
# Test: importability
# ---------------------------------------------------------------------------

class TestModuleImportability:

    def test_import_does_not_error(self):
        import importlib
        mod = importlib.import_module("provider_dispatch")
        assert mod.__name__ == "provider_dispatch"

    def test_no_optional_provider_imports_at_module_load(self):
        """provider_dispatch itself must not trigger spawn module imports at load time."""
        import importlib
        import sys

        # Remove spawn modules and provider_dispatch from sys.modules so we can
        # observe what loading provider_dispatch alone pulls in.
        to_remove = [
            k for k in sys.modules
            if any(s in k for s in ("codex_spawn", "gemini_spawn", "litellm_spawn", "provider_dispatch"))
        ]
        for k in to_remove:
            del sys.modules[k]

        importlib.import_module("provider_dispatch")

        # None of the optional spawn modules should have been imported as side effects.
        for mod_name in sys.modules:
            assert "codex_spawn" not in mod_name, f"codex_spawn imported at module load: {mod_name}"
            assert "gemini_spawn" not in mod_name, f"gemini_spawn imported at module load: {mod_name}"
            assert "litellm_spawn" not in mod_name, f"litellm_spawn imported at module load: {mod_name}"
