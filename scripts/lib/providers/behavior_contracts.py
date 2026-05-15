"""behavior_contracts.py — Wave 7 provider behavior contracts.

Codifies what each provider lane supports + how its outputs are normalized.
Used by routing_policy + spawn handlers to set capability flags + apply
provider-specific output normalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set


@dataclass(frozen=True)
class BehaviorContract:
    provider: str               # claude | litellm | claude_sdk
    sub_provider: str           # "" for claude, "deepseek"|"moonshot"|"zai" for litellm
    supports_streaming: bool
    supports_tool_calls: bool
    tool_call_shape: str        # "anthropic_tools" | "openai_functions" | "openai_tools"
    cache_control_supported: bool
    audit_shape: str            # "canonical_event" (all use ADR-016 shape)
    max_context_tokens: int
    max_output_tokens: int


# Static contract registry — loaded once, used everywhere
CONTRACTS: Dict[str, BehaviorContract] = {
    "claude/sonnet-4-6": BehaviorContract(
        provider="claude",
        sub_provider="",
        supports_streaming=True,
        supports_tool_calls=True,
        tool_call_shape="anthropic_tools",
        cache_control_supported=True,
        audit_shape="canonical_event",
        max_context_tokens=200_000,
        max_output_tokens=8192,
    ),
    "claude/haiku-4-5": BehaviorContract(
        provider="claude",
        sub_provider="",
        supports_streaming=True,
        supports_tool_calls=True,
        tool_call_shape="anthropic_tools",
        cache_control_supported=True,
        audit_shape="canonical_event",
        max_context_tokens=200_000,
        max_output_tokens=8192,
    ),
    "claude/opus": BehaviorContract(
        provider="claude",
        sub_provider="",
        supports_streaming=True,
        supports_tool_calls=True,
        tool_call_shape="anthropic_tools",
        cache_control_supported=True,
        audit_shape="canonical_event",
        max_context_tokens=200_000,
        max_output_tokens=8192,
    ),
    "litellm:deepseek:deepseek-v4-pro": BehaviorContract(
        provider="litellm",
        sub_provider="deepseek",
        supports_streaming=True,
        supports_tool_calls=True,
        tool_call_shape="openai_tools",  # DeepSeek follows OpenAI tools shape
        cache_control_supported=False,    # no native cache control via LiteLLM
        audit_shape="canonical_event",
        max_context_tokens=128_000,
        max_output_tokens=8192,
    ),
    "litellm:moonshot:kimi-k2-0905-default": BehaviorContract(
        provider="litellm",
        sub_provider="moonshot",
        supports_streaming=True,
        supports_tool_calls=True,
        tool_call_shape="openai_tools",
        cache_control_supported=False,
        audit_shape="canonical_event",
        max_context_tokens=200_000,  # Kimi's claim to fame
        max_output_tokens=8192,
    ),
    "litellm:moonshot:kimi-k2-6": BehaviorContract(
        provider="litellm",
        sub_provider="moonshot",
        supports_streaming=True,
        supports_tool_calls=True,
        tool_call_shape="openai_tools",
        cache_control_supported=False,
        audit_shape="canonical_event",
        max_context_tokens=200_000,
        max_output_tokens=8192,
    ),
    "litellm:zai:glm-5.1-default": BehaviorContract(
        provider="litellm",
        sub_provider="zai",
        supports_streaming=True,
        supports_tool_calls=True,
        tool_call_shape="openai_tools",  # via OpenRouter
        cache_control_supported=False,
        audit_shape="canonical_event",
        max_context_tokens=128_000,
        max_output_tokens=8192,
    ),
    "claude_sdk/sonnet-4-6": BehaviorContract(
        provider="claude_sdk",
        sub_provider="",
        supports_streaming=True,
        supports_tool_calls=True,
        tool_call_shape="anthropic_tools",
        cache_control_supported=True,
        audit_shape="canonical_event",
        max_context_tokens=200_000,
        max_output_tokens=8192,
    ),
}


def get_contract(lane: str) -> BehaviorContract:
    """Retrieve contract for a lane. Raises KeyError on unknown lane."""
    if lane not in CONTRACTS:
        raise KeyError(f"Unknown provider lane: {lane}. Known: {sorted(CONTRACTS.keys())}")
    return CONTRACTS[lane]


def get_lanes_by_provider(provider: str) -> Set[str]:
    """Return all lane-keys for a given provider."""
    return {lane for lane, c in CONTRACTS.items() if c.provider == provider}


def validate_audit_shape_uniform() -> bool:
    """Invariant: ALL contracts have audit_shape='canonical_event' per ADR-016."""
    return all(c.audit_shape == "canonical_event" for c in CONTRACTS.values())
