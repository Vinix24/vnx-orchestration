#!/usr/bin/env python3
"""Provider-aware observability tests for PR-3 (Feature 17).

Covers:
  1. Provider capability flags for known providers
  2. Observability quality projections
  3. Progress confidence projections
  4. Attachability flag
  5. Output-only fallback semantics
  6. Unknown provider fallback
  7. Registry completeness
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "lib"))

from provider_observability import (
    PROVIDER_REGISTRY,
    UNKNOWN_PROVIDER_CAPABILITIES,
    ObservabilityQuality,
    ProviderCapabilities,
    get_provider_capabilities,
    is_provider_known,
)


# ---------------------------------------------------------------------------
# 1. Provider capability flags — claude_code
# ---------------------------------------------------------------------------

class TestClaudeCodeCapabilities:

    def setup_method(self) -> None:
        self.caps = get_provider_capabilities("claude_code")

    def test_tool_call_visibility_enabled(self) -> None:
        assert self.caps.tool_call_visibility is True

    def test_structured_progress_events_enabled(self) -> None:
        assert self.caps.structured_progress_events is True

    def test_output_only_fallback_disabled(self) -> None:
        assert self.caps.output_only_fallback is False

    def test_can_attach_enabled(self) -> None:
        assert self.caps.can_attach is True

    def test_observability_quality_rich(self) -> None:
        assert self.caps.observability_quality() == ObservabilityQuality.RICH

    def test_progress_confidence_high(self) -> None:
        assert self.caps.progress_confidence() == "high"


# ---------------------------------------------------------------------------
# 2. Provider capability flags — gemini
# ---------------------------------------------------------------------------

class TestGeminiCapabilities:

    def setup_method(self) -> None:
        self.caps = get_provider_capabilities("gemini")

    def test_tool_call_visibility_disabled(self) -> None:
        assert self.caps.tool_call_visibility is False

    def test_structured_progress_events_enabled(self) -> None:
        assert self.caps.structured_progress_events is True

    def test_output_only_fallback_disabled(self) -> None:
        assert self.caps.output_only_fallback is False

    def test_can_attach_disabled(self) -> None:
        assert self.caps.can_attach is False

    def test_observability_quality_structured(self) -> None:
        assert self.caps.observability_quality() == ObservabilityQuality.STRUCTURED

    def test_progress_confidence_medium(self) -> None:
        assert self.caps.progress_confidence() == "medium"


# ---------------------------------------------------------------------------
# 3. Provider capability flags — codex_cli
# ---------------------------------------------------------------------------

class TestCodexCliCapabilities:

    def setup_method(self) -> None:
        self.caps = get_provider_capabilities("codex_cli")

    def test_tool_call_visibility_disabled(self) -> None:
        assert self.caps.tool_call_visibility is False

    def test_structured_progress_events_disabled(self) -> None:
        assert self.caps.structured_progress_events is False

    def test_output_only_fallback_enabled(self) -> None:
        assert self.caps.output_only_fallback is True

    def test_can_attach_disabled(self) -> None:
        assert self.caps.can_attach is False

    def test_observability_quality_output_only(self) -> None:
        assert self.caps.observability_quality() == ObservabilityQuality.OUTPUT_ONLY

    def test_progress_confidence_low(self) -> None:
        assert self.caps.progress_confidence() == "low"


# ---------------------------------------------------------------------------
# 4. Explicit output-only fallback provider
# ---------------------------------------------------------------------------

class TestOutputOnlyFallback:

    def setup_method(self) -> None:
        self.caps = get_provider_capabilities("output_only")

    def test_output_only_fallback_enabled(self) -> None:
        assert self.caps.output_only_fallback is True

    def test_no_tool_call_visibility(self) -> None:
        assert self.caps.tool_call_visibility is False

    def test_no_structured_progress(self) -> None:
        assert self.caps.structured_progress_events is False

    def test_no_attach(self) -> None:
        assert self.caps.can_attach is False

    def test_observability_quality_output_only(self) -> None:
        assert self.caps.observability_quality() == ObservabilityQuality.OUTPUT_ONLY

    def test_progress_confidence_low(self) -> None:
        assert self.caps.progress_confidence() == "low"


# ---------------------------------------------------------------------------
# 5. Unknown provider fallback
# ---------------------------------------------------------------------------

class TestUnknownProviderFallback:

    def test_unknown_provider_returns_output_only(self) -> None:
        caps = get_provider_capabilities("some_future_provider")
        assert caps.output_only_fallback is True

    def test_unknown_provider_quality_output_only(self) -> None:
        caps = get_provider_capabilities("nonexistent")
        assert caps.observability_quality() == ObservabilityQuality.OUTPUT_ONLY

    def test_unknown_provider_confidence_low(self) -> None:
        caps = get_provider_capabilities("nonexistent")
        assert caps.progress_confidence() == "low"

    def test_unknown_provider_no_attach(self) -> None:
        caps = get_provider_capabilities("nonexistent")
        assert caps.can_attach is False

    def test_is_provider_known_false_for_unknown(self) -> None:
        assert is_provider_known("nonexistent") is False

    def test_is_provider_known_true_for_claude_code(self) -> None:
        assert is_provider_known("claude_code") is True


# ---------------------------------------------------------------------------
# 6. Observability quality distinguishes capability levels
# ---------------------------------------------------------------------------

class TestObservabilityQualityDistinction:

    def test_rich_requires_both_flags(self) -> None:
        caps = ProviderCapabilities(
            provider_id="test",
            tool_call_visibility=True,
            structured_progress_events=True,
            output_only_fallback=False,
            can_attach=False,
        )
        assert caps.observability_quality() == ObservabilityQuality.RICH

    def test_structured_without_tool_call_detail(self) -> None:
        caps = ProviderCapabilities(
            provider_id="test",
            tool_call_visibility=False,
            structured_progress_events=True,
            output_only_fallback=False,
            can_attach=False,
        )
        assert caps.observability_quality() == ObservabilityQuality.STRUCTURED

    def test_output_only_when_flag_set(self) -> None:
        caps = ProviderCapabilities(
            provider_id="test",
            tool_call_visibility=True,  # flag set but output_only overrides
            structured_progress_events=True,
            output_only_fallback=True,
            can_attach=False,
        )
        assert caps.observability_quality() == ObservabilityQuality.OUTPUT_ONLY

    def test_output_only_when_no_events_and_no_flag(self) -> None:
        caps = ProviderCapabilities(
            provider_id="test",
            tool_call_visibility=False,
            structured_progress_events=False,
            output_only_fallback=False,
            can_attach=False,
        )
        assert caps.observability_quality() == ObservabilityQuality.OUTPUT_ONLY

    def test_quality_enum_values_distinct(self) -> None:
        assert ObservabilityQuality.RICH != ObservabilityQuality.STRUCTURED
        assert ObservabilityQuality.STRUCTURED != ObservabilityQuality.OUTPUT_ONLY
        assert ObservabilityQuality.RICH != ObservabilityQuality.OUTPUT_ONLY


# ---------------------------------------------------------------------------
# 7. Registry completeness
# ---------------------------------------------------------------------------

class TestRegistryCompleteness:

    def test_registry_contains_required_providers(self) -> None:
        required = {"claude_code", "gemini", "codex_cli", "output_only"}
        assert required.issubset(set(PROVIDER_REGISTRY.keys()))

    def test_all_registry_entries_have_provider_id(self) -> None:
        for pid, caps in PROVIDER_REGISTRY.items():
            assert caps.provider_id == pid

    def test_unknown_fallback_is_output_only(self) -> None:
        assert UNKNOWN_PROVIDER_CAPABILITIES.output_only_fallback is True

    def test_progress_confidence_values_are_valid(self) -> None:
        valid = {"high", "medium", "low"}
        for caps in PROVIDER_REGISTRY.values():
            assert caps.progress_confidence() in valid
