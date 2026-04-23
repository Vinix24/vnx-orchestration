#!/usr/bin/env python3
"""Provider-aware observability capability layer.

VNX executes dispatches across multiple AI providers (claude_code, gemini,
codex_cli). Each provider exposes a different level of runtime detail:
- claude_code: full tool-call visibility + structured progress events
- gemini: structured progress events, no tool-call detail
- codex_cli: output-only (no structured events)
- output_only: explicit fallback for providers with no internal observability

This module makes those distinctions explicit and queryable, so operators
know whether they are seeing rich runtime signals or coarse output evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict


# ---------------------------------------------------------------------------
# Observability quality levels
# ---------------------------------------------------------------------------

class ObservabilityQuality(Enum):
    """Describes the richness of observability a provider exposes."""
    RICH = "rich"             # Tool-call detail + structured progress events
    STRUCTURED = "structured" # Structured progress events, no tool-call detail
    OUTPUT_ONLY = "output_only"  # No structured events; coarse output evidence only


# ---------------------------------------------------------------------------
# Provider capabilities
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderCapabilities:
    """Capability flags for a single AI provider.

    Attributes:
        provider_id:              Canonical provider name.
        tool_call_visibility:     Provider exposes per-tool-call events.
        structured_progress_events: Provider emits structured progress signals
                                  (not just raw stdout).
        output_only_fallback:     Provider has no structured event channel;
                                  observability is limited to output text.
        can_attach:               Provider supports session attachment/resume.
    """
    provider_id: str
    tool_call_visibility: bool
    structured_progress_events: bool
    output_only_fallback: bool
    can_attach: bool

    # ------------------------------------------------------------------
    # Projections
    # ------------------------------------------------------------------

    def observability_quality(self) -> ObservabilityQuality:
        """Derive observability quality from capability flags."""
        if self.output_only_fallback:
            return ObservabilityQuality.OUTPUT_ONLY
        if self.tool_call_visibility and self.structured_progress_events:
            return ObservabilityQuality.RICH
        if self.structured_progress_events:
            return ObservabilityQuality.STRUCTURED
        # Neither structured events nor explicit output-only flag — degrade.
        return ObservabilityQuality.OUTPUT_ONLY

    def progress_confidence(self) -> str:
        """Human-readable confidence label for progress signals.

        Returns one of: 'high', 'medium', 'low'
        """
        quality = self.observability_quality()
        if quality == ObservabilityQuality.RICH:
            return "high"
        if quality == ObservabilityQuality.STRUCTURED:
            return "medium"
        return "low"


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

#: Known providers with their capability profiles.
PROVIDER_REGISTRY: Dict[str, ProviderCapabilities] = {
    "claude_code": ProviderCapabilities(
        provider_id="claude_code",
        tool_call_visibility=True,
        structured_progress_events=True,
        output_only_fallback=False,
        can_attach=True,
    ),
    "gemini": ProviderCapabilities(
        provider_id="gemini",
        tool_call_visibility=False,
        structured_progress_events=True,
        output_only_fallback=False,
        can_attach=False,
    ),
    "codex_cli": ProviderCapabilities(
        provider_id="codex_cli",
        tool_call_visibility=False,
        structured_progress_events=False,
        output_only_fallback=True,
        can_attach=False,
    ),
    "output_only": ProviderCapabilities(
        provider_id="output_only",
        tool_call_visibility=False,
        structured_progress_events=False,
        output_only_fallback=True,
        can_attach=False,
    ),
}

#: Fallback capabilities for unknown providers.
UNKNOWN_PROVIDER_CAPABILITIES = ProviderCapabilities(
    provider_id="unknown",
    tool_call_visibility=False,
    structured_progress_events=False,
    output_only_fallback=True,
    can_attach=False,
)


def get_provider_capabilities(provider_id: str) -> ProviderCapabilities:
    """Return capabilities for the given provider.

    Falls back to output-only for unknown providers rather than raising,
    so callers degrade gracefully.
    """
    return PROVIDER_REGISTRY.get(provider_id, UNKNOWN_PROVIDER_CAPABILITIES)


def is_provider_known(provider_id: str) -> bool:
    """Return True if the provider is in the registry."""
    return provider_id in PROVIDER_REGISTRY
