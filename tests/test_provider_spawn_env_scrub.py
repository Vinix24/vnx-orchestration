#!/usr/bin/env python3
"""test_provider_spawn_env_scrub.py — OI-1619 regression.

kimi_spawn.py, codex_spawn.py, and gemini_spawn.py each Popen a real worker CLI
directly (no SubprocessAdapter to route through — that's the claude lane's job,
covered separately by test_spawn_scrub_env_keys_contract.py). All three built their
child env as ``{**os.environ, **(extra_env or {})}`` with ZERO scrubbing: any secret
present in the parent process (``VNX_SMTP_PASS`` measured present in-process, same
finding class as the claude-lane gap fixed 2026-09-03) crossed straight into the
kimi/codex/gemini subprocess. Unlike litellm_spawn.py (which already had its own,
narrower ``_scrubbed_env()`` — ANTHROPIC_API_KEY + CLAUDE_CODE_OAUTH_TOKEN only), these
three had no scrub call of any kind.

The fix: ``env_scrub_patterns.scrub_env()`` (a small fnmatch-based free function
mirroring SubprocessAdapter.deliver()'s inline scrub loop, for callers that Popen
directly and have no SubprocessAdapter to route through) applied with
``DEFAULT_SCRUB_KEY_PATTERNS`` at the one place each of the three files builds its
Popen env.

Each test below drives the real top-level spawn_*() entry point end-to-end (not an
internal helper) with ``subprocess.Popen`` faked to capture the ``env=`` kwarg and then
raise ``FileNotFoundError`` — the same "binary not found" path every one of these three
functions already handles as a clean, immediate return (returncode=127), so the test
never needs to fake the streaming/drain machinery downstream. This proves the actual
GEDRAG (a secret demonstrably not reaching the child env), not a string or line number.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns import codex_spawn as cs  # noqa: E402
from provider_spawns import gemini_spawn as gs  # noqa: E402
from provider_spawns import kimi_spawn as ks  # noqa: E402

_FAKE_SMTP_PASS = "fake-test-smtp-secret-not-real"
_FAKE_WORKER_TOKEN = "fake-test-worker-token-not-real"


def _leaked_secrets_env() -> dict:
    """A fabricated (never-real) parent env carrying two secret shapes:
    an exact literal name (``VNX_SMTP_PASS``, listed explicitly in
    DEFAULT_SCRUB_KEY_PATTERNS) and a glob-matched name (``*_TOKEN``). ``PATH`` is a
    benign var that must survive the scrub — an over-eager scrub is as much a defect
    as an absent one.
    """
    return {
        "VNX_SMTP_PASS": _FAKE_SMTP_PASS,
        "SOME_SERVICE_TOKEN": _FAKE_WORKER_TOKEN,
        "PATH": "/usr/bin",
    }


def _capture_env_via_not_found_popen(captured: dict):
    """A fake Popen that records the env= kwarg then raises FileNotFoundError —
    the "binary not found" path every one of the three spawn functions already
    handles as an immediate, clean return, so the caller never touches the
    streaming/drain machinery that follows a real Popen success.
    """
    def _fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        raise FileNotFoundError("fake binary intentionally absent for this test")
    return _fake_popen


class TestKimiSpawnEnvScrub:
    def test_leaked_secrets_absent_from_final_popen_env(self):
        captured: dict = {}
        with patch.dict("os.environ", _leaked_secrets_env(), clear=True), \
                patch("provider_spawns.kimi_spawn.subprocess.Popen", _capture_env_via_not_found_popen(captured)):
            result = ks.spawn_kimi(prompt="test", dispatch_id="d-scrub-kimi", terminal_id="T1")

        assert result.returncode == 127, "expected the fake-missing-binary path"
        env = captured.get("env")
        assert env is not None, "Popen must receive an explicit env dict"
        assert "VNX_SMTP_PASS" not in env
        assert "SOME_SERVICE_TOKEN" not in env
        assert env.get("PATH") == "/usr/bin"


class TestCodexSpawnEnvScrub:
    def test_leaked_secrets_absent_from_final_popen_env(self):
        captured: dict = {}
        with patch.dict("os.environ", _leaked_secrets_env(), clear=True), \
                patch("provider_spawns.codex_spawn.subprocess.Popen", _capture_env_via_not_found_popen(captured)):
            result = cs.spawn_codex(
                prompt="test", model=None, dispatch_id="d-scrub-codex", terminal_id="T1",
            )

        assert result.returncode == 127, "expected the fake-missing-binary path"
        env = captured.get("env")
        assert env is not None, "Popen must receive an explicit env dict"
        assert "VNX_SMTP_PASS" not in env
        assert "SOME_SERVICE_TOKEN" not in env
        assert env.get("PATH") == "/usr/bin"


class TestGeminiSpawnEnvScrub:
    def test_leaked_secrets_absent_from_final_popen_env(self):
        captured: dict = {}
        with patch.dict("os.environ", _leaked_secrets_env(), clear=True), \
                patch("provider_spawns.gemini_spawn.subprocess.Popen", _capture_env_via_not_found_popen(captured)):
            result = gs.spawn_gemini(
                prompt="test", model="gemini-2.5-pro", dispatch_id="d-scrub-gemini", terminal_id="T1",
            )

        assert result.returncode == 127, "expected the fake-missing-binary path"
        env = captured.get("env")
        assert env is not None, "Popen must receive an explicit env dict"
        assert "VNX_SMTP_PASS" not in env
        assert "SOME_SERVICE_TOKEN" not in env
        assert env.get("PATH") == "/usr/bin"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
