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

import logging
import os
from typing import Literal

logger = logging.getLogger(__name__)

ObservabilityTier = Literal[1, 2, 3]

# Per-adapter default tiers (effective under typical/streaming config).
# These match the constants declared on each adapter class.
ADAPTER_DEFAULT_TIERS: dict[str, int] = {
    "claude": 1,   # Live streaming via subprocess_adapter
    "codex": 1,    # Live streaming via StreamingDrainerMixin
    "gemini": 1,   # Tier 1 when VNX_GEMINI_STREAM=1 (streaming); Tier 3 otherwise
    "litellm": 1,  # Tier 1 when streaming SSE works; Tier 2 when only [DONE]
    "ollama": 2,   # Tier 2 baseline (text-only); Tier 1 when tool_use detected
    # kimi streams per-event content-block messages over stream-json with full
    # tool_use/tool_result parity: kimi_spawn.py::normalize_kimi_event tags every
    # CanonicalEvent observability_tier=1 and _KimiNormalizerHost declares
    # provider_observability_tier=1. Tier 1 (live per-event streaming).
    "kimi": 1,
    # glm-harness and deepseek-harness ride the full claude CLI harness (tools +
    # agentic loop + live stream-json): both glm_harness_spawn.spawn_glm_harness
    # and deepseek_harness_spawn.spawn_deepseek_harness delegate transport to
    # provider_spawns.claude_spawn.spawn_claude (claude_adapter OBSERVABILITY_TIER=1).
    # Tier 1. The bare names ("glm"/"deepseek") are the registry keys
    # provider_dispatch._PROVIDER_TO_REGISTRY_KEY uses; the "-harness" spellings
    # are the receipt provider strings the harness lanes write via
    # frontmatter_fields(). Both spellings resolve to the same tier.
    "glm": 1,
    "deepseek": 1,
    "glm-harness": 1,
    "deepseek-harness": 1,
}

# Per-adapter guaranteed minimum tiers (worst-case).
ADAPTER_MINIMUM_TIERS: dict[str, int] = {
    "claude": 1,
    "codex": 1,
    "gemini": 3,   # Legacy path (VNX_GEMINI_STREAM=0) emits single synthetic result
    "litellm": 2,  # Fallback when streaming SSE unavailable
    "ollama": 2,   # Text-only baseline for non-tool-trained models
    # kimi has no final-only fallback path: _build_kimi_cmd always passes
    # `--output-format stream-json`, and even the legacy event_type stream
    # (pre-1.44) emits per-event streaming. Worst case is still Tier 1.
    "kimi": 1,
    # glm/deepseek harness lanes always ride spawn_claude's live stream-json
    # transport; there is no legacy synthetic-result path. Worst case Tier 1.
    "glm": 1,
    "deepseek": 1,
    "glm-harness": 1,
    "deepseek-harness": 1,
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
    For all others: returns ADAPTER_DEFAULT_TIERS[provider]. An unregistered
    provider falls back to tier 2 (safe default) and logs a WARNING with the
    provider name so a missing entry is never silent.
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

    tier = ADAPTER_DEFAULT_TIERS.get(provider)
    if tier is None:
        # A missing provider must never be a silent Tier-2 default: log it so the
        # next unregistered provider is visible instead of latent. This is what
        # made kimi/glm/deepseek fall through to Tier 2 unnoticed until wired.
        logger.warning(
            "observability_tier: provider %r has no registered tier — defaulting "
            "to tier 2 (limited streaming); add it to ADAPTER_DEFAULT_TIERS",
            provider,
        )
        return 2
    return tier


def get_governance_min_tier(governance_variant: str) -> int:
    """Return the min_observability_tier for a governance variant.

    Falls back to 1 (strictest) for unknown variants.
    """
    return GOVERNANCE_MIN_TIERS.get(governance_variant.lower(), 1)
