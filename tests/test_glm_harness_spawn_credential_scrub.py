#!/usr/bin/env python3
"""test_glm_harness_spawn_credential_scrub.py — audit S3 regression.

glm_harness_spawn.py mirrors deepseek_harness_spawn.py's account-safety contract (claude
CLI redirected to a local litellm→OpenRouter proxy via key-auth env). It shared the same
gap: _HARNESS_SCRUB_KEYS scrubbed ANTHROPIC_API_KEY but not CLAUDE_CODE_OAUTH_TOKEN, so a
cached OAuth session token could survive into the redirected CLI. See
test_deepseek_harness_spawn.py for the sibling coverage.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns import glm_harness_spawn as gh  # noqa: E402
from provider_spawns.glm_harness_spawn import spawn_glm_harness  # noqa: E402


class TestHarnessScrubKeys:
    def test_scrub_keys_cover_both_credential_forms(self):
        assert gh._HARNESS_SCRUB_KEYS == frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})


class TestFinalPopenEnvScrub:
    """Verify both credential forms are absent from the final child env reaching the OS process."""

    def _run_spawn(self, env_overrides: dict) -> dict:
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

        with patch.dict("os.environ", env_overrides), \
                patch.object(gh, "_proxy_reachable", lambda url, timeout=3.0: True), \
                patch.object(sa.subprocess, "Popen", _fake_popen), \
                patch("subprocess_adapter.os.setsid", lambda: None), \
                patch.object(sa.SubprocessAdapter, "read_events_with_timeout", _empty_events):
            spawn_glm_harness(
                prompt="Reply OK.",
                model=None,
                dispatch_id="d-glm-scrub-test",
                terminal_id="T1",
            )
        return captured.get("env")

    def test_anthropic_api_key_absent_from_final_popen_env(self):
        env = self._run_spawn({"ANTHROPIC_API_KEY": "sk-ant-real-production-key"})
        assert env is not None, "Popen must receive an explicit env dict"
        assert "ANTHROPIC_API_KEY" not in env
        assert env.get("ANTHROPIC_AUTH_TOKEN") == gh._proxy_key()

    def test_claude_code_oauth_token_absent_from_final_popen_env(self):
        env = self._run_spawn({"CLAUDE_CODE_OAUTH_TOKEN": "oauth-real-production-session-token"})
        assert env is not None, "Popen must receive an explicit env dict"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env.get("ANTHROPIC_AUTH_TOKEN") == gh._proxy_key()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
