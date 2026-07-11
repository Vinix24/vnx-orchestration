#!/usr/bin/env python3
"""test_deepseek_harness_spawn.py — Governed DeepSeek-harness lane.

Verifies the account-safety contract of spawn_deepseek_harness():

  - env: ANTHROPIC_BASE_URL points to api.deepseek.com/anthropic
  - env: ANTHROPIC_AUTH_TOKEN is the DeepSeek key (key-auth, not OAuth)
  - env: CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
  - argv: --strict-mcp-config --mcp-config '{"mcpServers":{}}' (MCP fully off)
  - model: defaults to deepseek-v4-pro; honours arg + VNX_DEEPSEEK_HARNESS_MODEL
  - frontmatter attributes provider=deepseek-harness, sub_provider=deepseek
  - FAIL-CLOSED: missing DEEPSEEK_API_KEY -> no spawn, returncode 1
  - caller extra_env cannot override the mandatory account-safety env

The transport (SubprocessAdapter) is mocked — these are argv/env contract tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns.claude_spawn import ClaudeSpawnResult  # noqa: E402
from provider_spawns import deepseek_harness_spawn as dh  # noqa: E402
from provider_spawns.deepseek_harness_spawn import (  # noqa: E402
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEFAULT_DEEPSEEK_HARNESS_MODEL,
    DeepSeekHarnessSpawnResult,
    build_harness_cli_args,
    build_harness_env,
    resolve_harness_model,
    spawn_deepseek_harness,
)

_FAKE_KEY = "sk-deepseek-test-key-1234567890abcd"


def _fake_claude_result(**overrides) -> ClaudeSpawnResult:
    base = dict(
        returncode=0,
        completion={"text": "OK", "subtype": "success"},
        events_written=3,
        session_id="sess-xyz",
        timed_out=False,
        token_usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
    )
    base.update(overrides)
    return ClaudeSpawnResult(**base)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestHarnessHelpers:
    def test_base_url_targets_deepseek_anthropic_endpoint(self):
        assert DEEPSEEK_ANTHROPIC_BASE_URL == "https://api.deepseek.com/anthropic"

    def test_build_harness_env_is_key_auth(self):
        env = build_harness_env(_FAKE_KEY)
        assert env["ANTHROPIC_BASE_URL"] == DEEPSEEK_ANTHROPIC_BASE_URL
        assert env["ANTHROPIC_AUTH_TOKEN"] == _FAKE_KEY
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        # Must NOT smuggle in an anthropic api key or OAuth marker.
        assert "ANTHROPIC_API_KEY" not in env

    def test_harness_scrub_keys_cover_both_credential_forms(self):
        """Audit S3: the scrub set must cover both the API key AND the OAuth session
        token — an inherited CLAUDE_CODE_OAUTH_TOKEN would otherwise sit next to the
        key-auth ANTHROPIC_AUTH_TOKEN override and reintroduce auth ambiguity."""
        assert dh._HARNESS_SCRUB_KEYS == frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})

    def test_mcp_fully_off_cli_args(self):
        # --mcp-config is variadic; JSON value first, boolean terminator last so
        # the positional prompt is not slurped into the config list.
        args = build_harness_cli_args()
        assert args == ["--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config"]
        assert args[-1] == "--strict-mcp-config", "boolean must terminate the variadic"

    def test_default_model_is_v4_pro(self):
        assert DEFAULT_DEEPSEEK_HARNESS_MODEL == "deepseek-v4-pro"

    def test_resolve_model_prefers_explicit(self):
        assert resolve_harness_model("deepseek-v4-flash") == "deepseek-v4-flash"

    def test_resolve_model_falls_back_to_default(self):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("VNX_DEEPSEEK_HARNESS_MODEL", None)
            assert resolve_harness_model(None) == "deepseek-v4-pro"
            assert resolve_harness_model("") == "deepseek-v4-pro"
            assert resolve_harness_model("   ") == "deepseek-v4-pro"

    def test_resolve_model_env_override(self):
        with patch.dict("os.environ", {"VNX_DEEPSEEK_HARNESS_MODEL": "deepseek-v4-flash"}):
            assert resolve_harness_model(None) == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# spawn argv/env contract (transport mocked at spawn_claude boundary)
# ---------------------------------------------------------------------------

class TestSpawnContract:
    def test_passes_keyauth_env_and_mcp_off_to_spawn_claude(self):
        captured = {}

        def _fake_spawn_claude(**kwargs):
            captured.update(kwargs)
            return _fake_claude_result()

        with patch.object(dh, "spawn_claude", _fake_spawn_claude):
            result = spawn_deepseek_harness(
                prompt="Reply OK.",
                model=None,
                dispatch_id="d-1",
                terminal_id="T1",
                api_key=_FAKE_KEY,
            )

        env = captured["extra_env"]
        assert env["ANTHROPIC_BASE_URL"] == DEEPSEEK_ANTHROPIC_BASE_URL
        assert env["ANTHROPIC_AUTH_TOKEN"] == _FAKE_KEY
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert captured["extra_cli_args"] == [
            "--mcp-config",
            '{"mcpServers":{}}',
            "--strict-mcp-config",
        ]
        assert captured["model"] == "deepseek-v4-pro"
        assert result.returncode == 0

    def test_explicit_model_forwarded(self):
        captured = {}

        def _fake_spawn_claude(**kwargs):
            captured.update(kwargs)
            return _fake_claude_result()

        with patch.object(dh, "spawn_claude", _fake_spawn_claude):
            spawn_deepseek_harness(
                prompt="x", model="deepseek-v4-flash",
                dispatch_id="d-1", terminal_id="T1", api_key=_FAKE_KEY,
            )
        assert captured["model"] == "deepseek-v4-flash"

    def test_api_key_resolved_from_env(self):
        captured = {}

        def _fake_spawn_claude(**kwargs):
            captured.update(kwargs)
            return _fake_claude_result()

        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": _FAKE_KEY}), \
                patch.object(dh, "spawn_claude", _fake_spawn_claude):
            spawn_deepseek_harness(
                prompt="x", model=None, dispatch_id="d-1", terminal_id="T1",
            )
        assert captured["extra_env"]["ANTHROPIC_AUTH_TOKEN"] == _FAKE_KEY

    def test_caller_extra_env_cannot_override_account_safety(self):
        """Mandatory harness env wins over caller-supplied extra_env."""
        captured = {}

        def _fake_spawn_claude(**kwargs):
            captured.update(kwargs)
            return _fake_claude_result()

        with patch.object(dh, "spawn_claude", _fake_spawn_claude):
            spawn_deepseek_harness(
                prompt="x", model=None, dispatch_id="d-1", terminal_id="T1",
                api_key=_FAKE_KEY,
                extra_env={
                    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                    "ANTHROPIC_AUTH_TOKEN": "oauth-token-should-be-ignored",
                    "VNX_OPERATOR_ID": "op-1",  # benign identity var preserved
                },
            )
        env = captured["extra_env"]
        assert env["ANTHROPIC_BASE_URL"] == DEEPSEEK_ANTHROPIC_BASE_URL
        assert env["ANTHROPIC_AUTH_TOKEN"] == _FAKE_KEY
        assert env["VNX_OPERATOR_ID"] == "op-1"

    def test_frontmatter_attributes_deepseek_harness(self):
        with patch.object(dh, "spawn_claude", lambda **k: _fake_claude_result()):
            result = spawn_deepseek_harness(
                prompt="x", model=None, dispatch_id="d-1", terminal_id="T1",
                api_key=_FAKE_KEY,
            )
        fm = result.frontmatter_fields()
        assert fm["provider"] == "deepseek-harness"
        assert fm["sub_provider"] == "deepseek"
        assert fm["token_usage"]["input"] == 10
        assert fm["token_usage"]["output"] == 5
        assert fm["token_usage"]["cache_read"] == 2

    def test_completion_text_surfaces_agent_response(self):
        """completion_text must expose the result-event text for the report body."""
        with patch.object(
            dh, "spawn_claude",
            lambda **k: _fake_claude_result(
                completion={"text": "VERDICT: APPROVE", "subtype": "success"}
            ),
        ):
            result = spawn_deepseek_harness(
                prompt="x", model=None, dispatch_id="d-1", terminal_id="T1",
                api_key=_FAKE_KEY,
            )
        assert result.completion_text == "VERDICT: APPROVE"

    def test_completion_text_empty_when_no_result_event(self):
        with patch.object(dh, "spawn_claude", lambda **k: _fake_claude_result(completion={})):
            result = spawn_deepseek_harness(
                prompt="x", model=None, dispatch_id="d-1", terminal_id="T1",
                api_key=_FAKE_KEY,
            )
        assert result.completion_text == ""

    def test_returns_harness_result_type(self):
        with patch.object(dh, "spawn_claude", lambda **k: _fake_claude_result()):
            result = spawn_deepseek_harness(
                prompt="x", model=None, dispatch_id="d-1", terminal_id="T1",
                api_key=_FAKE_KEY,
            )
        assert isinstance(result, DeepSeekHarnessSpawnResult)
        assert result.model == "deepseek-v4-pro"


# ---------------------------------------------------------------------------
# FAIL-CLOSED: no own key => never spawn (account safety)
# ---------------------------------------------------------------------------

class TestFailClosedNoKey:
    def test_missing_key_does_not_spawn(self):
        spawn_called = {"count": 0}

        def _fake_spawn_claude(**kwargs):
            spawn_called["count"] += 1
            return _fake_claude_result()

        with patch.dict("os.environ", {}, clear=False), \
                patch.object(dh, "spawn_claude", _fake_spawn_claude):
            import os
            os.environ.pop("DEEPSEEK_API_KEY", None)
            result = spawn_deepseek_harness(
                prompt="x", model=None, dispatch_id="d-1", terminal_id="T1",
                api_key=None,
            )

        assert spawn_called["count"] == 0, "must NOT spawn claude without an own key"
        assert result.returncode == 1
        assert result.error is not None
        assert "DEEPSEEK_API_KEY" in result.error

    def test_empty_key_does_not_spawn(self):
        spawn_called = {"count": 0}

        def _fake_spawn_claude(**kwargs):
            spawn_called["count"] += 1
            return _fake_claude_result()

        with patch.object(dh, "spawn_claude", _fake_spawn_claude):
            result = spawn_deepseek_harness(
                prompt="x", model=None, dispatch_id="d-1", terminal_id="T1",
                api_key="   ",
            )
        assert spawn_called["count"] == 0
        assert result.returncode == 1


# ---------------------------------------------------------------------------
# Fix 2 regression: ANTHROPIC_API_KEY must not reach the final Popen env
# ---------------------------------------------------------------------------

class TestFinalPopenEnvScrub:
    """Verify ANTHROPIC_API_KEY is actively deleted from the child env.

    Mocks subprocess.Popen at the subprocess_adapter layer (not spawn_claude)
    so the captured env is the FINAL merged dict that reaches the OS process.
    Also mocks read_events_with_timeout to avoid trying to read from the fake
    process stdout.
    """

    def test_anthropic_api_key_absent_from_final_popen_env(self):
        """ANTHROPIC_API_KEY must be absent even when present in os.environ."""
        import subprocess_adapter as sa

        captured = {}

        class _FakeProc:
            pid = 9999
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            proc = _FakeProc()
            # Provide real pipe fds so Popen tracking code doesn't crash.
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            return proc

        def _empty_events(self_adapter, terminal_id, **kw):
            return iter([])

        with patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "sk-ant-real-production-key", "DEEPSEEK_API_KEY": _FAKE_KEY},
        ), patch.object(sa.subprocess, "Popen", _fake_popen), \
                patch("subprocess_adapter.os.setsid", lambda: None), \
                patch.object(sa.SubprocessAdapter, "read_events_with_timeout", _empty_events):
            spawn_deepseek_harness(
                prompt="Reply OK.",
                model=None,
                dispatch_id="d-scrub-test",
                terminal_id="T1",
                api_key=_FAKE_KEY,
            )

        env = captured.get("env")
        assert env is not None, "Popen must receive an explicit env dict (not None)"
        assert "ANTHROPIC_API_KEY" not in env, (
            "ANTHROPIC_API_KEY must be scrubbed from the final Popen env "
            "(present in os.environ but must not reach the DeepSeek child process)"
        )
        assert env.get("ANTHROPIC_AUTH_TOKEN") == _FAKE_KEY, (
            "ANTHROPIC_AUTH_TOKEN (DeepSeek own key) must be present"
        )
        assert env.get("ANTHROPIC_BASE_URL") == DEEPSEEK_ANTHROPIC_BASE_URL

    def test_claude_code_oauth_token_absent_from_final_popen_env(self):
        """Audit S3: a cached CLAUDE_CODE_OAUTH_TOKEN must not survive into the
        redirected CLI either — only ANTHROPIC_API_KEY was scrubbed before this fix."""
        import subprocess_adapter as sa

        captured = {}

        class _FakeProc:
            pid = 9999
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            proc = _FakeProc()
            proc.stdout = MagicMock()
            proc.stderr = MagicMock()
            return proc

        def _empty_events(self_adapter, terminal_id, **kw):
            return iter([])

        with patch.dict(
            "os.environ",
            {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-real-production-session-token", "DEEPSEEK_API_KEY": _FAKE_KEY},
        ), patch.object(sa.subprocess, "Popen", _fake_popen), \
                patch("subprocess_adapter.os.setsid", lambda: None), \
                patch.object(sa.SubprocessAdapter, "read_events_with_timeout", _empty_events):
            spawn_deepseek_harness(
                prompt="Reply OK.",
                model=None,
                dispatch_id="d-scrub-oauth-test",
                terminal_id="T1",
                api_key=_FAKE_KEY,
            )

        env = captured.get("env")
        assert env is not None
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env, (
            "CLAUDE_CODE_OAUTH_TOKEN must be scrubbed from the final Popen env "
            "(present in os.environ but must not reach the DeepSeek child process)"
        )
        assert env.get("ANTHROPIC_AUTH_TOKEN") == _FAKE_KEY


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
