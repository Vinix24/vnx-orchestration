"""Tests for kimi-via-cli-only constraint: native CLI provider NEVER blocked.

The kimi-via-cli-only constraint targets litellm:moonshot API routes.
provider='kimi' (native CLI via `kimi login` OAuth) is ALWAYS valid and must
never trigger a violation, regardless of sub_provider or model values passed.

Dispatch-ID: 20260517-fix-smart-router-enforcer
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from constraint_enforcer import ConstraintEnforcer, HardConstraintViolation


@pytest.fixture
def enforcer() -> ConstraintEnforcer:
    return ConstraintEnforcer()


class TestKimiCliAlwaysValid:
    """provider='kimi' is the native CLI route and must NEVER be blocked."""

    def test_kimi_provider_via_cli_not_blocked(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="kimi", sub_provider=None, via="cli")

    def test_kimi_provider_no_via_not_blocked(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="kimi", sub_provider=None, via=None)

    def test_kimi_provider_with_model_not_blocked(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="kimi", sub_provider=None, model="kimi-k2-0905", via="cli")

    def test_kimi_provider_with_kimi_k2_6_model(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="kimi", sub_provider=None, model="kimi-k2-6", via="cli")

    def test_kimi_provider_case_insensitive(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="Kimi", sub_provider=None, via="cli")

    def test_kimi_provider_with_erroneous_sub_provider(self, enforcer: ConstraintEnforcer):
        """Even if sub_provider is erroneously set, native CLI provider wins."""
        enforcer.enforce(provider="kimi", sub_provider="moonshot", via="cli")

    def test_kimi_provider_with_moonshot_via(self, enforcer: ConstraintEnforcer):
        """provider='kimi' is native CLI — 'moonshot' via is irrelevant."""
        enforcer.enforce(provider="kimi", sub_provider=None, via="moonshot")


class TestLitellmMoonshotStillBlocked:
    """litellm:moonshot API route must remain blocked (not a regression)."""

    def test_litellm_moonshot_via_api_blocked(self, enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="kimi-via-cli-only"):
            enforcer.enforce(provider="litellm", sub_provider="moonshot", via="api")

    def test_litellm_moonshot_via_moonshot_blocked(self, enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="kimi-via-cli-only"):
            enforcer.enforce(provider="litellm", sub_provider="moonshot", via="moonshot")

    def test_litellm_moonshot_via_cli_not_blocked(self, enforcer: ConstraintEnforcer):
        """litellm via cli is not in the forbidden via list — allowed."""
        enforcer.enforce(provider="litellm", sub_provider="moonshot", via="cli")


class TestOtherNativeCliProvidersUnaffected:
    """Other native CLI providers must not be blocked by kimi-via-cli-only."""

    def test_claude_provider_not_blocked(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="claude", model="claude-opus-4-7", via="cli")

    def test_gemini_provider_not_blocked(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="gemini", model="gemini-2.5-pro", via="cli")

    def test_codex_provider_not_blocked(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="codex", via="cli")


class TestExistingConstraintsNotRegressed:
    """Ensure the enforcer fix doesn't break other constraints."""

    def test_zai_direct_still_blocked(self, enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="zai-via-openrouter-only"):
            enforcer.enforce(provider="zai", via="direct")

    def test_deprecated_glm_still_blocked(self, enforcer: ConstraintEnforcer):
        with pytest.raises(HardConstraintViolation, match="deprecated-glm-models"):
            enforcer.enforce(provider="zai", model="glm-4.5")

    def test_deepseek_harness_subscription_still_blocked(self, enforcer: ConstraintEnforcer):
        """Subscription-redirect path remains blocked after kimi enforcer fix."""
        with pytest.raises(HardConstraintViolation, match="deepseek-harness-subscription-blocked"):
            enforcer.enforce(provider="deepseek", via="claude_harness_subscription")

    def test_deepseek_harness_keyed_allowed(self, enforcer: ConstraintEnforcer):
        """Own-key + hardening path is allowed — kimi fix must not regress this."""
        enforcer.enforce(provider="deepseek", via="claude_harness_keyed")

    def test_litellm_deepseek_via_litellm_allowed(self, enforcer: ConstraintEnforcer):
        enforcer.enforce(provider="litellm", sub_provider="deepseek", via="litellm")

    def test_zai_via_openrouter_allowed(self, enforcer: ConstraintEnforcer, monkeypatch):
        # glm-via-harness-only now blocks plain litellm:zai entirely (GLM must run via
        # glm-harness); override it to isolate the zai-via-openrouter-only behavior under test.
        monkeypatch.setenv("VNX_OVERRIDE_GLM_VIA_HARNESS_ONLY", "1")
        enforcer.enforce(provider="litellm", sub_provider="zai", via="openrouter")
