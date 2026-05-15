#!/usr/bin/env python3
"""test_wave7_kimi_smoke.py — Wave 7 PR-7.2 Kimi K2 smoke tests.

Verifies Kimi K2 lane via LiteLLM Moonshot endpoint (no real API calls):

  test_provider_dispatch_routes_moonshot  — litellm:moonshot routes to
      spawn_litellm with model containing 'moonshot'
  test_kimi_requires_api_key             — missing MOONSHOT_API_KEY → non-zero exit
  test_kimi_model_alias_resolution       — kimi-k2-0905-default resolves to
      moonshot/kimi-k2-0905-preview; kimi-k2-6 resolves to moonshot/kimi-k2.6
  test_wave7_models_yaml_moonshot_valid  — moonshot entry has valid schema + 2 models
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_spawns.litellm_spawn import LiteLLMSpawnResult


# ---------------------------------------------------------------------------
# Test 1: provider_dispatch routes litellm:moonshot to spawn_litellm
# ---------------------------------------------------------------------------

class TestProviderDispatchRoutesMoonshot:
    """provider_dispatch.main routes --provider litellm:moonshot to spawn_litellm."""

    def test_provider_dispatch_routes_moonshot(self):
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
            with patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-test-moonshot"}):
                from provider_dispatch import main
                rc = main([
                    "--provider", "litellm:moonshot",
                    "--terminal-id", "T1",
                    "--dispatch-id", "test-dispatch-moonshot",
                    "--instruction", "Reply with one word.",
                ])

        assert rc == 0, f"expected exit 0, got {rc}"
        assert mock_spawn.called, "spawn_litellm was not called"
        call_kwargs = mock_spawn.call_args
        model = (call_kwargs[1] if call_kwargs[1] else {}).get("model") or (
            call_kwargs[0][1] if len(call_kwargs[0]) > 1 else ""
        )
        assert "moonshot" in model, (
            f"expected model containing 'moonshot', got {model!r}"
        )

    def test_provider_dispatch_routes_moonshot_premium_alias(self):
        mock_result = LiteLLMSpawnResult(
            returncode=0,
            completion_text="ok",
            events_written=1,
            session_id=None,
            timed_out=False,
            stopped_early=False,
            token_usage=None,
            error=None,
            event_writer_failures=0,
        )

        with patch("provider_spawns.litellm_spawn.spawn_litellm", return_value=mock_result) as mock_spawn:
            with patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-test-moonshot"}):
                from provider_dispatch import main
                rc = main([
                    "--provider", "litellm:moonshot:kimi-k2-6",
                    "--terminal-id", "T1",
                    "--dispatch-id", "test-dispatch-moonshot-k26",
                    "--instruction", "Reply with one word.",
                ])

        assert rc == 0, f"expected exit 0, got {rc}"
        assert mock_spawn.called, "spawn_litellm was not called"
        call_kwargs = mock_spawn.call_args
        model = (call_kwargs[1] if call_kwargs[1] else {}).get("model") or (
            call_kwargs[0][1] if len(call_kwargs[0]) > 1 else ""
        )
        assert "kimi-k2.6" in model, (
            f"expected model containing 'kimi-k2.6', got {model!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: missing MOONSHOT_API_KEY returns non-zero exit code
# ---------------------------------------------------------------------------

class TestKimiRequiresApiKey:
    """provider_dispatch exits non-zero when MOONSHOT_API_KEY is not set."""

    def test_kimi_requires_api_key(self):
        clean_env = {k: v for k, v in os.environ.items() if k != "MOONSHOT_API_KEY"}

        with patch.dict(os.environ, clean_env, clear=True):
            from provider_dispatch import main
            rc = main([
                "--provider", "litellm:moonshot",
                "--terminal-id", "T1",
                "--dispatch-id", "test-no-key",
                "--instruction", "test",
            ])

        assert rc != 0, "expected non-zero exit when MOONSHOT_API_KEY is missing"

    def test_kimi_runner_validates_moonshot_api_key(self):
        from adapters import _litellm_runner as runner

        env_without_key = {k: v for k, v in os.environ.items() if k != "MOONSHOT_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            ok, msg = runner._validate_provider_key("moonshot/kimi-k2-0905-preview")
        assert ok is False, f"expected ok=False without key, got ok={ok}"
        assert "MOONSHOT_API_KEY" in msg, f"expected MOONSHOT_API_KEY in msg, got: {msg!r}"

    def test_kimi_runner_passes_with_api_key(self):
        from adapters import _litellm_runner as runner

        with patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-test-key"}):
            ok, msg = runner._validate_provider_key("moonshot/kimi-k2.6")
        assert ok is True, f"expected ok=True with key, got ok={ok}"
        assert msg == "", f"expected empty msg, got: {msg!r}"


# ---------------------------------------------------------------------------
# Test 3: model alias resolution
# ---------------------------------------------------------------------------

class TestKimiModelAliasResolution:
    """_resolve_moonshot_model correctly maps aliases to litellm model strings."""

    def test_default_resolves_to_k2_0905_preview(self):
        from provider_dispatch import _resolve_moonshot_model
        model = _resolve_moonshot_model()
        assert model == "moonshot/kimi-k2-0905-preview", (
            f"expected moonshot/kimi-k2-0905-preview as default, got {model!r}"
        )

    def test_kimi_k2_0905_alias_resolves(self):
        from provider_dispatch import _resolve_moonshot_model
        model = _resolve_moonshot_model("kimi-k2-0905-default")
        assert model == "moonshot/kimi-k2-0905-preview", (
            f"kimi-k2-0905-default should resolve to moonshot/kimi-k2-0905-preview, got {model!r}"
        )

    def test_kimi_k2_6_alias_resolves(self):
        from provider_dispatch import _resolve_moonshot_model
        model = _resolve_moonshot_model("kimi-k2-6")
        assert model == "moonshot/kimi-k2.6", (
            f"kimi-k2-6 should resolve to moonshot/kimi-k2.6, got {model!r}"
        )

    def test_unknown_alias_falls_back_to_first_model(self):
        from provider_dispatch import _resolve_moonshot_model
        model = _resolve_moonshot_model("nonexistent-alias")
        # Falls back to first model in registry (kimi-k2-0905-default)
        assert model.startswith("moonshot/"), (
            f"unknown alias should fall back to a moonshot/ model, got {model!r}"
        )


# ---------------------------------------------------------------------------
# Test 4: wave7_models.yaml has valid moonshot entries
# ---------------------------------------------------------------------------

class TestWave7ModelsYamlMoonshotValid:
    """wave7_models.yaml contains a valid, enabled moonshot entry."""

    def test_wave7_models_yaml_moonshot_valid(self):
        from providers import provider_registry

        registry = provider_registry.load()

        assert "moonshot" in registry, "moonshot provider missing from registry"

        moonshot = registry["moonshot"]
        assert moonshot.enabled is True, "moonshot.enabled must be True after PR-7.2"
        assert moonshot.api_key_env == "MOONSHOT_API_KEY", (
            f"unexpected api_key_env: {moonshot.api_key_env!r}"
        )
        assert len(moonshot.models) == 2, (
            f"expected 2 moonshot models, got {len(moonshot.models)}"
        )

    def test_kimi_k2_0905_model_schema(self):
        from providers import provider_registry

        registry = provider_registry.load()
        model = registry["moonshot"].models["kimi-k2-0905-default"]

        assert model.litellm_name == "moonshot/kimi-k2-0905-preview", (
            f"unexpected litellm_name: {model.litellm_name!r}"
        )
        assert model.cost_input_per_mtok == pytest.approx(0.60)
        assert model.cost_output_per_mtok == pytest.approx(2.50)
        assert model.max_tokens == 8192
        assert model.supports_streaming is True
        assert model.supports_tool_calls is True
        assert "coding" in model.task_classes

    def test_kimi_k2_6_model_schema(self):
        from providers import provider_registry

        registry = provider_registry.load()
        model = registry["moonshot"].models["kimi-k2-6"]

        assert model.litellm_name == "moonshot/kimi-k2.6", (
            f"unexpected litellm_name: {model.litellm_name!r}"
        )
        assert model.cost_input_per_mtok == pytest.approx(0.95)
        assert model.cost_output_per_mtok == pytest.approx(4.00)
        assert model.max_tokens == 8192
        assert model.supports_streaming is True
        assert model.supports_tool_calls is True
        assert "coding-premium" in model.task_classes

    def test_get_default_model_returns_moonshot(self):
        from providers import provider_registry

        model = provider_registry.get_default_model("moonshot")
        assert model is not None, "get_default_model('moonshot') returned None"
        assert "moonshot" in model.litellm_name, (
            f"unexpected litellm_name from default: {model.litellm_name!r}"
        )
