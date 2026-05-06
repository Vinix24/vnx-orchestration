"""Tests for observability_tier.py — adapter tier registry and resolution.

Covers:
- ADAPTER_DEFAULT_TIERS contains all expected adapters
- ADAPTER_MINIMUM_TIERS are <= ADAPTER_DEFAULT_TIERS for every adapter
- resolve_effective_tier returns correct tier per provider
- Gemini tier follows VNX_GEMINI_STREAM env var
- GOVERNANCE_MIN_TIERS contains coding-strict and business-light
- get_governance_min_tier returns correct values
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lib"))

from observability_tier import (
    ADAPTER_DEFAULT_TIERS,
    ADAPTER_MINIMUM_TIERS,
    GOVERNANCE_MIN_TIERS,
    get_governance_min_tier,
    resolve_effective_tier,
)


EXPECTED_ADAPTERS = {"claude", "codex", "gemini", "litellm", "ollama"}


class TestAdapterTierRegistry:
    def test_all_adapters_present_in_default_tiers(self):
        for adapter in EXPECTED_ADAPTERS:
            assert adapter in ADAPTER_DEFAULT_TIERS, f"{adapter!r} missing from ADAPTER_DEFAULT_TIERS"

    def test_all_adapters_present_in_minimum_tiers(self):
        for adapter in EXPECTED_ADAPTERS:
            assert adapter in ADAPTER_MINIMUM_TIERS, f"{adapter!r} missing from ADAPTER_MINIMUM_TIERS"

    def test_tiers_are_integers_in_valid_range(self):
        for adapter, tier in ADAPTER_DEFAULT_TIERS.items():
            assert isinstance(tier, int), f"{adapter} default tier is not int"
            assert 1 <= tier <= 3, f"{adapter} default tier {tier} out of range"

    def test_minimum_tiers_are_integers_in_valid_range(self):
        for adapter, tier in ADAPTER_MINIMUM_TIERS.items():
            assert isinstance(tier, int), f"{adapter} minimum tier is not int"
            assert 1 <= tier <= 3, f"{adapter} minimum tier {tier} out of range"

    def test_minimum_tier_not_lower_than_default(self):
        # minimum tier cannot be MORE capable (lower number) than default
        for adapter in ADAPTER_DEFAULT_TIERS:
            if adapter in ADAPTER_MINIMUM_TIERS:
                assert ADAPTER_MINIMUM_TIERS[adapter] >= ADAPTER_DEFAULT_TIERS[adapter], (
                    f"{adapter}: minimum_tier ({ADAPTER_MINIMUM_TIERS[adapter]}) must be "
                    f">= default_tier ({ADAPTER_DEFAULT_TIERS[adapter]})"
                )

    def test_claude_is_tier_1(self):
        assert ADAPTER_DEFAULT_TIERS["claude"] == 1

    def test_codex_is_tier_1(self):
        assert ADAPTER_DEFAULT_TIERS["codex"] == 1

    def test_gemini_default_is_tier_1(self):
        # Default tier for gemini is Tier 1 (streaming path)
        assert ADAPTER_DEFAULT_TIERS["gemini"] == 1

    def test_ollama_default_is_tier_2(self):
        # Ollama baseline is text-only streaming
        assert ADAPTER_DEFAULT_TIERS["ollama"] == 2

    def test_gemini_minimum_is_tier_3(self):
        # Worst case is legacy path (no streaming)
        assert ADAPTER_MINIMUM_TIERS["gemini"] == 3


class TestResolveEffectiveTier:
    def test_claude_returns_tier_1(self):
        assert resolve_effective_tier("claude") == 1

    def test_codex_returns_tier_1(self):
        assert resolve_effective_tier("codex") == 1

    def test_gemini_tier_1_when_stream_enabled(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        assert resolve_effective_tier("gemini") == 1

    def test_gemini_tier_3_when_stream_disabled(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "0")
        assert resolve_effective_tier("gemini") == 3

    def test_gemini_tier_3_when_stream_unset(self, monkeypatch):
        monkeypatch.delenv("VNX_GEMINI_STREAM", raising=False)
        assert resolve_effective_tier("gemini") == 3

    def test_litellm_tier_1_when_streaming(self):
        assert resolve_effective_tier("litellm", streaming_enabled=True) == 1

    def test_litellm_tier_2_when_not_streaming(self):
        assert resolve_effective_tier("litellm", streaming_enabled=False) == 2

    def test_ollama_returns_tier_2_baseline(self):
        # Baseline tier for ollama is 2 (text-only streaming)
        assert resolve_effective_tier("ollama") == 2

    def test_unknown_provider_returns_safe_default(self):
        tier = resolve_effective_tier("unknown-provider")
        assert 1 <= tier <= 3

    def test_case_insensitive_provider(self, monkeypatch):
        monkeypatch.setenv("VNX_GEMINI_STREAM", "1")
        assert resolve_effective_tier("GEMINI") == resolve_effective_tier("gemini")


class TestGovernanceMinTiers:
    def test_coding_strict_requires_tier_1(self):
        assert GOVERNANCE_MIN_TIERS["coding-strict"] == 1

    def test_business_light_requires_tier_2(self):
        assert GOVERNANCE_MIN_TIERS["business-light"] == 2

    def test_default_requires_tier_1(self):
        assert GOVERNANCE_MIN_TIERS.get("default", 1) == 1

    def test_get_governance_min_tier_coding_strict(self):
        assert get_governance_min_tier("coding-strict") == 1

    def test_get_governance_min_tier_business_light(self):
        assert get_governance_min_tier("business-light") == 2

    def test_get_governance_min_tier_unknown_returns_1(self):
        # Unknown variants default to strictest (Tier 1) for safety
        assert get_governance_min_tier("nonexistent-variant") == 1

    def test_get_governance_min_tier_case_insensitive(self):
        assert get_governance_min_tier("CODING-STRICT") == get_governance_min_tier("coding-strict")

    def test_all_tiers_are_valid(self):
        for variant, tier in GOVERNANCE_MIN_TIERS.items():
            assert 1 <= tier <= 3, f"{variant!r} min_tier {tier} out of range"
