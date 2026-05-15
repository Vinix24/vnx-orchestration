#!/usr/bin/env python3
"""Wave 7 PR-7.5 — behavior_contracts.py tests.

Covers:
- All routing_policy.yaml lanes have a corresponding BehaviorContract.
- ADR-016 invariant: all contracts use audit_shape='canonical_event'.
- Unknown lane raises KeyError.
- DeepSeek uses openai_tools shape.
- Claude lanes use anthropic_tools shape.
- LiteLLM lanes have cache_control_supported=False.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_LIB = Path(__file__).resolve().parent.parent / "scripts" / "lib"
sys.path.insert(0, str(SCRIPTS_LIB))

from providers.behavior_contracts import (
    CONTRACTS,
    BehaviorContract,
    get_contract,
    get_lanes_by_provider,
    validate_audit_shape_uniform,
)

_ROUTING_POLICY_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "lib" / "providers" / "routing_policy.yaml"
)


def _load_routing_lanes() -> list[str]:
    """Extract all explicit lane values from routing_policy.yaml."""
    with open(_ROUTING_POLICY_PATH) as fh:
        data = yaml.safe_load(fh)
    lanes = []
    if data.get("default_lane"):
        lanes.append(data["default_lane"])
    for rule in data.get("rules") or []:
        if rule.get("lane"):
            lanes.append(rule["lane"])
    return lanes


class TestAllKnownLanesHaveContracts:
    def test_routing_policy_lanes_covered(self):
        """Every lane referenced in routing_policy.yaml must have a BehaviorContract."""
        routing_lanes = _load_routing_lanes()
        missing = [lane for lane in routing_lanes if lane not in CONTRACTS]
        assert missing == [], f"Lanes in routing_policy.yaml missing contracts: {missing}"

    def test_eight_contracts_registered(self):
        """Registry has exactly 8 contracts (all current lanes)."""
        assert len(CONTRACTS) == 8, (
            f"Expected 8 contracts, got {len(CONTRACTS)}. "
            f"Keys: {sorted(CONTRACTS.keys())}"
        )


class TestAuditShapeUniform:
    def test_validate_audit_shape_uniform_returns_true(self):
        """validate_audit_shape_uniform() is True — ADR-016 invariant holds."""
        assert validate_audit_shape_uniform() is True

    def test_all_contracts_have_canonical_event_audit_shape(self):
        """Every individual contract has audit_shape='canonical_event'."""
        for lane, contract in CONTRACTS.items():
            assert contract.audit_shape == "canonical_event", (
                f"Lane {lane!r} has audit_shape={contract.audit_shape!r}, expected 'canonical_event'"
            )


class TestGetContractUnknownLaneRaises:
    def test_raises_key_error_for_unknown_lane(self):
        """get_contract raises KeyError for an unregistered lane."""
        with pytest.raises(KeyError, match="Unknown provider lane"):
            get_contract("litellm:unknown:model-xyz")

    def test_error_message_contains_known_lanes(self):
        """KeyError message lists the known lanes for diagnostics."""
        with pytest.raises(KeyError) as exc_info:
            get_contract("bad-lane")
        assert "Known:" in str(exc_info.value)


class TestDeepSeekUsesOpenAIToolsShape:
    def test_deepseek_tool_call_shape(self):
        """DeepSeek V4 lane uses openai_tools shape (not Anthropic format)."""
        contract = get_contract("litellm:deepseek:deepseek-v4-pro")
        assert contract.tool_call_shape == "openai_tools"

    def test_deepseek_is_litellm_provider(self):
        contract = get_contract("litellm:deepseek:deepseek-v4-pro")
        assert contract.provider == "litellm"
        assert contract.sub_provider == "deepseek"


class TestClaudeUsesAnthropicToolsShape:
    def test_sonnet_tool_call_shape(self):
        """Sonnet 4.6 lane uses anthropic_tools shape."""
        contract = get_contract("claude/sonnet-4-6")
        assert contract.tool_call_shape == "anthropic_tools"

    def test_haiku_tool_call_shape(self):
        contract = get_contract("claude/haiku-4-5")
        assert contract.tool_call_shape == "anthropic_tools"

    def test_opus_tool_call_shape(self):
        contract = get_contract("claude/opus")
        assert contract.tool_call_shape == "anthropic_tools"

    def test_claude_lanes_count(self):
        """Exactly 3 claude provider lanes registered."""
        claude_lanes = get_lanes_by_provider("claude")
        assert len(claude_lanes) == 3


class TestLiteLLMLanesNoCacheControl:
    def test_deepseek_no_cache_control(self):
        contract = get_contract("litellm:deepseek:deepseek-v4-pro")
        assert contract.cache_control_supported is False

    def test_kimi_k2_0905_no_cache_control(self):
        contract = get_contract("litellm:moonshot:kimi-k2-0905-default")
        assert contract.cache_control_supported is False

    def test_kimi_k2_6_no_cache_control(self):
        contract = get_contract("litellm:moonshot:kimi-k2-6")
        assert contract.cache_control_supported is False

    def test_glm_no_cache_control(self):
        contract = get_contract("litellm:zai:glm-5.1-default")
        assert contract.cache_control_supported is False

    def test_all_litellm_lanes_no_cache_control(self):
        """Invariant: no LiteLLM lane supports cache_control (LiteLLM limitation)."""
        litellm_lanes = get_lanes_by_provider("litellm")
        failing = [
            lane for lane in litellm_lanes
            if CONTRACTS[lane].cache_control_supported
        ]
        assert failing == [], f"LiteLLM lanes with cache_control=True: {failing}"


class TestClaudeLanesCacheControl:
    def test_claude_lanes_support_cache_control(self):
        """All claude lanes support cache_control (native Claude feature)."""
        claude_lanes = get_lanes_by_provider("claude")
        failing = [
            lane for lane in claude_lanes
            if not CONTRACTS[lane].cache_control_supported
        ]
        assert failing == [], f"Claude lanes with cache_control=False: {failing}"


class TestGetLanesByProvider:
    def test_returns_set_for_known_provider(self):
        lanes = get_lanes_by_provider("litellm")
        assert isinstance(lanes, set)
        assert len(lanes) > 0

    def test_returns_empty_set_for_unknown_provider(self):
        lanes = get_lanes_by_provider("ollama")
        assert lanes == set()


class TestContractDataIntegrity:
    def test_all_max_tokens_positive(self):
        for lane, c in CONTRACTS.items():
            assert c.max_context_tokens > 0, f"{lane}: max_context_tokens <= 0"
            assert c.max_output_tokens > 0, f"{lane}: max_output_tokens <= 0"

    def test_valid_tool_call_shapes(self):
        valid_shapes = {"anthropic_tools", "openai_functions", "openai_tools"}
        for lane, c in CONTRACTS.items():
            assert c.tool_call_shape in valid_shapes, (
                f"{lane}: unknown tool_call_shape {c.tool_call_shape!r}"
            )

    def test_kimi_large_context_window(self):
        """Kimi lanes have 200K context (their differentiated capability)."""
        k1 = get_contract("litellm:moonshot:kimi-k2-0905-default")
        k2 = get_contract("litellm:moonshot:kimi-k2-6")
        assert k1.max_context_tokens == 200_000
        assert k2.max_context_tokens == 200_000
