"""PR-10 — provider-string canonicalization tests.

Covers the five fixes in PR-10:
  P2-1: model sentinel "default"/None must not over-reject via model-not-in-current-registry
  P2-2: deepseek-harness registry key maps to "deepseek"
  kimi: kimi-k3 / kimi-k2-7 (verified kimi-cli 1.46.0 registry keys) pass constraint,
        spawn receives the verified CLI arg form; kimi-k2-6/kimi-k2.6 is retired
        upstream (20260721-kimi-lane-hardening) and is now dispatch_allowed=false
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

    def test_kimi_k3_passes_constraint(self, enforcer):
        """model=kimi-k3 (verified kimi-cli 1.46.0 default) must pass the registry constraint."""
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-k3", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_kimi_k2_7_passes_constraint(self, enforcer):
        """model=kimi-k2-7 (verified kimi-cli 1.46.0 model) must pass the registry constraint."""
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-k2-7", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" not in codes, codes

    def test_kimi_k2_6_now_deprecated_blocked(self, enforcer):
        """kimi-k2-6/kimi-k2.6 is retired in kimi-cli 1.46.0 (no such model in the CLI's own
        config; superseded by kimi-for-coding/K2.7) — disabled via dispatch_allowed=false so
        the registry check rejects it instead of a worker silently receiving a dead CLI arg."""
        violations = enforcer.check_constraints(
            provider="kimi", model="kimi-k2-6", check_registry=True,
        )
        codes = [v.code for v in violations]
        assert "model-not-in-current-registry" in codes, codes

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

    def test_kimi_cli_resolve_arg_k3_returns_verified_string(self):
        import provider_dispatch as pd
        result = pd._kimi_resolve_cli_model_arg("kimi-k3")
        assert result == "kimi-code/k3"

    def test_kimi_cli_resolve_arg_k2_7_returns_verified_string(self):
        import provider_dispatch as pd
        result = pd._kimi_resolve_cli_model_arg("kimi-k2-7")
        assert result == "kimi-code/kimi-for-coding"

    def test_kimi_cli_resolve_arg_default_unchanged(self):
        import provider_dispatch as pd
        result = pd._kimi_resolve_cli_model_arg("kimi-default")
        assert result == "kimi-default"

    def test_kimi_cli_resolve_arg_k2_6_raises_disabled(self):
        """kimi-k2-6 is dispatch_allowed=false (retired) — resolving it must fail loud,
        never silently pass the old dash-form key through as a `-m` argument."""
        import provider_dispatch as pd
        with pytest.raises(pd.KimiModelResolutionError):
            pd._kimi_resolve_cli_model_arg("kimi-k2-6")

    def test_kimi_constraint_fail_closed_on_k2_6(self, enforcer):
        """kimi-k2-6 IS now blocked — it is retired/dispatch_allowed=false, not a forbidden route."""
        with pytest.raises(ConstraintViolationError, match="model-not-in-current-registry"):
            enforcer.enforce(provider="kimi", model="kimi-k2-6", check_registry=True)

    def test_kimi_constraint_not_fail_closed_on_k3(self, enforcer):
        """kimi-k3 must NOT raise ConstraintViolationError — the verified, dispatchable default."""
        try:
            enforcer.enforce(provider="kimi", model="kimi-k3", check_registry=True)
        except ConstraintViolationError as e:
            pytest.fail(f"kimi-k3 should not be blocked but got: {e}")


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

    def test_kimi_cli_kimi_k2_6_dispatch_disabled(self):
        """kimi-k2-6 is retired upstream (20260721-kimi-lane-hardening) — dispatch_allowed=False."""
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-k2-6"]
        assert entry.dispatch_allowed is False

    def test_kimi_cli_kimi_k3_dispatch_allowed(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-k3"]
        assert entry.dispatch_allowed is True

    def test_kimi_cli_kimi_k2_7_dispatch_allowed(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-k2-7"]
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

    def test_kimi_cli_k2_6_cli_model_arg_cleared(self):
        """The stale dot-form cli_model_arg is cleared (not kept) on the disabled entry —
        it was never verified against a live kimi-cli 1.46.0 model."""
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-k2-6"]
        assert not entry.cli_model_arg

    def test_kimi_cli_k3_has_verified_cli_model_arg(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-k3"]
        assert entry.cli_model_arg == "kimi-code/k3"

    def test_kimi_cli_k2_7_has_verified_cli_model_arg(self):
        from providers.provider_registry import load
        registry = load()
        entry = registry["kimi_cli"].models["kimi-k2-7"]
        assert entry.cli_model_arg == "kimi-code/kimi-for-coding"

    def test_kimi_cli_default_model_flag(self):
        from providers.provider_registry import load
        registry = load()
        assert registry["kimi_cli"].default_model == "kimi-k3"

    def test_moonshot_not_a_governed_dispatch_route(self, enforcer):
        """moonshot models are dispatch_allowed=False — registry check rejects them."""
        violations = enforcer.check_constraints(
            provider="litellm", sub_provider="moonshot", model="kimi-k2-6", check_registry=True,
        )
        codes = [v.code for v in violations]
        # litellm:moonshot hits kimi-via-cli-only (via=moonshot is forbidden);
        # additionally registry model is dispatch_allowed=False
        assert "kimi-via-cli-only" in codes or "model-not-in-current-registry" in codes, codes
