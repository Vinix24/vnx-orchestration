"""PR-10 — provider-string canonicalization tests.

Covers the five fixes in PR-10:
  P2-1: model sentinel "default"/None must not over-reject via model-not-in-current-registry
  P2-2: deepseek-harness registry key maps to "deepseek"
  kimi: kimi-k2.6 (CLI dots) and kimi-k2-6 (registry dashes) both pass constraint;
        spawn receives CLI arg form (kimi-k2.6)
  GLM:  glm-5/5.1 route -> litellm:zai; glm-4.5/4.6 stay blocked
  split: kimi_cli entries are dispatch_allowed=True; moonshot entries are dispatch_allowed=False
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(_LIB))

from providers.constraint_enforcer import (
    ConstraintEnforcer,
    ConstraintViolationError,
    HardConstraintViolation,
    _registry_key_for,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def enforcer() -> ConstraintEnforcer:
    return ConstraintEnforcer()


# ---------------------------------------------------------------------------
# P2-1 — model sentinel "default" / None / "" must not over-reject
# ---------------------------------------------------------------------------

class TestModelSentinelNotOverReject:

    def test_gemini_model_default_no_registry_violation(self, enforcer):
        violations = enforcer.check_constraints(provider="gemini", model="default", check_registry=True)
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_codex_model_default_no_registry_violation(self, enforcer):
        violations = enforcer.check_constraints(provider="codex", model="default", check_registry=True)
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_gemini_model_none_no_registry_violation(self, enforcer):
        violations = enforcer.check_constraints(provider="gemini", model=None, check_registry=True)
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_codex_model_empty_no_registry_violation(self, enforcer):
        violations = enforcer.check_constraints(provider="codex", model="", check_registry=True)
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_claude_model_default_no_registry_violation(self, enforcer):
        violations = enforcer.check_constraints(provider="claude", model="default", check_registry=True)
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_bogus_explicit_model_still_rejects(self, enforcer):
        violations = enforcer.check_constraints(
            provider="codex", model="definitely-not-a-real-model-xyz", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" in codes, codes

    def test_bogus_explicit_model_gemini_still_rejects(self, enforcer):
        violations = enforcer.check_constraints(
            provider="gemini", model="gpt-4-turbo-not-a-google-model", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" in codes, codes


# ---------------------------------------------------------------------------
# P2-2 — deepseek-harness registry key
# ---------------------------------------------------------------------------

class TestDeepseekHarnessRegistryKey:

    def test_deepseek_harness_maps_to_deepseek(self):
        assert _registry_key_for("deepseek-harness", None) == "deepseek"

    def test_deepseek_harness_underscore_maps_to_deepseek(self):
        assert _registry_key_for("deepseek_harness", None) == "deepseek"

    def test_deepseek_direct_maps_to_deepseek(self):
        assert _registry_key_for("deepseek", None) == "deepseek"

    def test_other_providers_unaffected(self):
        assert _registry_key_for("claude", None) == "anthropic"
        assert _registry_key_for("codex", None) == "openai"
        assert _registry_key_for("gemini", None) == "google"
        assert _registry_key_for("kimi", None) == "kimi_cli"


# ---------------------------------------------------------------------------
# kimi key↔arg — both forms pass constraint; spawn gets CLI arg
# ---------------------------------------------------------------------------

class TestKimiKeyArgMapping:

    def test_kimi_k2_6_dot_form_passes_constraint(self, enforcer):
        """VNX_KIMI_MODEL=kimi-k2.6 (CLI form) must pass the registry constraint."""
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-k2.6", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_kimi_k2_6_dash_form_passes_constraint(self, enforcer):
        """model=kimi-k2-6 (registry key form) must pass the registry constraint."""
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-k2-6", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_kimi_default_passes_constraint(self, enforcer):
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-default", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_kimi_k2_0905_passes_constraint(self, enforcer):
        # kimi-k2-0905 is not in kimi_cli registry — it's in moonshot (dispatch_allowed=false).
        # This should reject since kimi_cli has no entry for it.
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-k2-0905-default", check_registry=True,
        )
        codes = [v.code for v in violations]
        # kimi-k2-0905-default is only in moonshot (dispatch_allowed=false), not kimi_cli
        assert "model-not-in-current-registry" in codes, codes

    def test_kimi_cli_resolve_arg_dot_form_returns_dot(self):
        import provider_dispatch as pd
        result = pd._kimi_resolve_cli_model_arg("kimi-k2.6")
        assert result == "kimi-k2.6"

    def test_kimi_cli_resolve_arg_dash_form_returns_dot(self):
        import provider_dispatch as pd
        result = pd._kimi_resolve_cli_model_arg("kimi-k2-6")
        assert result == "kimi-k2.6"

    def test_kimi_cli_resolve_arg_default_unchanged(self):
        import provider_dispatch as pd
        result = pd._kimi_resolve_cli_model_arg("kimi-default")
        assert result == "kimi-default"

    def test_kimi_constraint_not_fail_closed_on_k2_6(self, enforcer):
        """k2.6 must NOT raise ConstraintViolationError — not a forbidden route."""
        try:
            enforcer.enforce(provider="kimi", model="kimi-k2.6", check_registry=True)
        except ConstraintViolationError as e:
            pytest.fail(f"kimi-k2.6 should not be blocked but got: {e}")


# ---------------------------------------------------------------------------
# GLM → litellm:zai routing
# ---------------------------------------------------------------------------

class TestGlmRouting:

    def test_glm_5_routes_to_litellm_zai(self):
        from smart_router import parse_route_model_id
        provider, model_alias = parse_route_model_id("glm-5")
        assert provider == "litellm:zai", provider
        assert model_alias == "glm-5"

    def test_glm_5_1_routes_to_litellm_zai(self):
        from smart_router import parse_route_model_id
        provider, model_alias = parse_route_model_id("glm-5.1")
        assert provider == "litellm:zai", provider
        assert model_alias == "glm-5.1"

    def test_glm_4_5_still_blocked(self, enforcer):
        with pytest.raises(HardConstraintViolation, match="deprecated-glm-models"):
            enforcer.enforce(provider="litellm", sub_provider="zai", model="glm-4.5")

    def test_glm_4_6_still_blocked(self, enforcer):
        with pytest.raises(HardConstraintViolation, match="deprecated-glm-models"):
            enforcer.enforce(provider="litellm", sub_provider="zai", model="glm-4.6")

    def test_glm_5_allowed_via_zai(self, enforcer):
        """glm-5 via litellm:zai must not be blocked."""
        violations = enforcer.check_constraints(
            provider="litellm", sub_provider="zai", model="glm-5",
        )
        blocking = [v for v in violations if v.severity == "blocking"]
        assert not blocking, [v.code for v in blocking]

    def test_glm_route_provider_not_openrouter(self):
        from smart_router import parse_route_model_id
        provider, _ = parse_route_model_id("glm-5")
        assert "openrouter" not in provider, f"GLM should route to litellm:zai, got: {provider}"


# ---------------------------------------------------------------------------
# Kimi prod/baseline split — dispatch_allowed field
# ---------------------------------------------------------------------------

class TestKimiDispatchAllowedSplit:

    def test_kimi_cli_kimi_default_dispatch_allowed(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-default"]
        assert entry.dispatch_allowed is True

    def test_kimi_cli_kimi_k2_6_dispatch_allowed(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-k2-6"]
        assert entry.dispatch_allowed is True

    def test_moonshot_kimi_k2_0905_not_dispatch_allowed(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["moonshot"].models["kimi-k2-0905-default"]
        assert entry.dispatch_allowed is False

    def test_moonshot_kimi_k2_6_not_dispatch_allowed(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["moonshot"].models["kimi-k2-6"]
        assert entry.dispatch_allowed is False

    def test_kimi_cli_k2_6_has_cli_model_arg(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-k2-6"]
        assert entry.cli_model_arg == "kimi-k2.6"

    def test_moonshot_not_a_governed_dispatch_route(self, enforcer):
        """moonshot models are dispatch_allowed=False — registry check rejects them."""
        violations = enforcer.check_constraints(
            provider="litellm", sub_provider="moonshot", model="kimi-k2-6", check_registry=True,
        )
        codes = [v.code for v in violations]
        # litellm:moonshot hits kimi-via-cli-only (via=moonshot is forbidden);
        # additionally registry model is dispatch_allowed=False
        assert "kimi-via-cli-only" in codes or "model-not-in-current-registry" in codes, codes
