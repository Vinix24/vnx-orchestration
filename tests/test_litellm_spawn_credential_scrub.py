#!/usr/bin/env python3
"""test_litellm_spawn_credential_scrub.py — audit S1/S2 regression.

litellm_spawn.py drives external, non-Anthropic models (DeepSeek/OpenRouter/GLM) in a
subprocess the model itself can direct via the agentic runner's run_command tool
(shell=True, no allowlist — audit S1). Full os.environ inheritance would hand that
subprocess the Anthropic account's own credentials, readable/exfiltratable by an
external model. Covers both Popen sites:

  - _start_litellm_subprocess (the one-shot spawn_litellm() path)
  - spawn_litellm_agentic() (the agentic tool-use loop)

and the shared _scrubbed_env() helper both sites now use.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns import litellm_spawn as ls  # noqa: E402

_LEAKED_ANTHROPIC_KEY = "sk-ant-real-production-key"
_LEAKED_OAUTH_TOKEN = "oauth-real-production-session-token"


def _leaked_credentials_env() -> dict:
    return {
        "ANTHROPIC_API_KEY": _LEAKED_ANTHROPIC_KEY,
        "CLAUDE_CODE_OAUTH_TOKEN": _LEAKED_OAUTH_TOKEN,
        "PATH": "/usr/bin",  # a benign var must survive the scrub
    }


class TestScrubbedEnvHelper:
    def test_strips_both_credential_keys(self):
        with patch.dict("os.environ", _leaked_credentials_env(), clear=True):
            env = ls._scrubbed_env(None)
        assert "ANTHROPIC_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["PATH"] == "/usr/bin"

    def test_extra_env_cannot_reintroduce_credentials(self):
        """A caller-supplied extra_env carrying either key must still be scrubbed."""
        with patch.dict("os.environ", {}, clear=True):
            env = ls._scrubbed_env({
                "ANTHROPIC_API_KEY": _LEAKED_ANTHROPIC_KEY,
                "CLAUDE_CODE_OAUTH_TOKEN": _LEAKED_OAUTH_TOKEN,
                "DEEPSEEK_API_KEY": "sk-deepseek-own-key",
            })
        assert "ANTHROPIC_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["DEEPSEEK_API_KEY"] == "sk-deepseek-own-key"


class TestOneShotSpawnPopenEnv:
    """spawn_litellm()'s underlying Popen call must never receive Anthropic credentials."""

    def test_final_popen_env_excludes_anthropic_credentials(self):
        captured = {}

        class _FakeProc:
            returncode = 0
            stdin = MagicMock()
            stdout = MagicMock()
            stderr = MagicMock()

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        with patch.dict("os.environ", _leaked_credentials_env(), clear=True), \
                patch("provider_spawns.litellm_spawn.subprocess.Popen", _fake_popen):
            ls._start_litellm_subprocess(
                runner_path="/fake/runner.py",
                payload_json="{}",
                extra_env=None,
                cwd=None,
            )

        env = captured.get("env")
        assert env is not None, "Popen must receive an explicit env dict"
        assert "ANTHROPIC_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


class TestAgenticSpawnPopenEnv:
    """spawn_litellm_agentic()'s Popen call must never receive Anthropic credentials.

    This is the higher-stakes site: the spawned runner gives the external model a
    run_command tool that inherits this exact env (audit S1 + S2 combined).
    """

    def test_final_popen_env_excludes_anthropic_credentials(self):
        captured = {}

        class _FakeProc:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return b"", b""

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        def _fake_popen(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        with patch.dict("os.environ", _leaked_credentials_env(), clear=True), \
                patch("provider_spawns.litellm_spawn.subprocess.Popen", _fake_popen):
            ls.spawn_litellm_agentic(
                prompt="test",
                model="anthropic/claude-sonnet-4-6",
                dispatch_id="d-scrub-agentic",
                terminal_id="T1",
                cwd=".",
            )

        env = captured.get("env")
        assert env is not None, "Popen must receive an explicit env dict"
        assert "ANTHROPIC_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
