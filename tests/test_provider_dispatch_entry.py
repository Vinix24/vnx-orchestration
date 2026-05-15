#!/usr/bin/env python3
"""Wave 4.6 PR-4.6.1 — provider_dispatch.py entry-point tests.

Covers:
- Claude provider delegates to subprocess_dispatch with unchanged argv semantics.
- Codex/Gemini/LiteLLM providers raise SystemExit(64) with PR reference in message.
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
# Test: claude provider delegates to subprocess_dispatch
# ---------------------------------------------------------------------------

class TestProviderClaudeDelegatesToSubprocessDispatch:

    def test_delegates_on_success(self):
        """deliver_with_recovery is called and its return value gates exit code."""
        mock_deliver = MagicMock(return_value=True)
        mock_extract = MagicMock(return_value=None)

        with patch("subprocess_dispatch.deliver_with_recovery", mock_deliver), \
             patch("subprocess_dispatch._extract_role_from_instruction", mock_extract):
            result = provider_dispatch.main(_make_claude_argv())

        assert result == 0
        mock_deliver.assert_called_once()

    def test_delegates_core_kwargs(self):
        """terminal_id, dispatch_id, instruction forwarded to deliver_with_recovery."""
        mock_deliver = MagicMock(return_value=True)

        with patch("subprocess_dispatch.deliver_with_recovery", mock_deliver), \
             patch("subprocess_dispatch._extract_role_from_instruction", return_value=None):
            provider_dispatch.main(_make_claude_argv())

        call_kwargs = mock_deliver.call_args[1]
        assert call_kwargs["terminal_id"] == "T1"
        assert call_kwargs["dispatch_id"] == "test-pr461-smoke"
        assert call_kwargs["instruction"] == "noop"

    def test_failed_deliver_returns_exit_code_1(self):
        """When deliver_with_recovery returns False, main returns 1."""
        with patch("subprocess_dispatch.deliver_with_recovery", return_value=False), \
             patch("subprocess_dispatch._extract_role_from_instruction", return_value=None):
            result = provider_dispatch.main(_make_claude_argv())
        assert result == 1

    def test_no_auto_commit_forwarded(self):
        """--no-auto-commit flips auto_commit=False in deliver_with_recovery."""
        mock_deliver = MagicMock(return_value=True)
        argv = _make_claude_argv() + ["--no-auto-commit"]

        with patch("subprocess_dispatch.deliver_with_recovery", mock_deliver), \
             patch("subprocess_dispatch._extract_role_from_instruction", return_value=None):
            provider_dispatch.main(argv)

        assert mock_deliver.call_args[1]["auto_commit"] is False

    def test_role_extracted_from_instruction_when_absent(self):
        """When --role is omitted, role is extracted from instruction body."""
        mock_deliver = MagicMock(return_value=True)
        argv = [
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "test-role-extraction",
            "--instruction", "Role: security-engineer\n\nDo the thing.",
        ]

        with patch("subprocess_dispatch.deliver_with_recovery", mock_deliver):
            provider_dispatch.main(argv)

        assert mock_deliver.call_args[1]["role"] == "security-engineer"

    def test_explicit_role_not_overridden_by_instruction(self):
        """--role flag takes precedence over Role: header in instruction."""
        mock_deliver = MagicMock(return_value=True)
        argv = [
            "--provider", "claude",
            "--terminal-id", "T1",
            "--dispatch-id", "test-explicit-role",
            "--instruction", "Role: backend-developer\n\nTask.",
            "--role", "architect",
        ]

        with patch("subprocess_dispatch.deliver_with_recovery", mock_deliver), \
             patch("subprocess_dispatch._extract_role_from_instruction", return_value="backend-developer"):
            provider_dispatch.main(argv)

        assert mock_deliver.call_args[1]["role"] == "architect"

    def test_dispatch_paths_split_on_comma(self):
        """--dispatch-paths is split into a list for deliver_with_recovery."""
        mock_deliver = MagicMock(return_value=True)
        argv = _make_claude_argv(**{"--dispatch-paths": "scripts/lib,tests"})

        with patch("subprocess_dispatch.deliver_with_recovery", mock_deliver), \
             patch("subprocess_dispatch._extract_role_from_instruction", return_value=None):
            provider_dispatch.main(argv)

        assert mock_deliver.call_args[1]["dispatch_paths"] == ["scripts/lib", "tests"]

    def test_pr_id_forwarded(self):
        """--pr-id is forwarded to deliver_with_recovery."""
        mock_deliver = MagicMock(return_value=True)
        argv = _make_claude_argv() + ["--pr-id", "488"]

        with patch("subprocess_dispatch.deliver_with_recovery", mock_deliver), \
             patch("subprocess_dispatch._extract_role_from_instruction", return_value=None):
            provider_dispatch.main(argv)

        assert mock_deliver.call_args[1]["pr_id"] == "488"


# ---------------------------------------------------------------------------
# Test: codex provider is routed to spawn_codex (PR-4.6.3 implemented)
# ---------------------------------------------------------------------------

class TestProviderCodexRouted:

    def test_codex_routes_to_spawn_codex(self):
        """--provider codex calls spawn_codex and returns 0 on success."""
        from unittest.mock import MagicMock
        from provider_spawns.codex_spawn import CodexSpawnResult

        mock_result = CodexSpawnResult(
            returncode=0,
            completion_text="ok",
            events_written=1,
            session_id=None,
            timed_out=False,
        )

        argv = ["--provider", "codex", "--terminal-id", "T1",
                "--dispatch-id", "test-codex-routed", "--instruction", "noop"]

        with patch("provider_dispatch._dispatch_codex", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(argv)

        assert rc == 0
        mock_dispatch.assert_called_once()

    def test_codex_does_not_raise_system_exit_64(self):
        """--provider codex must no longer raise SystemExit(64)."""
        from provider_spawns.codex_spawn import CodexSpawnResult

        argv = ["--provider", "codex", "--terminal-id", "T1",
                "--dispatch-id", "test-codex-ok", "--instruction", "noop"]

        with patch("provider_dispatch._dispatch_codex", return_value=0):
            try:
                rc = provider_dispatch.main(argv)
            except SystemExit as exc:
                pytest.fail(f"provider codex raised SystemExit({exc.code}): should be routed now")


# ---------------------------------------------------------------------------
# Test: gemini provider is routed to spawn_gemini (PR-4.6.4 implemented)
# ---------------------------------------------------------------------------

class TestProviderGeminiRouted:

    def test_gemini_routes_to_dispatch_gemini(self):
        """--provider gemini calls _dispatch_gemini and returns 0 on success."""
        argv = ["--provider", "gemini", "--terminal-id", "T3",
                "--dispatch-id", "test-gemini-routed", "--instruction", "noop"]

        with patch("provider_dispatch._dispatch_gemini", return_value=0) as mock_dispatch:
            rc = provider_dispatch.main(argv)

        assert rc == 0
        mock_dispatch.assert_called_once()

    def test_gemini_does_not_raise_system_exit_64(self):
        """--provider gemini must no longer raise SystemExit(64)."""
        argv = ["--provider", "gemini", "--terminal-id", "T3",
                "--dispatch-id", "test-gemini-ok", "--instruction", "noop"]

        with patch("provider_dispatch._dispatch_gemini", return_value=0):
            try:
                rc = provider_dispatch.main(argv)
            except SystemExit as exc:
                pytest.fail(f"provider gemini raised SystemExit({exc.code}): should be routed now")


# ---------------------------------------------------------------------------
# Test: litellm:<model> raises SystemExit(64) mentioning PR-4.6.5
# ---------------------------------------------------------------------------

class TestProviderLitellmRaisesNotImplemented:

    def test_exit_code_64(self, capsys):
        argv = ["--provider", "litellm:deepseek-v4-pro", "--terminal-id", "T1",
                "--dispatch-id", "test-litellm", "--instruction", "noop"]
        with pytest.raises(SystemExit) as exc_info:
            provider_dispatch.main(argv)
        assert exc_info.value.code == 64

    def test_message_mentions_pr_4_6_5(self, capsys):
        argv = ["--provider", "litellm:deepseek-v4-pro", "--terminal-id", "T1",
                "--dispatch-id", "test-litellm", "--instruction", "noop"]
        with pytest.raises(SystemExit):
            provider_dispatch.main(argv)
        captured = capsys.readouterr()
        assert "PR-4.6.5" in captured.err

    def test_various_litellm_models(self, capsys):
        for model in ("litellm:kimi-k2", "litellm:glm-5.1", "litellm:groq:llama3"):
            argv = ["--provider", model, "--terminal-id", "T1",
                    "--dispatch-id", "test-litellm", "--instruction", "noop"]
            with pytest.raises(SystemExit) as exc_info:
                provider_dispatch.main(argv)
            assert exc_info.value.code == 64, f"Expected exit 64 for {model}"


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
        """litellm_spawn (not yet implemented) must not be imported at module load."""
        import sys
        # codex_spawn (PR-4.6.3) and gemini_spawn (PR-4.6.4) are now implemented.
        # litellm_spawn is a future PR and must not exist yet.
        for mod_name in list(sys.modules.keys()):
            assert "litellm_spawn" not in mod_name, f"litellm_spawn imported unexpectedly: {mod_name}"
