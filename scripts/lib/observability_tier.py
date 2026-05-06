#!/usr/bin/env python3
"""observability_tier.py — Adapter observability tier registry and resolution.

Tier definitions:
  Tier 1: Live per-event streaming (full observability, tool_use parity)
  Tier 2: Streaming but limited (text-only or final-only streaming)
  Tier 3: Final-only synthetic result (single event emitted after completion)

Adapters expose OBSERVABILITY_TIER (default config) and
OBSERVABILITY_TIER_MINIMUM (worst-case guaranteed tier).

Governance defaults:
  coding-strict: min_observability_tier = 1
  business-light: min_observability_tier = 2
  default: min_observability_tier = 1
"""
from __future__ import annotations

import os
from typing import Literal

ObservabilityTier = Literal[1, 2, 3]

# Per-adapter default tiers (effective under typical/streaming config).
# These match the constants declared on each adapter class.
ADAPTER_DEFAULT_TIERS: dict[str, int] = {
    "claude": 1,   # Live streaming via subprocess_adapter
    "codex": 1,    # Live streaming via StreamingDrainerMixin
    "gemini": 1,   # Tier 1 when VNX_GEMINI_STREAM=1 (streaming); Tier 3 otherwise
    "litellm": 1,  # Tier 1 when streaming SSE works; Tier 2 when only [DONE]
    "ollama": 2,   # Tier 2 baseline (text-only); Tier 1 when tool_use detected
}

# Per-adapter guaranteed minimum tiers (worst-case).
ADAPTER_MINIMUM_TIERS: dict[str, int] = {
    "claude": 1,
    "codex": 1,
    "gemini": 3,   # Legacy path (VNX_GEMINI_STREAM=0) emits single synthetic result
    "litellm": 2,  # Fallback when streaming SSE unavailable
    "ollama": 2,   # Text-only baseline for non-tool-trained models
}

# Governance variant minimum tier requirements.
# coding-strict: full observability for code-writing dispatches
# business-light: streaming optional, final-only acceptable
GOVERNANCE_MIN_TIERS: dict[str, int] = {
    "coding-strict": 1,
    "business-light": 2,
    "default": 1,
    "light": 2,
    "minimal": 3,
}


def resolve_effective_tier(provider: str, *, streaming_enabled: bool = True) -> int:
    """Return the effective observability tier for a provider given runtime config.

    For Gemini: checks VNX_GEMINI_STREAM env var when streaming_enabled is True.
    For all others: returns ADAPTER_DEFAULT_TIERS[provider] or tier 2 (safe default).
    """
    provider = provider.lower()

    if provider == "gemini":
        gemini_stream = os.environ.get("VNX_GEMINI_STREAM", "0").strip() == "1"
        return 1 if gemini_stream else 3

    if provider == "litellm":
        # Tier 1 when streaming is enabled (default), Tier 2 otherwise
        return 1 if streaming_enabled else 2

    if provider == "ollama":
        # Ollama baseline is Tier 2; Tier 1 detected at runtime when tool_use fires
        return ADAPTER_DEFAULT_TIERS.get("ollama", 2)

    return ADAPTER_DEFAULT_TIERS.get(provider, 2)


def get_governance_min_tier(governance_variant: str) -> int:
    """Return the min_observability_tier for a governance variant.

    Falls back to 1 (strictest) for unknown variants.
    """
    return GOVERNANCE_MIN_TIERS.get(governance_variant.lower(), 1)
