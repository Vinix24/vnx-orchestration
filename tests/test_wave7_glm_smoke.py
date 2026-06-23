#!/usr/bin/env python3
"""test_wave7_glm_smoke.py — Wave 7 PR-7.3 GLM-5.1 smoke tests.

Verifies GLM-5.1 lane via LiteLLM OpenRouter endpoint (no real API calls):

  test_provider_dispatch_routes_zai_via_openrouter — litellm:zai routes to
      spawn_litellm with model containing 'openrouter'
  test_zai_requires_openrouter_key                 — missing OPENROUTER_API_KEY → non-zero exit
  test_glm45_legacy_rejected                       — --model glm-4.5 raises ValueError
  test_glm_model_alias_resolution                  — glm-5.1-default resolves to
      openrouter/z-ai/glm-5
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
# Test 1: provider_dispatch routes litellm:zai to spawn_litellm via OpenRouter
# ---------------------------------------------------------------------------

class TestProviderDispatchRoutesZaiViaOpenRouter:
    """provider_dispatch.main routes --provider litellm:zai to spawn_litellm with openrouter model."""

    def test_provider_dispatch_routes_zai_via_openrouter(self):
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
            # glm-via-harness-only now blocks plain litellm:zai in production (GLM must run via
            # glm-harness); this test exercises the plain-runner routing, so it opens the documented
            # benchmark/legacy override — the same one run_benchmark.py sets for its zai baselines.
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test-key",
                                         "VNX_OVERRIDE_GLM_VIA_HARNESS_ONLY": "1"}):
                from provider_dispatch import main
                rc = main([
                    "--provider", "litellm:zai",
                    "--terminal-id", "T1",
                    "--dispatch-id", "test-dispatch-zai",
                    "--instruction", "Reply with one word.",
                ])

        assert rc == 0, f"expected exit 0, got {rc}"
        assert mock_spawn.called, "spawn_litellm was not called"
        call_kwargs = mock_spawn.call_args
        model = (call_kwargs[1] if call_kwargs[1] else {}).get("model") or (
            call_kwargs[0][1] if len(call_kwargs[0]) > 1 else ""
        )
        assert "openrouter" in model, (
            f"expected model containing 'openrouter', got {model!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: missing OPENROUTER_API_KEY returns non-zero exit code
# ---------------------------------------------------------------------------

class TestZaiRequiresOpenRouterKey:
    """provider_dispatch exits non-zero when OPENROUTER_API_KEY is not set."""

    def test_zai_requires_openrouter_key(self):
        clean_env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}

        with patch.dict(os.environ, clean_env, clear=True):
            from provider_dispatch import main
            rc = main([
                "--provider", "litellm:zai",
                "--terminal-id", "T1",
                "--dispatch-id", "test-no-key",
                "--instruction", "test",
            ])

        assert rc != 0, "expected non-zero exit when OPENROUTER_API_KEY is missing"

    def test_zai_runner_validates_openrouter_api_key(self):
        from adapters import _litellm_runner as runner

        env_without_key = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            ok, msg = runner._validate_provider_key("openrouter/z-ai/glm-5")
        assert ok is False, f"expected ok=False without key, got ok={ok}"
        assert "OPENROUTER_API_KEY" in msg, f"expected OPENROUTER_API_KEY in msg, got: {msg!r}"

    def test_zai_runner_passes_with_api_key(self):
        from adapters import _litellm_runner as runner

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}):
            ok, msg = runner._validate_provider_key("openrouter/z-ai/glm-5")
        assert ok is True, f"expected ok=True with key, got ok={ok}"
        assert msg == "", f"expected empty msg, got: {msg!r}"


# ---------------------------------------------------------------------------
# Test 3: deprecated GLM-4.5/4.6 models are rejected
# ---------------------------------------------------------------------------

class TestGlmLegacyRejected:
    """Passing deprecated GLM model names is blocked by constraint enforcer."""

    def test_glm45_legacy_rejected(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}):
            from provider_dispatch import main
            rc = main([
                "--provider", "litellm:zai",
                "--model", "glm-4.5",
                "--terminal-id", "T1",
                "--dispatch-id", "test-legacy",
                "--instruction", "test",
            ])
        assert rc == 1, f"expected exit 1 (constraint blocks deprecated model), got {rc}"

    def test_glm46_legacy_rejected(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}):
            from provider_dispatch import main
            rc = main([
                "--provider", "litellm:zai",
                "--model", "glm-4.6",
                "--terminal-id", "T1",
                "--dispatch-id", "test-legacy-46",
                "--instruction", "test",
            ])
        assert rc == 1, f"expected exit 1 (constraint blocks deprecated model), got {rc}"

    def test_validate_zai_model_not_legacy_raises_for_deprecated(self):
        from provider_dispatch import _validate_zai_model_not_legacy

        with pytest.raises(ValueError, match="GLM-4.5/4.6 are LEGACY"):
            _validate_zai_model_not_legacy("glm-4.5")

    def test_validate_zai_model_not_legacy_passes_for_current(self):
        from provider_dispatch import _validate_zai_model_not_legacy

        _validate_zai_model_not_legacy("glm-5.1-default")
        _validate_zai_model_not_legacy("sonnet")
        _validate_zai_model_not_legacy("")


# ---------------------------------------------------------------------------
# Test 4: model alias resolution
# ---------------------------------------------------------------------------

class TestGlmModelAliasResolution:
    """_resolve_zai_model correctly maps aliases to litellm model strings."""

    def test_default_resolves_to_openrouter_glm5(self):
        from provider_dispatch import _resolve_zai_model
        model = _resolve_zai_model()
        assert model == "openrouter/z-ai/glm-5", (
            f"expected openrouter/z-ai/glm-5 as default, got {model!r}"
        )

    def test_glm51_default_alias_resolves(self):
        from provider_dispatch import _resolve_zai_model
        model = _resolve_zai_model("glm-5.1-default")
        assert model == "openrouter/z-ai/glm-5", (
            f"glm-5.1-default should resolve to openrouter/z-ai/glm-5, got {model!r}"
        )

    def test_unknown_alias_falls_back_to_first_model(self):
        from provider_dispatch import _resolve_zai_model
        model = _resolve_zai_model("nonexistent-alias")
        assert "openrouter" in model, (
            f"unknown alias should fall back to an openrouter model, got {model!r}"
        )

    def test_wave7_models_yaml_zai_valid(self):
        from providers import provider_registry

        registry = provider_registry.load()

        assert "zai" in registry, "zai provider missing from registry"
        zai = registry["zai"]
        assert zai.enabled is True, "zai.enabled must be True after PR-7.3"
        assert zai.api_key_env == "OPENROUTER_API_KEY", (
            f"unexpected api_key_env: {zai.api_key_env!r}"
        )
        assert len(zai.models) == 3, (
            # glm-5.1-default + glm-5.1 + glm-5.2 (grew from the original 1 as GLM versions landed)
            f"expected 3 zai models, got {len(zai.models)}"
        )

    def test_glm51_default_model_schema(self):
        from providers import provider_registry

        registry = provider_registry.load()
        model = registry["zai"].models["glm-5.1-default"]

        assert model.litellm_name == "openrouter/z-ai/glm-5", (
            f"unexpected litellm_name: {model.litellm_name!r}"
        )
        assert model.cost_input_per_mtok == pytest.approx(0.50)
        assert model.cost_output_per_mtok == pytest.approx(2.50)
        assert model.max_tokens == 8192
        assert model.supports_streaming is True
        assert model.supports_tool_calls is True
        assert "coding" in model.task_classes
        assert "review" in model.task_classes

    def test_get_default_model_returns_zai_model(self):
        from providers import provider_registry

        model = provider_registry.get_default_model("zai")
        assert model is not None, "get_default_model('zai') returned None after PR-7.3"
        assert "openrouter" in model.litellm_name, (
            f"expected openrouter in litellm_name, got {model.litellm_name!r}"
        )
