#!/usr/bin/env python3
"""test_wave7_deepseek_smoke.py — Wave 7 PR-7.1 DeepSeek smoke tests.

Verifies DeepSeek V4 lane via LiteLLM bridge (no real API calls):

  test_provider_dispatch_routes_deepseek   — litellm:deepseek routes to
      spawn_litellm with model containing 'deepseek'
  test_deepseek_requires_api_key           — missing DEEPSEEK_API_KEY → non-zero exit
  test_runner_validates_deepseek_api_key   — _litellm_runner._validate_provider_key
      returns (False, ...) without key, (True, '') with key
  test_wave7_models_yaml_valid             — wave7_models.yaml parses and deepseek
      entry has correct schema
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns.litellm_spawn import LiteLLMSpawnResult


# ---------------------------------------------------------------------------
# Test 1: provider_dispatch routes litellm:deepseek to spawn_litellm
# ---------------------------------------------------------------------------

class TestProviderDispatchRoutesDeepSeek:
    """provider_dispatch.main routes --provider litellm:deepseek to spawn_litellm."""

    def test_provider_dispatch_routes_deepseek(self):
        mock_result = LiteLLMSpawnResult(
            returncode=0,
            completion_text="ok",
            events_written=2,
            session_id=None,
            timed_out=False,
            stopped_early=False,
            token_usage=None,
            error=None,
            event_writer_failures=0,
        )

        with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=mock_result) as mock_spawn:
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-key"}):
                from provider_dispatch import main
                rc = main([
                    "--provider", "litellm:deepseek",
                    "--terminal-id", "T1",
                    "--dispatch-id", "test-dispatch-deepseek",
                    "--instruction", "Reply with one word.",
                ])

        assert rc == 0, f"expected exit 0, got {rc}"
        assert mock_spawn.called, "spawn_litellm was not called"
        call_kwargs = mock_spawn.call_args
        model = (call_kwargs[1] if call_kwargs[1] else {}).get("model") or (
            call_kwargs[0][1] if len(call_kwargs[0]) > 1 else ""
        )
        assert "deepseek" in model, (
            f"expected model containing 'deepseek', got {model!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: missing DEEPSEEK_API_KEY returns non-zero exit code
# ---------------------------------------------------------------------------

class TestDeepSeekRequiresApiKey:
    """provider_dispatch exits non-zero when DEEPSEEK_API_KEY is not set."""

    def test_deepseek_requires_api_key(self):
        clean_env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}

        with patch.dict(os.environ, clean_env, clear=True):
            from provider_dispatch import main
            rc = main([
                "--provider", "litellm:deepseek",
                "--terminal-id", "T1",
                "--dispatch-id", "test-no-key",
                "--instruction", "test",
            ])

        assert rc != 0, "expected non-zero exit when DEEPSEEK_API_KEY is missing"


# ---------------------------------------------------------------------------
# Test 3: _litellm_runner._validate_provider_key validates DEEPSEEK_API_KEY
# ---------------------------------------------------------------------------

class TestRunnerValidatesDeepSeekApiKey:
    """_litellm_runner._validate_provider_key returns (False, msg) when key absent."""

    def _load_runner(self):
        from adapters import _litellm_runner as runner
        return runner

    def test_validate_returns_false_without_key(self):
        runner = self._load_runner()
        env_without_key = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            ok, msg = runner._validate_provider_key("deepseek/deepseek-v3.2")
        assert ok is False, f"expected ok=False, got ok={ok}"
        assert "DEEPSEEK_API_KEY" in msg, f"expected DEEPSEEK_API_KEY in msg, got: {msg!r}"

    def test_validate_returns_true_with_key(self):
        runner = self._load_runner()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-key"}):
            ok, msg = runner._validate_provider_key("deepseek/deepseek-v3.2")
        assert ok is True, f"expected ok=True, got ok={ok}"
        assert msg == "", f"expected empty msg, got: {msg!r}"

    def test_validate_passes_for_non_keyed_provider(self):
        runner = self._load_runner()
        clean_env = {k: v for k, v in os.environ.items() if k not in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY")}
        with patch.dict(os.environ, clean_env, clear=True):
            ok, msg = runner._validate_provider_key("anthropic/claude-sonnet-4-6")
        assert ok is True, f"anthropic provider should not require key check, got ok={ok}"


# ---------------------------------------------------------------------------
# Test 4: wave7_models.yaml parses with valid deepseek entry
# ---------------------------------------------------------------------------

class TestWave7ModelsYamlValid:
    """wave7_models.yaml contains a valid deepseek entry conforming to schema."""

    def test_wave7_models_yaml_valid(self):
        from providers import provider_registry

        registry = provider_registry.load()

        assert "deepseek" in registry, "deepseek provider missing from registry"

        deepseek = registry["deepseek"]
        assert deepseek.enabled is True, "deepseek.enabled must be True"
        assert deepseek.api_key_env == "DEEPSEEK_API_KEY", (
            f"unexpected api_key_env: {deepseek.api_key_env!r}"
        )
        assert deepseek.models, "deepseek.models must not be empty"
        assert "deepseek-v4-pro" in deepseek.models, "deepseek-v4-pro model entry missing"

        model = deepseek.models["deepseek-v4-pro"]
        assert model.litellm_name == "deepseek/deepseek-v3.2", (
            f"unexpected litellm_name: {model.litellm_name!r}"
        )
        assert model.cost_input_per_mtok == pytest.approx(0.28), (
            f"unexpected input cost: {model.cost_input_per_mtok}"
        )
        assert model.cost_output_per_mtok == pytest.approx(0.40), (
            f"unexpected output cost: {model.cost_output_per_mtok}"
        )
        assert model.supports_streaming is True
        assert model.supports_tool_calls is True
        assert "coding" in model.task_classes

    def test_inactive_providers_disabled(self):
        from providers import provider_registry

        registry = provider_registry.load()

        assert "moonshot" in registry, "moonshot provider missing from registry"
        assert registry["moonshot"].enabled is False, "moonshot must be disabled (PR-7.2)"

        assert "zai" in registry, "zai provider missing from registry"
        assert registry["zai"].enabled is False, "zai must be disabled (PR-7.3)"

    def test_get_default_model_returns_deepseek(self):
        from providers import provider_registry

        model = provider_registry.get_default_model("deepseek")
        assert model is not None, "get_default_model('deepseek') returned None"
        assert "deepseek" in model.litellm_name, (
            f"unexpected litellm_name from default: {model.litellm_name!r}"
        )

    def test_get_default_model_returns_none_for_disabled(self):
        from providers import provider_registry

        model = provider_registry.get_default_model("moonshot")
        assert model is None, (
            f"expected None for disabled moonshot provider, got {model!r}"
        )
