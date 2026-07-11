#!/usr/bin/env python3
"""test_provider_dispatch_openrouter_arbitrary.py — openrouter-arbitrary lane skeleton.

Track: openrouter-arbitrary (OpenRouter-arbitrary / OpenAI-compat proxy-gated lane
class). This PR is the minimal seam: --provider litellm:openrouter[:<vendor>/<model>]
routed through the existing litellm dispatch machinery, proving out ONE OpenAI-compat
model (openai/gpt-4o-mini via OpenRouter) end-to-end.

Covers:
- registration: openrouter is a fast-fail key-req'd, defaulted litellm sub-provider
- _resolve_openrouter_model: sentinel/absent alias -> the one proven model;
  any other alias is raw OpenRouter pass-through (the "arbitrary" part)
- _build_lane_key: default alias vs. arbitrary alias
- behavior contract: the one proven model has a registered contract (openai_tools);
  an arbitrary model proceeds uncontracted (no KeyError)
- _dispatch_litellm fast-fails (EX_USAGE) without OPENROUTER_API_KEY
- _dispatch_litellm resolves model + lane correctly for bare and aliased provider
  strings and passes them through to spawn_litellm
- constraint pre-flight: NOT registry-curated (arbitrary by design), via=openrouter
  clears cleanly, no blocking violation
- cost rate table resolves real pricing for the one proven model
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

import provider_dispatch as pd  # noqa: E402
from provider_dispatch import _build_lane_key, _resolve_openrouter_model  # noqa: E402
from providers.behavior_contracts import get_contract  # noqa: E402
from provider_spawns.litellm_spawn import LiteLLMSpawnResult  # noqa: E402

_FAKE_KEY = "sk-or-test-key-1234567890abcd"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_openrouter_requires_openrouter_api_key(self):
        assert pd._SUB_PROVIDER_KEY_REQS["openrouter"] == "OPENROUTER_API_KEY"

    def test_openrouter_has_a_default_model(self):
        assert pd._LITELLM_SUB_PROVIDER_DEFAULTS["openrouter"] == "openrouter/openai/gpt-4o-mini"

    def test_openrouter_default_alias_is_registered(self):
        assert pd._SUB_PROVIDER_DEFAULT_ALIAS["openrouter"] == "gpt-4o-mini-default"


# ---------------------------------------------------------------------------
# _resolve_openrouter_model — arbitrary pass-through
# ---------------------------------------------------------------------------

class TestResolveOpenrouterModel:
    def test_no_alias_resolves_to_proven_default(self):
        assert _resolve_openrouter_model(None) == "openrouter/openai/gpt-4o-mini"

    def test_blank_alias_resolves_to_proven_default(self):
        assert _resolve_openrouter_model("  ") == "openrouter/openai/gpt-4o-mini"

    def test_sentinel_default_alias_resolves_to_proven_default(self):
        assert _resolve_openrouter_model("gpt-4o-mini-default") == "openrouter/openai/gpt-4o-mini"

    def test_arbitrary_alias_is_prefixed_with_openrouter(self):
        assert (
            _resolve_openrouter_model("anthropic/claude-3-haiku")
            == "openrouter/anthropic/claude-3-haiku"
        )

    def test_already_prefixed_alias_is_not_double_prefixed(self):
        assert (
            _resolve_openrouter_model("openrouter/anthropic/claude-3-haiku")
            == "openrouter/anthropic/claude-3-haiku"
        )


# ---------------------------------------------------------------------------
# _build_lane_key
# ---------------------------------------------------------------------------

class TestBuildLaneKey:
    def test_default_alias(self):
        assert _build_lane_key("openrouter", None) == "litellm:openrouter:gpt-4o-mini-default"

    def test_arbitrary_alias(self):
        assert (
            _build_lane_key("openrouter", "anthropic/claude-3-haiku")
            == "litellm:openrouter:anthropic/claude-3-haiku"
        )


# ---------------------------------------------------------------------------
# Behavior contract: proven model is contracted, arbitrary models are not
# ---------------------------------------------------------------------------

class TestBehaviorContract:
    def test_proven_default_has_openai_tools_contract(self):
        contract = get_contract("litellm:openrouter:gpt-4o-mini-default")
        assert contract.tool_call_shape == "openai_tools"
        assert contract.provider == "litellm"
        assert contract.sub_provider == "openrouter"

    def test_arbitrary_model_has_no_contract(self):
        with pytest.raises(KeyError):
            get_contract("litellm:openrouter:some/unregistered-model")


# ---------------------------------------------------------------------------
# Constraint pre-flight
# ---------------------------------------------------------------------------

class TestConstraintPreflight:
    def _args(self, provider="litellm:openrouter", model="sonnet"):
        ns = MagicMock()
        ns.provider = provider
        ns.model = model
        ns.terminal_id = "T1"
        ns.role = "backend-developer"
        return ns

    def test_registry_check_disabled_bare(self):
        assert pd._constraint_registry_check_enabled(self._args(), "litellm:openrouter") is False

    def test_registry_check_disabled_with_alias(self):
        provider = "litellm:openrouter:anthropic/claude-3-haiku"
        assert pd._constraint_registry_check_enabled(self._args(provider), provider) is False

    def test_constraint_model_bare_resolves_to_default(self):
        model = pd._constraint_model_for_provider(self._args(), "litellm:openrouter")
        assert model == "openrouter/openai/gpt-4o-mini"

    def test_constraint_model_with_alias_returns_alias(self):
        provider = "litellm:openrouter:anthropic/claude-3-haiku"
        model = pd._constraint_model_for_provider(self._args(provider), provider)
        assert model == "anthropic/claude-3-haiku"

    def test_check_constraints_does_not_raise_for_openrouter(self):
        """The lane is arbitrary-by-design — no forbid_route entry should block it."""
        args = self._args()
        args.instruction = "Reply OK."
        args.dispatch_id = "test-openrouter-preflight"
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": _FAKE_KEY}):
            violations = pd._check_constraints(args, "litellm:openrouter")
        blocking = [v for v in violations if v.severity == "blocking"]
        assert blocking == []


# ---------------------------------------------------------------------------
# _dispatch_litellm — fast-fail + routing
# ---------------------------------------------------------------------------

class TestDispatchLitellmFastFail:
    def test_missing_openrouter_key_returns_ex_usage(self):
        argv = [
            "--provider", "litellm:openrouter",
            "--terminal-id", "T1",
            "--dispatch-id", "test-openrouter-nokey",
            "--instruction", "echo hello",
        ]
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            rc = pd.main(argv)
        assert rc == pd._EX_USAGE


class TestDispatchLitellmRouting:
    def test_bare_provider_resolves_default_model_and_lane(self):
        captured_kwargs: dict = {}

        def fake_spawn(**kwargs):
            captured_kwargs.update(kwargs)
            return LiteLLMSpawnResult(
                returncode=0, completion_text="ok", events_written=1,
                session_id=None, timed_out=False,
            )

        argv = [
            "--provider", "litellm:openrouter",
            "--terminal-id", "T1",
            "--dispatch-id", "test-openrouter-bare",
            "--instruction", "echo hello",
        ]

        with patch("provider_spawns.litellm_spawn.spawn_litellm", fake_spawn), \
                patch.dict("os.environ", {"OPENROUTER_API_KEY": _FAKE_KEY}):
            rc = pd.main(argv)

        assert rc == 0
        assert captured_kwargs["model"] == "openrouter/openai/gpt-4o-mini"
        assert captured_kwargs["lane"] == "litellm:openrouter:gpt-4o-mini-default"
        assert captured_kwargs["tool_call_shape"] == "openai_tools"
        assert captured_kwargs["sub_provider"] == "openrouter"

    def test_arbitrary_alias_resolves_raw_passthrough_model(self):
        """Proves the 'arbitrary' part: any OpenRouter model path can be dispatched
        without a code change, at the cost of running uncontracted (no KeyError)."""
        captured_kwargs: dict = {}

        def fake_spawn(**kwargs):
            captured_kwargs.update(kwargs)
            return LiteLLMSpawnResult(
                returncode=0, completion_text="ok", events_written=1,
                session_id=None, timed_out=False,
            )

        argv = [
            "--provider", "litellm:openrouter:anthropic/claude-3-haiku",
            "--terminal-id", "T1",
            "--dispatch-id", "test-openrouter-arbitrary",
            "--instruction", "echo hello",
        ]

        with patch("provider_spawns.litellm_spawn.spawn_litellm", fake_spawn), \
                patch.dict("os.environ", {"OPENROUTER_API_KEY": _FAKE_KEY}):
            rc = pd.main(argv)

        assert rc == 0
        assert captured_kwargs["model"] == "openrouter/anthropic/claude-3-haiku"
        assert captured_kwargs["lane"] == "litellm:openrouter:anthropic/claude-3-haiku"
        assert captured_kwargs["tool_call_shape"] is None  # uncontracted, not an error

    def test_env_model_override_wins_over_alias(self):
        captured_kwargs: dict = {}

        def fake_spawn(**kwargs):
            captured_kwargs.update(kwargs)
            return LiteLLMSpawnResult(
                returncode=0, completion_text="ok", events_written=1,
                session_id=None, timed_out=False,
            )

        argv = [
            "--provider", "litellm:openrouter:anthropic/claude-3-haiku",
            "--terminal-id", "T1",
            "--dispatch-id", "test-openrouter-envoverride",
            "--instruction", "echo hello",
        ]

        with patch("provider_spawns.litellm_spawn.spawn_litellm", fake_spawn), \
                patch.dict("os.environ", {
                    "OPENROUTER_API_KEY": _FAKE_KEY,
                    "VNX_LITELLM_MODEL": "openrouter/google/gemini-2.5-flash",
                }):
            rc = pd.main(argv)

        assert rc == 0
        assert captured_kwargs["model"] == "openrouter/google/gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Cost rate table — the one proven model resolves real pricing
# ---------------------------------------------------------------------------

class TestCostResolution:
    def test_proven_default_resolves_pricing(self):
        from provider_costs import resolve_cost_usd

        cost = resolve_cost_usd(
            "litellm:openrouter", "openrouter/openai/gpt-4o-mini",
            input_tokens=1_000_000, output_tokens=1_000_000,
        )
        assert cost == pytest.approx(0.75)

    def test_compute_cost_end_to_end_via_rate_table_fallback(self):
        """_compute_cost falls back to the rate table when the wave7 registry has
        no 'openrouter' section (by design — this lane is not registry-curated)."""
        cost = pd._compute_cost(
            "litellm:openrouter", "openrouter/openai/gpt-4o-mini",
            {"input": 1_000_000, "output": 1_000_000},
        )
        assert cost == pytest.approx(0.75)
